from shoplist_crawler import cli, db, info
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
