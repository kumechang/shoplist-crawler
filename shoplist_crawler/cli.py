from __future__ import annotations

import argparse
import os
import random
import sys
import time

from . import api, categories, db, info, partition
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


def cmd_collect_categories(args) -> int:
    appid = args.appid or os.environ.get("YAHOO_APP_ID")
    if not appid:
        print("エラー: Client ID(appid)を --appid か環境変数 YAHOO_APP_ID で渡してください", file=sys.stderr)
        return 2
    conn = db.connect(args.db)
    session = new_session()
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None

    try:
        targets = categories.list_targets(session, appid, args.root_id, depth=args.depth, delay=args.delay)
    except api.ApiError as e:
        print(f"カテゴリ一覧の取得に失敗: {e}", file=sys.stderr)
        return 1
    print(f"対象カテゴリ {len(targets)} 件 (root={args.root_id}, {args.depth}階層目まで)", flush=True)
    if args.list_only:
        for t in targets:
            print(f"{t.category_id}\t{t.depth}\t{t.name}")
        return 0
    if args.estimate:
        return _estimate(session, appid, targets, args)

    crawler = partition.Crawler(
        conn, session, appid, sort=args.sort, page_size=args.page_size, delay=args.delay,
        deadline=deadline, log=lambda m: print(m, flush=True),
    )
    try:
        for i, t in enumerate(targets, 1):
            print(f"--- カテゴリ {i}/{len(targets)}: {t.name} (id={t.category_id}) ---", flush=True)
            crawler.crawl(t)
    except partition.Deadline:
        print("時間切れのため中断します(進捗は保存済み。次回は続きから再開します)")
        print(db.counts(conn))
        return 0
    except api.ApiError as e:
        print(f"APIエラー: {e} (ここまでの分は保存済み。再実行すると続きから再開します)", file=sys.stderr)
        print(db.counts(conn))
        if e.status == 429:
            print("回数制限(429)に達したため中断します。時間を置いて再実行してください", file=sys.stderr)
            return 3
        return 1
    print(f"全カテゴリの検索が完了しました (同価格の商品が多く並び順で補った価格帯: {crawler.saturated})")
    print(db.counts(conn))
    return 0


def _estimate(session, appid, targets, args) -> int:
    """各カテゴリーの商品件数を1リクエストずつ調べて、必要なリクエスト数・時間を見積もる。"""
    total_items = 0
    for i, t in enumerate(targets, 1):
        try:
            total = next(api.iter_pages(session, appid, genre_category_id=t.category_id, page_size=1, delay=args.delay))[0]
        except api.ApiError as e:
            print(f"APIエラー: {e}", file=sys.stderr)
            return 3 if e.status == 429 else 1
        total_items += total
        print(f"[{i}/{len(targets)}] {t.name}: {total:,} 件", flush=True)
        time.sleep(args.delay)
    pages = -(-total_items // args.page_size)
    requests_n = int(pages * 1.1)  # 価格帯の分割に使う調査リクエスト分を1割と見込む
    hours = requests_n * (args.delay + 0.3) / 3600  # 0.3秒は通信時間の目安
    print(f"\n商品 合計 約{total_items:,} 件 → 必要なリクエスト 約{requests_n:,} 回 → 約{hours:,.1f} 時間 (間隔 {args.delay} 秒)")
    print("(1日に動かせる時間で割ると、必要な日数の目安になります)")
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
    n = db.export_csv(conn, args.out, args.format)
    print(f"{n} 件を {args.out} に出力しました")
    return 0


def cmd_sample(args) -> int:
    rows = db.sample_rows(db.connect(args.db), args.n)
    if not rows:
        print("取得済みの店舗がありません")
        return 0
    for i, row in enumerate(rows, 1):
        print(f"--- {i}/{len(rows)} ---")
        for k, v in row.items():
            if v not in ("", None):
                print(f"{k}: {v}")
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

    cc = sub.add_parser("collect-categories", help="カテゴリーごとに商品検索して、店舗IDとカテゴリーを集める")
    cc.add_argument("--root-id", type=int, default=13457, help="トップ階層のジャンルカテゴリID (default: 13457=ファッション)")
    cc.add_argument("--depth", type=int, default=3, help="何階層目のカテゴリーまで分けて検索するか (default: 3)")
    cc.add_argument("--appid", help="Yahoo!のClient ID (または環境変数 YAHOO_APP_ID)")
    cc.add_argument("--sort", default="-score", choices=api.SORTS)
    cc.add_argument("--page-size", type=int, default=api.PAGE_SIZE, help="1リクエストの商品数(default: 50)")
    cc.add_argument("--delay", type=float, default=2.0, help="APIリクエスト間隔(秒)")
    cc.add_argument("--estimate", action="store_true", help="各カテゴリーの商品件数を調べて、必要な時間を見積もるだけ")
    cc.add_argument("--max-minutes", type=float, help="この時間を過ぎたら次のカテゴリー前で中断(再実行で続きから)")
    cc.add_argument("--list-only", action="store_true", help="対象カテゴリーを表示するだけ(商品検索はしない)")
    cc.set_defaults(func=cmd_collect_categories)

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
    e.add_argument("--format", choices=("sample", "full"), default="sample",
                   help="sample=指定の列(店舗×カテゴリーごとに1行) / full=全項目(1店舗1行)")
    e.set_defaults(func=cmd_export)

    m = sub.add_parser("sample", help="取得済みの店舗をランダムに数件表示する")
    m.add_argument("-n", type=int, default=5)
    m.set_defaults(func=cmd_sample)

    s = sub.add_parser("status", help="進捗を表示する")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
