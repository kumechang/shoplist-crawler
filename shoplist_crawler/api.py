"""Yahoo!ショッピング 商品検索API(v3)から店舗IDを集める。

検索結果の各商品に含まれる ``seller`` (店舗ID・店舗名) を拾うだけ。
APIは1クエリあたり先頭1000件までしか返さないため、キーワードと並び順を変えて
何度も検索し、店舗IDの重複を除いて集める。
"""
from __future__ import annotations

import time
from typing import Iterator

import requests

from .http import get_with_retry

ENDPOINT = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
# 公式ドキュメント: https://developer.yahoo.co.jp/webapi/shopping/v3/itemsearch.html
PAGE_SIZE = 50  # results。ドキュメントの例が50。上限の記載はないので安全側に倒している
MAX_POSITION = 1000  # 「start + results の合計は1,000が上限」
SORTS = ("-score", "+price", "-price", "-review_count")  # ドキュメントに載っている並び順


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def iter_sellers(
    session: requests.Session,
    appid: str,
    query: str,
    *,
    sort: str = "-score",
    genre_category_id: int | None = None,
    delay: float = 1.0,
    sleep=time.sleep,
) -> Iterator[tuple[str, str]]:
    """1つの (query, sort) について (seller_id, seller_name) を順に返す。"""
    start = 1
    while start < MAX_POSITION:
        results = min(PAGE_SIZE, MAX_POSITION - start)  # start + results <= 1000
        params = {
            "appid": appid,
            "query": query,
            "sort": sort,
            "results": results,
            "start": start,
        }
        if genre_category_id is not None:
            params["genre_category_id"] = genre_category_id
        resp = get_with_retry(session, ENDPOINT, params=params)
        if resp.status_code != 200:
            raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
        data = resp.json()
        hits = data.get("hits") or []
        for hit in hits:
            seller = hit.get("seller") or {}
            sid = seller.get("sellerId")
            if sid:
                yield sid, seller.get("name") or ""
        total = data.get("totalResultsAvailable", 0)
        start += len(hits)
        if not hits or len(hits) < results or start > total:
            break
        sleep(delay)
