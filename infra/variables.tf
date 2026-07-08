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
