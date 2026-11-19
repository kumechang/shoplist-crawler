from pathlib import Path

import pytest
import requests

from shoplist_crawler import anamall, cli, db
from conftest import FIXTURES, FakeResponse


@pytest.fixture
def shops_html():
    return (FIXTURES / "anamall_shops.html").read_text(encoding="utf-8")


@pytest.fixture
def guide_html():
    return (FIXTURES / "anamall_guide.html").read_text(encoding="utf-8")


def test_parse_shop_list_dedupes_and_reads_group_mark(shops_html):
    got = anamall.parse_shop_list(shops_html)
    assert got == [
        ("0101", "サンプルショップ ANA Mall店", ""),
        ("0202", "ANAグループ直営店 & テスト", "ANAグループ"),  # data-cate に値がある店舗だけカテゴリーが付く
    ]  # 重複リンク・「おすすめ」欄の店(<li data-kana> の外)は拾わない


def test_parse_guide_reads_company_block_and_contact_section(guide_html):
    assert anamall.parse_guide(guide_html) == {
        "businessKind": "株式会社サンプル",
        "name": "サンプルショップ ANA Mall店",
        "companyPost": "100-0001",
        "companyAddress": "東京都千代田区千代田1-1 サンプルビル3F",
        "companyPerson": "山田 太郎",
        "contactPerson": "鈴木 花子",
        "securityPerson": "鈴木 花子",
        "contactTel": "03-0000-0000",  # 配送の説明にある 0120-000-000 ではなく、お問い合わせ先の欄から
        "contactFax": "03-0000-0001",
        "contactMail": "shop@example.com",
    }


def test_parse_guide_without_contact_details_and_layout_change(guide_html):
    no_contact = guide_html[: guide_html.index("<section>\n <h3>お問い合わせ先")]
    s = anamall.parse_guide(no_contact)
    assert s["businessKind"] == "株式会社サンプル" and "contactTel" not in s and "contactMail" not in s
    assert anamall.parse_guide("<html><body>何もない</body></html>") is None


def test_fetch_guide_statuses(monkeypatch, guide_html):
    def run(resp):
        monkeypatch.setattr(anamall, "get_with_retry", lambda *a, **k: resp)
        return anamall.fetch_guide(None, "0101")

    assert run(FakeResponse(200, guide_html)).status == "ok"
    assert run(FakeResponse(404)).status == "not_found"
    assert run(FakeResponse(503)).status == "error"
    r = run(FakeResponse(200, "<html>変わった</html>"))
    assert r.status == "error" and "会社概要" in r.error

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(anamall, "get_with_retry", boom)
    assert anamall.fetch_guide(None, "0101").status == "error"


def test_fetch_shop_list_errors(monkeypatch):
    monkeypatch.setattr(anamall, "get_with_retry", lambda *a, **k: FakeResponse(503))
    with pytest.raises(RuntimeError):
        anamall.fetch_shop_list(None)
    monkeypatch.setattr(anamall, "get_with_retry", lambda *a, **k: FakeResponse(200, "<html></html>"))
    with pytest.raises(RuntimeError, match="1件も"):
        anamall.fetch_shop_list(None)


def test_to_sample_row(guide_html):
    row = anamall.to_sample_row("0101", anamall.parse_guide(guide_html))
    assert row == {
        "URL": "https://anamall.ana.co.jp/shop/shop/guide.aspx?shop=0101",
        "会社名（商号）": "株式会社サンプル",
        "郵便番号": "〒100-0001",
        "住所": "東京都千代田区千代田1-1 サンプルビル3F",
        "代表者": "山田太郎",
        "ストア名": "サンプルショップ ANA Mall店",
        "ストア名（フリガナ）": "",
        "運営責任者": "鈴木 花子",
        "お問い合わせ電話番号": "03-0000-0000",
        "お問い合わせメールアドレス": "shop@example.com",
    }


def test_cli_discover_fetch_export_for_anamall(tmp_path, monkeypatch, shops_html, guide_html, capsys):
    dbp = str(tmp_path / "ana.db")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(anamall, "get_with_retry", lambda session, url, **k: FakeResponse(200, shops_html if "shops.aspx" in url else guide_html))

    assert cli.main(["--db", dbp, "anamall-discover"]) == 0
    assert "ショップ一覧 2 店舗 / 新規 2 店舗" in capsys.readouterr().out
    assert cli.main(["--db", dbp, "anamall-discover"]) == 0  # 2回目は新規0
    assert "新規 0 店舗" in capsys.readouterr().out

    assert cli.main(["--db", dbp, "fetch", "--site", "anamall", "--delay", "0"]) == 0
    assert db.counts(db.connect(dbp))["ok"] == 2

    out = tmp_path / "ana.csv"
    assert cli.main(["--db", dbp, "export", "--site", "anamall", "--out", str(out)]) == 0
    import csv
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert [(r["URL"][-4:], r["カテゴリー"]) for r in rows] == [("0101", ""), ("0202", "ANAグループ")]
    assert rows[0]["会社名（商号）"] == "株式会社サンプル"


def test_anamall_default_delay_follows_crawl_delay(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_run_fetch_loop", lambda conn, targets, save, delay, fetch=None: seen.append((delay, fetch)) or 0)
    dbp = str(tmp_path / "t.db")
    cli.main(["--db", dbp, "fetch", "--site", "anamall"])
    cli.main(["--db", dbp, "fetch"])
    cli.main(["--db", dbp, "refresh", "--site", "anamall"])
    cli.main(["--db", dbp, "fetch", "--site", "anamall", "--delay", "3"])
    assert [d for d, _ in seen] == [10.0, 2.0, 10.0, 3.0]
    assert seen[0][1] is anamall.fetch_guide and seen[1][1] is None
