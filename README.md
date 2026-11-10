# shoplist-crawler

Yahoo!ショッピングの各店舗の「会社概要」ページ
(`https://store.shopping.yahoo.co.jp/<店舗ID>/info.html`) の情報を集める個人用ツールです。

1. **collect**: 商品検索API(v3)で店舗IDを集める
2. **fetch**: 店舗ごとに `info.html` を取得して保存する(中断しても続きから再開)
3. **export**: CSVに書き出す

`info.html` には `__NEXT_DATA__` というJSONが埋め込まれているので、HTMLの見た目ではなくそのJSONを読みます。
取得できる主な項目: ストア名/フリガナ/紹介文、評価、会社名、代表者、住所、運営責任者、電話/FAX/メール、営業時間、関連ストアURL。

## セットアップ

```bash
pip install -e '.[dev]'
```

Yahoo!デベロッパーネットワークでアプリケーションを登録し、Client ID(appid)を取得してください(無料)。

```bash
export YAHOO_APP_ID=取得したClientID
```

## 使い方

```bash
# 1. 店舗IDを集める(キーワードを増やすほど店舗が増える)
shoplist collect --queries-file queries.example.txt --all-sorts

# 既にIDを知っている場合は手動で追加もできる
shoplist add-ids lafitte

# 2. 会社概要ページを取得する(Ctrl-Cで止めても、再実行で続きから)
shoplist fetch
shoplist fetch --limit 100          # 試しに100件だけ
shoplist fetch --retry-errors       # 前回エラーだった店舗をやり直す

# 3. CSVに出力 / 進捗確認
shoplist export --out shops.csv
shoplist status
```

データは `shops.db`(SQLite)に保存されます(`--db` で変更可)。`raw_json` に店舗情報の生データを保存しているので、
後から別の項目が欲しくなってもページを再取得する必要はありません。

## マナー・注意

- `fetch` はデフォルトで約2〜3秒間隔でアクセスします。`--delay` を短くしすぎないでください。429/5xxは自動で間隔を空けて再試行します。
- 商品検索APIは1クエリあたり先頭1000件までです。`--all-sorts` で並び順を変えて取りこぼしを減らせます。
- 代表者名・メール・電話番号が含まれます。個人利用の範囲にとどめ、再配布や営業メールの一斉送信などには使わないでください。
- Yahoo!ショッピング/Yahoo!デベロッパーネットワークの利用規約は各自で確認してください。

## テスト

```bash
pytest
```
