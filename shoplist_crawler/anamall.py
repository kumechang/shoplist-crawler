"""ANA Mall (https://anamall.ana.co.jp/) の店舗情報。

- 店舗一覧: /feature/shops.aspx (約250店舗。店舗コードは4桁)
- 店舗ごとの会社情報: /shop/shop/guide.aspx?shop=<4桁コード> (全店舗が同じ形式)

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

_LABELS = {
    "販売事業者名": "businessKind",
    "ショップ名": "name",
    "所在地": "_address",
    "代表者": "companyPerson",
    "店舗運営責任者": "contactPerson",
    "店舗セキュリティ責任者": "securityPerson",
}
_POST = re.compile(r"〒\s*(\d{3}-?\d{4})\s*(.*)")


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
    t = re.sub(r"[ \t　]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def parse_guide(html: str) -> dict | None:
    """店舗ガイドから会社情報を取り出す。見つからなければ None(ページの作りが変わった可能性)。"""
    t = _text(html)
    # 目次にも「会社概要」が出るので、「販売事業者名」の行がある場所を基準にする
    anchor = t.find("販売事業者名")
    if anchor < 0:
        return None
    seller: dict = {}
    for line in t[anchor : anchor + 1500].split("\n"):
        m = re.match(r"\s*([^\s:：]+)\s*[:：]\s*(.*)", line)
        if m and m.group(1) in _LABELS:
            seller.setdefault(_LABELS[m.group(1)], m.group(2).strip())
    if "businessKind" not in seller and "name" not in seller:
        return None
    addr = seller.pop("_address", "")
    pm = _POST.match(addr)
    if pm:
        seller["companyPost"], seller["companyAddress"] = pm.group(1), pm.group(2).strip()
    elif addr:
        seller["companyAddress"] = addr
    # お問い合わせ先(店舗が自由に書く欄)から、電話・FAX・メールを拾う。
    # 文章の途中の「お問い合わせ先」ではなく、見出しの行(その言葉だけの行)を基準にする。
    lines = t.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("販売事業者名"))
    heads = [i for i, ln in enumerate(lines) if i > start and ln.strip() == "お問い合わせ先"]
    contact = "\n".join(lines[heads[-1] : heads[-1] + 25]) if heads else ""
    for key, pat in (
        ("contactTel", r"電話(?:番号)?\s*[:：]\s*([\d\-－()（） ]{8,})"),
        ("contactFax", r"(?:FAX|ＦＡＸ|ファックス)(?:番号)?\s*[:：]\s*([\d\-－()（） ]{8,})"),
        ("contactMail", r"[:：]\s*([\w.+-]+@[\w-]+(?:\.[\w-]+)+)"),
    ):
        m = re.search(pat, contact)
        if m:
            seller[key] = m.group(1).strip()
    if "contactMail" not in seller:
        m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", contact)
        if m:
            seller["contactMail"] = m.group(0)
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
    seller = parse_guide(resp.text)
    if seller is None:
        return FetchResult("error", error="会社概要が見つかりません(店舗ガイドの作りが変わった/独自の書式の可能性)")
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
