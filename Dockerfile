# Lambda container image (ARM64 Graviton).
# Built by scripts/deploy.sh:  docker buildx build --platform linux/arm64 ...
FROM python:3.12-slim-bookworm

# ── System dependencies ────────────────────────────────────────────────────────
# Chromium is pinned: 150.0.7871.46 from current bookworm-security crashes with
# SIGTRAP during startup on arm64 containers (both Lambda and local Docker), so
# we install the last known-good build from snapshot.debian.org. All apt
# packages come from the same snapshot for a reproducible image.
ARG CHROMIUM_VERSION=146.0.7680.177-1~deb12u1
ARG DEBIAN_SNAPSHOT=20260408T000000Z
RUN set -eux; \
  printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\nAcquire::http::No-Cache "true";\nAcquire::Check-Valid-Until "false";\n' \
    > /etc/apt/apt.conf.d/99fixbadproxy; \
  printf 'deb https://snapshot.debian.org/archive/debian/%s/ bookworm main\ndeb https://snapshot.debian.org/archive/debian-security/%s/ bookworm-security main\n' \
    "${DEBIAN_SNAPSHOT}" "${DEBIAN_SNAPSHOT}" > /etc/apt/sources.list.d/snapshot.list; \
  rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; \
  apt-get update; \
  apt-get install -y --no-install-recommends --fix-missing \
    "chromium=${CHROMIUM_VERSION}" \
    "chromium-common=${CHROMIUM_VERSION}" \
    "chromium-driver=${CHROMIUM_VERSION}" \
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

# ── Application package ────────────────────────────────────────────────────────
COPY src/gfpt ./gfpt

# ── Lambda runtime ─────────────────────────────────────────────────────────────
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["gfpt.handler.handler"]
