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
PAGE_SIZE = 100
MAX_POSITION = 1000  # start + results の上限
SORTS = ("-score", "+price", "-price", "-review_count", "-sold")


class ApiError(RuntimeError):
    pass


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
    while start <= MAX_POSITION:
        results = min(PAGE_SIZE, MAX_POSITION - start + 1)
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
            raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        hits = data.get("hits") or []
        for hit in hits:
            seller = hit.get("seller") or {}
            sid = seller.get("sellerId")
            if sid:
                yield sid, seller.get("name") or ""
        total = data.get("totalResultsAvailable", 0)
        start += len(hits) or results
        if not hits or len(hits) < results or start > min(total, MAX_POSITION):
            break
        sleep(delay)
