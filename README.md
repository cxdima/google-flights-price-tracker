# Google Flights Price Tracker

Monitors the saved-flights list of one Google account on Google Flights and alerts its Telegram users (two of us, in practice) whenever a flight hits a new price low. Runs on AWS Lambda — an arm64 container image with headless Chromium — on an EventBridge schedule, every 15 minutes by default (`SCHEDULE_MINUTES` in `.env`).

---

## Architecture

```
EventBridge (rate: 15 min, input {"source": "eventbridge"})
        │
        ▼
  AWS Lambda (arm64 container, 2 GB) ──── S3 (session cookies, flight manifest, run state, crash screenshots)
        │                            ──── DynamoDB (price history, 1-year TTL)
        │                            ──── CloudWatch Logs (14-day retention)
        ▼
  Headless Chromium
    └─ Stealth fingerprint patches (navigator.webdriver, plugins, chrome runtime)
    └─ Session restore from S3 → skip login on warm runs
    └─ CDP network interception → captures GetSolutionPrices POSTs
    └─ Chrome killed early; captured calls re-fired from plain Python
        │
        ▼
  Telegram alerts → broadcast to all non-paused users


Telegram commands (separate path):
  Telegram servers ──(X-Telegram-Bot-Api-Secret-Token)──▶ Lambda Function URL
                                                              │
                                                              ▼
                                                   private reply to the sender
```

### Strict event routing

The handler recognizes exactly two callers, each with its own gate:

- **EventBridge schedule** → runs the tracker. Matched on the explicit `source` field the rule injects into the event.
- **Telegram webhook** (the public Function URL) → bot commands only. Every request must carry the secret token Telegram was configured with (`X-Telegram-Bot-Api-Secret-Token`); anything else gets a 403.

Any other payload is logged and ignored. This closes a v1 security/cost hole: the old handler fell through to a full tracker run for any payload it didn't recognize, so anyone who found the public URL could trigger 2 GB Chromium invocations at will. The public URL can now never start a tracker run.

---

## How price detection works

1. **Session restore** — cookies from `s3://…/sessions/latest.json` are injected via CDP and Chrome navigates to `google.com/travel/flights/saves`. If Google still accepts the session, the entire login flow (~20–30 s) is skipped. Sessions older than 6 hours are discarded and a full login runs instead (email → password → TOTP → dismissal of passkey/recovery interstitials).
2. **CDP capture** — performance logs are polled for the `GetSolutionPrices` POSTs the saves page fires as it renders. The page is scrolled stepwise to trigger lazy-loaded flight rows; polling exits after 2.5 s of network silence so progressive batches aren't cut off.
3. **Chrome released early** — cookies and User-Agent are extracted, the session is saved back to S3, and Chrome is killed (~800 MB freed) before any parsing or DB work.
4. **Re-fire from Python** — the captured POSTs are re-fired from plain Python (stdlib `urllib`, up to 4 threads) using the live session cookies. No extra page loads.
5. **Parse** — responses are parsed for `wrb.fr` frames containing `(flight_id, price)` tuples; the lowest price per flight wins.
6. **Compare** — each price is checked against the most recent record in DynamoDB.
7. **Alert** — if the price is lower (or the flight is new), a row is appended to DynamoDB and a Telegram alert goes out to every non-paused user.

If a run fails, users are notified once after 3 consecutive failures (not on every blip) and once on recovery.

---

## How flights stay in sync

The S3 manifest is reconciled on every run (`tracking/reconcile.py`):

- A flight seen in this run's page metadata or price response is alive — merged, counter reset.
- **New saved flights are picked up automatically** and announced with a "Now tracking" alert.
- **Flights removed from Google Flights are pruned** after being absent for 2 consecutive runs, with a "No longer tracking" Telegram notice. The 2-run grace period means one flaky scrape (missed scroll batch, partial page) can't wipe the list.
- A run that produced zero flights prunes nothing — an empty result is far more likely a broken scrape than an emptied saved list.

This fixes the v1 bug where flights removed from the saved list stayed in the manifest (and `/flights` output) forever.

---

## Telegram bot

Multi-user via `TELEGRAM_USERS="chat_id:Name,chat_id:Name"`. Price alerts broadcast to all non-paused users; health notices (failure streak / recovery) go to everyone; command replies are always private to the sender. Messages from chats not in the list are ignored silently — replying would confirm the bot exists to whoever is probing it.

| Command | Response |
|---|---|
| `/status` | Last run: time, flights tracked, new lows, runtime (plus your pause state) |
| `/flights` | Every monitored flight grouped by route and date, with prices and booking links |
| `/pause` | Stop price alerts for you only — others are unaffected |
| `/resume` | Re-enable your alerts |
| `/help` | Command list (also the reply to any unrecognized command) |

The webhook is protected by a secret token deterministically derived from the bot token (`sha256("gfpt-webhook:<token>")`, first 40 hex chars), so the deploy script and the Lambda agree without managing an extra secret. It is registered with Telegram automatically at deploy (stage 10).

---

## Cost

Designed to run for pocket change. At the default 15-minute schedule:

| Item | Monthly cost | Why |
|---|---|---|
| Lambda compute | ≈ $0 | Always-free tier is 400k GB-s/mo; ~2,880 runs × ~40 s warm × 2 GB ≈ 230k GB-s |
| Lambda requests | $0 | ~2,880 scheduled runs + webhook calls, vs 1M free |
| DynamoDB | ≈ $0 | On-demand; a handful of reads and only new-low writes per run |
| S3 | ≈ $0 | A few small JSON objects; screenshots/debug objects expire after 3 days |
| CloudWatch Logs | ≈ $0 | Bounded by 14-day retention |
| ECR | $0.10–0.20 | Lifecycle policy keeps only 3 images (~600 MB each at $0.10/GB-mo) |
| **Total** | **< $1/mo** | Dominated by ECR image storage |

Cost guards added in v2:

- **ECR lifecycle policy** — every deploy pushes a ~600 MB image; without the policy old images accumulate forever. Only the newest 3 are kept (current + two rollback candidates).
- **Explicit CloudWatch log group** with 14-day retention — the auto-created group kept every log line forever.
- **512 MB ephemeral storage** — the free allocation; the Chrome profile (images and cache disabled) stays far below it.
- **No unauthenticated path that can start Chrome** — see event routing above.
- S3 lifecycle rules (7-day non-current versions, 3-day debug/screenshot expiry) and a 1-year DynamoDB TTL keep storage flat.

Note: 15 minutes keeps Lambda comfortably inside the free tier; a 10-minute schedule is borderline over it.

---

## Project layout

```
.
├── src/gfpt/                  # Application package (copied into the image)
│   ├── config.py              # Settings + constants; all env access in one place
│   ├── models.py              # Frozen dataclasses shared across the package
│   ├── handler.py             # Lambda entry point — strict event routing
│   ├── tracking/
│   │   ├── browser.py         # Chromium lifecycle + stealth fingerprint patches
│   │   ├── auth.py            # Google login, TOTP, S3 session save/restore
│   │   ├── capture.py         # CDP interception of GetSolutionPrices POSTs
│   │   ├── metadata.py        # Flight metadata from AF_initDataCallback blocks
│   │   ├── prices.py          # Re-fire captured calls, parse wrb.fr price frames
│   │   ├── reconcile.py       # Manifest reconciliation (auto-add / prune flights)
│   │   └── runner.py          # One tracker run, orchestrated end to end
│   ├── storage/
│   │   ├── state.py           # S3-backed JSON state (session, manifest, prefs, health)
│   │   └── dynamo.py          # DynamoDB price history (append-only, 1-year TTL)
│   └── bot/
│       ├── telegram.py        # Bot API client + webhook secret derivation
│       ├── users.py           # Authorized users and per-user mute preferences
│       ├── commands.py        # /status /flights /pause /resume /help
│       ├── format.py          # Message builders (pure functions, unit-tested)
│       └── notifier.py        # Alert fan-out to users
│
├── infra/                     # Terraform: ECR, S3, DynamoDB, IAM, Lambda, EventBridge
│   ├── main.tf
│   └── variables.tf
│
├── scripts/
│   ├── deploy.sh              # 10-stage build + deploy pipeline
│   └── run_local.py           # Staged integration harness (visible Chrome)
│
├── tests/                     # Unit tests — no AWS, no browser
├── Dockerfile                 # arm64 Lambda container (Chromium + chromedriver)
├── Makefile                   # test / lint / deploy / run-local / logs
├── pyproject.toml
├── requirements.txt           # Runtime deps: boto3, selenium, pyotp, awslambdaric
└── .env.example               # Template — copy to .env and fill in secrets
```

---

## Prerequisites

| Tool | Version |
|---|---|
| Python | 3.11+ |
| Terraform | 1.5+ |
| Docker | with buildx |
| AWS CLI | v2 |

AWS credentials must be configured (`aws configure` or environment variables) with permissions to manage Lambda, ECR, S3, DynamoDB, IAM, EventBridge, and CloudWatch Logs. Secrets are passed to the Lambda directly as Terraform variables from `.env` — no Parameter Store or Secrets Manager involved.

---

## Setup

### 1. Configure secrets

```bash
cp .env.example .env
```

Edit `.env`:

```ini
GOOGLE_EMAIL=you@gmail.com
GOOGLE_PASSWORD=your-password
TOTP_SECRET=your-totp-base32-secret

TELEGRAM_BOT_TOKEN=123456:AAAA...
TELEGRAM_USERS=123456789:Dmitry,987654321:Alex

AWS_REGION=us-east-1
PROJECT_NAME=gfpricetracker
SCHEDULE_MINUTES=15
```

- **`TELEGRAM_USERS`** — comma-separated `chat_id:Name` pairs. To find a chat ID, message [@userinfobot](https://t.me/userinfobot) on Telegram; it replies with your numeric ID. Every listed user gets alerts and can use the bot commands.
- **`TOTP_SECRET`** — the Base32 seed from your Google Account's 2FA setup (the string behind the QR code). If 2FA is already set up in an authenticator app, export the secret from there.

### 2. Deploy

```bash
make deploy
```

The deploy script (`scripts/deploy.sh`) runs 10 stages:

| Stage | Action |
|---|---|
| 1 | `terraform init` |
| 2 | Best-effort import of pre-existing AWS resources (prevents AlreadyExists) |
| 3 | Apply base infrastructure (ECR, S3, DynamoDB, IAM, log group, schedule rule) |
| 4 | Log in to ECR |
| 5 | Build and push the arm64 Docker image |
| 6 | Import Lambda resources now that an image URI exists |
| 7 | Apply Lambda + Function URL + schedule target |
| 8 | Wait for the Lambda update to propagate |
| 9 | Register the Telegram webhook with the secret token (before the smoke test, so a failed test can't leave the bot rejecting Telegram traffic) |
| 10 | Test invoke (`{"source": "deploy-test"}`) — prints CloudWatch tail, fails the deploy if the run fails |

### 3. Local development

```bash
make test        # Unit tests — no browser, no AWS
make lint        # Ruff over src/ and tests/
make run-local   # Full pipeline against live Chrome (visible window)
make logs        # Tail the Lambda's CloudWatch logs
```

`run_local.py` runs the pipeline in independent stages (`browser`, `auth`, `intercept`, `prices`, `storage`) so failures are easy to pinpoint:

```bash
python scripts/run_local.py                  # all stages, visible Chrome
python scripts/run_local.py --stage auth     # a single stage
python scripts/run_local.py --headless       # headless, as in Lambda
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_EMAIL` | Yes | Google account whose saved flights are tracked |
| `GOOGLE_PASSWORD` | Yes | Google account password |
| `TOTP_SECRET` | Yes | Base32 TOTP seed for 2FA |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_USERS` | Yes | `chat_id:Name` pairs, comma-separated |
| `TELEGRAM_CHAT_ID` | Legacy | Comma-separated chat IDs — fallback if `TELEGRAM_USERS` is unset |
| `AWS_REGION` | No | AWS region (default: `us-east-1`) |
| `PROJECT_NAME` | No | Prefix for all AWS resources (default: `gfpricetracker`) |
| `SCHEDULE_MINUTES` | No | Minutes between runs (default: 15) |
| `S3_BUCKET` | Auto | Injected into the Lambda by Terraform |
| `DYNAMODB_TABLE` | Auto | Injected into the Lambda by Terraform |
| `HYDRATE_SECS` | Auto | Max seconds to wait for GetSolutionPrices calls (default: 45) |
| `CHROME_BINARY` / `CHROMEDRIVER_PATH` | No | Set by the Dockerfile; override for local runs if needed |

---

## Infrastructure

All AWS resources are managed by Terraform in `infra/`.

| Resource | Details |
|---|---|
| ECR repository | Lambda container images; lifecycle policy keeps the newest 3 |
| Lambda function | arm64, 2 GB RAM, 120 s timeout, 512 MB ephemeral storage |
| EventBridge rule | `rate(15 minutes)` by default; injects `{"source": "eventbridge"}` |
| Lambda Function URL | Public (Telegram must reach it); auth enforced in the handler via the webhook secret token |
| S3 bucket | Session cookies, flight manifest, run state, crash screenshots; versioned with lifecycle expiry |
| DynamoDB table | Price history keyed by `flight_id + ts`, on-demand billing, 1-year TTL |
| CloudWatch log group | Explicitly declared with 14-day retention |
| IAM | Least-privilege: `GetObject/PutObject/HeadObject` on the bucket, `PutItem/GetItem/Query` on the table |
