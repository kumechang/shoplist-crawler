"""③既存店舗の情報更新(refresh) と ①新店舗の発見のやり直し(recrawl) のテスト。"""
import csv
import json
import sqlite3

from shoplist_crawler import api, categories, cli, db, info, partition
from conftest import FakeResponse

OLD = "2000-01-01T00:00:00+00:00"


def seller(**kw):
    base = {"name": "店A", "businessKind": "株式会社A", "contactTel": "03-0000-0000", "contactMail": "a@example.com",
            "companyPost": "100-0001", "companyAddress": "東京都千代田区1-1", "ratingAverage": 4.5, "isOpen": True}
    return {**base, **kw}


def add_ok(conn, sid, s, fetched_at=OLD):
    db.add_ids(conn, [(sid, "")])
    db.save_result(conn, sid, "ok", s, "")
    conn.execute("UPDATE sellers SET fetched_at=? WHERE seller_id=?", (fetched_at, sid))
    conn.commit()


# ---------- DB ----------
def test_connect_migrates_old_schema(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE sellers (seller_id TEXT PRIMARY KEY, api_name TEXT, discovered_at TEXT NOT NULL,
        fetched_at TEXT, status TEXT, error TEXT, raw_json TEXT);
        INSERT INTO sellers(seller_id, discovered_at, status) VALUES ('x', 'd', 'ok');""")
    old.commit(); old.close()
    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sellers)")}
    assert {"closed_at", "last_changed_at"} <= cols
    assert db.counts(conn)["ok"] == 1  # 既存データは残る


def test_refresh_records_changes_and_ignores_rating_noise(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    add_ok(conn, "a", seller())
    new = seller(contactTel="06-1111-1111", ratingAverage=3.9)  # 評価の変化だけでは変更扱いにならない
    assert db.save_refresh(conn, "a", "ok", new, "") == "ok(変更 1 項目)"
    rows = list(db.iter_changes(conn, 1))
    assert [(r["項目"], r["変更前"], r["変更後"]) for r in rows] == [("お問い合わせ電話番号", "03-0000-0000", "06-1111-1111")]
    assert json.loads(conn.execute("SELECT raw_json FROM sellers").fetchone()[0])["contactTel"] == "06-1111-1111"
    assert conn.execute("SELECT last_changed_at FROM sellers").fetchone()[0]
    # 変更がなければ履歴は増えず、取得時刻だけ更新される
    before = conn.execute("SELECT fetched_at FROM sellers").fetchone()[0]
    assert db.save_refresh(conn, "a", "ok", new, "") == "ok(変更なし)"
    assert len(list(db.iter_changes(conn, 1))) == 1
    assert conn.execute("SELECT fetched_at FROM sellers").fetchone()[0] >= before


def test_refresh_closed_keeps_last_data_and_reopen(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    add_ok(conn, "a", seller())
    assert db.save_refresh(conn, "a", "not_found", None, "").startswith("closed")
    status, raw, closed_at = conn.execute("SELECT status, raw_json, closed_at FROM sellers").fetchone()
    assert status == "closed" and closed_at and json.loads(raw)["name"] == "店A"  # 最後の内容は残る
    # 閉店済みがまた404でも、状態の変更は記録しない
    assert db.save_refresh(conn, "a", "not_found", None, "") == "closed"
    assert [r["項目"] for r in db.iter_changes(conn, 1)] == ["状態"]
    # 再開店
    assert db.save_refresh(conn, "a", "ok", seller(name="店A新"), "").startswith("ok")
    assert conn.execute("SELECT status, closed_at FROM sellers").fetchone() == ("ok", None)
    assert [(r["変更前"], r["変更後"]) for r in db.iter_changes(conn, 1) if r["項目"] == "状態"][0:2] == [("closed", "ok"), ("ok", "closed")]


def test_refresh_error_keeps_data_and_time(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    add_ok(conn, "a", seller())
    assert db.save_refresh(conn, "a", "error", None, "HTTP 503") == "error"
    status, fetched, err = conn.execute("SELECT status, fetched_at, error FROM sellers").fetchone()
    assert (status, fetched, err) == ("ok", OLD, "HTTP 503")  # データも取得時刻も変えない → 次回また対象になる
    assert not list(db.iter_changes(conn, 1))


def test_refresh_ids_selection_and_order(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    fresh = db._now()
    add_ok(conn, "old1", seller(), "2020-01-01T00:00:00+00:00")
    add_ok(conn, "old2", seller(), "2021-01-01T00:00:00+00:00")
    add_ok(conn, "fresh", seller(), fresh)
    add_ok(conn, "closed-old", seller(), "2020-06-01T00:00:00+00:00")
    db.save_refresh(conn, "closed-old", "not_found", None, "")
    conn.execute("UPDATE sellers SET fetched_at=? WHERE seller_id='closed-old'", ("2020-06-01T00:00:00+00:00",))
    add_ok(conn, "closed-recent", seller(), "2020-06-01T00:00:00+00:00")
    db.save_refresh(conn, "closed-recent", "not_found", None, "")  # 取得時刻=今 → 4倍の間隔にも届かない
    conn.commit()
    assert db.refresh_ids(conn, 90) == ["old1", "closed-old", "old2"]  # 古い順。freshは対象外
    assert db.refresh_ids(conn, 90, limit=1) == ["old1"]


def test_export_include_closed_and_full_columns(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    add_ok(conn, "a", seller())
    add_ok(conn, "b", seller(name="店B"))
    db.save_refresh(conn, "b", "not_found", None, "")
    out = tmp_path / "o.csv"
    assert db.export_csv(conn, str(out)) == 1                          # 既定では閉店は出さない
    assert db.export_csv(conn, str(out), include_closed=True) == 2
    assert db.export_csv(conn, str(out), "full", include_closed=True) == 2
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert {r["seller_id"]: r["status"] for r in rows} == {"a": "ok", "b": "closed"}


def test_recent_activity_and_changes_csv(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    add_ok(conn, "a", seller())
    db.save_refresh(conn, "a", "ok", seller(contactMail="new@example.com"), "")
    add_ok(conn, "b", seller())
    db.save_refresh(conn, "b", "not_found", None, "")
    assert db.recent_activity(conn, 30) == {"新規発見": 2, "変更あり": 1, "閉店検出": 1}
    out = tmp_path / "c.csv"
    assert db.export_changes(conn, str(out), 30) == 2
    assert next(csv.reader(open(out, encoding="utf-8-sig"))) == db.CHANGE_HEADERS


# ---------- ①新店舗の発見のやり直し ----------
def test_stale_units_are_crawled_again_and_new_sellers_found(monkeypatch, tmp_path):
    items = [(0, "shop-old", 1000)]
    calls = []

    def fake_get(session, url, params=None, **kw):
        calls.append(params)
        hits = [{"seller": {"sellerId": sid, "name": sid}, "parentGenreCategories": [{"depth": 2, "id": 2, "name": "メンズ"}],
                 "genreCategory": {"id": 9, "name": "靴", "depth": 3}} for _, sid, _ in items]
        return FakeResponse(200, json_data={"totalResultsAvailable": len(hits), "hits": hits})

    monkeypatch.setattr(api, "get_with_retry", fake_get)
    conn = db.connect(str(tmp_path / "t.db"))
    target = categories.Target(9, 3, "靴")
    mk = lambda days: partition.Crawler(conn, None, "A", delay=0, sleep=lambda s: None, log=lambda m: None, recrawl_days=days)  # noqa: E731

    mk(30).crawl(target)
    assert len(calls) == 1
    items.append((1, "shop-new", 1000))  # あとから新しい店舗ができた
    mk(30).crawl(target)
    assert len(calls) == 1  # 30日以内なら検索しない
    conn.execute("UPDATE crawl_units SET done_at=?", (OLD,))  # 進捗が古くなった状態にする
    conn.commit()
    c = mk(30)
    c.crawl(target)
    assert len(calls) == 2 and c.new_sellers == 1
    assert {r[0] for r in conn.execute("SELECT seller_id FROM sellers")} == {"shop-old", "shop-new"}
    # recrawl_days=None なら期限切れにならない(従来動作)
    conn.execute("UPDATE crawl_units SET done_at=?", (OLD,))
    conn.commit()
    mk(None).crawl(target)
    assert len(calls) == 2


# ---------- CLI ----------
def _stub_fetch(monkeypatch, mapping):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.info, "fetch_info", lambda session, sid: mapping[sid] if sid in mapping else info.FetchResult("error", error="HTTP 503"))


def test_cli_refresh_end_to_end(tmp_path, monkeypatch, capsys):
    dbp = str(tmp_path / "t.db")
    conn = db.connect(dbp)
    add_ok(conn, "a", seller())
    add_ok(conn, "b", seller())
    add_ok(conn, "new", seller(), db._now())  # 最近取得した店舗は対象外
    _stub_fetch(monkeypatch, {"a": info.FetchResult("ok", seller(contactTel="09-9999-9999")), "b": info.FetchResult("not_found")})
    assert cli.main(["--db", dbp, "refresh", "--older-than-days", "90"]) == 0
    out = capsys.readouterr().out
    assert "更新チェック対象 2 件" in out and "a: ok(変更 1 項目)" in out and "b: closed" in out
    assert "直近30日: {'新規発見': 3, '変更あり': 1, '閉店検出': 1}" in out
    assert cli.main(["--db", dbp, "changes", "--since-days", "1"]) == 0
    shown = capsys.readouterr().out
    assert "お問い合わせ電話番号: '03-0000-0000' → '09-9999-9999'" in shown and "状態: 'ok' → 'closed'" in shown
    assert cli.main(["--db", dbp, "status"]) == 0
    assert "'closed': 1" in capsys.readouterr().out


def test_cli_stops_after_consecutive_errors(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    conn = db.connect(dbp)
    for i in range(30):
        add_ok(conn, f"s{i:02d}", seller())
    calls = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.info, "fetch_info", lambda session, sid: (calls.append(sid), info.FetchResult("error", error="HTTP 503"))[1])
    assert cli.main(["--db", dbp, "refresh"]) == 1
    assert len(calls) == cli.MAX_CONSECUTIVE_ERRORS  # 叩き続けない
    assert db.counts(conn)["ok"] == 30  # エラーでデータは壊れない


def test_cli_fetch_also_stops_after_consecutive_errors(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    conn = db.connect(dbp)
    db.add_ids(conn, [(f"s{i:02d}", "") for i in range(30)])
    calls = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.info, "fetch_info", lambda session, sid: (calls.append(sid), info.FetchResult("error", error="x"))[1])
    assert cli.main(["--db", dbp, "fetch"]) == 1
    assert len(calls) == cli.MAX_CONSECUTIVE_ERRORS
