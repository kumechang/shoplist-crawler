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
    assert db.counts(conn) == {"total": 3, "pending": 1, "ok": 1, "closed": 0, "not_found": 0, "error": 1}


def _seed(tmp_path, info_html):
    conn = db.connect(str(tmp_path / "t.db"))
    db.add_ids(conn, [("example-shop", ""), ("gone", ""), ("nocat", "")])
    db.save_result(conn, "example-shop", "ok", info.parse_seller(info_html), "")
    db.save_result(conn, "nocat", "ok", info.parse_seller(info_html), "")
    db.save_result(conn, "gone", "not_found", None, "")
    db.add_seller_categories(conn, [
        ("example-shop", "メンズファッション/その他メンズファッション"),
        ("example-shop", "レディースファッション/バッグ"),
        ("example-shop", "レディースファッション/バッグ"),  # 重複は1つにまとまる
        ("gone", "x/y"),
    ])
    return conn


def test_export_sample_format_one_row_per_category(tmp_path, info_html):
    conn = _seed(tmp_path, info_html)
    out = tmp_path / "o.csv"
    assert db.export_csv(conn, str(out)) == 3  # example-shop x2カテゴリー + nocat(カテゴリー空欄) / goneは対象外
    raw = out.read_bytes().decode("utf-8-sig")
    assert raw.splitlines()[0] == ",".join(info.SAMPLE_HEADERS)
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert [(r["URL"].split("/")[3], r["カテゴリー"]) for r in rows] == [
        ("example-shop", "メンズファッション/その他メンズファッション"),
        ("example-shop", "レディースファッション/バッグ"),
        ("nocat", ""),
    ]
    assert rows[0]["郵便番号"] == "〒100-0001"


def test_export_full_format(tmp_path, info_html):
    conn = _seed(tmp_path, info_html)
    out = tmp_path / "o.csv"
    assert db.export_csv(conn, str(out), "full") == 2
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert rows[0]["seller_id"] == "example-shop"
    assert rows[0]["url"] == "https://store.shopping.yahoo.co.jp/example-shop/"
    assert rows[0]["company_address"].startswith("東京都千代田区")


def test_crawl_unit_progress(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    assert db.unit_state(conn, "1:all") is None
    db.set_unit(conn, "1:all", "split", 5000)
    db.set_unit(conn, "1:all", "done", 5000)  # 上書きできる
    assert db.unit_state(conn, "1:all") == "done" and db.unit_state(conn, "1:0-499") is None
