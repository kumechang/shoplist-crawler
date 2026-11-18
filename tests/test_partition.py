"""APIの挙動(999件の上限・価格フィルタ)を模したサーバを使い、取りこぼしなく集まることを確かめる。"""
import random

import pytest

from shoplist_crawler import api, categories, db, partition
from conftest import FakeResponse

TARGET = categories.Target(9, 3, "その他メンズファッション")
CHAIN = [{"depth": 1, "id": 1, "name": "ファッション"}, {"depth": 2, "id": 2, "name": "メンズファッション"}]


class FakeShop:
    """items: [(item_id, seller_id, price)] 全て category 9。1検索で取れるのは start+results<=1000 まで。"""

    def __init__(self, items):
        self.items = items
        self.calls = 0

    def get(self, session, url, params=None, **kw):
        self.calls += 1
        p = params
        assert int(p["start"]) + int(p["results"]) <= 1000, "APIの上限を超えたリクエスト"
        assert int(p["genre_category_id"]) == 9
        lo = int(p.get("price_from", 0))
        hi = int(p.get("price_to", 10**12))
        rows = [i for i in self.items if lo <= i[2] <= hi]
        order = {
            "-score": lambda i: i[0],
            "+price": lambda i: (i[2], i[0]),
            "-price": lambda i: (-i[2], i[0]),
            "-review_count": lambda i: -i[0],
        }[p["sort"]]
        rows.sort(key=order)
        s, n = int(p["start"]) - 1, int(p["results"])
        hits = [{"seller": {"sellerId": sid, "name": sid}, "parentGenreCategories": CHAIN,
                 "genreCategory": {"id": 9, "name": "その他メンズファッション", "depth": 3}} for _, sid, _ in rows[s:s + n]]
        return FakeResponse(200, json_data={"totalResultsAvailable": len(rows), "hits": hits})


def run(monkeypatch, tmp_path, items, **kw):
    shop = FakeShop(items)
    monkeypatch.setattr(api, "get_with_retry", shop.get)
    conn = db.connect(str(tmp_path / "t.db"))
    crawler = partition.Crawler(conn, None, "APPID", delay=0, sleep=lambda s: None, log=lambda m: None, **kw)
    crawler.crawl(TARGET)
    return conn, crawler, shop


def test_collects_every_seller_beyond_the_999_item_limit(monkeypatch, tmp_path):
    rng = random.Random(1)
    # 6000商品 / 400店舗。最初の999件(-score順=item_id順)だけでは全店舗には届かない
    items = [(n, f"shop{rng.randrange(400)}", rng.choice([300, 800, 1200, 2500, 3900, 9800, 15000, 49000, 250000])) for n in range(6000)]
    expected = {sid for _, sid, _ in items}
    first_page_only = {sid for _, sid, _ in sorted(items)[:999]}
    assert first_page_only < expected  # 前提: 上限だけでは取りこぼす

    conn, crawler, shop = run(monkeypatch, tmp_path, items)
    got = {r[0] for r in conn.execute("SELECT seller_id FROM sellers")}
    assert got == expected
    assert {r[0] for r in conn.execute("SELECT DISTINCT category FROM seller_categories")} == {"メンズファッション/その他メンズファッション"}
    assert crawler.saturated == 0


def test_same_price_items_over_limit_use_other_sorts(monkeypatch, tmp_path):
    # 1,000円ちょうどの商品が1500個 → 価格では分けられない。並び順を変えて両端から取る
    items = [(n, f"shop{n % 600}", 1000) for n in range(1500)]
    conn, crawler, _ = run(monkeypatch, tmp_path, items)
    assert crawler.saturated == 1
    got = {r[0] for r in conn.execute("SELECT seller_id FROM sellers")}
    assert got == {f"shop{n}" for n in range(600)}


def test_resume_after_deadline_does_not_repeat_finished_work(monkeypatch, tmp_path):
    items = [(n, f"shop{n % 50}", 100 + n * 7) for n in range(3000)]
    shop = FakeShop(items)
    monkeypatch.setattr(api, "get_with_retry", shop.get)
    conn = db.connect(str(tmp_path / "t.db"))

    # 1回目: すぐ時間切れになるが、分割までは進める
    n_calls = {"n": 0}
    real_get = shop.get

    def limited(*a, **k):
        n_calls["n"] += 1
        if n_calls["n"] > 6:
            raise partition.Deadline()
        return real_get(*a, **k)

    monkeypatch.setattr(api, "get_with_retry", limited)
    c1 = partition.Crawler(conn, None, "A", delay=0, sleep=lambda s: None, log=lambda m: None)
    with pytest.raises(partition.Deadline):
        c1.crawl(TARGET)
    assert db.unit_state(conn, "9:all") == "split"

    # 2回目: 完了したユニットはAPIを叩かない
    monkeypatch.setattr(api, "get_with_retry", real_get)
    done_units = {r[0] for r in conn.execute("SELECT unit FROM crawl_units WHERE state='done'")}
    shop.calls = 0
    c2 = partition.Crawler(conn, None, "A", delay=0, sleep=lambda s: None, log=lambda m: None)
    c2.crawl(TARGET)
    assert {r[0] for r in conn.execute("SELECT seller_id FROM sellers")} == {f"shop{n}" for n in range(50)}
    c3_calls = shop.calls
    c3 = partition.Crawler(conn, None, "A", delay=0, sleep=lambda s: None, log=lambda m: None)
    c3.crawl(TARGET)
    assert shop.calls == c3_calls  # 完了後の再実行は何もしない
    assert done_units  # 1回目の完了分は保存されていた


def test_children_splits_without_overlap_or_gap():
    bands = partition.children(None, None)
    assert bands[0][0] == 0 and bands[-1][1] == 99_999_999
    assert all(b[1] + 1 == n[0] for b, n in zip(bands, bands[1:]))
    lo, hi = 500, 999
    a, b = partition.children(lo, hi)
    assert a == (500, 749) and b == (750, 999)
    assert partition.children(1000, 1000) == []


# ---------- 幅優先(パス) ----------
def test_max_level_defers_deeper_bands_and_next_pass_resumes(monkeypatch, tmp_path):
    rng = random.Random(3)
    # 500〜999円の価格帯に商品が集中(約3000件 > 999件)。ほかの価格帯は少ない
    items = [(n, f"shop{rng.randrange(300)}", rng.randrange(500, 1000) if n % 5 < 3 else rng.choice([300, 1200, 2500, 9800]))
             for n in range(5000)]
    shop = FakeShop(items)
    calls = []

    def recording(*a, **k):
        calls.append(dict(k["params"]))
        return shop.get(*a, **k)

    monkeypatch.setattr(api, "get_with_retry", lambda session, url, params=None, **kw: recording(session, url, params=params, **kw))
    conn = db.connect(str(tmp_path / "t.db"))
    c = partition.Crawler(conn, None, "A", delay=0, sleep=lambda s: None, log=lambda m: None, max_level=1)
    c.crawl(TARGET)
    bands = {(lo, hi) for lo, hi in partition.children(None, None)}
    seen = {(int(p["price_from"]), int(p["price_to"])) for p in calls if "price_from" in p}
    assert seen <= bands            # 初期の価格帯より細かい検索はしていない
    assert c.deferred > 0           # 見送った価格帯がある(=まだ終わっていない)

    c.max_level, c.deferred = None, 0   # 次のパス: 続きから最後まで
    c.crawl(TARGET)
    assert c.deferred == 0
    assert {r[0] for r in conn.execute("SELECT seller_id FROM sellers")} == {sid for _, sid, _ in items}


def test_cli_passes_visit_all_categories_before_going_deeper(tmp_path, monkeypatch):
    """カテゴリーAを最後まで掘ってからBに行くのではなく、全カテゴリーを粗い価格帯で一巡してから深く掘る。"""
    from shoplist_crawler import cli

    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(partition.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.categories, "list_targets", lambda *a, **k: [categories.Target(1, 3, "A"), categories.Target(2, 3, "B")])
    bands = set(partition.children(None, None))
    calls = []

    def fake_pages(session, appid, *, genre_category_id, price_from=None, price_to=None, **kw):
        calls.append((genre_category_id, price_from, price_to))
        # 低価格帯(2000円未満)に商品が集中している想定。幅100円未満まで割れば999件に収まる
        wide = price_from is None or (price_from < 2000 and (price_to - price_from) >= 100)
        yield (5000 if wide else 10), [{"seller": {"sellerId": f"s{genre_category_id}-{price_from}", "name": "n"},
                                          "parentGenreCategories": CHAIN, "genreCategory": {"id": 9, "name": "x", "depth": 3}}]

    monkeypatch.setattr(cli.api, "iter_pages", fake_pages)
    assert cli.main(["--db", str(tmp_path / "t.db"), "collect-categories", "--appid", "X", "--delay", "0"]) == 0
    level1 = [i for i, (cid, lo, hi) in enumerate(calls) if lo is not None and (lo, hi) in bands]
    deeper = [i for i, (cid, lo, hi) in enumerate(calls) if lo is not None and (lo, hi) not in bands]
    assert level1 and deeper
    assert max(level1) < min(deeper)  # 全カテゴリーの粗い価格帯が先
    assert {calls[i][0] for i in level1} == {1, 2}
