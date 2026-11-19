"""取得できなかったサイトに、実際の実行環境(GitHub Actions)からアクセスして、どう返るかを確認する。

各URLに1回だけアクセスし(再試行なし・サイトごとに間隔を空ける)、ステータス・転送先・サイズ・タイトル・
会社情報に関係するキーワードの有無だけを出力する。本文は出力しない(`dump` を付けた公開ドキュメントと robots.txt を除く)。
ブロックされていても、名乗りを変えたりしてすり抜けることはしない。
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import urlparse

import requests

from .http import USER_AGENT

KEYWORDS = ["特定商取引", "運営責任者", "代表者", "所在地", "会社概要", "販売業者", "__NEXT_DATA__"]
_META_CHARSET = re.compile(rb'charset=["\']?([\w-]+)', re.I)


def parse_targets(text: str) -> list[tuple[str, str, str]]:
    """`ラベル | URL | モード(任意: dump / structure)` の行を読む。#で始まる行と空行は無視。"""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) >= 2 and parts[1].startswith("http"):
            mode = parts[2] if len(parts) > 2 and parts[2] in ("dump", "structure") else ""
            out.append((parts[0], parts[1], mode))
    return out


def decode(content: bytes, content_type: str = "") -> str:
    """Content-Type や meta の charset (EUC-JP, Shift_JIS など)に従って文字列にする。"""
    cs = None
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        cs = m.group(1)
    else:
        m2 = _META_CHARSET.search(content[:4000])
        cs = m2.group(1).decode("ascii", "ignore") if m2 else "utf-8"
    try:
        return content.decode(cs, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def page_text(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    t = re.sub(r"<[^>]+>", "\n", t)
    return re.sub(r"\s*\n\s*", "\n", t).strip()


def structure(html: str) -> str:
    """ページの構造だけを返す(値は出さない): タグの数と、見出し・項目名(th / dt / h1〜h4)の文字列。
    項目名は短い文字列だけを対象にし、本文や値(td / dd)は出力しない。"""
    tags = {t: len(re.findall(rf"<{t}[\s>]", html, re.I)) for t in ("table", "tr", "th", "td", "dl", "dt", "dd", "ul", "li", "h1", "h2", "h3", "h4")}
    labels, seen = [], set()
    for m in re.finditer(r"<(th|dt|h[1-4])[^>]*>(.*?)</\1>", html, re.S | re.I):
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if 0 < len(txt) <= 24 and txt not in seen:
            seen.add(txt)
            labels.append(f"{m.group(1).lower()}:{txt}")
    # 骨格: 最初のいくつかの <h3> について、次の <h3> までに続くタグの名前だけ(文字や値は出さない)
    skeleton = []
    parts = re.split(r"(?=<h3[\s>])", html)
    for part in parts[1:5]:
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", part.split("</h3>")[0])).strip()[:14]
        after = part.split("</h3>", 1)[1] if "</h3>" in part else ""
        seq = re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)", after)[:10]
        skeleton.append(f"{label}→{'>'.join(seq) or '(タグなし: 直後が文字)'}")
    return ("タグ数 " + " ".join(f"{k}={v}" for k, v in tags.items() if v) + "\n項目名 " + " / ".join(labels[:80])
            + ("\n骨格 " + " | ".join(skeleton) if skeleton else ""))


def probe_one(session: requests.Session, label: str, url: str, mode: str | bool, timeout: float = 30.0) -> dict:
    dump = mode in ("dump", True)
    shape = mode == "structure"
    res = {"label": label, "url": url}
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        res.update(status=f"接続できない({type(e).__name__})", final="", size=0, title="", flags=[], server="", excerpt="")
        return res
    body = resp.content
    text = decode(body, resp.headers.get("Content-Type", ""))
    tm = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    title = re.sub(r"\s+", " ", tm.group(1)).strip()[:70] if tm else ""
    res.update(
        status=str(resp.status_code),
        final=resp.url if resp.url != url else "",
        size=len(body),
        title=title,
        flags=[k for k in KEYWORDS if k in text],
        server=resp.headers.get("Server", ""),
        excerpt=(text[:6000] if "robots.txt" in url else page_text(text)[:6000]) if dump else (structure(text) if shape else ""),
    )
    return res


def run(targets: list[tuple[str, str, str]], delay: float = 3.0, timeout: float = 30.0, sleep=time.sleep) -> list[dict]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja"})
    results, last_host = [], None
    for label, url, mode in targets:
        host = urlparse(url).netloc
        if last_host is not None:
            sleep(delay)  # サイトに関係なく、毎回間隔を空ける(同じサイトへの連続アクセスを避ける)
        last_host = host
        r = probe_one(session, label, url, mode, timeout)
        results.append(r)
        flags = ",".join(r["flags"]) or "-"
        print(f"[{r['status']}] {label} | {url}", flush=True)
        print(f"      転送先={r['final'] or '-'} / {r['size']}B / server={r['server'] or '-'} / title={r['title'] or '-'} / キーワード={flags}", flush=True)
        if r["excerpt"]:
            print("      ----- 本文(公開ドキュメント/robots.txt)または構造 -----")
            print("\n".join("      " + ln for ln in r["excerpt"].splitlines()[:120]), flush=True)
    return results


def write_summary(results: list[dict]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("### 確認結果\n\n| 結果 | 対象 | サイズ | タイトル | キーワード |\n|---|---|---|---|---|\n")
        for r in results:
            f.write(f"| {r['status']} | {r['label']} | {r['size']}B | {r['title'].replace('|', '/')} | {', '.join(r['flags']) or '-'} |\n")
