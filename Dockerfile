# KickDropsMiner — Dockerized desktop GUI served over the web (noVNC).
#
# Built on jlesage/baseimage-gui, which provides Xvfb + a window manager + VNC +
# a noVNC web client (port 5800). The existing CustomTkinter GUI and the Chromium
# browser that undetected-chromedriver drives both render inside that virtual
# display, so the whole app is usable from a browser — ideal for Unraid.
FROM jlesage/baseimage-gui:debian-12-v4

LABEL org.opencontainers.image.title="Kick Drops Miner" \
      org.opencontainers.image.description="Dockerized KickDropsMiner with a noVNC web GUI" \
      org.opencontainers.image.source="https://github.com/HyperBeats/KickDropsMiner"

ENV APP_NAME="Kick Drops Miner" \
    # Persist config.json, cookies/ and chrome_data/ on the mounted volume.
    KDM_DATA_DIR=/config \
    # Use the matched system chromedriver shipped below (no runtime download).
    KDM_CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    PATH=/opt/venv/bin:$PATH \
    # Larger default desktop so the GUI + a Chromium window fit comfortably.
    DISPLAY_WIDTH=1600 \
    DISPLAY_HEIGHT=900

# Runtime deps: Chromium + matched driver, Python + Tk, fonts.
# The base image enables apt Recommends, which drags in systemd/udev/upower —
# systemd's postinst then fails in-container (mkdir /var/log). Force Recommends
# AND Suggests off so only hard deps install (chromium needs only libudev1).
RUN printf 'APT::Install-Recommends "false";\nAPT::Install-Suggests "false";\n' \
        > /etc/apt/apt.conf.d/99-no-recommends && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        chromium \
        chromium-driver \
        python3 \
        python3-venv \
        python3-tk \
        python3-pip \
        fonts-dejavu \
        ca-certificates \
        tzdata && \
    # Venv with system site-packages so the apt-provided tkinter is importable.
    python3 -m venv --system-site-packages /opt/venv && \
    rm -rf /var/lib/apt/lists/*

# Python dependencies.
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

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

# Default config seed: only used to create /config/config.json on first run if the
# volume is empty (the app copies its own defaults; this is a convenience fallback).
VOLUME ["/config"]

EXPOSE 5800 5900
