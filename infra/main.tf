terraform {
  required_version = ">= 1.5.0"
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
      S3_BUCKET      = aws_s3_bucket.profile.bucket
      DYNAMODB_TABLE = aws_dynamodb_table.prices.name
      HYDRATE_SECS   = "45"

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
