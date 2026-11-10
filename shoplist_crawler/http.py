"""共通のHTTP設定。"""
from __future__ import annotations

import random
import time

import requests

USER_AGENT = "shoplist-crawler/0.1 (personal use)"
RETRY_STATUS = {429, 500, 502, 503, 504}


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja"})
    return s


def get_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: dict | None = None,
    retries: int = 4,
    timeout: float = 20.0,
    sleep=time.sleep,
) -> requests.Response:
    """429/5xx/通信エラーは指数バックオフで再試行する。最終的な応答(またはエラー)を返す/投げる。"""
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            if attempt == retries:
                raise
        else:
            if resp.status_code not in RETRY_STATUS or attempt == retries:
                return resp
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.isdigit():
                sleep(min(int(retry_after), 120))
                continue
        sleep(min(2 ** (attempt + 1), 60) + random.random())
    raise AssertionError("unreachable")
