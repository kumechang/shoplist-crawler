#!/usr/bin/env bash
# DB/CSVの暗号化・復号 (AES-256-CBC + PBKDF2)。パスフレーズは環境変数 DB_PASSPHRASE で渡す。
#   DB_PASSPHRASE=... scripts/crypt.sh enc shops.db shops.db.enc
#   DB_PASSPHRASE=... scripts/crypt.sh dec shops.db.enc shops.db
set -euo pipefail
mode=${1:?usage: crypt.sh enc|dec IN OUT}; in=${2:?}; out=${3:?}
: "${DB_PASSPHRASE:?環境変数 DB_PASSPHRASE が未設定です}"
case "$mode" in
  enc) flag=-e ;;
  dec) flag=-d ;;
  *) echo "mode は enc か dec" >&2; exit 2 ;;
esac
if ! openssl enc "$flag" -aes-256-cbc -pbkdf2 -iter 600000 -salt -in "$in" -out "$out" -pass env:DB_PASSPHRASE; then
  rm -f "$out"; exit 1
fi
