#!/bin/sh
# Launch the headless miner. Runs Chrome headful under a virtual X display
# unless KDM_HEADLESS is truthy. We start Xvfb ourselves (instead of xvfb-run)
# and `exec` python so tini delivers SIGTERM straight to python for a clean
# shutdown (some xvfb-run builds don't forward signals to the child).
set -e

# Seed an example config into the data volume on first run so users have a
# starting point. (Channels can also come purely from the KDM_CHANNELS env.)
if [ -n "$KDM_DATA_DIR" ] && [ ! -f "$KDM_DATA_DIR/config.json" ] && \
   [ -f /app/config.example.json ]; then
    mkdir -p "$KDM_DATA_DIR"
    cp /app/config.example.json "$KDM_DATA_DIR/config.json" || true
fi

# Clear stale Chrome singleton locks left by a previous crash; otherwise the
# next launch can fail with "cannot connect to chrome ... not reachable".
if [ -n "$KDM_DATA_DIR" ]; then
    rm -f "$KDM_DATA_DIR/chrome_data/Singleton"* 2>/dev/null || true
fi

cd /app

case "$(printf '%s' "${KDM_HEADLESS:-0}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on|y)
        # True headless Chrome — no display needed.
        exec python runminer.py
        ;;
    *)
        # Headful under Xvfb. Start the X server in the background, then exec
        # python so it is PID-forwarded SIGTERM by tini.
        rm -f /tmp/.X99-lock 2>/dev/null || true
        Xvfb :99 -screen 0 1600x900x24 -nolisten tcp >/dev/null 2>&1 &
        XVFB_PID=$!
        trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT
        export DISPLAY=:99
        # Give Xvfb a moment to come up.
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            [ -e /tmp/.X11-unix/X99 ] && break
            sleep 0.3
        done
        exec python runminer.py
        ;;
esac
