variable "project_name" {
  type    = string
  default = "gfpricetracker"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "image_uri" {
  type    = string
  default = ""
}

# Minutes between tracker runs. 15 keeps Lambda comfortably inside the
# always-free tier (400k GB-s/month); 10 is borderline over it.
variable "schedule_minutes" {
  type    = number
  default = 15
}

variable "log_retention_days" {
  type    = number
  default = 14
}

# Launch Chrome only every Nth run; runs between refire the saved price
# requests without a browser (~10s instead of ~60-90s of billed time).
# 1 = Chrome every run (the pre-refire behavior).
variable "browser_every_n" {
  type    = number
  default = 1
}

# Email address for the CloudWatch safety-net alarms (Lambda errors, missed
# schedules, consecutive scrape failures). The SNS subscription must be
# confirmed once by clicking the link AWS emails after the first apply.
variable "alert_email" {
  type    = string
  default = "d.moiseenkonl@gmail.com"
}

variable "google_email" {
  type      = string
  sensitive = true
  default   = ""
}

variable "google_password" {
  type      = string
  sensitive = true
  default   = ""
}

variable "totp_secret" {
  type      = string
  sensitive = true
  default   = ""
}

variable "telegram_bot_token" {
  type      = string
  sensitive = true
  default   = ""
}

# Preferred: "chat_id:Name,chat_id:Name"
variable "telegram_users" {
  type      = string
  sensitive = true
  default   = ""
}

# Legacy fallback: comma-separated chat IDs (kept for compatibility)
variable "telegram_chat_id" {
  type      = string
  sensitive = true
  default   = ""
}
