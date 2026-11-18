"""カテゴリー内の商品を価格帯で分けて検索し、1検索あたり999件の上限を超えて店舗を集める。

1. カテゴリー全体を検索し、総件数(totalResultsAvailable)が999件以内なら全部取って完了。
2. 超えるなら価格帯(下の LADDER)に分割し、各価格帯について1.から繰り返す。
3. それでも超える価格帯は半分ずつに割っていく(価格の両端は含む。重なりなし)。
4. 1円刻みまで割っても999件を超える価格帯(同じ価格の商品が大量)だけは、並び順を変えて検索する。
進捗はDBに保存するので、中断しても続きから再開できる。
recrawl_days を指定すると、その日数より古い進捗は検索し直す(あとから増えた店舗を見つけるため)。
"""
from __future__ import annotations

import time

from . import api, categories, db

# 価格帯の初期分割(円)。[0,499], [500,999], ... , [1000000, 99999999]
LADDER = [0, 500, 1000, 1500, 2000, 3000, 4000, 5000, 7000, 10000, 15000, 20000,
          30000, 50000, 100000, 300000, 1000000, 100000000]


# 幅優先で進めるための「分割の深さ」の上限(パスごと)。最後の None は無制限。
# 深さ: 0=カテゴリー全体, 1=初期の価格帯, 2=その半分, 3=そのまた半分 ...
# まず全カテゴリーを粗い価格帯で一巡し(新しい店舗が早く多く見つかる)、そのあと細かい価格帯へ進む。
PASS_LEVELS = (1, 2, 3, 5, 8, 12, None)


class Deadline(Exception):
    """時間切れ。進捗は保存済みなので、次回は続きから再開できる。"""


def children(lo: int | None, hi: int | None) -> list[tuple[int, int]]:
    """価格帯 (lo, hi) の分割先。lo=None は「価格指定なし(カテゴリー全体)」。"""
    if lo is None:
        return [(LADDER[i], LADDER[i + 1] - 1) for i in range(len(LADDER) - 1)]
    if lo >= hi:
        return []
    mid = (lo + hi) // 2
    return [(lo, mid), (mid + 1, hi)]


def unit_key(category_id: int, lo: int | None, hi: int | None) -> str:
    return f"{category_id}:all" if lo is None else f"{category_id}:{lo}-{hi}"


class Crawler:
    def __init__(self, conn, session, appid, *, sort="-score", page_size=api.PAGE_SIZE, delay=2.0,
                 deadline: float | None = None, recrawl_days: float | None = None,
                 max_level: int | None = None, sleep=None, log=print):
        self.conn, self.session, self.appid = conn, session, appid
        self.sort, self.page_size, self.delay = sort, page_size, delay
        self.deadline, self.log = deadline, log
        # 呼び出し時に time.sleep を引く(テストで差し替えられるように、既定値を読み込み時に固定しない)
        self.sleep = sleep if sleep is not None else (lambda sec: time.sleep(sec))
        self.recrawl_days = recrawl_days  # この日数より古い進捗は「未実施」とみなして検索し直す(新店舗の発見)
        self.max_level = max_level  # これより深い価格帯は今回は見送る(次のパスで処理する)
        self.deferred = 0  # 今回のパスで見送った価格帯の数。0なら全部終わっている
        self.new_sellers = 0
        self.saturated = 0  # 同価格の商品が999件超で、並び順の違いで集めた価格帯の数

    def crawl(self, target: categories.Target) -> None:
        self._unit(target, None, None, 0)

    def _fetch(self, target, lo, hi, sort, first_page_only: bool) -> int:
        found: list[tuple[str, str]] = []
        pairs: list[tuple[str, str]] = []
        total = 0
        try:
            for total, hits in api.iter_pages(
                self.session, self.appid, genre_category_id=target.category_id,
                price_from=lo, price_to=hi, sort=sort, page_size=self.page_size, delay=self.delay,
            ):
                for hit in hits:
                    seller = hit.get("seller") or {}
                    sid = seller.get("sellerId")
                    if sid:
                        found.append((sid, seller.get("name") or ""))
                        pairs.append((sid, categories.category_path(hit, target.depth, target.name)))
                if first_page_only and total > api.MAX_REACHABLE:
                    break  # 多すぎるので分割する。1ページ目の分だけ使う
        finally:  # エラーや中断でも、ここまでの分は保存する
            self.new_sellers += db.add_ids(self.conn, found)
            db.add_seller_categories(self.conn, pairs)
        return total

    def _unit(self, target, lo, hi, level: int) -> None:
        key = unit_key(target.category_id, lo, hi)
        state = db.unit_state(self.conn, key, self.recrawl_days)
        if state == "done":
            return
        if state != "split":
            if self.max_level is not None and level > self.max_level:
                self.deferred += 1
                return
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise Deadline()
            total = self._fetch(target, lo, hi, self.sort, first_page_only=True)
            label = f"{target.name} {'全体' if lo is None else f'{lo}-{hi}円'}"
            kids = children(lo, hi)
            if total <= api.MAX_REACHABLE:
                db.set_unit(self.conn, key, "done", total)
                self.log(f"[{label}] 商品 {total} 件 → 取得完了 (累計の新規店舗 {self.new_sellers})")
                self.sleep(self.delay)
                return
            if kids:
                db.set_unit(self.conn, key, "split", total)
                self.log(f"[{label}] 商品 {total} 件 → 価格帯に分割")
                self.sleep(self.delay)
            else:
                # 1円刻みでも999件を超える: 並び順を変えて取れるだけ取る
                for s in api.SORTS:
                    self._fetch(target, lo, hi, s, first_page_only=False)
                    self.sleep(self.delay)
                self.saturated += 1
                db.set_unit(self.conn, key, "done", total)
                self.log(f"[{label}] 商品 {total} 件(同価格) → 並び順を変えて取得")
                return
        for k_lo, k_hi in children(lo, hi):
            self._unit(target, k_lo, k_hi, level + 1)
