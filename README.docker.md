# KickDropsMiner — Docker (Unraid-ready)

A Dockerized build of [KickDropsMiner](https://github.com/HyperBeats/KickDropsMiner)
that runs headless on a server and exposes its desktop GUI through your **browser**
— no VNC client needed. Built for self-hosting on **Unraid**, Synology, or any Docker host.

It follows the same approach as the
[Dockerized Twitch Drops Miner](https://github.com/fireph/docker-twitch-drops-miner):
the desktop app runs inside [`jlesage/baseimage-gui`](https://github.com/jlesage/docker-baseimage-gui)
(Xvfb + VNC + **noVNC web UI on port 5800**).

> **How it differs from Twitch Drops Miner:** Twitch's miner talks to an API and needs no
> browser (its image is ~30 MB). KickDropsMiner watches Kick.com streams by driving a **real
> Chromium browser** via Selenium / `undetected-chromedriver`, so this image bundles Chromium
> and is correspondingly larger.

---

## Quick start

### docker compose (recommended)

```bash
git clone <this-repo> kickdropsminer
cd kickdropsminer
docker compose up -d --build
```

Then open **http://<host-ip>:5800**.

### docker run

```bash
docker build -t kickdropsminer:local .

docker run -d \
  --name kickdropsminer \
  -p 5800:5800 \
  -v "$PWD/config:/config" \
  -e USER_ID=1000 \
  -e GROUP_ID=1000 \
  -e TZ=Europe/Berlin \
  --shm-size=1g \
  --restart unless-stopped \
  kickdropsminer:local
```

---

## First-run / usage

1. Open `http://<host>:5800` — the KickDropsMiner GUI appears in the web desktop.
2. **Sign in to Kick:** use the app's cookie/login workflow. Cookies are stored under
   `/config/cookies/` and reused on restart.
3. Add live Kick stream URLs with minute targets to the queue and start mining. A Chromium
   window opens inside the desktop and the watch timer increments.
4. Everything persists across restarts because config, cookies and the Chrome profile all
   live in the mounted `/config` volume.

---

## Volumes

| Container path | Purpose                                            | Required |
|----------------|----------------------------------------------------|----------|
| `/config`      | `config.json`, `cookies/`, `chrome_data/` (profile) | ✅ Yes   |

Make sure the host folder is owned by the `USER_ID`/`GROUP_ID` you pass (default `1000:1000`),
or `chmod -R 777` it if you don't care about ownership.

## Key environment variables

| Variable          | Default          | Description                                   |
|-------------------|------------------|-----------------------------------------------|
| `USER_ID`         | `1000`           | uid that owns `/config` on the host           |
| `GROUP_ID`        | `1000`           | gid for file permissions                      |
| `TZ`              | `UTC`            | Container timezone                            |
| `DISPLAY_WIDTH`   | `1600`           | Web desktop width                             |
| `DISPLAY_HEIGHT`  | `900`            | Web desktop height                            |
| `DARK_MODE`       | `0`              | Dark mode for the noVNC shell (`1` to enable) |
| `KDM_DATA_DIR`    | `/config`        | Where the app stores its data (leave as is)   |
| `KDM_CHROMEDRIVER_PATH` | `/usr/bin/chromedriver` | System driver to use (leave as is)   |

Full list of base-image options (web auth, TLS, etc.):
<https://github.com/jlesage/docker-baseimage-gui#environment-variables>

---

## Unraid

A Community-Apps-style template is provided at [`unraid/kickdropsminer.xml`](unraid/kickdropsminer.xml).
After building/publishing the image, import the template (or point it at your registry image),
set the `/config` path, and Unraid will expose the **WebUI** button → `http://[IP]:5800`.

---

## Caveats

- **Bot detection:** Kick is fronted by Cloudflare. A browser running in a datacenter/container
  can occasionally be challenged. This image runs Chromium **non-headless inside Xvfb** (the
  default `hide_player=false`), which is the most resistant configuration, but success isn't
  guaranteed in every network environment.
- **Restart periodically:** like the Twitch image, a daily restart (`--restart unless-stopped`
  plus an external scheduler, or Unraid's restart) helps recover from transient errors.
- **amd64 only:** the image targets x86_64 (Unraid). arm64 is not built.
- **Audio:** there's no audio device in the container; `mute` defaults to `true`.

## What was changed from upstream

This repo vendors the upstream source plus three small Linux/Docker patches:

- `utils/helpers.py` — honors `KDM_DATA_DIR` so data lives on the `/config` volume.
- `core/config.py` — falls back to `KDM_CHROMEDRIVER_PATH` for an offline, version-matched driver.
- `config.json` — `mute` defaults to `true`.

Windows-only artifacts (`chromedriver-win64/`, `run.bat`) are omitted from the image.
