#!/bin/sh
# Launch the headless miner. Runs Chrome headful under Xvfb unless KDM_HEADLESS=1.
set -e

# Seed an example config into the data volume on first run so users have a
# starting point. (Channels can also come purely from the KDM_CHANNELS env.)
if [ -n "$KDM_DATA_DIR" ] && [ ! -f "$KDM_DATA_DIR/config.json" ] && \
   [ -f /app/config.miner.example.json ]; then
    mkdir -p "$KDM_DATA_DIR"
    cp /app/config.miner.example.json "$KDM_DATA_DIR/config.json" || true
fi

# Clear stale Chrome singleton locks left by a previous crash; otherwise the
# next launch can fail with "cannot connect to chrome ... not reachable".
if [ -n "$KDM_DATA_DIR" ]; then
    rm -f "$KDM_DATA_DIR/chrome_data/Singleton"* 2>/dev/null || true
fi

cd /app

# KDM_HEADLESS truthy -> let Chrome run true-headless (no display needed).
case "$(printf '%s' "${KDM_HEADLESS:-0}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on|y)
        exec python runminer.py
        ;;
    *)
        # Headful: provide a virtual display via xvfb-run.
        exec xvfb-run -a --server-args="-screen 0 1600x900x24" python runminer.py
        ;;
esac
