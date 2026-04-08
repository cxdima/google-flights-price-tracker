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

variable "telegram_chat_id" {
  type      = string
  sensitive = true
  default   = ""
}
