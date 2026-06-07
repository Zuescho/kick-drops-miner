# kick-drops-miner — headless Kick.com drops miner (no GUI, no noVNC, no Tk).
#
# A plain long-lived Python process that logs to stdout (Docker-friendly). It
# drives system Chromium via Selenium + undetected-chromedriver. Chrome runs
# under Xvfb (headful-in-a-virtual-display) because undetected-chromedriver is
# most reliable against Kick's Cloudflare when not in --headless mode; set
# KDM_HEADLESS=1 to force true headless instead.
#
# Debian slim ships a matched chromium + chromium-driver pair (no musl quirks).
FROM python:3.12-slim

LABEL org.opencontainers.image.title="kick-drops-miner" \
      org.opencontainers.image.description="Headless Kick.com drops miner: watches channels for drops, logs to stdout" \
      org.opencontainers.image.source="https://github.com/Zuescho/kick-drops-miner"

ENV KDM_DATA_DIR=/config \
    # System chromedriver, copied to a world-writable path because
    # undetected-chromedriver patches the driver binary in place.
    KDM_CHROMEDRIVER_PATH=/usr/local/bin/chromedriver \
    # Tell the app it runs in a container (enables GPU-disable Chrome flags).
    KDM_CONTAINER=1 \
    # Run Chrome headful under Xvfb by default (most robust vs. Cloudflare).
    KDM_HEADLESS=0 \
    # Stream Python stdout/stderr to the container log unbuffered.
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runtime deps: Chromium + matched chromedriver, Xvfb (virtual display), fonts.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        xvfb \
        xauth \
        fonts-dejavu-core \
        fonts-liberation \
        tini \
        ca-certificates \
        tzdata \
        # Explicit GL/GBM/NSS/ALSA libs so Chromium has a software renderer and
        # doesn't die with "page crash" / "no renderer" under --no-install-recommends.
        libgl1 \
        libegl1 \
        libgbm1 \
        libnss3 \
        libasound2 \
        libxkbcommon0 \
        libxshmfence1 && \
    rm -rf /var/lib/apt/lists/* && \
    # undetected-chromedriver patches the driver in place -> needs a writable copy.
    cp "$(command -v chromedriver)" /usr/local/bin/chromedriver && \
    chmod 0777 /usr/local/bin/chromedriver

# Python deps (selenium + undetected-chromedriver, no GUI).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm -rf /tmp/* /root/.cache

# Application source: the miner package + its entrypoint, plus a seed config.
WORKDIR /app
COPY miner/ /app/miner/
COPY runminer.py /app/
COPY config.example.json /app/config.example.json
COPY startminer.sh /startminer.sh
RUN chmod +x /startminer.sh

VOLUME ["/config"]

# tini reaps Chrome's child processes and forwards SIGTERM for a clean shutdown.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/startminer.sh"]
