# KickDropsMiner — Headless miner

A **ground-up, headless rewrite** of the watching engine (package `miner/`). No
GUI, no noVNC, no Tkinter — just a long-lived Python process that logs to stdout,
ideal for a Docker container on **Unraid** / any Docker host.

> read config → log in with saved cookies → watch each configured Kick channel
> for its target minutes → rotate to the next → repeat.

It reuses **one** long-lived Chromium (via `undetected-chromedriver`, which
bypasses Kick's Cloudflare) for both API calls and watching, accounts for **real
wall-clock** watch time, auto-restarts a dead driver, and shuts down cleanly on
SIGTERM.

This runs alongside the legacy GUI image — the GUI `Dockerfile` is unchanged; the
headless miner has its own `Dockerfile.miner`.

---

## Quick start (Docker)

```bash
docker build -f Dockerfile.miner -t kickdropsminer-headless .

docker run -d \
  --name kdm-miner \
  --shm-size=1g \
  -v /path/to/config:/config \
  -e KDM_CHANNELS="https://kick.com/some-streamer=120, https://kick.com/another=60" \
  -e TZ=Europe/Berlin \
  kickdropsminer-headless
```

Watch the logs:

```bash
docker logs -f kdm-miner
```

> `--shm-size=1g` keeps Chromium from crashing on the default 64 MB `/dev/shm`.

You **must** seed a logged-in cookie file before drops will be credited — see
[Seeding cookies](#seeding-cookies).

---

## Configuration

Config is `{KDM_DATA_DIR}/config.json` (i.e. `/config/config.json` in Docker),
**overlaid with environment variables**. On first run an example is copied from
`config.miner.example.json`. If `config.json` is missing, channels are read from
the `KDM_CHANNELS` env var instead. Loading **never crashes** — bad values are
logged and defaults are used.

### `config.json` schema (new — does NOT reuse the legacy GUI keys)

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
  "offline_grace_checks": 2,
  "loop_forever": true,
  "poll_offline_seconds": 60,
  "auto_campaigns": false
}
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `channels` | list | `[]` | Channels to watch. Each is `{url, minutes, category_id?}` or a bare URL string. `minutes: 0` = watch indefinitely. |
| `headless` | bool | `false` | `false` = headful Chrome under Xvfb (most robust vs. Cloudflare). |
| `force_160p` | bool | `true` | Force lowest stream quality (160p) to save bandwidth/CPU. |
| `mute` | bool | `true` | Keep the `<video>` muted. |
| `offline_grace_checks` | int | `2` | Consecutive offline checks before a channel is considered offline. |
| `loop_forever` | bool | `true` | After the queue drains, start over. |
| `poll_offline_seconds` | int | `60` | When a channel is offline and no live alternative exists, wait this long. |
| `auto_campaigns` | bool | `false` | Also enqueue a live channel from each active drop campaign. |

A bare-string channel entry uses `KDM_DEFAULT_MINUTES` (default `120`).

### Environment variables

| Env var | Effect |
|---|---|
| `KDM_CHANNELS` | Channels when no `config.json`: comma- **or** newline-separated `url` or `url=minutes`, e.g. `https://kick.com/a=120, https://kick.com/b`. |
| `KDM_DATA_DIR` | Data dir holding `config.json`, `cookies/`, `chrome_data/`. Docker default `/config`. |
| `KDM_HEADLESS` | Truthy → true-headless Chrome (no Xvfb). Default headful under Xvfb in the image. Overrides `headless` in JSON. |
| `KDM_DEFAULT_MINUTES` | Minutes for channels with no explicit minutes. Default `120`. |
| `KDM_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / … Default `INFO`. |
| `KDM_CHROMEDRIVER_PATH` | Path to chromedriver. Set by the image to `/usr/local/bin/chromedriver`. |
| `KDM_CONTAINER` | Truthy → container Chrome flags (GPU disable, etc.). Set to `1` by the image. |
| `KDM_LOOP_FOREVER` | Override `loop_forever`. |
| `KDM_AUTO_CAMPAIGNS` | Override `auto_campaigns`. |
| `KDM_FORCE_160P` / `KDM_MUTE` | Override `force_160p` / `mute`. |
| `KDM_POLL_OFFLINE_SECONDS` / `KDM_OFFLINE_GRACE_CHECKS` | Override the matching JSON keys. |

Env wins over JSON for the toggle vars listed as "override" above.

---

## Seeding cookies

The miner authenticates entirely from saved cookies — it does **not** log in
interactively. It needs a Kick cookie file at:

```
{KDM_DATA_DIR}/cookies/kick.com.json   (e.g. /config/cookies/kick.com.json)
```

The file is a **JSON array** of cookie objects (Selenium / `driver.get_cookies()`
format), and **must include a logged-in `session_token` cookie** — that value
also becomes the `Authorization: Bearer` token for the drops endpoints. Example
shape:

```json
[
  { "name": "session_token", "value": "<your session token>", "domain": ".kick.com", "path": "/" },
  { "name": "kick_session", "value": "...", "domain": ".kick.com", "path": "/" }
]
```

How to obtain it:

- **From the legacy GUI app:** sign in via its "Sign in" flow; it writes the same
  `cookies/kick.com.json` into its data dir — copy that file into the miner's
  `/config/cookies/`.
- **From your browser:** log into kick.com, then export your kick.com cookies with
  a cookie-export extension (e.g. "Cookie-Editor" → Export → JSON). Ensure the
  `session_token` cookie is present, and save the JSON array as
  `kick.com.json` under `/config/cookies/`.

If no cookies are present the miner still runs but watches anonymously and drops
won't be credited; a warning is logged.

---

## Running under Unraid

- Add the container from the headless `Dockerfile.miner` image.
- Map the **`/config`** path to e.g. `/mnt/user/appdata/kickdropsminer-headless`
  (holds `config.json`, `cookies/`, `chrome_data/`).
- Set **`KDM_CHANNELS`** (or place a `config.json` in the mapped dir).
- Add the extra parameter **`--shm-size=1g`**.
- Drop your `kick.com.json` into `<appdata>/cookies/` before/after first start.
- There is **no web UI** — monitor it via the container log.

---

## Local run (no Docker)

```bash
pip install -r requirements.miner.txt
# Windows: chromedriver auto-resolved by undetected-chromedriver, or set KDM_CHROMEDRIVER_PATH
set KDM_CHANNELS=https://kick.com/some-streamer=120
set KDM_DATA_DIR=.
python runminer.py
```

Cookies go in `./cookies/kick.com.json`. Press Ctrl-C for a clean shutdown.
