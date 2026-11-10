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
