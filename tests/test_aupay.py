import csv
import time

import pytest
import requests

from shoplist_crawler import aupay, cli, db


def sjis(html: str) -> bytes:
    return html.encode("cp932")


class Resp:
    def __init__(self, status=200, body=b""):
        self.status_code, self.content = status, body
        self.headers = {}


def result_page(rows, total, first):
    last = first + len(rows) - 1 if rows else 0
    items = "".join(f'<div class="shop_box"><p class="shop_name"><a href="/user/{i}?e=shopsearch10">{n}</a></p></div>' for i, n in rows)
    head = f"お店検索結果一覧 {first:,}件～{last:,}件(全{total}件)" if rows else "お店検索結果一覧 0 件 指定のキーワードに該当するお店はありませんでした"
    stray = '<a href="https://wowma.jp/user/26621972" class="x">広告</a>'  # 全ページに出る別のリンク(結果ではない)
    return sjis(f"<html><title>t</title><body>{stray}<h2>{head}</h2>{items}</body></html>")


KMEM = """<html><body><h2>注目のカテゴリ</h2></body>
<h3>お店の名前</h3><p><a href="/user/1">サンプルショップ au PAY マーケット店</a></p>
<h3>お店の会員番号</h3><p>12345678</p>
<h3>販売事業者名</h3><p>架空株式会社</p>
<h3>担当部署名</h3><p>EC事業部</p>
<h3>通信販売業務責任者</h3><p><div>鈴木 花子</div></p>
<h3>個人情報保護責任者</h3><p>鈴木 花子</p>
<h3>住所</h3><p>〒100-0001<br>東京都千代田区千代田1-1 サンプルビル3F</p>
<h3>電話番号</h3><p>03-1111-2222</p>
<h3>FAX番号</h3><p>03-1111-3333</p>
<h3>メールアドレス</h3><p>shop@example.com</p>
<h3>支払い・配送時期</h3><p>03-9999-9999 は別の番号</p>
</body></html>"""


def test_parse_search_reads_rows_and_total_with_thousands_separators():
    total, shops = aupay.parse_search(result_page([("111", "A店 au PAY マーケット店"), ("222", "B&C店")], 1691, 1681).decode("cp932"))
    assert total == 1691 and shops == [("111", "A店 au PAY マーケット店"), ("222", "B&C店")]  # 広告のリンクは拾わない


def test_parse_search_empty_result():
    assert aupay.parse_search(result_page([], 0, 1).decode("cp932")) == (0, [])


def test_parse_kmem_reads_labeled_values_and_optional_fields():
    s = aupay.parse_kmem(KMEM)
    assert s == {
        "name": "サンプルショップ au PAY マーケット店", "businessKind": "架空株式会社", "contactPerson": "鈴木 花子",
        "securityPerson": "鈴木 花子", "companyPost": "100-0001", "companyAddress": "東京都千代田区千代田1-1 サンプルビル3F",
        "contactTel": "03-1111-2222", "contactFax": "03-1111-3333", "contactMail": "shop@example.com",
    }
    minimal = aupay.parse_kmem("<h3>お店の名前</h3><p>X店</p><h3>販売事業者名</h3><p>X商事</p><h3>住所</h3><p>大阪府大阪市1-1</p>")
    assert minimal == {"name": "X店", "businessKind": "X商事", "companyAddress": "大阪府大阪市1-1"}  # 電話・メールなしでも読める
    assert aupay.parse_kmem("<html><h3>エラー</h3><p>店舗が見つかりません</p></html>") is None


def test_fetch_kmem_statuses(monkeypatch):
    def run(resp):
        monkeypatch.setattr(aupay, "get_with_retry", lambda *a, **k: resp)
        return aupay.fetch_kmem(None, "1")

    assert run(Resp(200, sjis(KMEM))).status == "ok"
    assert run(Resp(404)).status == "not_found"
    assert run(Resp(503)).status == "error"
    r = run(Resp(200, sjis("<html>エラー</html>")))
    assert r.status == "error" and r.layout


def test_search_url_is_shift_jis_encoded(monkeypatch):
    seen = []
    monkeypatch.setattr(aupay, "get_with_retry", lambda s, url, **k: seen.append(url) or Resp(200, result_page([], 0, 1)))
    aupay.search_page(None, "ショップ", 2)
    assert seen == ["https://wowma.jp/bep/m/klists1?sn=%83V%83%87%83b%83v&page=2"]  # UTF-8 では「該当なし」になる(実際に踏んだ)


def test_query_chars_normalizes_and_skips_unsearchable():
    c = aupay.query_chars(["ワールドｼｮｯﾌﾟ！", "店 店", "😀☆"])
    assert c["ワ"] == 1 and c["シ"] == 1 and c["店"] == 1  # 半角カナはNFKCで全角に。1店舗の名前に何度出ても1回
    assert "！" not in c and "😀" not in c and " " not in c
    assert aupay.usable_query("😀") is None and aupay.usable_query("ア") == "ア"


# ---------- 店舗の発見(1文字ずつ検索して、見つかった店舗名の文字を足す) ----------
def fake_site(monkeypatch, pages_by_query, calls):
    def search_page(session, query, page):
        calls.append((query, page))
        rows = pages_by_query.get(query, [])
        chunk = rows[(page - 1) * aupay.PAGE_SIZE : page * aupay.PAGE_SIZE]
        return len(rows), chunk

    monkeypatch.setattr(aupay, "search_page", search_page)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)


def test_discover_snowballs_new_characters_and_resumes(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    shops = [(str(1000 + i), f"店{i}") for i in range(70)]  # 「店」で70件(3ページ)。名前に数字も含む
    calls = []
    fake_site(monkeypatch, {"店": shops, "0": shops[:5]}, calls)
    conn = db.connect(dbp)
    db.add_queries(conn, aupay.SITE, ["店"], seed=True)
    db.bump_queries(conn, aupay.SITE, {"店": 99})  # 「店」を最優先にする
    real = aupay.search_page
    n = {"i": 0}

    def limited(session, query, page):  # 2ページ取ったところで、実行が中断される
        n["i"] += 1
        if n["i"] > 2:
            raise KeyboardInterrupt
        return real(session, query, page)

    monkeypatch.setattr(aupay, "search_page", limited)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["--db", dbp, "aupay-discover", "--delay", "0"])
    assert [c for c in calls] == [("店", 1), ("店", 2)]
    assert conn.execute("SELECT pages_done, done_at IS NULL FROM search_queries WHERE query='店'").fetchone() == (2, 1)

    monkeypatch.setattr(aupay, "search_page", real)
    calls.clear()
    assert cli.main(["--db", dbp, "aupay-discover", "--delay", "0"]) == 0
    assert calls[0] == ("店", 3)  # 済んだ2ページは繰り返さず、3ページ目から再開
    assert {r[0] for r in conn.execute("SELECT seller_id FROM sellers")} == {s[0] for s in shops}
    q = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT query, pages_done, done_at IS NOT NULL FROM search_queries")}
    assert q["店"] == (3, 1) and all(done for _, done in q.values())  # 全検索語が完了
    # 見つかった店舗名の文字(数字の「1」〜「9」はシードにもあるが、頻度が加わる)が検索語に入っている
    assert conn.execute("SELECT freq FROM search_queries WHERE query='店'").fetchone()[0] > 99


def test_discover_writes_done_file_only_when_finished(tmp_path, monkeypatch):
    dbp, done = str(tmp_path / "t.db"), tmp_path / "aupay.done"
    fake_site(monkeypatch, {}, [])
    assert cli.main(["--db", dbp, "aupay-discover", "--delay", "0", "--done-file", str(done)]) == 0
    assert done.exists()
    done.unlink()
    clock = iter([0.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock, 10**12))
    conn = db.connect(dbp)
    conn.execute("UPDATE search_queries SET done_at=NULL, pages_done=0")
    conn.commit()
    assert cli.main(["--db", dbp, "aupay-discover", "--delay", "0", "--max-minutes", "1", "--done-file", str(done)]) == 0
    assert not done.exists()  # 時間切れでは作らない


def test_discover_stops_on_blocking_status_and_keeps_progress(tmp_path, monkeypatch, capsys):
    dbp = str(tmp_path / "t.db")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    calls = []

    def search_page(session, query, page):
        calls.append(query)
        if len(calls) == 3:
            raise aupay.SearchError("HTTP 403", 403)
        return 1, [(f"{len(calls)}0000", f"ショップ{len(calls)}")]

    monkeypatch.setattr(aupay, "search_page", search_page)
    assert cli.main(["--db", dbp, "aupay-discover", "--delay", "0"]) == 3  # 403/429/503は、叩き続けずに止まる
    assert len(calls) == 3 and db.counts(db.connect(dbp))["total"] == 2  # それまでの分は保存


def test_fetch_and_export_for_aupay(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(aupay, "get_with_retry", lambda s, url, **k: Resp(200, sjis(KMEM)))
    db.add_ids(db.connect(dbp), [("43013064", "サンプルショップ")])
    assert cli.main(["--db", dbp, "fetch", "--site", "aupay", "--delay", "0"]) == 0
    out = tmp_path / "aupay.csv"
    assert cli.main(["--db", dbp, "export", "--site", "aupay", "--out", str(out)]) == 0
    row = next(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert row["URL"] == "https://wowma.jp/bep/m/kmem?user=43013064" and row["会社名（商号）"] == "架空株式会社"
    assert row["住所"] == "東京都千代田区千代田1-1 サンプルビル3F" and row["郵便番号"] == "〒100-0001"
    assert row["運営責任者"] == "鈴木 花子" and row["お問い合わせメールアドレス"] == "shop@example.com" and row["代表者"] == ""
