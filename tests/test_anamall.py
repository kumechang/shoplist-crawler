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


# ---------- 店舗ごとの書式の違い(実際に見つかった4通りを、架空の内容で再現) ----------
def _page(*lines: str) -> str:
    nav = "<ul><li>会社概要</li><li>配送について</li><li>お問い合わせ先</li></ul>"
    return "<html><body>" + nav + "<br>".join(lines) + "</body></html>"


SHIPPING = ["配送について", "ヤマト運輸でお届けします。", "TEL：0120-111-111 は運送会社の番号です", "お支払について"]


def test_variant_bullet_prefixed_labels():  # 「●販売事業者名：…」
    html = _page(
        "会社概要", "●販売事業者名：架空株式会社", "●ショップ名：架空ショップ ANA Mall店",
        "●所在地：〒150-0001 東京都渋谷区1-2-3", "●代表者：佐藤 一郎", "●TEL：03-1111-2222",
        *SHIPPING, "お問い合わせ先", "窓口：架空株式会社", "TEL：03-3333-4444", "営業時間：10:00-17:00", "E-mail：help@example.org",
    )
    s = anamall.parse_guide(html)
    assert s["businessKind"] == "架空株式会社" and s["companyPost"] == "150-0001" and s["companyPerson"] == "佐藤 一郎"
    assert s["contactTel"] == "03-3333-4444" and s["contactMail"] == "help@example.org"  # 配送の説明の電話番号は拾わない


def test_variant_bracket_headings_with_value_on_next_line():  # 「【販売事業者名】」の次の行に値
    html = _page(
        "会社概要", "【販売事業者名】", "架空商事株式会社", "【ショップ名】", "架空商事 ANA Mall店",
        "【所在地】", "〒530-0001 大阪府大阪市北区1-1", "【代表者】", "田中 次郎", "TEL：06-1111-2222 FAX：06-1111-3333",
        *SHIPPING, "お問い合わせ先", "TEL：06-5555-6666",
    )
    s = anamall.parse_guide(html)
    assert (s["businessKind"], s["name"], s["companyPost"], s["companyPerson"]) == ("架空商事株式会社", "架空商事 ANA Mall店", "530-0001", "田中 次郎")
    assert s["contactTel"] == "06-5555-6666" and "contactMail" not in s


def test_variant_other_label_names_and_company_phone_fallback():  # 「販売業者」「事業責任者」。お問い合わせ先に電話がなければ会社の電話
    html = _page(
        "会社概要", "販売業者：架空酒販株式会社", "ショップ名：架空酒販 ANA Mall店", "事業責任者：鈴木 三郎",
        "所在地：神奈川県横浜市1-1", "TEL：045-111-2222", *SHIPPING, "お問い合わせ先", "窓口の名称:カスタマーサポート", "お問い合わせフォーム:https://example.org/contact",
    )
    s = anamall.parse_guide(html)
    assert s["businessKind"] == "架空酒販株式会社" and s["contactPerson"] == "鈴木 三郎"
    assert s["companyAddress"] == "神奈川県横浜市1-1" and "companyPost" not in s
    assert s["contactTel"] == "045-111-2222" and "companyTel" not in s


def test_free_text_format_is_reported_not_crashed(monkeypatch):  # 項目ではなく文章で書かれた書式
    html = _page("会社概要", "1.事業者の名称・住所・代表者氏名", "当社は架空株式会社(東京都港区)、代表は山田です。", "お問い合わせ先", "TEL：03-0000-0000")
    assert anamall.parse_guide(html) is None
    monkeypatch.setattr(anamall, "get_with_retry", lambda *a, **k: FakeResponse(200, html))
    r = anamall.fetch_guide(None, "0010")
    assert r.status == "error" and r.layout is True


def test_label_in_middle_of_line_does_not_crash(monkeypatch):
    """以前は、行の途中にだけ「販売事業者名」がある書式で StopIteration になり、実行全体が落ちた。"""
    html = _page("会社概要", "※販売事業者名は下記のとおりです。", "お問い合わせ先", "TEL：03-0000-0000")
    assert anamall.parse_guide(html) is None
    monkeypatch.setattr(anamall, "parse_guide", lambda h: (_ for _ in ()).throw(ValueError("想定外")))
    monkeypatch.setattr(anamall, "get_with_retry", lambda *a, **k: FakeResponse(200, "<html></html>"))
    r = anamall.fetch_guide(None, "0001")
    assert r.status == "error" and r.layout and "ValueError" in r.error  # 例外も、その店舗だけのエラーにする


def test_layout_errors_do_not_abort_the_run_but_network_errors_do(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    conn = db.connect(dbp)
    db.add_ids(conn, [(f"{i:04d}", "") for i in range(40)])
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    from shoplist_crawler.info import FetchResult
    calls = []
    monkeypatch.setattr(cli.anamall, "fetch_guide", lambda s, sid: (calls.append(sid), FetchResult("error", error="書式", layout=True))[1])
    assert cli.main(["--db", dbp, "fetch", "--site", "anamall", "--delay", "0"]) == 0
    assert len(calls) == 40  # 書式エラーが40件続いても、止まらない
    calls.clear()
    conn.execute("UPDATE sellers SET status=NULL, error=NULL")
    conn.commit()
    monkeypatch.setattr(cli.anamall, "fetch_guide", lambda s, sid: (calls.append(sid), FetchResult("error", error="HTTP 503"))[1])
    assert cli.main(["--db", dbp, "fetch", "--site", "anamall", "--delay", "0"]) == 1
    assert len(calls) == cli.MAX_CONSECUTIVE_ERRORS  # 通信エラーが続くときは、従来どおり止まる
