#!/bin/sh
# Launched by jlesage/baseimage-gui inside the virtual display.
set -e

# Seed config.json into the data volume on first run so users have something to edit.
if [ -n "$KDM_DATA_DIR" ] && [ ! -f "$KDM_DATA_DIR/config.json" ] && [ -f /app/config.json ]; then
    mkdir -p "$KDM_DATA_DIR"
    cp /app/config.json "$KDM_DATA_DIR/config.json" || true
fi

# Clear stale Chrome singleton locks left by a previous crash; otherwise the
# next launch can fail with "cannot connect to chrome ... not reachable".
if [ -n "$KDM_DATA_DIR" ]; then
    rm -f "$KDM_DATA_DIR/chrome_data/Singleton"* 2>/dev/null || true
fi

cd /app
exec /opt/venv/bin/python3 main.py
