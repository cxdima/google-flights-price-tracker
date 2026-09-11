terraform {
  required_version = ">= 1.10.0"

  # State lives in S3 so it survives this laptop. The bucket is created out of
  # band (see README "Bootstrapping from scratch") because Terraform can't
  # manage the bucket that holds its own state.
  #
  # NOTE: state contains the Lambda env block, i.e. GOOGLE_PASSWORD, TOTP_SECRET
  # and TELEGRAM_BOT_TOKEN in plaintext. The bucket is private, versioned,
  # SSE-S3 encrypted and TLS-only. Do not relax any of that.
  #
  # use_lockfile: native S3 locking (TF >= 1.10) — no DynamoDB lock table.
  backend "s3" {
    bucket       = "gfpricetracker-terraform-state"
    key          = "gfpricetracker/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "me" {}

locals {
  name       = var.project_name
  account_id = data.aws_caller_identity.me.account_id
}

# ── Container registry ─────────────────────────────────────────────────────────

resource "aws_ecr_repository" "repo" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

# Every deploy pushes a new ~600 MB image; without this policy old images
# accumulate at $0.10/GB-month forever. Keep the newest 3 (current + two
# rollback candidates).
resource "aws_ecr_lifecycle_policy" "repo" {
  repository = aws_ecr_repository.repo.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep only the newest 3 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

# ── State bucket ───────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "profile" {
  bucket        = "${local.name}-profile-${local.account_id}"
  force_destroy = true
}

# This bucket holds live Google session cookies — belt-and-braces against
# any future policy/ACL mistake making it public.
resource "aws_s3_bucket_public_access_block" "profile" {
  bucket                  = aws_s3_bucket.profile.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "profile" {
  bucket = aws_s3_bucket.profile.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "profile" {
  bucket = aws_s3_bucket.profile.id
  rule {
    id     = "keep-5-versions"
    status = "Enabled"
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    filter {}
  }
  rule {
    id     = "expire-debug-objects"
    status = "Enabled"
    filter {
      prefix = "debug/"
    }
    expiration {
      days = 3
    }
  }
  rule {
    id     = "expire-screenshots"
    status = "Enabled"
    filter {
      prefix = "screenshots/"
    }
    expiration {
      days = 3
    }
  }
}

# ── Price history ──────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "prices" {
  name         = "${local.name}-prices"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "flight_id"
  range_key    = "ts"

  attribute {
    name = "flight_id"
    type = "S"
  }

  attribute {
    name = "ts"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Project = local.name
  }
}

# ── IAM ────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "lambda_exec" {
  name = "${local.name}-lambda-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "basic_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_policy" "lambda_app" {
  name = "${local.name}-lambda-app"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.profile.arn}/*"
      },
      {
        # Without ListBucket, GetObject on a missing key returns 403 instead
        # of 404 — making a real IAM breakage indistinguishable from normal
        # first-run state, which once masked a silent state reset.
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.profile.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.prices.arn
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_app_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_app.arn
}

# ── Logs ───────────────────────────────────────────────────────────────────────

# Declared explicitly so retention is bounded — the auto-created group kept
# every log line forever (a slow, silent cost leak).
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

# ── Lambda ─────────────────────────────────────────────────────────────────────
# Secrets are passed directly as Terraform variables (from .env via deploy.sh)
# and set as Lambda environment variables.

resource "aws_lambda_function" "tracker" {
  count         = var.image_uri != "" ? 1 : 0
  function_name = local.name
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  memory_size   = 2048 # Chrome needs the headroom; 2 GB ≈ 1.2 vCPUs on arm64
  timeout       = 120
  architectures = ["arm64"]

  # Caps worst-case spend if the public webhook URL is ever flooded:
  # 1 tracker run + a few webhook replies is all this app ever needs.
  reserved_concurrent_executions = 5

  # 512 MB is the free allocation; the Chrome profile (images + cache
  # disabled) stays far below it.
  ephemeral_storage {
    size = 512
  }

  environment {
    variables = {
      S3_BUCKET       = aws_s3_bucket.profile.bucket
      DYNAMODB_TABLE  = aws_dynamodb_table.prices.name
      HYDRATE_SECS    = "45"
      BROWSER_EVERY_N = tostring(var.browser_every_n)

      GOOGLE_EMAIL       = var.google_email
      GOOGLE_PASSWORD    = var.google_password
      TOTP_SECRET        = var.totp_secret
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
      TELEGRAM_USERS     = var.telegram_users
      TELEGRAM_CHAT_ID   = var.telegram_chat_id
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.basic_logs,
    aws_iam_role_policy_attachment.lambda_app_attach,
    aws_cloudwatch_log_group.lambda,
  ]
}

# EventBridge invokes the Lambda asynchronously, where AWS defaults to 2
# hidden retries. A timed-out run would re-launch Chrome (and re-login to
# Google) up to 3x in quick succession — tripling cost and producing exactly
# the burst-login pattern that trips Google's anomaly detection. The
# 15-minute schedule IS the retry mechanism.
resource "aws_lambda_function_event_invoke_config" "no_retries" {
  count                        = var.image_uri != "" ? 1 : 0
  function_name                = aws_lambda_function.tracker[0].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}

# ── Safety net: external alarms ────────────────────────────────────────────────
# Every in-app alert (Telegram) requires the tracker code to actually run.
# These alarms cover the failure modes that produce silence instead: process
# kills (timeout/OOM), a disabled schedule, and consecutive scrape failures
# when Telegram itself is the broken part.

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Timeouts, OOM kills, and handler crashes — failures the in-app failure
# streak can never record because the process dies first.
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${local.name}-lambda-errors"
  alarm_description   = "Tracker Lambda reported errors (timeout, OOM, or crash)"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = local.name }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# Dead-man's-switch: the schedule should produce ~4 invocations/hour. Missing
# data IS the outage signal (disabled rule, deleted permission, throttled to
# zero), so it must be treated as breaching.
resource "aws_cloudwatch_metric_alarm" "lambda_silent" {
  alarm_name          = "${local.name}-not-running"
  alarm_description   = "Tracker Lambda has stopped being invoked (schedule dead?)"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = local.name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 2
  threshold           = 2
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# Caught scrape failures (login rot, parser breakage, S3 write failures) end
# the run cleanly, so they never hit the Errors metric — but the runner logs
# a stable "Tracker run failed" line on every caught failure, and the log
# write does not depend on S3 or Telegram.
resource "aws_cloudwatch_log_metric_filter" "run_failed" {
  name           = "${local.name}-run-failed"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "\"Tracker run failed\""

  metric_transformation {
    name          = "RunFailed"
    namespace     = "GFPT"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "runs_failing" {
  alarm_name          = "${local.name}-runs-failing"
  alarm_description   = "3+ consecutive tracker runs failed in the last hour"
  namespace           = "GFPT"
  metric_name         = "RunFailed"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# ── Schedule ───────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${local.name}-schedule"
  schedule_expression = "rate(${var.schedule_minutes} minutes)"
  description         = "Trigger ${local.name} every ${var.schedule_minutes} minutes"
}

resource "aws_cloudwatch_event_target" "lambda" {
  count     = var.image_uri != "" ? 1 : 0
  rule      = aws_cloudwatch_event_rule.schedule.name
  target_id = "lambda"
  arn       = aws_lambda_function.tracker[0].arn
  # The handler routes on this source field — only scheduled events (and
  # nothing arriving via the public Function URL) may start a tracker run.
  input = jsonencode({ source = "eventbridge" })
}

resource "aws_lambda_permission" "allow_eventbridge" {
  count               = var.image_uri != "" ? 1 : 0
  statement_id_prefix = "AllowEventBridgeInvoke-"
  action              = "lambda:InvokeFunction"
  function_name       = aws_lambda_function.tracker[0].function_name
  principal           = "events.amazonaws.com"
  source_arn          = aws_cloudwatch_event_rule.schedule.arn
}

# ── Telegram webhook endpoint ──────────────────────────────────────────────────
# Public URL by necessity (Telegram must reach it). Application-layer auth:
# the handler rejects any request without the webhook secret token header.

resource "aws_lambda_function_url" "webhook" {
  count              = var.image_uri != "" ? 1 : 0
  function_name      = aws_lambda_function.tracker[0].function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "allow_function_url" {
  count                  = var.image_uri != "" ? 1 : 0
  statement_id           = "AllowFunctionURL"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.tracker[0].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# ── Outputs ────────────────────────────────────────────────────────────────────

output "webhook_url" {
  value = var.image_uri != "" ? aws_lambda_function_url.webhook[0].function_url : "(not yet deployed)"
}

output "aws_region" {
  value = var.aws_region
}

output "ecr_repository_url" {
  value = aws_ecr_repository.repo.repository_url
}

output "s3_bucket" {
  value = aws_s3_bucket.profile.bucket
}

output "dynamodb_table" {
  value = aws_dynamodb_table.prices.name
}

output "lambda_name" {
  value = var.image_uri != "" ? aws_lambda_function.tracker[0].function_name : "(not yet deployed)"
}

output "schedule" {
  value = aws_cloudwatch_event_rule.schedule.schedule_expression
}
