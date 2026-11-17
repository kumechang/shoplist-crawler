from __future__ import annotations

import argparse
import os
import random
import sys
import time

from . import api, db, info
from .http import new_session


def _read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]


def cmd_collect(args) -> int:
    appid = args.appid or os.environ.get("YAHOO_APP_ID")
    if not appid:
        print("エラー: Yahoo!デベロッパーネットワークのClient ID(appid)を --appid か環境変数 YAHOO_APP_ID で渡してください", file=sys.stderr)
        return 2
    queries = list(args.query or [])
    if args.queries_file:
        queries += _read_lines(args.queries_file)
    if not queries:
        print("エラー: -q か --queries-file でキーワードを指定してください", file=sys.stderr)
        return 2
    sorts = api.SORTS if args.all_sorts else (args.sort,)

    conn = db.connect(args.db)
    session = new_session()
    failures = 0
    for q in queries:
        for sort in sorts:
            found: list[tuple[str, str]] = []
            try:
                for item in api.iter_sellers(session, appid, q, sort=sort, delay=args.delay):
                    found.append(item)
            except api.ApiError as e:
                # ここまでに集めた分は捨てずに保存する
                new = db.add_ids(conn, found)
                print(f"[{q} {sort}] APIエラー: {e} (それまでの新規 {new} 件は保存)", file=sys.stderr)
                failures += 1
                if e.status == 429:
                    print("回数制限(429)に達したため中断します。集めた分は保存済みです。時間を置いて再実行してください", file=sys.stderr)
                    print(db.counts(conn))
                    return 3
                if e.status in (401, 403) or failures >= 3:
                    print("認証エラー、または連続失敗のため中断します", file=sys.stderr)
                    return 1
                continue
            failures = 0
            new = db.add_ids(conn, found)
            print(f"[{q} {sort}] 店舗 {len(set(s for s, _ in found))} 件中 新規 {new} 件", flush=True)
            time.sleep(args.delay)
    print(db.counts(conn))
    return 0


def cmd_add_ids(args) -> int:
    ids = list(args.ids or [])
    if args.file:
        ids += _read_lines(args.file)
    conn = db.connect(args.db)
    new = db.add_ids(conn, [(i, "") for i in ids])
    print(f"新規 {new} 件を追加")
    return 0


def cmd_fetch(args) -> int:
    conn = db.connect(args.db)
    targets = db.pending_ids(conn, retry_errors=args.retry_errors, limit=args.limit)
    print(f"取得対象 {len(targets)} 件")
    session = new_session()
    try:
        for i, sid in enumerate(targets, 1):
            r = info.fetch_info(session, sid)
            db.save_result(conn, sid, r.status, r.seller, r.error)
            detail = f" {r.error}" if r.error else ""
            print(f"[{i}/{len(targets)}] {sid}: {r.status}{detail}", flush=True)
            time.sleep(args.delay + random.random() * args.delay * 0.5)
    except KeyboardInterrupt:
        print("\n中断しました(取得済み分は保存済み。再度実行すると続きから再開します)")
    print(db.counts(conn))
    return 0


def cmd_export(args) -> int:
    conn = db.connect(args.db)
    n = db.export_csv(conn, args.out)
    print(f"{n} 件を {args.out} に出力しました")
    return 0


def cmd_status(args) -> int:
    print(db.counts(db.connect(args.db)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shoplist", description="Yahoo!ショッピング 店舗情報収集(個人用)")
    p.add_argument("--db", default="shops.db", help="SQLiteファイル (default: shops.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="商品検索APIから店舗IDを集める")
    c.add_argument("-q", "--query", action="append", help="検索キーワード(複数指定可)")
    c.add_argument("--queries-file", help="キーワードを1行1つで書いたファイル")
    c.add_argument("--appid", help="Yahoo!のClient ID (または環境変数 YAHOO_APP_ID)")
    c.add_argument("--sort", default="-score", choices=api.SORTS)
    c.add_argument("--all-sorts", action="store_true", help="全ての並び順で検索して取りこぼしを減らす")
    c.add_argument("--delay", type=float, default=2.0, help="APIリクエスト間隔(秒)")
    c.set_defaults(func=cmd_collect)

    a = sub.add_parser("add-ids", help="店舗IDを手動で追加する")
    a.add_argument("ids", nargs="*")
    a.add_argument("--file", help="店舗IDを1行1つで書いたファイル")
    a.set_defaults(func=cmd_add_ids)

    f = sub.add_parser("fetch", help="未取得の店舗の会社概要ページを取得する")
    f.add_argument("--delay", type=float, default=2.0, help="リクエスト間隔(秒)。ランダムに最大50%%上乗せ")
    f.add_argument("--limit", type=int, help="今回取得する最大件数")
    f.add_argument("--retry-errors", action="store_true", help="前回エラーだった店舗も再取得")
    f.set_defaults(func=cmd_fetch)

    e = sub.add_parser("export", help="取得済みの店舗情報をCSVに出力する")
    e.add_argument("--out", default="shops.csv")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("status", help="進捗を表示する")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
