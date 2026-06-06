# KickDropsMiner — Dockerized desktop GUI served over the web (noVNC).
#
# Built on jlesage/baseimage-gui (Alpine), which provides Xvfb + a window manager +
# VNC + a noVNC web client (port 5800). The existing CustomTkinter GUI and the
# Chromium browser that undetected-chromedriver drives both render inside that
# virtual display, so the whole app is usable from a browser — ideal for Unraid.
#
# Alpine is used (matching jlesage's own browser images) because the Debian base
# pulls systemd as a transitive dependency of the Chromium/GTK stack, whose
# post-install script is incompatible with this base image's /var/log layout.
FROM jlesage/baseimage-gui:alpine-3.23-v4

LABEL org.opencontainers.image.title="Kick Drops Miner" \
      org.opencontainers.image.description="Dockerized KickDropsMiner with a noVNC web GUI" \
      org.opencontainers.image.source="https://github.com/HyperBeats/KickDropsMiner"

ENV APP_NAME="Kick Drops Miner" \
    # Persist config.json, cookies/ and chrome_data/ on the mounted volume.
    KDM_DATA_DIR=/config \
    # Use the matched musl chromedriver shipped below (no runtime download).
    # Points at a world-writable copy because undetected-chromedriver patches
    # the driver binary in place, which the non-root app user can't do to /usr/bin.
    KDM_CHROMEDRIVER_PATH=/usr/local/bin/chromedriver \
    # Tell the app it runs in a container (enables GPU-disable Chrome flags).
    KDM_CONTAINER=1 \
    # Stream Python stdout/stderr to the container log (shows real tracebacks).
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    # Larger default desktop so the GUI + a Chromium window fit comfortably.
    DISPLAY_WIDTH=1600 \
    DISPLAY_HEIGHT=900

# Runtime deps: Chromium + matched (musl) chromedriver, Python + Tk + Pillow, fonts.
RUN add-pkg \
        chromium \
        chromium-chromedriver \
        python3 \
        py3-pip \
        python3-tkinter \
        py3-pillow \
        nss \
        freetype \
        harfbuzz \
        ttf-freefont \
        font-dejavu \
        tzdata && \
    # undetected-chromedriver patches the driver binary in place, so it must be
    # writable by the non-root app user — give it a world-writable copy.
    cp /usr/bin/chromedriver /usr/local/bin/chromedriver && \
    chmod 0777 /usr/local/bin/chromedriver

# Python dependencies in a venv (system site-packages so apt-provided tkinter /
# Pillow are importable). Build deps are added temporarily for any wheels that
# need compiling, then removed to keep the image small.
COPY requirements.docker.txt /tmp/requirements.txt
RUN apk add --no-cache --virtual .build-deps build-base python3-dev && \
    python3 -m venv --system-site-packages /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt && \
    apk del .build-deps && \
    rm -rf /tmp/* /root/.cache

# Application source.
COPY core/ /app/core/
COPY ui/ /app/ui/
COPY utils/ /app/utils/
COPY locales/ /app/locales/
COPY assets/ /app/assets/
COPY main.py config.json /app/

# App icon shown in the noVNC tab + start script.
COPY startapp.sh /startapp.sh
RUN chmod +x /startapp.sh && \
    install_app_icon.sh /app/assets/logo.png && \
    set-cont-env APP_NAME "$APP_NAME"

VOLUME ["/config"]

EXPOSE 5800 5900
