import csv

from shoplist_crawler import db, info


def test_add_ids_dedupes_and_tracks_pending(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    assert db.add_ids(conn, [("a", "A"), ("b", "B"), ("a", "A")]) == 2
    assert db.add_ids(conn, [("a", "A"), ("c", "C")]) == 1
    assert db.pending_ids(conn) == ["a", "b", "c"]
    assert db.pending_ids(conn, limit=2) == ["a", "b"]


def test_save_result_and_retry_errors(tmp_path, info_html):
    conn = db.connect(str(tmp_path / "t.db"))
    db.add_ids(conn, [("a", ""), ("b", ""), ("c", "")])
    db.save_result(conn, "a", "ok", info.parse_seller(info_html), "")
    db.save_result(conn, "b", "error", None, "HTTP 503")
    assert db.pending_ids(conn) == ["c"]
    assert db.pending_ids(conn, retry_errors=True) == ["b", "c"]
    assert db.counts(conn) == {"total": 3, "pending": 1, "ok": 1, "not_found": 0, "error": 1}


def test_export_csv(tmp_path, info_html):
    conn = db.connect(str(tmp_path / "t.db"))
    db.add_ids(conn, [("example-shop", ""), ("gone", "")])
    db.save_result(conn, "example-shop", "ok", info.parse_seller(info_html), "")
    db.save_result(conn, "gone", "not_found", None, "")
    out = tmp_path / "o.csv"
    assert db.export_csv(conn, str(out)) == 1
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert rows[0]["seller_id"] == "example-shop"
    assert rows[0]["url"] == "https://store.shopping.yahoo.co.jp/example-shop/"
    assert rows[0]["company_address"] == "東京都千代田区千代田1-1"
