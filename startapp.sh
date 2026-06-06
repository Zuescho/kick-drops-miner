#!/bin/sh
# Launched by jlesage/baseimage-gui inside the virtual display.
set -e

# Seed config.json into the data volume on first run so users have something to edit.
if [ -n "$KDM_DATA_DIR" ] && [ ! -f "$KDM_DATA_DIR/config.json" ] && [ -f /app/config.json ]; then
    mkdir -p "$KDM_DATA_DIR"
    cp /app/config.json "$KDM_DATA_DIR/config.json" || true
fi

cd /app
exec /opt/venv/bin/python3 main.py
