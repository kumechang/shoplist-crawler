"""SQLiteへの保存。店舗ID(発見済み)と取得結果を1テーブルで管理する。"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

from .info import EXPORT_FIELDS, SAMPLE_HEADERS, diff_sellers, to_row, to_sample_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS sellers (
    seller_id     TEXT PRIMARY KEY,
    api_name      TEXT,
    discovered_at TEXT NOT NULL,
    fetched_at    TEXT,
    status        TEXT,          -- NULL=未取得 / ok / closed(取得できていた店舗が404に) / not_found / error
    error         TEXT,
    raw_json      TEXT,          -- info.html の seller をそのまま保存(閉店後も最後の内容を残す)
    closed_at       TEXT,        -- 閉店(404)を検出した時刻
    last_changed_at TEXT         -- 更新チェックで内容の変化を検出した最後の時刻
);
CREATE INDEX IF NOT EXISTS idx_sellers_status ON sellers(status);

-- 更新チェックで検出した変更の履歴(個人情報を含むので、公開しないこと)
CREATE TABLE IF NOT EXISTS seller_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id  TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_at ON seller_changes(changed_at);

-- 店舗がどのカテゴリー(例: メンズファッション/その他メンズファッション)の商品検索で見つかったか
CREATE TABLE IF NOT EXISTS seller_categories (
    seller_id TEXT NOT NULL,
    category  TEXT NOT NULL,
    PRIMARY KEY (seller_id, category)
);

-- カテゴリー検索の進捗。1ユニット = カテゴリー×価格帯(例 "2495:all", "2495:500-999")。
-- state: done=全件取得済み / split=件数が多いので価格帯に分割済み(子の進捗を見る)
CREATE TABLE IF NOT EXISTS crawl_units (
    unit    TEXT PRIMARY KEY,
    state   TEXT NOT NULL,
    total   INTEGER,
    done_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cutoff(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    # 古いバージョンで作ったDB(Artifactから復元したもの)に、後から増えた列を足す
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sellers)")}
    for col in ("closed_at", "last_changed_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sellers ADD COLUMN {col} TEXT")
    conn.commit()
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


def unit_state(conn: sqlite3.Connection, unit: str, max_age_days: float | None = None) -> str | None:
    """検索ユニットの進捗。max_age_days より古い記録は「未実施」として扱う(新店舗を見つけ直すため)。"""
    row = conn.execute("SELECT state, done_at FROM crawl_units WHERE unit=?", (unit,)).fetchone()
    if not row:
        return None
    if max_age_days and row[1] < _cutoff(max_age_days):
        return None
    return row[0]


def set_unit(conn: sqlite3.Connection, unit: str, state: str, total: int | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO crawl_units(unit, state, total, done_at) VALUES (?,?,?,?)",
        (unit, state, total, _now()),
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


def refresh_ids(conn: sqlite3.Connection, older_than_days: float, *, limit: int | None = None) -> list[str]:
    """更新チェックの対象。取得が古い順。営業中(ok)は older_than_days 日、閉店・不明は4倍の間隔で再確認する。"""
    sql = (
        "SELECT seller_id FROM sellers WHERE "
        "(status='ok' AND fetched_at < ?) OR (status IN ('closed','not_found') AND fetched_at < ?) "
        "ORDER BY fetched_at, seller_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r[0] for r in conn.execute(sql, (_cutoff(older_than_days), _cutoff(older_than_days * 4)))]


def save_refresh(conn: sqlite3.Connection, seller_id: str, status: str, seller: dict | None, error: str) -> str:
    """更新チェックの結果を保存し、変更を履歴に残す。ログ用の短い結果文字列を返す。"""
    old_status, old_raw = conn.execute(
        "SELECT status, raw_json FROM sellers WHERE seller_id=?", (seller_id,)
    ).fetchone()
    now = _now()
    changes: list[tuple[str, str, str]] = []
    if status == "ok":
        if old_raw:
            changes = diff_sellers(json.loads(old_raw), seller)
        if old_status in ("closed", "not_found"):
            changes.append(("状態", old_status, "ok"))
        conn.execute(
            "UPDATE sellers SET status='ok', raw_json=?, fetched_at=?, error=NULL, closed_at=NULL, "
            "last_changed_at=CASE WHEN ? THEN ? ELSE last_changed_at END WHERE seller_id=?",
            (json.dumps(seller, ensure_ascii=False), now, bool(changes), now, seller_id),
        )
        label = f"ok(変更 {len(changes)} 項目)" if changes else "ok(変更なし)"
    elif status == "not_found":
        if old_status == "ok":  # 取得できていた店舗が消えた = 閉店。最後の内容は残す
            changes.append(("状態", "ok", "closed"))
            conn.execute(
                "UPDATE sellers SET status='closed', closed_at=?, fetched_at=?, last_changed_at=? WHERE seller_id=?",
                (now, now, now, seller_id),
            )
            label = "closed(閉店・削除を検出)"
        else:
            conn.execute("UPDATE sellers SET fetched_at=? WHERE seller_id=?", (now, seller_id))
            label = old_status or "not_found"
    else:  # error: 前回のデータと取得時刻はそのまま(次回また対象になる)
        conn.execute("UPDATE sellers SET error=? WHERE seller_id=?", (error, seller_id))
        label = "error"
    conn.executemany(
        "INSERT INTO seller_changes(seller_id, changed_at, field, old_value, new_value) VALUES (?,?,?,?,?)",
        [(seller_id, now, f, o, n) for f, o, n in changes],
    )
    conn.commit()
    return label


def iter_changes(conn: sqlite3.Connection, since_days: float) -> Iterator[dict]:
    for at, sid, field, old, new in conn.execute(
        "SELECT changed_at, seller_id, field, old_value, new_value FROM seller_changes "
        "WHERE changed_at >= ? ORDER BY changed_at DESC, id DESC", (_cutoff(since_days),)
    ):
        yield {"変更日時": at, "URL": f"https://store.shopping.yahoo.co.jp/{sid}/info.html",
               "項目": field, "変更前": old, "変更後": new}


CHANGE_HEADERS = ["変更日時", "URL", "項目", "変更前", "変更後"]


def export_changes(conn: sqlite3.Connection, path: str, since_days: float) -> int:
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CHANGE_HEADERS)
        w.writeheader()
        for row in iter_changes(conn, since_days):
            w.writerow(row)
            n += 1
    return n


def recent_activity(conn: sqlite3.Connection, days: float) -> dict[str, int]:
    """直近days日の動き: 新規発見した店舗数・変更を検出した店舗数・閉店を検出した店舗数。"""
    c = _cutoff(days)
    one = lambda sql: conn.execute(sql, (c,)).fetchone()[0]  # noqa: E731
    return {
        "新規発見": one("SELECT COUNT(*) FROM sellers WHERE discovered_at >= ?"),
        "変更あり": one("SELECT COUNT(DISTINCT seller_id) FROM seller_changes WHERE changed_at >= ? AND field != '状態'"),
        "閉店検出": one("SELECT COUNT(*) FROM sellers WHERE closed_at >= ?"),
    }


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {"total": 0, "pending": 0, "ok": 0, "closed": 0, "not_found": 0, "error": 0}
    for status, n in conn.execute("SELECT status, COUNT(*) FROM sellers GROUP BY status"):
        out["pending" if status is None else status] = n
        out["total"] += n
    return out


def _statuses(include_closed: bool) -> str:
    return "('ok','closed')" if include_closed else "('ok')"


FULL_EXTRA = ["status", "discovered_at", "fetched_at", "last_changed_at"]


def iter_rows(conn: sqlite3.Connection, include_closed: bool = False) -> Iterator[dict]:
    for sid, raw, status, disc, fetched, changed in conn.execute(
        "SELECT seller_id, raw_json, status, discovered_at, fetched_at, last_changed_at FROM sellers "
        f"WHERE status IN {_statuses(include_closed)} ORDER BY seller_id"
    ):
        row = {"seller_id": sid, "url": f"https://store.shopping.yahoo.co.jp/{sid}/", "status": status,
               "discovered_at": disc, "fetched_at": fetched, "last_changed_at": changed or ""}
        row.update(to_row(json.loads(raw)))
        yield row


def iter_sample_rows(conn: sqlite3.Connection, include_closed: bool = False) -> Iterator[dict]:
    """サンプルCSVと同じ列。店舗×カテゴリーごとに1行(カテゴリー不明の店舗は空欄で1行)。"""
    cats: dict[str, list[str]] = {}
    for sid, cat in conn.execute("SELECT seller_id, category FROM seller_categories ORDER BY seller_id, category"):
        cats.setdefault(sid, []).append(cat)
    for sid, raw in conn.execute(
        f"SELECT seller_id, raw_json FROM sellers WHERE status IN {_statuses(include_closed)} ORDER BY seller_id"
    ):
        base = to_sample_row(sid, json.loads(raw))
        for cat in cats.get(sid) or [""]:
            yield {**base, "カテゴリー": cat}


def export_csv(conn: sqlite3.Connection, path: str, fmt: str = "sample", include_closed: bool = False) -> int:
    if fmt == "sample":
        fields, rows = SAMPLE_HEADERS, iter_sample_rows(conn, include_closed)
    else:
        fields = ["seller_id", "url"] + FULL_EXTRA + [c for c, _ in EXPORT_FIELDS]
        rows = iter_rows(conn, include_closed)
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
