import pytest

from shoplist_crawler import cli, db, info, partition
from conftest import FakeResponse


def test_collect_requires_appid(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("YAHOO_APP_ID", raising=False)
    assert cli.main(["--db", str(tmp_path / "t.db"), "collect", "-q", "x"]) == 2
    assert "appid" in capsys.readouterr().err


def test_add_ids_then_fetch_then_export(tmp_path, monkeypatch, info_html):
    dbp = str(tmp_path / "t.db")
    monkeypatch.setattr(info, "get_with_retry", lambda *a, **k: FakeResponse(200, info_html))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["--db", dbp, "add-ids", "example-shop"]) == 0
    assert cli.main(["--db", dbp, "fetch", "--delay", "0"]) == 0
    out = tmp_path / "o.csv"
    assert cli.main(["--db", dbp, "export", "--out", str(out)]) == 0
    assert "サンプルストア" in out.read_text(encoding="utf-8-sig")
    assert "お問い合わせメールアドレス" in out.read_text(encoding="utf-8-sig")
    assert db.counts(db.connect(dbp))["ok"] == 1


def test_collect_keeps_ids_found_before_api_error(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    def fake_iter(session, appid, query, *, sort, delay):
        yield "a", "A"
        yield "b", "B"
        raise cli.api.ApiError("HTTP 400: bad", 400)

    monkeypatch.setattr(cli.api, "iter_sellers", fake_iter)
    rc = cli.main(["--db", dbp, "collect", "--appid", "X", "-q", "q1", "-q", "q2"])
    assert rc == 0  # 400は飛ばして次のクエリへ(3連続で中断)
    assert db.counts(db.connect(dbp))["total"] == 2


def test_collect_aborts_on_auth_error(tmp_path, monkeypatch):
    def fake_iter(session, appid, query, *, sort, delay):
        raise cli.api.ApiError("HTTP 403", 403)
        yield

    monkeypatch.setattr(cli.api, "iter_sellers", fake_iter)
    assert cli.main(["--db", str(tmp_path / "t.db"), "collect", "--appid", "X", "-q", "q1", "-q", "q2"]) == 1


def test_collect_stops_gracefully_on_rate_limit(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    calls = []

    def fake_iter(session, appid, query, *, sort, delay):
        calls.append(query)
        yield "a", "A"
        raise cli.api.ApiError("HTTP 429", 429)

    monkeypatch.setattr(cli.api, "iter_sellers", fake_iter)
    rc = cli.main(["--db", dbp, "collect", "--appid", "X", "-q", "q1", "-q", "q2"])
    assert rc == 3
    assert calls == ["q1"]  # 429後は叩き続けない
    assert db.counts(db.connect(dbp))["total"] == 1  # それまでの分は保存


def test_sample_prints_fetched_stores(tmp_path, monkeypatch, info_html, capsys):
    dbp = str(tmp_path / "t.db")
    conn = db.connect(dbp)
    db.add_ids(conn, [("example-shop", "")])
    db.save_result(conn, "example-shop", "ok", info.parse_seller(info_html), "")
    assert cli.main(["--db", dbp, "sample", "-n", "3"]) == 0
    out = capsys.readouterr().out
    assert "store_name: サンプルストア" in out and "seller_id: example-shop" in out


def _hit(sid, leaf="その他メンズファッション", parents=None):
    parents = parents or [
        {"depth": 1, "id": 13457, "name": "ファッション"},
        {"depth": 2, "id": 2495, "name": "メンズファッション"},
    ]
    return {"seller": {"sellerId": sid, "name": sid.upper()}, "parentGenreCategories": parents,
            "genreCategory": {"id": 9, "name": leaf, "depth": 3}}


def _patch_targets(monkeypatch, *targets):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(partition.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.categories, "list_targets", lambda *a, **k: list(targets))


def test_collect_categories_saves_sellers_with_category_and_resumes(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    _patch_targets(monkeypatch, cli.categories.Target(9, 3, "その他メンズファッション"))
    calls = []

    def fake_pages(session, appid, **kw):
        calls.append(kw["genre_category_id"])
        yield 2, [_hit("a"), _hit("b")]

    monkeypatch.setattr(cli.api, "iter_pages", fake_pages)
    args = ["--db", dbp, "collect-categories", "--appid", "X", "--delay", "0"]
    assert cli.main(args) == 0
    assert calls == [9]  # 総件数2件(<=999)なので1回で完了
    conn = db.connect(dbp)
    assert conn.execute("SELECT seller_id, category FROM seller_categories ORDER BY 1").fetchall() == [
        ("a", "メンズファッション/その他メンズファッション"), ("b", "メンズファッション/その他メンズファッション")]
    assert db.counts(conn)["total"] == 2
    assert cli.main(args) == 0  # 再実行しても何も検索しない(再開機能)
    assert calls == [9]


def test_collect_categories_429_keeps_partial_and_stops(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    _patch_targets(monkeypatch, cli.categories.Target(1, 3, "c1"), cli.categories.Target(2, 3, "c2"))
    calls = []

    def fake_pages(session, appid, **kw):
        calls.append(kw["genre_category_id"])
        yield 500, [_hit("a")]
        raise cli.api.ApiError("HTTP 429", 429)

    monkeypatch.setattr(cli.api, "iter_pages", fake_pages)
    assert cli.main(["--db", dbp, "collect-categories", "--appid", "X", "--delay", "0"]) == 3
    assert calls == [1]  # 2つ目のカテゴリーは叩かない
    conn = db.connect(dbp)
    assert db.counts(conn)["total"] == 1  # それまでの分は保存
    assert db.unit_state(conn, "1:all") is None  # 未完了なので次回やり直す


def test_collect_categories_list_only(tmp_path, monkeypatch, capsys):
    _patch_targets(monkeypatch, cli.categories.Target(9, 3, "名前"))
    monkeypatch.setattr(cli.api, "iter_pages", lambda *a, **k: pytest.fail("検索してはいけない"))
    assert cli.main(["--db", str(tmp_path / "t.db"), "collect-categories", "--appid", "X", "--list-only"]) == 0
    assert "9\t3\t名前" in capsys.readouterr().out


def test_collect_categories_estimate(tmp_path, monkeypatch, capsys):
    _patch_targets(monkeypatch, cli.categories.Target(1, 3, "c1"), cli.categories.Target(2, 3, "c2"))
    monkeypatch.setattr(cli.api, "iter_pages", lambda *a, **k: iter([(1000, []) if k["genre_category_id"] == 1 else (2000, [])]))
    assert cli.main(["--db", str(tmp_path / "t.db"), "collect-categories", "--appid", "X", "--estimate"]) == 0
    out = capsys.readouterr().out
    assert "c1: 1,000 件" in out and "合計 約3,000 件" in out and "約66 回" in out


def test_pending_count(tmp_path, capsys):
    dbp = str(tmp_path / "t.db")
    conn = db.connect(dbp)
    db.add_ids(conn, [("a", ""), ("b", ""), ("c", "")])
    db.save_result(conn, "a", "ok", {"name": "x"}, "")
    assert cli.main(["--db", dbp, "pending-count"]) == 0
    assert capsys.readouterr().out.strip() == "2"


def test_done_file_only_when_discovery_finished(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    done = tmp_path / "discover.done"
    _patch_targets(monkeypatch, cli.categories.Target(9, 3, "x"))
    args = ["--db", dbp, "collect-categories", "--appid", "X", "--delay", "0", "--done-file", str(done)]

    def finished(session, appid, **kw):
        yield 2, [_hit("a")]

    monkeypatch.setattr(cli.api, "iter_pages", finished)
    assert cli.main(args) == 0 and done.exists()  # 最後まで終わった

    # 前回のファイルは、今回終わらなければ残さない(時間切れ)
    conn = db.connect(dbp)
    conn.execute("DELETE FROM crawl_units")
    conn.commit()
    done.write_text("done\n")

    def endless(session, appid, **kw):
        yield 50000, [_hit("b")]

    monkeypatch.setattr(cli.api, "iter_pages", endless)
    clock = iter([0.0])  # 最初の1回(締め切りの計算)だけ0。以降は十分に先の時刻 → すぐ時間切れになる
    monkeypatch.setattr(partition.time, "monotonic", lambda: next(clock, 10**12))
    assert cli.main(args + ["--max-minutes", "1"]) == 0
    assert not done.exists()
