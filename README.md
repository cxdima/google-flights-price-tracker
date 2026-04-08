# Google Flights Price Tracker

Monitors saved Google Flights and sends a Telegram alert whenever a new price low is detected. Runs on AWS Lambda on a 20-minute schedule.

---

## Architecture

```
EventBridge (rate: 20 min)
        │
        ▼
  AWS Lambda  ──── S3 (session cookies + crash screenshots)
        │      ──── DynamoDB (price history)
        │      ──── CloudWatch Logs
        ▼
  Headless Chrome (Chromium)
    └─ Stealth fingerprint patches (navigator.webdriver, plugins, chrome runtime)
    └─ Session restore from S3 → skip login on warm runs
    └─ CDP network interception → captures GetSolutionPrices POST
    └─ Re-fires API call from Python with live session cookies
        │
        ▼
  Telegram Bot  ←── /status webhook (Lambda Function URL)
```

### How session caching works

After every successful login the browser cookies are serialised as JSON and stored in S3 at `sessions/latest.json`. On the next invocation those cookies are injected into Chrome before any navigation. If Google still considers the session valid the entire login flow (~20–30 s) is skipped. Sessions older than 6 hours are automatically discarded and a fresh login is forced.

---

## Project layout

```
.
├── src/               # Lambda container — all application code
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config.py      # Environment variables and constants
│   ├── browser.py     # Chrome lifecycle + stealth fingerprint patches
│   ├── auth.py        # Google login + S3 session save / restore
│   ├── tracker.py     # CDP interception, metadata parsing, price parsing
│   ├── storage.py     # DynamoDB reads / writes, S3 screenshot uploads
│   ├── notifier.py    # Telegram alerts and /status webhook handler
│   └── handler.py     # Lambda entry point and orchestration
│
├── infra/             # Terraform infrastructure (AWS)
│   ├── main.tf
│   └── variables.tf
│
├── scripts/
│   └── deploy.sh      # End-to-end build + deploy pipeline
│
├── tests/
│   └── test_local.py  # Run the full flow locally (visible Chrome window)
│
├── .env.example       # Template — copy to .env and fill in secrets
└── README.md
```

---

## Prerequisites

| Tool | Version |
|---|---|
| Python | 3.11+ |
| Terraform | 1.5+ |
| Docker | 24+ (with buildx) |
| AWS CLI | v2 |

AWS credentials must be configured (`aws configure` or environment variables) with permissions to manage Lambda, ECR, S3, DynamoDB, IAM, EventBridge, and SSM Parameter Store.

---

## Setup

### 1. Clone and configure secrets

```bash
git clone <repo-url>
cd google-flights-price-tracker

cp .env.example .env
```

Edit `.env`:

```ini
GOOGLE_EMAIL=you@gmail.com
GOOGLE_PASSWORD=your-password
TOTP_SECRET=your-totp-base32-secret   # from Google 2FA setup

TELEGRAM_BOT_TOKEN=123456:AAAA...
TELEGRAM_CHAT_ID=123456789            # comma-separated for multiple recipients

AWS_REGION=us-east-1
PROJECT_NAME=gfpricetracker
```

> **TOTP secret** — this is the Base32 seed from your Google Account's 2FA setup page (the string you scan as a QR code). If you set up 2FA with an authenticator app, export the secret from there.

### 2. Deploy to AWS

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

The deploy script runs 9 stages automatically:

| Stage | Action |
|---|---|
| 0 | Push secrets to SSM Parameter Store (encrypted) |
| 1 | `terraform init` |
| 2 | Import any pre-existing AWS resources |
| 3 | Apply base infrastructure (S3, DynamoDB, IAM) |
| 4 | Log in to ECR |
| 5 | Build and push the Docker image |
| 5.5 | Import Lambda into Terraform state |
| 6 | Apply Lambda + EventBridge schedule |
| 7 | Wait for Lambda update |
| 8 | Test invoke (prints CloudWatch tail) |
| 9 | Register Telegram webhook |

### 3. Test locally

```bash
pip install -r src/requirements.txt
python tests/test_local.py          # opens a visible Chrome window
python tests/test_local.py --headless
```

---

## Infrastructure

All AWS resources are managed by Terraform in `infra/`.

| Resource | Purpose |
|---|---|
| ECR repository | Stores the Lambda container image |
| Lambda function | Runs on a 20-minute schedule; 3 GB RAM, 5 GB `/tmp`, 8-min timeout |
| EventBridge rule | Fires every 20 minutes |
| Lambda Function URL | Receives Telegram webhook (no auth — protected by chat ID allowlist) |
| S3 bucket | Stores session cookies and crash screenshots |
| DynamoDB table | Price history keyed by `flight_id + timestamp` (1-year TTL) |
| SSM Parameter Store | Encrypted secret storage |

---

## Telegram commands

| Command | Response |
|---|---|
| `/status` | Last run time, flight count, prices updated, runtime |

Only chat IDs listed in `TELEGRAM_CHAT_ID` receive alerts and can issue commands.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_EMAIL` | Yes | Google account email |
| `GOOGLE_PASSWORD` | Yes | Google account password |
| `TOTP_SECRET` | Yes | Base32 TOTP seed for 2FA |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Comma-separated chat IDs for alerts |
| `S3_BUCKET` | Auto | Set by Terraform |
| `DYNAMODB_TABLE` | Auto | Set by Terraform |
| `HYDRATE_SECS` | No | Seconds to wait for API call (default: 20) |
| `AWS_REGION` | No | AWS region (default: us-east-1) |

---

## How price detection works

1. Chrome navigates to `google.com/travel/flights/saves` with your session.
2. CDP network logging captures the `GetSolutionPrices` POST request automatically fired by the page.
3. Python re-fires that same POST using your live session cookies (no additional page loads).
4. The response is parsed for `wrb.fr` frames containing `(flight_id, price)` tuples.
5. Each price is compared against the lowest price stored in DynamoDB.
6. If the new price is lower, it is written to DynamoDB and a Telegram alert is sent.
