"""店舗の「会社概要」ページ(info.html)の取得と解析。

ページには ``<script id="__NEXT_DATA__">`` にJSONが埋め込まれており、
``props.pageProps.seller`` に会社概要の内容が入っている。HTMLの見た目には依存しない。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import requests

from .http import get_with_retry

INFO_URL = "https://store.shopping.yahoo.co.jp/{seller_id}/info.html"
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)

# CSVに出力する列: (CSV列名, sellerのキー)
EXPORT_FIELDS = [
    ("store_name", "name"),
    ("store_name_kana", "nameKana"),
    ("description", "description"),
    ("rating_average", "ratingAverage"),
    ("rating_count", "ratingCount"),
    ("is_open", "isOpen"),
    ("company_name", "businessKind"),
    ("company_person", "companyPerson"),
    ("company_post", "companyPost"),
    ("company_address", "companyAddress"),
    ("contact_person", "contactPerson"),
    ("contact_post", "contactPost"),
    ("contact_address", "contactAddress"),  # 合成(都道府県+市区町村+番地)
    ("contact_tel", "contactTel"),
    ("contact_fax", "contactFax"),
    ("contact_mail", "contactMail"),
    ("business_hours", "businessDate"),
    ("related_site_name", "siteName1"),
    ("related_site_url", "siteUrl1"),
]


@dataclass
class FetchResult:
    status: str  # ok / not_found / error
    seller: dict | None = None
    error: str = ""


def parse_seller(html: str) -> dict | None:
    """HTMLから seller 辞書を取り出す。見つからなければ None。"""
    m = _NEXT_DATA.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        seller = data["props"]["pageProps"]["seller"]
    except (ValueError, KeyError, TypeError):
        return None
    return seller if isinstance(seller, dict) else None


def to_row(seller: dict) -> dict:
    """sellerの生データをCSV用の1行にする。"""
    row = {}
    for col, key in EXPORT_FIELDS:
        if key == "contactAddress":
            value = "".join(
                str(seller.get(k) or "")
                for k in ("contactPref", "contactCity", "contactAddr")
            )
        else:
            value = seller.get(key, "")
        row[col] = "" if value is None else value
    return row


def fetch_info(session: requests.Session, seller_id: str) -> FetchResult:
    try:
        resp = get_with_retry(session, INFO_URL.format(seller_id=seller_id))
    except requests.RequestException as e:
        return FetchResult("error", error=f"{type(e).__name__}: {e}")
    if resp.status_code == 404:
        return FetchResult("not_found")
    if resp.status_code != 200:
        return FetchResult("error", error=f"HTTP {resp.status_code}")
    seller = parse_seller(resp.text)
    if seller is None:
        return FetchResult("error", error="__NEXT_DATA__ not found (page layout changed?)")
    if seller.get("isNotFound"):
        return FetchResult("not_found")
    return FetchResult("ok", seller=seller)
