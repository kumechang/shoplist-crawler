"""ジャンルカテゴリの列挙と、商品(hit)からのカテゴリーパス組み立て。

- 子カテゴリのID一覧: カテゴリID取得API(V1 categorySearch, XML)
  https://developer.yahoo.co.jp/webapi/shopping/shopping/v1/categorysearch.html
- カテゴリー名: 商品検索API(V3)の ``parentGenreCategories`` / ``genreCategory`` の名前を使う
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

from .api import ApiError
from .http import get_with_retry

V1_ENDPOINT = "https://shopping.yahooapis.jp/ShoppingWebService/V1/categorySearch"


@dataclass(frozen=True)
class Target:
    category_id: int
    depth: int  # トップ(ファッション等)=1
    name: str  # 表示用(V1のShort名)。CSVのカテゴリーにはV3の名前を使う


def parse_children(xml_bytes: bytes) -> list[tuple[int, str]]:
    """categorySearch(XML)のレスポンスから (子カテゴリID, 名前) を取り出す。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ApiError(f"カテゴリAPIの応答をXMLとして読めません: {e}") from e
    for el in root.iter():  # 名前空間を外して扱いやすくする
        el.tag = el.tag.rpartition("}")[2]
    out = []
    for child in root.iter("Child"):
        cid = (child.findtext("Id") or "").strip()
        title = child.find("Title")
        name = ""
        if title is not None:
            name = (title.findtext("Short") or title.findtext("Medium") or "").strip()
        if cid.isdigit():
            out.append((int(cid), name))
    return out


def child_categories(session: requests.Session, appid: str, category_id: int) -> list[tuple[int, str]]:
    resp = get_with_retry(
        session, V1_ENDPOINT, params={"appid": appid, "category_id": category_id},
        retries=5, backoff=10.0, max_wait=120.0,
    )
    if resp.status_code != 200:
        raise ApiError(f"カテゴリAPI HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
    return parse_children(resp.content)


def list_targets(
    session: requests.Session,
    appid: str,
    root_id: int,
    *,
    depth: int = 3,
    delay: float = 2.0,
    sleep=time.sleep,
    children=child_categories,
) -> list[Target]:
    """root_id(トップ階層のカテゴリ)の配下を depth 階層目まで辿り、検索対象のカテゴリ一覧を返す。

    子を持たないカテゴリは、depthより浅くてもそれ自身を対象にする。
    """
    if depth < 2:
        raise ValueError("depth は2以上(トップ階層=1)")
    targets: list[Target] = []

    def walk(cid: int, d: int, name: str) -> None:
        if d >= depth:
            targets.append(Target(cid, d, name))
            return
        kids = children(session, appid, cid)
        sleep(delay)
        if not kids:
            targets.append(Target(cid, d, name))
            return
        for kid_id, kid_name in kids:
            walk(kid_id, d + 1, kid_name)

    walk(root_id, 1, "")
    return targets


def category_path(hit: dict, target_depth: int, fallback: str = "") -> str:
    """商品(hit)のカテゴリーを「2階層目/3階層目」形式にする。トップ階層(ファッション等)は省く。"""
    chain = list(hit.get("parentGenreCategories") or [])
    if hit.get("genreCategory"):
        chain.append(hit["genreCategory"])
    chain = [c for c in chain if c.get("name") and isinstance(c.get("depth"), int)]
    chain.sort(key=lambda c: c["depth"])
    names = [c["name"] for c in chain if 2 <= c["depth"] <= target_depth]
    return "/".join(names) or fallback
