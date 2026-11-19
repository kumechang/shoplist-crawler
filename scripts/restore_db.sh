#!/usr/bin/env bash
# 直近の実行のArtifact(暗号化済みDB)を探して shops.db に復元する。
# 見つからなければ新規スタート。復号に失敗(パスフレーズ違い)したら、上書き事故を避けるため失敗させる。
set -uo pipefail
# サイトごとに差し替えられる(既定は Yahoo 用)
WORKFLOW_FILE=${WORKFLOW_FILE:-crawl.yml}
ARTIFACT_NAME=${ARTIFACT_NAME:-shops-data}
ENC_FILE=${ENC_FILE:-shops.db.enc}
DB_FILE=${DB_FILE:-shops.db}
: "${GITHUB_OUTPUT:=/dev/null}"

for id in $(gh run list --workflow "$WORKFLOW_FILE" --limit 40 --json databaseId --jq '.[].databaseId'); do
  [ "$id" = "${GITHUB_RUN_ID:-}" ] && continue
  rm -rf restored
  if gh run download "$id" -n "$ARTIFACT_NAME" -D restored >/dev/null 2>&1 && [ -f "restored/$ENC_FILE" ]; then
    if scripts/crypt.sh dec "restored/$ENC_FILE" "$DB_FILE"; then
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
