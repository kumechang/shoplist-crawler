"""ANA Mall (https://anamall.ana.co.jp/) の店舗情報。

- 店舗一覧: /feature/shops.aspx (約250店舗。店舗コードは4桁)
- 店舗ごとの会社情報: /shop/shop/guide.aspx?shop=<4桁コード>
  (**書式は店舗ごとに違う**。「販売事業者名 : …」「●販売事業者名：…」「【販売事業者名】」+次の行、「販売業者」「事業責任者」など。
  項目ではなく文章で書かれた店舗は読めず、error として記録する)

robots.txt が AI クローラー向けに Crawl-delay: 10 を求めているので、既定のアクセス間隔は10秒。
取得した項目は、Yahoo と同じキー名(name, businessKind, companyPost ...)に対応づけて保存するので、
更新チェック(refresh)や変更履歴など、既存の仕組みをそのまま使える。
"""
from __future__ import annotations

import html as htmlmod
import re

import requests

from .http import get_with_retry
from .info import FetchResult, SAMPLE_HEADERS, squash_japanese_spaces

BASE = "https://anamall.ana.co.jp"
LIST_URL = BASE + "/feature/shops.aspx"
GUIDE_URL = BASE + "/shop/shop/guide.aspx?shop={code}"
DEFAULT_DELAY = 10.0  # robots.txt の Crawl-delay に合わせる

_SHOP_BLOCK = re.compile(r'<li data-kana="[^"]*">(.*?)</li>', re.S)
_SHOP_LINK = re.compile(r'/shop/top/(\d{4})-\d{4}/')
_SHOP_NAME = re.compile(r'class="shop-text"[^>]*>\s*<p>(.*?)</p>', re.S)
# 「ANAグループ」の店舗だけ、属性 data-cate に値が入る(表示用の文字は全店舗にあるが、値が空の店舗では隠れている)
_ICON = re.compile(r'<span class="block-top-shop--item--icon--ana"[^>]*data-cate="([^"]*)"')

_BULLETS = "●■◆□○▼▽・※ \t"
# 店舗ガイドは店舗ごとに書式が違う(「販売事業者名 : …」「●販売事業者名：…」「【販売事業者名】」+次の行 など)。
# 見つかった言い方を、キーに対応づける。
_SYNONYMS = {
    "businessKind": ("販売事業者名", "販売業者名", "販売業者", "販売事業者", "事業者名"),
    "name": ("ショップ名", "店舗名", "ストア名"),
    "_address": ("所在地", "住所"),
    "companyPerson": ("代表者", "代表者名"),
    "contactPerson": ("店舗運営責任者", "運営責任者", "事業責任者", "販売責任者"),
    "securityPerson": ("店舗セキュリティ責任者",),
    "_tel": ("TEL", "ＴＥＬ", "電話番号", "電話"),
    "_fax": ("FAX", "ＦＡＸ", "FAX番号", "ファックス番号"),
    "_mail": ("E-mail", "Email", "メールアドレス", "メール", "Mail"),
}
_LABEL_TO_KEY = {lab: key for key, labs in _SYNONYMS.items() for lab in labs}
_POST = re.compile(r"〒\s*(\d{3}-?\d{4})\s*(.*)")
_BRACKET = re.compile(r"^【([^】]{1,14})】\s*(.*)$")
_COLON = re.compile(r"^([^\s:：【】]{1,14})\s*[:：]\s*(.*)$")
_CONTACT_HEAD = re.compile(r"^(お問い合わせ先|お問合せ先|お問い合わせ窓口|お問合せ窓口|窓口)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\d{2,4}[-－ー]\d{2,4}[-－ー]\d{3,4}")
_COMPANY_LINES = 20  # 会社概要の項目は、開始行から数行〜十数行以内に並ぶ。広げると配送の説明などの電話番号を拾ってしまう
_CONTACT_LINES = 14  # 「お問い合わせ先」の見出しの直後に、窓口・TEL・FAX・メールが並ぶ
_CONTACT_HEADS = ("お問い合わせ先", "お問合せ先", "お問い合わせ窓口", "お問合せ窓口")


def parse_shop_list(html: str) -> list[tuple[str, str, str]]:
    """一覧ページから (店舗コード, 店舗名, カテゴリー) を返す。カテゴリーは「ANAグループ」の印がある店舗だけ。"""
    out, seen = [], set()
    for block in _SHOP_BLOCK.findall(html):
        m = _SHOP_LINK.search(block)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        n = _SHOP_NAME.search(block)
        name = htmlmod.unescape(re.sub(r"<[^>]+>", "", n.group(1))).strip() if n else ""
        icon = _ICON.search(block)
        out.append((m.group(1), name, htmlmod.unescape(icon.group(1)).strip() if icon else ""))
    return out


def _text(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    t = re.sub(r"<(br|/p|/tr|/li|/h\d|/div|/dd|/dt|/th|/td)[^>]*>", "\n", t)
    t = htmlmod.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t\u3000]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def _label_value(lines: list[str], i: int) -> tuple[str, str, int] | None:
    """lines[i] が「ラベル: 値」「ラベル：値」「【ラベル】値」「【ラベル】」(値は次の行) のとき (ラベル, 値, 使った行数) を返す。"""
    ln = lines[i].strip().lstrip(_BULLETS)
    m = _BRACKET.match(ln)
    if m:
        label, value = m.group(1).strip(), m.group(2).strip()
        if not value and i + 1 < len(lines):  # 値が次の行にある書式
            return label, lines[i + 1].strip().lstrip(_BULLETS), 2
        return label, value, 1
    m = _COLON.match(ln)
    return (m.group(1), m.group(2).strip(), 1) if m else None


def parse_guide(html: str) -> dict | None:
    """店舗ガイドから会社情報を取り出す。読み取れなければ None(独自の書式、またはページの作りが変わった)。

    書式の違い(「●」などの記号、【 】の見出し+次の行、TELの位置)は吸収する。
    「1.事業者の名称・住所・代表者氏名」の下に文章を書くだけ、のような書式は読み取れない。
    """
    lines = _text(html).split("\n")
    start = None
    for i in range(len(lines)):
        lv = _label_value(lines, i)
        if lv and _LABEL_TO_KEY.get(lv[0]) in ("businessKind", "name") and lv[1]:
            start = i
            break
    if start is None:
        return None
    seller: dict = {}
    i, end = start, min(len(lines), start + _COMPANY_LINES)
    while i < end:  # 1) 会社概要の周辺: 事業者名・ショップ名・所在地・代表者・責任者・(会社の)電話
        lv = _label_value(lines, i)
        if lv:
            label, value, used = lv
            key = _LABEL_TO_KEY.get(label)
            if key and value:
                if key == "_tel":
                    pm = _PHONE.search(value)
                    if pm:
                        seller.setdefault("companyTel", pm.group(0))
                elif key not in ("_fax", "_mail"):
                    seller.setdefault(key, value)
            i += used
        else:
            i += 1
    # 2) 「お問い合わせ先」の見出し(その言葉だけの行)の直後: 電話・FAX・メール。本文中の言及や、規約の中の記述は見ない
    head = next((j for j in range(start + 1, len(lines)) if lines[j].strip().lstrip(_BULLETS) in _CONTACT_HEADS), None)
    if head is not None:
        j, stop = head + 1, min(len(lines), head + 1 + _CONTACT_LINES)
        while j < stop:
            lv = _label_value(lines, j)
            if lv:
                label, value, used = lv
                key = _LABEL_TO_KEY.get(label)
                if key == "_tel":
                    pm = _PHONE.search(value)
                    if pm:
                        seller.setdefault("contactTel", pm.group(0))
                elif key == "_fax":
                    pm = _PHONE.search(value)
                    if pm:
                        seller.setdefault("contactFax", pm.group(0))
                elif key == "_mail":
                    em = _EMAIL.search(value)
                    if em:
                        seller.setdefault("contactMail", em.group(0))
                j += used
            else:
                j += 1
    if "businessKind" not in seller and "name" not in seller:
        return None
    addr = seller.pop("_address", "")
    pm = _POST.match(addr)
    if pm:
        seller["companyPost"], seller["companyAddress"] = pm.group(1), pm.group(2).strip()
    elif addr:
        seller["companyAddress"] = addr
    # お問い合わせ先の電話は、無ければ会社の電話で代用する
    if "contactTel" not in seller and "companyTel" in seller:
        seller["contactTel"] = seller["companyTel"]
    seller.pop("companyTel", None)
    return seller


def fetch_guide(session: requests.Session, code: str) -> FetchResult:
    try:
        resp = get_with_retry(session, GUIDE_URL.format(code=code))
    except requests.RequestException as e:
        return FetchResult("error", error=f"{type(e).__name__}: {e}")
    if resp.status_code == 404:
        return FetchResult("not_found")
    if resp.status_code != 200:
        return FetchResult("error", error=f"HTTP {resp.status_code}")
    try:
        seller = parse_guide(resp.text)
    except Exception as e:  # 1ページの変わった書式で、全体を止めない
        return FetchResult("error", error=f"解析に失敗: {type(e).__name__}", layout=True)
    if seller is None:
        return FetchResult("error", error="会社概要を読み取れません(独自の書式の可能性)", layout=True)
    return FetchResult("ok", seller=seller)


def fetch_shop_list(session: requests.Session) -> list[tuple[str, str, str]]:
    resp = get_with_retry(session, LIST_URL)
    if resp.status_code != 200:
        raise RuntimeError(f"ショップ一覧の取得に失敗: HTTP {resp.status_code}")
    shops = parse_shop_list(resp.text)
    if not shops:
        raise RuntimeError("ショップ一覧から店舗を1件も読み取れません(ページの作りが変わった可能性)")
    return shops


def to_sample_row(code: str, seller: dict) -> dict:
    """Yahoo と同じ11列(カテゴリー以外)。ANA Mall にはフリガナがないので空欄。"""
    post = seller.get("companyPost") or ""
    row = {h: "" for h in SAMPLE_HEADERS}
    row.update({
        "URL": GUIDE_URL.format(code=code),
        "会社名（商号）": seller.get("businessKind") or "",
        "郵便番号": f"〒{post}" if post else "",
        "住所": seller.get("companyAddress") or "",
        "代表者": squash_japanese_spaces(seller.get("companyPerson") or ""),
        "ストア名": seller.get("name") or "",
        "運営責任者": seller.get("contactPerson") or "",
        "お問い合わせ電話番号": seller.get("contactTel") or "",
        "お問い合わせメールアドレス": seller.get("contactMail") or "",
    })
    del row["カテゴリー"]
    return row
