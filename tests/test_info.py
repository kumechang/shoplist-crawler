import requests

from shoplist_crawler import info
from conftest import FakeResponse


def test_parse_seller(info_html):
    s = info.parse_seller(info_html)
    assert s["id"] == "example-shop"
    assert s["companyAddress"] == "東京都千代田区千代田1-1"


def test_parse_seller_returns_none_when_layout_changed():
    assert info.parse_seller("<html>no data</html>") is None
    broken = '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'
    assert info.parse_seller(broken) is None


def test_to_row_composes_contact_address(info_html):
    row = info.to_row(info.parse_seller(info_html))
    assert row["store_name"] == "サンプルストア"
    assert row["contact_address"] == "東京都千代田区千代田 1-1"
    assert row["contact_mail"] == "info@example.com"
    assert row["is_open"] is True


def test_fetch_info_statuses(monkeypatch, info_html):
    def run(resp):
        monkeypatch.setattr(info, "get_with_retry", lambda *a, **k: resp)
        return info.fetch_info(None, "x")

    assert run(FakeResponse(200, info_html)).status == "ok"
    assert run(FakeResponse(404)).status == "not_found"
    assert run(FakeResponse(403)).status == "error"
    r = run(FakeResponse(200, "<html></html>"))
    assert r.status == "error" and "__NEXT_DATA__" in r.error


def test_fetch_info_not_found_flag(monkeypatch):
    html = '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"seller":{"isNotFound":true}}}}</script>'
    monkeypatch.setattr(info, "get_with_retry", lambda *a, **k: FakeResponse(200, html))
    assert info.fetch_info(None, "x").status == "not_found"


def test_fetch_info_network_error(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(info, "get_with_retry", boom)
    r = info.fetch_info(None, "x")
    assert r.status == "error" and "ConnectionError" in r.error
