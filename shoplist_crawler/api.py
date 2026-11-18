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
MAX_REACHABLE = MAX_POSITION - 1  # 1つの検索で実際に取れる最大件数(1〜999件目)
SORTS = ("-score", "+price", "-price", "-review_count")  # ドキュメントに載っている並び順


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def iter_pages(
    session: requests.Session,
    appid: str,
    *,
    query: str | None = None,
    genre_category_id: int | None = None,
    price_from: int | None = None,
    price_to: int | None = None,
    sort: str = "-score",
    page_size: int = PAGE_SIZE,
    delay: float = 2.0,
    sleep=time.sleep,
) -> Iterator[tuple[int, list[dict]]]:
    """1つの検索条件について、ページごとに (totalResultsAvailable, hits) を返す。

    query か genre_category_id のどちらかは必須。最大でも先頭999件までしか取れない
    (price_from/price_to は両端を含む。価格帯で分けて検索すれば上限を超えて集められる)。
    """
    if not query and genre_category_id is None:
        raise ValueError("query か genre_category_id が必要です")
    start = 1
    while start < MAX_POSITION:
        results = min(page_size, MAX_POSITION - start)  # start + results <= 1000
        params = {"appid": appid, "sort": sort, "results": results, "start": start}
        if price_from is not None:
            params["price_from"] = price_from
        if price_to is not None:
            params["price_to"] = price_to
        if query:
            params["query"] = query
        if genre_category_id is not None:
            params["genre_category_id"] = genre_category_id
        # 429(回数制限)は短時間では解けないことがあるので、待ち時間を長めに取る(10,20,40,80,120秒)
        resp = get_with_retry(session, ENDPOINT, params=params, retries=5, backoff=10.0, max_wait=120.0)
        if resp.status_code != 200:
            raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
        data = resp.json()
        hits = data.get("hits") or []
        total = data.get("totalResultsAvailable", 0)
        yield total, hits
        start += len(hits)
        if not hits or len(hits) < results or start > total:
            break
        sleep(delay)


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
    for _total, hits in iter_pages(
        session, appid, query=query, genre_category_id=genre_category_id, sort=sort, delay=delay, sleep=sleep
    ):
        for hit in hits:
            seller = hit.get("seller") or {}
            sid = seller.get("sellerId")
            if sid:
                yield sid, seller.get("name") or ""
