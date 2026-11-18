import requests

from shoplist_crawler import info
from conftest import FakeResponse


def test_parse_seller(info_html):
    s = info.parse_seller(info_html)
    assert s["id"] == "example-shop"
    assert s["companyAddress"] == "東京都千代田区千代田1-1サンプルビル 3F"


def test_parse_seller_returns_none_when_layout_changed():
    assert info.parse_seller("<html>no data</html>") is None
    broken = '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'
    assert info.parse_seller(broken) is None


def test_to_row_composes_contact_address(info_html):
    row = info.to_row(info.parse_seller(info_html))
    assert row["store_name"] == "サンプルストア"
    assert row["contact_address"] == "東京都千代田区千代田 1-1サンプルビル 3F"  # 建物名まで含む
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


def test_to_sample_row_matches_requested_columns(info_html):
    row = info.to_sample_row("example-shop", info.parse_seller(info_html))
    assert row == {
        "URL": "https://store.shopping.yahoo.co.jp/example-shop/info.html",
        "会社名（商号）": "株式会社サンプル",
        "郵便番号": "〒100-0001",
        "住所": "東京都千代田区千代田 1-1サンプルビル 3F",
        "代表者": "山田太郎",  # 日本語の姓名の間の空白は除く(サンプルに合わせる)
        "ストア名": "サンプルストア",
        "ストア名（フリガナ）": "サンプルストア",
        "運営責任者": "鈴木 花子",
        "お問い合わせ電話番号": "03-0000-0000",
        "お問い合わせメールアドレス": "info@example.com",
    }


def test_to_sample_row_falls_back_to_company_address():
    row = info.to_sample_row("x", {"companyAddress": "大阪府大阪市1-1", "companyPost": ""})
    assert row["住所"] == "大阪府大阪市1-1" and row["郵便番号"] == ""


def test_squash_japanese_spaces_keeps_latin_names():
    f = info.squash_japanese_spaces
    assert f("伊藤 憲博") == "伊藤憲博" and f("名鏡\u3000美和子") == "名鏡美和子"
    assert f("John Smith") == "John Smith"
    assert f("田中 John") == "田中 John"
