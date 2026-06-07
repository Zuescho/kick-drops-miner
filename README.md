<p align="center">
  <img src="assets/logo.png" width="96" alt="kick-drops-miner logo">
</p>

<h1 align="center">kick-drops-miner</h1>

<p align="center">
  A robust, <b>headless</b> Kick.com drops miner for Docker / Unraid.<br>
  No GUI, no noVNC — a long-lived process that logs to stdout.
</p>

---

`kick-drops-miner` watches the Kick channels you configure for their target
minutes, then rotates to the next, looping forever:

> read config → log in with saved cookies → watch each channel for its target
> minutes → rotate to the next → repeat.

It drives **one** long-lived Chromium (via `undetected-chromedriver`, which gets
past Kick's Cloudflare) for both the Kick API calls and the actual watching. It
accounts for **real playing time** (not wall-clock), auto-recovers a dead or
crashed browser, re-authenticates itself over multi-day runs, and shuts down
cleanly on `SIGTERM`.

> This started as a fork of [HyperBeats/KickDropsMiner](https://github.com/HyperBeats/KickDropsMiner)
> (a CustomTkinter GUI app) but has diverged into a standalone, headless,
> container-first rewrite — the GUI is gone; only the `miner/` engine remains.

---

## Quick start (Docker)

```bash
docker run -d \
  --name kick-drops-miner \
  --shm-size=1g \
  -v /path/to/config:/config \
  -e KDM_CHANNELS="https://kick.com/some-streamer=120, https://kick.com/another=60" \
  -e TZ=Europe/Berlin \
  ghcr.io/zuescho/kick-drops-miner:latest

docker logs -f kick-drops-miner
```

Or with compose (`docker-compose.yml` is included):

```bash
docker compose up -d && docker compose logs -f
```

> `--shm-size=1g` keeps Chromium from crashing on the default 64 MB `/dev/shm`.

You **must** seed a logged-in cookie file before drops will be credited — see
[Seeding cookies](#seeding-cookies). To build locally instead of pulling:
`docker build -t kick-drops-miner .`

---

## Configuration

Config is `{KDM_DATA_DIR}/config.json` (i.e. `/config/config.json` in Docker),
**overlaid with environment variables**. On first run an example is copied from
`config.example.json`. If `config.json` is missing, channels are read from the
`KDM_CHANNELS` env var instead. Loading **never crashes** — bad values are logged
and defaults used.

### `config.json` schema

```json
{
  "channels": [
    { "url": "https://kick.com/some-streamer", "minutes": 120, "category_id": null },
    { "url": "https://kick.com/another-streamer", "minutes": 60 },
    "https://kick.com/bare-url-uses-default-minutes"
  ],
  "headless": false,
  "force_160p": true,
  "mute": true,
  "offline_grace_checks": 4,
  "loop_forever": true,
  "poll_offline_seconds": 60,
  "auto_campaigns": false,
  "campaign_minutes": 0,
  "driver_recycle_hours": 6,
  "heartbeat_seconds": 300,
  "progress_log": true
}
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `channels` | list | `[]` | Channels to watch. Each is `{url, minutes, category_id?}` or a bare URL string. `minutes: 0` = watch indefinitely. |
| `headless` | bool | `false` | `false` = headful Chrome under Xvfb (most robust vs. Cloudflare; recommended). |
| `force_160p` | bool | `true` | Force lowest stream quality (160p) to save bandwidth/CPU. |
| `mute` | bool | `true` | Keep the `<video>` muted. |
| `offline_grace_checks` | int | `4` | Consecutive offline checks before a channel is abandoned (~1 min grace, so a BRB blip isn't dropped). |
| `loop_forever` | bool | `true` | After the queue drains, start over. |
| `poll_offline_seconds` | int | `60` | When a channel is offline and no live alternative exists, wait this long (idle cycles back off exponentially). |
| `auto_campaigns` | bool | `false` | Also enqueue a live channel from each active drop campaign. |
| `campaign_minutes` | int | `0` | Rotation slice for auto-campaign channels (`0` = use `KDM_DEFAULT_MINUTES`) so one 24/7 streamer can't pin the queue. |
| `driver_recycle_hours` | num | `6` | Proactively recycle Chrome past this age to cap renderer-memory growth over long runs. |
| `heartbeat_seconds` | int | `300` | Log a `still watching …` line (and refresh session/progress) on this cadence during a long watch. |
| `progress_log` | bool | `true` | Periodically log drops progress so you can confirm drops are actually crediting. |

A bare-string channel entry uses `KDM_DEFAULT_MINUTES` (default `120`).

### Environment variables

| Env var | Effect |
|---|---|
| `KDM_CHANNELS` | Channels when no `config.json`: comma- **or** newline-separated `url` or `url=minutes`. |
| `KDM_DATA_DIR` | Data dir holding `config.json`, `cookies/`, `chrome_data/`. Docker default `/config`. |
| `KDM_HEADLESS` | Truthy → true-headless Chrome (no Xvfb). Default headful under Xvfb. Overrides `headless`. |
| `KDM_DEFAULT_MINUTES` | Minutes for channels with no explicit minutes. Default `120`. |
| `KDM_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Default `INFO`. |
| `KDM_QUIET_LIBS` | `0` to stop suppressing noisy urllib3/selenium retry logs (default: suppressed). |
| `KDM_CHROMEDRIVER_PATH` | Path to chromedriver. Set by the image to `/usr/local/bin/chromedriver`. |
| `KDM_CONTAINER` | Truthy → container Chrome flags (GPU disable, etc.). Set to `1` by the image. |
| `KDM_LOOP_FOREVER` / `KDM_AUTO_CAMPAIGNS` | Override the matching JSON keys. |
| `KDM_FORCE_160P` / `KDM_MUTE` | Override `force_160p` / `mute`. |
| `KDM_POLL_OFFLINE_SECONDS` / `KDM_OFFLINE_GRACE_CHECKS` | Override the matching JSON keys. |
| `TZ` | Timezone for log timestamps (default UTC). |

Env wins over JSON for the toggles listed as "override" above.

---

## Seeding cookies

The miner authenticates entirely from saved cookies — it does **not** log in
interactively. It needs a Kick cookie file at:

```
{KDM_DATA_DIR}/cookies/kick.com.json   (e.g. /config/cookies/kick.com.json)
```

The file is a **JSON array** of cookie objects (Selenium `get_cookies()` format)
and **must include a logged-in `session_token` cookie** — that value also becomes
the `Authorization: Bearer` token for the drops endpoints. Example shape:

```json
[
  { "name": "session_token", "value": "<your session token>", "domain": ".kick.com", "path": "/" },
  { "name": "kick_session", "value": "...", "domain": ".kick.com", "path": "/" }
]
```

How to obtain it: log into kick.com in your browser, then export your kick.com
cookies with a cookie-export extension (e.g. **Cookie-Editor → Export → JSON**),
make sure `session_token` is present, and save the JSON array as `kick.com.json`
under `/config/cookies/`.

If no cookies are present the miner still runs but watches anonymously and drops
won't be credited (a warning is logged). The miner refreshes and re-saves cookies
from the live browser every 30 min, so a rotated `session_token` keeps working
and survives restarts.

---

## Running under Unraid

A template is provided at [`unraid/kick-drops-miner.xml`](unraid/kick-drops-miner.xml).

- **Repository:** `ghcr.io/zuescho/kick-drops-miner:latest`
- Map **`/config`** → e.g. `/mnt/user/appdata/kick-drops-miner/config`
  (holds `config.json`, `cookies/`, `chrome_data/`).
- Set **`KDM_CHANNELS`** (or place a `config.json` in the mapped dir).
- Add extra parameter **`--shm-size=1g`**.
- Drop your `kick.com.json` into `<appdata>/config/cookies/`.
- There is **no web UI** — monitor via the container log.

---

## How it works / robustness

One process, one long-lived Chromium, reused for everything. Designed to run
unattended for multi-day campaigns:

- **Credit-accurate watch time** — accrues only while the `<video>` is actually
  playing (`currentTime` advancing) *and* the channel is confirmed live within
  the last 90s. A stalled/blocked player or an API blackout pauses the clock
  instead of banking fake "watched" minutes.
- **No silent hangs** — every page load / in-page fetch is time-bounded; a stuck
  navigation is aborted, and a dead *or* crashed-renderer tab is detected and the
  browser recreated.
- **Player watchdog** — if playback stalls, the player is re-played and, past a
  grace window, the channel is reloaded (160p + visibility re-applied); after
  repeated failures it rotates on.
- **Session self-heal** — cookies/bearer are re-read from the live browser and
  persisted, so auth survives token rotation and restarts.
- **Rate-limit aware** — a `429/403/Cloudflare` response is distinguished from a
  real "offline" answer; polling backs off instead of hammering.
- **Memory-capped** — the driver is proactively recycled (default every 6h),
  including by slicing long/indefinite watches, to bound renderer growth.
- **Clean shutdown** — `SIGTERM` stops promptly and quits Chromium once.

`progress_log` periodically prints drops progress (e.g.
`drops progress: Rust Drops -> 45/120 min (in progress)`) so you can confirm
drops are actually crediting.

> **Headless note:** the default is *headful Chrome under Xvfb* because some drop
> systems gate crediting on the page being visible/focused. `KDM_HEADLESS=1`
> forces true headless and is best-effort (a visibility spoof is injected) but
> not guaranteed to credit — prefer the default.

---

## Local run (no Docker)

```bash
pip install -r requirements.txt
# Chrome/Chromium + a matching chromedriver must be installed; or set KDM_CHROMEDRIVER_PATH.
export KDM_CHANNELS="https://kick.com/some-streamer=120"
export KDM_DATA_DIR=.
python runminer.py
```

Cookies go in `./cookies/kick.com.json`. Press Ctrl-C for a clean shutdown.

---

## Credits

Forked from [HyperBeats/KickDropsMiner](https://github.com/HyperBeats/KickDropsMiner);
the headless engine and container tooling are a rewrite. Original licensing from
the upstream project is retained.
