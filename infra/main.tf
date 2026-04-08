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

resource "aws_ecr_repository" "repo" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "aws_s3_bucket" "profile" {
  bucket        = "${local.name}-profile-${local.account_id}"
  force_destroy = true
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
        Action   = ["s3:GetObject", "s3:PutObject", "s3:HeadObject"]
        Resource = "${aws_s3_bucket.profile.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.prices.arn
      },
      {
        Effect = "Allow"
        Action = [
          "logs:DescribeLogStreams",
          "logs:GetLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${local.name}:*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_app_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_app.arn
}

  # Secrets are passed directly as Terraform variables (from .env via deploy.sh)
  # and set as Lambda environment variables — no SSM/Secrets Manager needed.

resource "aws_lambda_function" "tracker" {
  count         = var.image_uri != "" ? 1 : 0
  function_name = local.name
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  memory_size   = 2048
  timeout       = 120
  architectures = ["arm64"]

  ephemeral_storage {
    size = 1024
  }

  environment {
    variables = {
      S3_BUCKET      = aws_s3_bucket.profile.bucket
      DYNAMODB_TABLE = aws_dynamodb_table.prices.name
      HYDRATE_SECS   = "45"

      # Secrets passed directly as TF variables from .env — no SSM needed
      GOOGLE_EMAIL       = var.google_email
      GOOGLE_PASSWORD    = var.google_password
      TOTP_SECRET        = var.totp_secret
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
      TELEGRAM_CHAT_ID   = var.telegram_chat_id
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.basic_logs,
    aws_iam_role_policy_attachment.lambda_app_attach,
  ]
}

resource "aws_cloudwatch_event_rule" "every_10min" {
  name                = "${local.name}-every-10min"
  schedule_expression = "rate(10 minutes)"
  description         = "Trigger ${local.name} every 10 minutes"
}

resource "aws_cloudwatch_event_target" "lambda" {
  count     = var.image_uri != "" ? 1 : 0
  rule      = aws_cloudwatch_event_rule.every_10min.name
  target_id = "lambda"
  arn       = aws_lambda_function.tracker[0].arn
  input     = jsonencode({ source = "eventbridge", schedule = "10min" })
}

resource "aws_lambda_permission" "allow_eventbridge" {
  count               = var.image_uri != "" ? 1 : 0
  statement_id_prefix = "AllowEventBridgeInvoke-"
  action              = "lambda:InvokeFunction"
  function_name       = aws_lambda_function.tracker[0].function_name
  principal           = "events.amazonaws.com"
  source_arn          = aws_cloudwatch_event_rule.every_10min.arn
}

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
  value = aws_cloudwatch_event_rule.every_10min.schedule_expression
}
