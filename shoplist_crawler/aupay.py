"""au PAY マーケット (https://wowma.jp/) の店舗情報。

- 店舗の発見: 店舗名検索 `/bep/m/klists1?sn=<店舗名の一部(Shift_JIS)>&page=N` (30件ずつ。全件の最後のページまで取れる)。
  店舗の一覧そのものはどこにも載っていないので、1文字ずつ(ひらがな・カタカナ・英数字)検索し、
  見つかった店舗名に出てくる新しい文字を検索語に足していく。
- 店舗ごとの情報: `/bep/m/kmem?user=<店舗ID>` (項目名が <h3>、値が次の <p> に並ぶ。全店舗で同じ作り)

robots.txt で禁止されているのは `/bep/m/kshop?user=*`、`/user/*/list`、価格帯つきの検索などで、上記は禁止されていない。
ページは Shift_JIS。取得した項目は Yahoo と同じキー名に対応づけて保存するので、更新チェックや変更履歴などを共用できる。
"""
from __future__ import annotations

import html as htmlmod
import re
import unicodedata
import urllib.parse
from collections import Counter

import requests

from .http import get_with_retry
from .info import FetchResult, SAMPLE_HEADERS, squash_japanese_spaces

BASE = "https://wowma.jp"
SEARCH_URL = BASE + "/bep/m/klists1?sn={sn}&page={page}"
KMEM_URL = BASE + "/bep/m/kmem?user={shop_id}"
PAGE_SIZE = 30
DEFAULT_DELAY = 3.0
SITE = "aupay"

HIRAGANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
KATAKANA = "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲンヴー"
SEEDS = list(HIRAGANA + KATAKANA + "0123456789" + "abcdefghijklmnopqrstuvwxyz")

_ROW = re.compile(r'<a href="/user/(\d+)\?e=shopsearch10[^"]*">(.*?)</a>', re.S)
_TOTAL = re.compile(r"([\d,]+)\s*件\s*～\s*([\d,]+)\s*件\s*\(\s*全\s*([\d,]+)\s*件\s*\)")
_H3 = re.compile(r"<h3[^>]*>(.*?)</h3>(.*?)(?=<h[23][\s>]|$)", re.S)
_POST = re.compile(r"〒\s*(\d{3}-?\d{4})\s*(.*)", re.S)
_PHONE = re.compile(r"\d{2,4}[-－ー]\d{2,4}[-－ー]\d{3,4}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LABELS = {
    "お店の名前": "name",
    "販売事業者名": "businessKind",
    "通信販売業務責任者": "contactPerson",
    "個人情報保護責任者": "securityPerson",
    "住所": "_address",
    "電話番号": "_tel",
    "FAX番号": "_fax",
    "メールアドレス": "_mail",
}


class SearchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def decode(resp) -> str:
    """ページは Shift_JIS (Windows拡張のCP932)。"""
    return resp.content.decode("cp932", errors="replace")


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def usable_query(q: str) -> str | None:
    """検索語にできる文字だけを返す。文字・数字で、Shift_JISで表せるものに限る(NFKCで正規化)。"""
    q = unicodedata.normalize("NFKC", q).strip()
    if not q or unicodedata.category(q[0])[0] not in "LN":
        return None
    try:
        q.encode("cp932")
    except UnicodeEncodeError:
        return None
    return q


def query_chars(names) -> Counter:
    """店舗名から、検索語に使える文字を数える(1店舗の名前に何度出ても1回)。"""
    c: Counter = Counter()
    for name in names:
        for ch in set(unicodedata.normalize("NFKC", name)):
            q = usable_query(ch)
            if q:
                c[q] += 1
    return c


def parse_search(html: str) -> tuple[int, list[tuple[str, str]]]:
    """店舗名検索の結果ページから (総件数, [(店舗ID, 店舗名)]) を返す。"""
    shops = [(m.group(1), _strip(m.group(2))) for m in _ROW.finditer(html)]
    text = _strip(html)
    m = _TOTAL.search(text)
    if m:
        return int(m.group(3).replace(",", "")), shops
    return (0 if "該当するお店はありませんでした" in text else len(shops)), shops


def search_page(session: requests.Session, query: str, page: int) -> tuple[int, list[tuple[str, str]]]:
    sn = urllib.parse.quote(query, encoding="cp932")
    resp = get_with_retry(session, SEARCH_URL.format(sn=sn, page=page))
    if resp.status_code != 200:
        raise SearchError(f"HTTP {resp.status_code}", resp.status_code)
    return parse_search(decode(resp))


def parse_kmem(html: str) -> dict | None:
    """店舗情報ページから会社情報を取り出す。読み取れなければ None。"""
    seller: dict = {}
    for m in _H3.finditer(html):
        key = _LABELS.get(_strip(m.group(1)))
        if not key:
            continue
        value = _strip(m.group(2))
        if not value:
            continue
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
        else:
            seller.setdefault(key, value)
    if "businessKind" not in seller and "name" not in seller:
        return None
    addr = seller.pop("_address", "")
    pm = _POST.match(addr)
    if pm:
        seller["companyPost"], seller["companyAddress"] = pm.group(1), pm.group(2).strip()
    elif addr:
        seller["companyAddress"] = addr
    return seller


def fetch_kmem(session: requests.Session, shop_id: str) -> FetchResult:
    try:
        resp = get_with_retry(session, KMEM_URL.format(shop_id=shop_id))
    except requests.RequestException as e:
        return FetchResult("error", error=f"{type(e).__name__}: {e}")
    if resp.status_code == 404:
        return FetchResult("not_found")
    if resp.status_code != 200:
        return FetchResult("error", error=f"HTTP {resp.status_code}")
    try:
        seller = parse_kmem(decode(resp))
    except Exception as e:  # 1ページで全体を止めない
        return FetchResult("error", error=f"解析に失敗: {type(e).__name__}", layout=True)
    if seller is None:
        return FetchResult("error", error="店舗情報を読み取れません(エラーページ、または作りが変わった可能性)", layout=True)
    return FetchResult("ok", seller=seller)


def to_sample_row(shop_id: str, seller: dict) -> dict:
    """Yahoo と同じ11列(カテゴリー以外)。au PAY には代表者・フリガナ・カテゴリーの欄がないので空欄。
    「運営責任者」には「通信販売業務責任者」を入れる。"""
    post = seller.get("companyPost") or ""
    row = {h: "" for h in SAMPLE_HEADERS}
    row.update({
        "URL": KMEM_URL.format(shop_id=shop_id),
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
