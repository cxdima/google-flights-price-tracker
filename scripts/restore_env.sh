#!/usr/bin/env bash
# Rebuild .env from SSM Parameter Store.
#
# Everything in .env is mirrored to /gfpricetracker/* in SSM (secrets as
# SecureString on the free AWS-managed alias/aws/ssm key, config as String).
# On a fresh machine: authenticate to AWS, run this, then `make deploy`.
set -euo pipefail

PREFIX="${SSM_PREFIX:-/gfpricetracker}"
REGION="${AWS_REGION:-us-east-1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/.env"

command -v aws >/dev/null || { echo "ERROR: aws CLI not found"; exit 1; }

if [[ -e "${OUT}" ]]; then
  echo "ERROR: ${OUT} already exists — refusing to overwrite."
  echo "       Move it aside first if you really want to regenerate it."
  exit 1
fi

echo "Fetching ${PREFIX}/* from SSM (${REGION})..."
TMP="$(mktemp)"
trap 'rm -f "${TMP}"' EXIT
# Written via mktemp + mv so a failed fetch never leaves a partial .env.
aws ssm get-parameters-by-path \
  --path "${PREFIX}" --recursive --with-decryption \
  --region "${REGION}" \
  --query 'Parameters[].[Name,Value]' --output text \
  | sed "s|^${PREFIX}/||" \
  | awk -F'\t' 'NF==2 {print $1"="$2}' \
  | sort > "${TMP}"

COUNT="$(wc -l < "${TMP}" | tr -d ' ')"
[[ "${COUNT}" -gt 0 ]] || { echo "ERROR: no parameters found under ${PREFIX}"; exit 1; }

for k in GOOGLE_EMAIL GOOGLE_PASSWORD TOTP_SECRET TELEGRAM_BOT_TOKEN; do
  grep -q "^${k}=" "${TMP}" || { echo "ERROR: required key ${k} missing from SSM"; exit 1; }
done

mv "${TMP}" "${OUT}"
trap - EXIT
chmod 600 "${OUT}"
echo "Wrote ${OUT} (${COUNT} keys, mode 600)."
