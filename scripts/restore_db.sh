#!/usr/bin/env bash
# 直近の実行のArtifact(暗号化済みDB)を探して shops.db に復元する。
# 見つからなければ新規スタート。復号に失敗(パスフレーズ違い)したら、上書き事故を避けるため失敗させる。
set -uo pipefail
: "${GITHUB_OUTPUT:=/dev/null}"

for id in $(gh run list --workflow crawl.yml --limit 20 --json databaseId --jq '.[].databaseId'); do
  [ "$id" = "${GITHUB_RUN_ID:-}" ] && continue
  rm -rf restored
  if gh run download "$id" -n shops-data -D restored >/dev/null 2>&1 && [ -f restored/shops.db.enc ]; then
    if scripts/crypt.sh dec restored/shops.db.enc shops.db; then
      echo "run $id のArtifactから復元しました"
      echo "restored=true" >> "$GITHUB_OUTPUT"
      exit 0
    fi
    echo "::error::復号に失敗しました。DB_PASSPHRASE が前回と同じか確認してください" >&2
    exit 1
  fi
done
echo "復元できるArtifactがありません。新規に開始します"
echo "restored=false" >> "$GITHUB_OUTPUT"
