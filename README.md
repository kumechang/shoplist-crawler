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
# 1. カテゴリーごとに店舗IDを集める(推奨)。デフォルトは 13457=ファッション の2階層目/3階層目
shoplist collect-categories --list-only      # まず対象カテゴリーの一覧だけ確認
shoplist collect-categories --max-minutes 60 # 実行。中断しても、再実行すると続きから再開
shoplist collect-categories --root-id 2498   # 別のトップ階層(例: 食品)

# キーワードで集めることもできる / 既にIDを知っている場合は手動で追加
shoplist collect --queries-file queries.example.txt --all-sorts
shoplist add-ids lafitte

# 2. 会社概要ページを取得する(Ctrl-Cで止めても、再実行で続きから)
shoplist fetch
shoplist fetch --limit 100          # 試しに100件だけ
shoplist fetch --retry-errors       # 前回エラーだった店舗をやり直す

# 3. CSVに出力 / 進捗確認 / 数件を表示
shoplist export --out shops.csv     # 下記の11列。店舗×カテゴリーごとに1行
shoplist export --format full       # 全項目(1店舗1行)
shoplist status
shoplist sample -n 5
```

### 出力するCSVの列

`URL, 会社名（商号）, 郵便番号, 住所, 代表者, ストア名, ストア名（フリガナ）, 運営責任者, お問い合わせ電話番号, お問い合わせメールアドレス, カテゴリー`

- `カテゴリー` は `メンズファッション/その他メンズファッション` のように「2階層目/3階層目」(トップの「ファッション」は省略)。
  1つの店舗が複数カテゴリーで見つかった場合は、カテゴリーごとに行を分けます。カテゴリーが不明な店舗(キーワード検索や手動追加)は空欄の1行です。
- `住所` はページの「お問い合わせ情報」の住所(建物名まで)です。`代表者` は日本語の姓名の間の空白を除いています(例: `伊藤憲博`)。
- 閉店して404になった店舗は取得できないので、CSVには出ません。
- `カテゴリー` は、そのカテゴリーの商品検索(各並び順の先頭999件まで)に出てきた店舗です。商品数が多いカテゴリーでは、全店舗は拾えません。`--all-sorts` で並び順を変えて取りこぼしを減らせます。

データは `shops.db`(SQLite)に保存されます(`--db` で変更可)。`raw_json` に店舗情報の生データを保存しているので、
後から別の項目が欲しくなってもページを再取得する必要はありません。

## GitHub Actionsで動かす

`.github/workflows/crawl.yml` が、週1回(月曜 12:17 JST)と手動実行(Actions タブ → crawl → Run workflow)で動きます。

**初回設定**: リポジトリの Settings → Secrets and variables → Actions に2つ登録してください。

| Secret | 内容 |
|---|---|
| `YAHOO_APP_ID` | Yahoo!デベロッパーネットワークのClient ID |
| `DB_PASSPHRASE` | データ暗号化用のパスフレーズ(長いランダム文字列。控えを必ず保管) |

**流れ**: 前回のArtifactから `shops.db` を復元 → `collect-categories` でカテゴリーごとに店舗IDを収集(最大100分。進捗は保存され、毎回続きから) → `fetch` で最大 `limit` 件(既定5000)取得(最大3.5時間) → CSV出力 → **暗号化して**Artifactに保存。
手動実行では、トップ階層のカテゴリID(`root_id`、既定13457=ファッション)と全並び順(`all_sorts`)を指定できます。

**重要: このリポジトリはpublicです。** Artifactは誰でもダウンロードできるため、代表者名・メール・電話番号を含むDBとCSVは
`DB_PASSPHRASE` で暗号化したものだけを保存します。平文のデータはコミットもアップロードもしないでください。
`DB_PASSPHRASE` を失う、または変えると、過去のデータは復号できなくなります。

結果の取り出し(Actionsの実行ページ → Artifacts → `shops-data` をダウンロードして展開):

```bash
export DB_PASSPHRASE='設定したパスフレーズ'
scripts/crypt.sh dec shops.csv.enc shops.csv
```

DBを復号すれば、ランダムに数件を表示して中身を確認できます。

```bash
scripts/crypt.sh dec shops.db.enc shops.db
shoplist sample -n 5
```

注意:
- Artifactの保存期間は最大90日です。週1回の実行で毎回更新されるので消えませんが、90日以上実行が止まるとデータが失われます。
- publicリポジトリでは、60日間リポジトリへの活動がないとスケジュール実行が自動で無効になります。止まったらActionsタブから再有効化してください。
- GitHubのサーバーのIPからだと、Yahoo側にアクセスを弾かれる可能性があります。まず手動実行で、`limit` を小さく(例: 20)して試してください。

## マナー・注意

- `fetch` はデフォルトで約2〜3秒間隔でアクセスします。`--delay` を短くしすぎないでください。429/5xxは自動で間隔を空けて再試行します。
- 商品検索APIは1クエリあたり先頭1000件までです。リクエスト間隔は既定で2秒です。それでも回数制限(429)になった場合は、待ってから再試行し、それでも解けなければ集めた分を保存して中断します(時間を置いて再実行してください。制限の条件は公開されていません)。`--all-sorts` で並び順を変えて取りこぼしを減らせます。
- 代表者名・メール・電話番号が含まれます。個人利用の範囲にとどめ、再配布や営業メールの一斉送信などには使わないでください。
- Yahoo!ショッピング/Yahoo!デベロッパーネットワークの利用規約は各自で確認してください。

## テスト

```bash
pytest
```
