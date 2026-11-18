"""SQLiteへの保存。店舗ID(発見済み)と取得結果を1テーブルで管理する。"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Iterator

from .info import EXPORT_FIELDS, SAMPLE_HEADERS, to_row, to_sample_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS sellers (
    seller_id     TEXT PRIMARY KEY,
    api_name      TEXT,
    discovered_at TEXT NOT NULL,
    fetched_at    TEXT,
    status        TEXT,          -- NULL=未取得 / ok / not_found / error
    error         TEXT,
    raw_json      TEXT           -- info.html の seller をそのまま保存
);
CREATE INDEX IF NOT EXISTS idx_sellers_status ON sellers(status);

-- 店舗がどのカテゴリー(例: メンズファッション/その他メンズファッション)の商品検索で見つかったか
CREATE TABLE IF NOT EXISTS seller_categories (
    seller_id TEXT NOT NULL,
    category  TEXT NOT NULL,
    PRIMARY KEY (seller_id, category)
);

-- カテゴリー検索の進捗(中断しても続きから再開するため)
CREATE TABLE IF NOT EXISTS category_crawl (
    category_id INTEGER NOT NULL,
    sort        TEXT NOT NULL,
    done_at     TEXT NOT NULL,
    PRIMARY KEY (category_id, sort)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def add_ids(conn: sqlite3.Connection, items: Iterable[tuple[str, str]]) -> int:
    """(seller_id, name) を追加し、新規に追加できた件数を返す。"""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO sellers(seller_id, api_name, discovered_at) VALUES (?,?,?)",
        [(sid, name, _now()) for sid, name in items],
    )
    conn.commit()
    return conn.total_changes - before


def add_seller_categories(conn: sqlite3.Connection, pairs: Iterable[tuple[str, str]]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO seller_categories(seller_id, category) VALUES (?,?)",
        [(sid, cat) for sid, cat in pairs if cat],
    )
    conn.commit()


def crawl_done(conn: sqlite3.Connection, category_id: int, sort: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM category_crawl WHERE category_id=? AND sort=?", (category_id, sort)
    ).fetchone() is not None


def mark_crawled(conn: sqlite3.Connection, category_id: int, sorts: Iterable[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO category_crawl(category_id, sort, done_at) VALUES (?,?,?)",
        [(category_id, s, _now()) for s in sorts],
    )
    conn.commit()


def pending_ids(conn: sqlite3.Connection, *, retry_errors: bool = False, limit: int | None = None) -> list[str]:
    where = "status IS NULL" + (" OR status = 'error'" if retry_errors else "")
    sql = f"SELECT seller_id FROM sellers WHERE {where} ORDER BY discovered_at, seller_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r[0] for r in conn.execute(sql)]


def save_result(conn: sqlite3.Connection, seller_id: str, status: str, seller: dict | None, error: str) -> None:
    conn.execute(
        "UPDATE sellers SET fetched_at=?, status=?, error=?, raw_json=? WHERE seller_id=?",
        (_now(), status, error or None, json.dumps(seller, ensure_ascii=False) if seller else None, seller_id),
    )
    conn.commit()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {"total": 0, "pending": 0, "ok": 0, "not_found": 0, "error": 0}
    for status, n in conn.execute("SELECT status, COUNT(*) FROM sellers GROUP BY status"):
        out["pending" if status is None else status] = n
        out["total"] += n
    return out


def iter_rows(conn: sqlite3.Connection) -> Iterator[dict]:
    for sid, raw in conn.execute("SELECT seller_id, raw_json FROM sellers WHERE status='ok' ORDER BY seller_id"):
        row = {"seller_id": sid, "url": f"https://store.shopping.yahoo.co.jp/{sid}/"}
        row.update(to_row(json.loads(raw)))
        yield row


def iter_sample_rows(conn: sqlite3.Connection) -> Iterator[dict]:
    """サンプルCSVと同じ列。店舗×カテゴリーごとに1行(カテゴリー不明の店舗は空欄で1行)。"""
    cats: dict[str, list[str]] = {}
    for sid, cat in conn.execute("SELECT seller_id, category FROM seller_categories ORDER BY seller_id, category"):
        cats.setdefault(sid, []).append(cat)
    for sid, raw in conn.execute("SELECT seller_id, raw_json FROM sellers WHERE status='ok' ORDER BY seller_id"):
        base = to_sample_row(sid, json.loads(raw))
        for cat in cats.get(sid) or [""]:
            yield {**base, "カテゴリー": cat}


def export_csv(conn: sqlite3.Connection, path: str, fmt: str = "sample") -> int:
    if fmt == "sample":
        fields, rows = SAMPLE_HEADERS, iter_sample_rows(conn)
    else:
        fields, rows = ["seller_id", "url"] + [c for c, _ in EXPORT_FIELDS], iter_rows(conn)
    n = 0
    # utf-8-sig: Excelで開いても文字化けしない
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)
            n += 1
    return n


def sample_rows(conn: sqlite3.Connection, n: int = 5) -> list[dict]:
    """取得済みの店舗からランダムにn件を返す。"""
    rows = conn.execute(
        "SELECT seller_id, raw_json FROM sellers WHERE status='ok' ORDER BY RANDOM() LIMIT ?", (n,)
    ).fetchall()
    out = []
    for sid, raw in rows:
        row = {"seller_id": sid, "url": f"https://store.shopping.yahoo.co.jp/{sid}/"}
        row.update(to_row(json.loads(raw)))
        out.append(row)
    return out
