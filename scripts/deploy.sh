#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-gfpricetracker}"
AWS_REGION="${AWS_REGION:-us-east-1}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${ROOT}/infra"
APP_DIR="${ROOT}/src"
ENV_FILE="${ROOT}/.env"

need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not found"; exit 1; }; }
need terraform; need aws; need docker; need python3

req_env() {
  local k="$1"
  if [[ -z "${!k:-}" ]]; then
    echo "ERROR: Missing ${k} in .env"
    exit 1
  fi
}

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ${PROJECT_NAME} deploy (Lambda + EventBridge, 10-min schedule)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Region : ${AWS_REGION}"

# ---- .env loader ----
if [[ -f "${ENV_FILE}" ]]; then
  echo "[env] Loading ${ENV_FILE}"
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
else
  echo "ERROR: .env not found at ${ENV_FILE}"
  exit 1
fi

req_env GOOGLE_EMAIL
req_env GOOGLE_PASSWORD
req_env TOTP_SECRET
req_env TELEGRAM_BOT_TOKEN
req_env TELEGRAM_CHAT_ID

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Account: ${ACCOUNT_ID}"

# Common TF args — secrets passed directly as variables (no SSM needed)
TF_COMMON_VARS=(
  -var "aws_region=${AWS_REGION}"
  -var "project_name=${PROJECT_NAME}"
  -var "google_email=${GOOGLE_EMAIL}"
  -var "google_password=${GOOGLE_PASSWORD}"
  -var "totp_secret=${TOTP_SECRET}"
  -var "telegram_bot_token=${TELEGRAM_BOT_TOKEN}"
  -var "telegram_chat_id=${TELEGRAM_CHAT_ID}"
)

tf() { terraform -chdir="${TF_DIR}" "$@"; }

tf_state_has() {
  local addr="$1"
  tf state show -no-color "${addr}" >/dev/null 2>&1
}

tf_import() {
  local addr="$1"
  local id="$2"
  shift 2

  # If already in state, don't spam errors.
  if tf_state_has "${addr}"; then
    echo "  - import ${addr} <= ${id}"
    echo "    already in state (skip)"
    return 0
  fi

  echo "  - import ${addr} <= ${id}"
  if tf import -input=false \
      "${TF_COMMON_VARS[@]}" \
      "$@" \
      "${addr}" "${id}"
  then
    echo "    imported"
  else
    echo "    import failed (continuing)"
  fi
}

echo ""
echo "[1] Terraform init"
tf init -upgrade -input=false >/dev/null

# Detect existing Lambda + its current image URI (prevents destroy, enables import).
EXISTING_IMAGE_URI=""
if aws lambda get-function --region "${AWS_REGION}" --function-name "${PROJECT_NAME}" >/dev/null 2>&1; then
  EXISTING_IMAGE_URI="$(aws lambda get-function \
    --region "${AWS_REGION}" \
    --function-name "${PROJECT_NAME}" \
    --query 'Code.ImageUri' --output text 2>/dev/null || true)"
  if [[ "${EXISTING_IMAGE_URI}" == "None" || "${EXISTING_IMAGE_URI}" == "null" ]]; then
    EXISTING_IMAGE_URI=""
  fi
fi

echo ""
echo "[2] Best-effort import of existing resources (prevents AlreadyExists)"
tf_import "aws_ecr_repository.repo" "${PROJECT_NAME}"
tf_import "aws_s3_bucket.profile" "${PROJECT_NAME}-profile-${ACCOUNT_ID}"
tf_import "aws_dynamodb_table.prices" "${PROJECT_NAME}-prices"
tf_import "aws_iam_role.lambda_exec" "${PROJECT_NAME}-lambda-exec"
tf_import "aws_iam_policy.lambda_app" "arn:aws:iam::${ACCOUNT_ID}:policy/${PROJECT_NAME}-lambda-app"
tf_import "aws_iam_role_policy_attachment.basic_logs" \
  "${PROJECT_NAME}-lambda-exec/arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
tf_import "aws_iam_role_policy_attachment.lambda_app_attach" \
  "${PROJECT_NAME}-lambda-exec/arn:aws:iam::${ACCOUNT_ID}:policy/${PROJECT_NAME}-lambda-app"
tf_import "aws_s3_bucket_versioning.profile" "${PROJECT_NAME}-profile-${ACCOUNT_ID}"
tf_import "aws_s3_bucket_lifecycle_configuration.profile" "${PROJECT_NAME}-profile-${ACCOUNT_ID}"
tf_import "aws_cloudwatch_event_rule.every_10min" "${PROJECT_NAME}-every-10min"

# Only import Lambda-related resources if they exist in AWS AND Terraform config can "see" them.
# Because count depends on image_uri, we must pass a non-empty image_uri for import to be valid.
if [[ -n "${EXISTING_IMAGE_URI}" ]]; then
  tf_import "aws_lambda_function.tracker[0]" "${PROJECT_NAME}" \
    -var "image_uri=${EXISTING_IMAGE_URI}"

  tf_import "aws_cloudwatch_event_target.lambda[0]" "${PROJECT_NAME}-every-10min/lambda" \
    -var "image_uri=${EXISTING_IMAGE_URI}"
fi

echo ""
echo "[3] Apply infra (no Lambda image yet)"
tf apply -auto-approve -input=false \
  "${TF_COMMON_VARS[@]}" \
  -var "image_uri=${EXISTING_IMAGE_URI}"

REPO_URL="$(tf output -raw ecr_repository_url)"
DYNAMODB_TABLE="$(tf output -raw dynamodb_table)"

echo "  ECR   : ${REPO_URL}"
echo "  DDB   : ${DYNAMODB_TABLE}"

echo ""
echo "[4] ECR login"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin \
      "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null

TAG="${TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
IMAGE_URI="${REPO_URL}:${TAG}"

echo ""
echo "[5] Build & push image ${IMAGE_URI}"
docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  -t "${IMAGE_URI}" \
  --push \
  "${APP_DIR}"

echo ""
echo "[5.5] Import Lambda resources now that config is enabled (image_uri is non-empty)"
if aws lambda get-function --region "${AWS_REGION}" --function-name "${PROJECT_NAME}" >/dev/null 2>&1; then
  tf_import "aws_lambda_function.tracker[0]" "${PROJECT_NAME}" \
    -var "image_uri=${IMAGE_URI}"

  tf_import "aws_cloudwatch_event_target.lambda[0]" "${PROJECT_NAME}-every-10min/lambda" \
    -var "image_uri=${IMAGE_URI}"
fi

echo ""
echo "[6] Apply Lambda + schedule"
tf apply -auto-approve -input=false \
  "${TF_COMMON_VARS[@]}" \
  -var "image_uri=${IMAGE_URI}"

LAMBDA_NAME="$(tf output -raw lambda_name)"
echo "  Lambda: ${LAMBDA_NAME}"

echo ""
echo "[7] Wait for Lambda update to propagate"
aws lambda wait function-updated \
  --region "${AWS_REGION}" \
  --function-name "${LAMBDA_NAME}"

echo ""
echo "[8] Test invoke (prints tail logs)"
aws lambda invoke \
  --region "${AWS_REGION}" \
  --function-name "${LAMBDA_NAME}" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"source":"deploy-test"}' \
  --log-type Tail \
  --cli-read-timeout 0 \
  /tmp/gfpt_out.json > /tmp/gfpt_meta.json

python3 - <<'PY'
import json, base64, sys
meta = json.load(open("/tmp/gfpt_meta.json"))
log  = base64.b64decode(meta.get("LogResult","") or b"").decode("utf-8","replace")
out  = json.load(open("/tmp/gfpt_out.json"))
print(log[-4000:])
print("━━━ Result ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print(json.dumps(out, indent=2))
sys.exit(0 if out.get("ok") else 1)
PY

echo ""
echo "[9] Register Telegram webhook"
WEBHOOK_URL="$(tf output -raw webhook_url)"
RESPONSE="$(curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${WEBHOOK_URL}")"
if echo "${RESPONSE}" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)"; then
  echo "  ✓ Webhook set → ${WEBHOOK_URL}"
else
  echo "  ✗ setWebhook failed: ${RESPONSE}"
  exit 1
fi

echo ""
echo "DEPLOY COMPLETE"
echo "CloudWatch Logs: /aws/lambda/${LAMBDA_NAME}"
