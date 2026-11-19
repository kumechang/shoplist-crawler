import requests

from shoplist_crawler import cli, probe


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None, url="https://example.com/"):
        self.status_code, self.content, self.headers, self.url = status, body, headers or {}, url


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = responses, []
        self.headers = {}

    def get(self, url, timeout=None, allow_redirects=True):
        self.calls.append(url)
        r = self.responses[url]
        if isinstance(r, Exception):
            raise r
        return r


def test_parse_targets_ignores_comments_and_reads_dump_flag():
    text = "# コメント\n\nA | https://a.example/ | dump\nB | https://b.example/x\nbad line\nC | notaurl\n"
    assert probe.parse_targets(text) == [("A", "https://a.example/", "dump"), ("B", "https://b.example/x", "")]
    assert probe.parse_targets("S | https://s.example/ | structure") == [("S", "https://s.example/", "structure")]


def test_decode_follows_meta_charset_for_euc_jp():
    html = "<html><head><meta charset=euc-jp><title>【楽天市場】テスト</title></head>代表者:石井</html>".encode("euc_jp")
    assert "代表者:石井" in probe.decode(html)
    assert "テスト" in probe.decode("<title>テスト</title>".encode("shift_jis"), "text/html; charset=Shift_JIS")


def test_probe_one_reports_status_title_and_keywords_without_body():
    html = "<html><title> 会社 概要 </title><body>特定商取引法に基づく表記 / 運営責任者: 非公開のはずの値</body></html>".encode()
    s = FakeSession({"https://x.example/": FakeResp(200, html, {"Server": "AkamaiGHost"}, url="https://x.example/final")})
    r = probe.probe_one(s, "X", "https://x.example/", "")
    assert (r["status"], r["final"], r["title"], r["server"]) == ("200", "https://x.example/final", "会社 概要", "AkamaiGHost")
    assert r["flags"] == ["特定商取引", "運営責任者"] and r["excerpt"] == ""  # 本文は出力しない


def test_probe_one_handles_errors_and_blocks():
    s = FakeSession({
        "https://down.example/": requests.ConnectionError("x"),
        "https://blocked.example/": FakeResp(403, b"<html><title>Access Denied</title></html>"),
    })
    assert probe.probe_one(s, "D", "https://down.example/", "")["status"].startswith("接続できない(ConnectionError")
    r = probe.probe_one(s, "B", "https://blocked.example/", "")
    assert r["status"] == "403" and r["title"] == "Access Denied"


def test_run_waits_between_requests_and_dumps_only_when_flagged(capsys):
    s_urls = {"https://a.example/robots.txt": FakeResp(200, b"User-agent: *\nDisallow: /x"), "https://b.example/": FakeResp(200, b"<title>t</title>")}
    waits = []
    import shoplist_crawler.probe as p
    orig = p.requests.Session
    p.requests.Session = lambda: FakeSession(s_urls)
    try:
        res = probe.run([("robots", "https://a.example/robots.txt", "dump"), ("top", "https://b.example/", "")], delay=3, sleep=waits.append)
    finally:
        p.requests.Session = orig
    assert waits == [3] and [r["status"] for r in res] == ["200", "200"]
    out = capsys.readouterr().out
    assert "Disallow: /x" in out and "本文" in out


def test_cli_probe_requires_file(tmp_path):
    assert cli.main(["probe", str(tmp_path / "none.txt")]) == 2


def test_structure_mode_prints_labels_but_never_values():
    html = ("<html><title>t</title><table><tr><th>販売事業者名</th><td>秘密株式会社</td></tr>"
            "<tr><th>代表者</th><td>山田 太郎</td></tr></table><dl><dt>電話番号</dt><dd>03-1234-5678</dd></dl>"
            "<h2>運営者情報</h2></html>").encode()
    s = FakeSession({"https://x.example/": FakeResp(200, html)})
    r = probe.probe_one(s, "X", "https://x.example/", "structure")
    out = r["excerpt"]
    assert "th:販売事業者名" in out and "th:代表者" in out and "dt:電話番号" in out and "h2:運営者情報" in out
    assert "table=1" in out and "th=2" in out
    assert "秘密株式会社" not in out and "山田" not in out and "03-1234-5678" not in out  # 値は出さない


def test_structure_mode_shows_tag_skeleton_after_headings_without_values():
    html = "<h3>販売事業者名</h3><p>秘密株式会社</p><h3>電話番号</h3><ul><li>03-1234-5678</li></ul><h3>備考</h3>直接の文字".encode()
    s = FakeSession({"https://x.example/": FakeResp(200, html)})
    out = probe.probe_one(s, "X", "https://x.example/", "structure")["excerpt"]
    assert "販売事業者名→p" in out and "電話番号→ul>li" in out and "備考→(タグなし" in out
    assert "秘密株式会社" not in out and "03-1234-5678" not in out
