# Build for ARM64 Graviton Lambda:
#   docker buildx build --platform linux/arm64 -t <tag> --push .
FROM python:3.12-slim-bookworm

# ── System dependencies ────────────────────────────────────────────────────────
RUN set -eux; \
  printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\nAcquire::http::No-Cache "true";\n' \
    > /etc/apt/apt.conf.d/99fixbadproxy; \
  apt-get update; \
  apt-get install -y --no-install-recommends --fix-missing \
    chromium \
    chromium-driver \
    ca-certificates \
    fonts-liberation \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2; \
  # Normalise chromedriver path (differs between Debian versions)
  if [ -x /usr/lib/chromium/chromedriver ] && [ ! -x /usr/bin/chromedriver ]; then \
    ln -s /usr/lib/chromium/chromedriver /usr/bin/chromedriver; \
  fi; \
  rm -rf /var/lib/apt/lists/*

# ── Runtime environment ────────────────────────────────────────────────────────
ENV HOME=/tmp \
    XDG_RUNTIME_DIR=/tmp \
    DBUS_SESSION_BUS_ADDRESS=/dev/null \
    CHROME_BINARY=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── Python dependencies ────────────────────────────────────────────────────────
WORKDIR /var/task
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ─────────────────────────────────────────────────────────
COPY config.py   .
COPY browser.py  .
COPY auth.py     .
COPY tracker.py  .
COPY storage.py  .
COPY notifier.py .
COPY handler.py  .

# ── Lambda runtime ─────────────────────────────────────────────────────────────
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["handler.handler"]
