# `miner/` — robust headless Kick drops miner

This package is a **ground-up, headless rewrite** of the watching engine. It does
**not** import from or depend on the legacy `core/` or `ui/` packages, and it has
**no GUI** (no Tkinter, no customtkinter, no noVNC requirement). It runs as a
plain long-lived Python process suitable for a Docker container on Unraid:

> read config → log in with saved cookies → watch each configured Kick channel
> for its target minutes → rotate to the next → repeat.

## Why the rewrite (fragility we are fixing)

The legacy app:
- entangles all queue/rotation logic inside a 2879-line Tkinter `App`;
- spawns a **fresh** `undetected_chromedriver` Chrome for *every* campaign fetch
  and *every* watch, all sharing one `chrome_data` profile → Singleton-lock
  conflicts (it `rm`s `Singleton*` on boot to paper over this);
- counts watch time as `elapsed_seconds += 1` per loop iteration (drifts from
  real time);
- has no driver-crash recovery and brittle live detection.

The rewrite fixes these by: **one long-lived browser** reused for both API calls
and watching; **real wall-clock** time accounting; **driver auto-restart**;
clean **SIGTERM** shutdown; structured **stdout logging** (Docker-friendly);
config from **env + JSON**, no GUI.

## Module ownership

| Module | Owner | Responsibility |
|---|---|---|
| `miner/browser.py` | **Engine agent** | `Browser`: one long-lived Chrome, in-page JSON fetch, auto-restart |
| `miner/kick.py` | **Engine agent** | `KickClient`: live status, category, campaigns, progress, livestreams |
| `miner/watcher.py` | **Engine agent** | `watch_channel(...)`: watch one channel w/ real-time accounting |
| `miner/config.py` | **Runtime agent** | `Channel`, `MinerConfig`, `.load()` from env + JSON |
| `miner/auth.py` | **Runtime agent** | cookie load/save, `session_token` → bearer |
| `miner/scheduler.py` | **Runtime agent** | `Scheduler`: build queue, rotate, offline/category fallback |
| `miner/runner.py` | **Runtime agent** | `main()` entrypoint: logging + signal handling |
| `runminer.py` (repo root) | **Runtime agent** | thin `from miner.runner import main` shim |
| `miner/log.py` | **Runtime agent** | `get_logger(name)` stdlib-logging helper |
| Dockerfile / startapp.sh / README.docker.md | **Runtime agent** | run the headless miner instead of the GUI |

Agents own **disjoint files** and may run in parallel. Code strictly against the
signatures below — do not change a shared signature without it being reflected here.

## Hard contract (signatures both sides depend on)

### `miner/log.py` (Runtime)
```python
import logging
def get_logger(name: str) -> logging.Logger: ...
# Configured once in runner.main(): level from env KDM_LOG_LEVEL (default INFO),
# format "%(asctime)s %(levelname)s %(name)s %(message)s" to stdout.
```

### `miner/browser.py` (Engine)
```python
class Browser:
    """Owns ONE long-lived Chrome/Chromium, reused for every API fetch and every
    watch. Persistent user-data-dir keeps Cloudflare clearance. Auto-recreates a
    dead driver. Built on undetected_chromedriver (kept: it bypasses Kick's
    Cloudflare; plain selenium gets blocked)."""

    def __init__(self, *, headless: bool, chromedriver_path: str | None,
                 user_data_dir: str, container: bool, log) -> None: ...

    def start(self) -> None:
        """Create the driver and navigate to https://kick.com (so in-page fetch()
        runs from the kick.com origin). Clears stale Singleton* locks in
        user_data_dir first. Idempotent-ish: safe to call once at startup."""

    def ensure_alive(self) -> None:
        """If the driver is dead/unreachable (probe with driver.title or
        current_url in try/except), quit() and recreate it, then re-navigate to
        kick.com. Callers may invoke before important operations."""

    def get(self, url: str) -> None:
        """Navigate the single tab. Calls ensure_alive() first."""

    def fetch_json(self, url: str, *, bearer: str | None = None,
                   timeout: float = 12.0) -> dict | None:
        """Run an in-page `fetch(url, {credentials:'include'})` from the current
        page origin (must be kick.com), optionally with `Authorization: Bearer`.
        Returns parsed JSON dict, or None on network error / non-JSON / blocked.
        Uses execute_async_script. Never raises."""

    def execute_script(self, script: str, *args): ...
    def execute_async_script(self, script: str, *args): ...

    def add_cookies(self, cookies: list[dict]) -> None:
        """Add each cookie (dropping null 'expiry'); swallow per-cookie errors."""
    def get_cookies(self) -> list[dict]: ...

    def set_session_storage(self, key: str, value: str) -> None:
        """Best-effort sessionStorage.setItem; used for stream_quality='160'."""

    def quit(self) -> None:
        """Quit the driver, swallow errors. Safe to call multiple times."""

    @property
    def driver(self):
        """The underlying selenium driver (or None before start())."""
```

### `miner/kick.py` (Engine)
```python
def channel_slug_from_url(url: str) -> str | None:
    """'https://kick.com/foo' -> 'foo'; non-kick or invalid -> None.
    Also accepts a bare slug 'foo' -> 'foo'."""

class KickClient:
    """Stateless helpers over a Browser. All methods return None / [] on failure,
    never raise. Endpoints (proven against Cloudflare via in-page fetch):
      live/category:  https://kick.com/api/v2/channels/{slug}
      campaigns:      https://web.kick.com/api/v1/drops/campaigns
      progress:       https://web.kick.com/api/v1/drops/progress  (needs bearer)
      livestreams:    https://web.kick.com/api/v1/livestreams?limit={n}&sort=viewer_count_desc&category_id={id}
    Note: web.kick.com endpoints are fetched from a page on the kick.com origin;
    the Browser is on https://kick.com so credentials are sent."""

    def __init__(self, browser: "Browser", log) -> None: ...

    def is_live(self, channel: str) -> bool | None:
        """True/False if known, None if unknown (network/parse error). channel may
        be a slug or full url. Reads livestream.is_live from v2 channels."""

    def channel_info(self, channel: str) -> dict | None:
        """Raw v2 channels payload (dict) or None."""

    def current_category_id(self, channel: str) -> int | None:
        """livestream.categories[0].id when live, else None."""

    def fetch_campaigns(self, bearer: str | None = None) -> list[dict]:
        """Normalized campaigns. Each: {id, name, game, game_slug, game_image,
        status, starts_at, ends_at, rewards, category_id, channels:[{slug,url}]}.
        category_id is best-effort (campaign.category.id if present)."""

    def fetch_progress(self, bearer: str | None = None) -> list[dict]:
        """Raw progress 'data' list (dicts). [] on failure."""

    def live_streamers_by_category(self, category_id, limit: int = 24) -> list[str]:
        """Channel URLs ('https://kick.com/{slug}') currently live in a category."""
```

### `miner/watcher.py` (Engine)
```python
from dataclasses import dataclass

# reasons:
COMPLETED = "completed"        # reached target_seconds
OFFLINE = "offline"            # went offline past grace
WRONG_CATEGORY = "wrong_category"
STOPPED = "stopped"            # stop_event set
ERROR = "error"               # unrecoverable

@dataclass
class WatchResult:
    reason: str                # one of the constants above
    watched_seconds: int       # REAL live seconds accrued this call

def watch_channel(browser: "Browser", kick: "KickClient", channel_url: str, *,
                  target_seconds: int, stop_event, required_category_id=None,
                  offline_grace_checks: int = 2, force_160p: bool = True,
                  mute: bool = True, on_tick=None, log=None) -> WatchResult:
    """Navigate `browser` to channel_url and accrue REAL watched time while the
    channel is live (use a monotonic clock; only count intervals during which the
    stream is live, NOT loop ticks). Periodically (~every 10-15s, jittered):
      - refresh live status via kick.is_live(); after `offline_grace_checks`
        consecutive offline checks -> return WatchResult(OFFLINE, ...);
      - if required_category_id set, check kick.current_category_id(); on a
        definite mismatch -> return WatchResult(WRONG_CATEGORY, ...);
      - keep the <video> muted (if mute) and playing.
    Return COMPLETED when accrued live seconds >= target_seconds (target_seconds<=0
    means watch indefinitely until offline/stopped). Check stop_event frequently
    (at least once per second) -> STOPPED. Set force_160p via
    browser.set_session_storage('stream_quality','160') BEFORE navigation.
    on_tick(watched_seconds:int, live:bool) is called ~once/sec if provided.
    Never raise; convert exceptions to WatchResult(ERROR, ...)."""
```

### `miner/config.py` (Runtime)
```python
from dataclasses import dataclass, field

@dataclass
class Channel:
    url: str
    minutes: int                       # 0 => watch indefinitely
    category_id: int | None = None     # optional required category

@dataclass
class MinerConfig:
    channels: list[Channel] = field(default_factory=list)
    headless: bool = False             # default False: headful under Xvfb in Docker
    chromedriver_path: str | None = None
    data_dir: str = "."                # holds config.json, cookies/, chrome_data/
    container: bool = False
    force_160p: bool = True
    mute: bool = True
    offline_grace_checks: int = 2
    loop_forever: bool = True          # after finishing the queue, start over
    poll_offline_seconds: int = 60     # when a channel is offline & no live alt, wait this long
    auto_campaigns: bool = False       # if True, also enqueue live channels from active campaigns
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> "MinerConfig":
        """Build config from {data_dir}/config.json overlaid with env vars.
        data_dir from env KDM_DATA_DIR (default '.'). chromedriver_path falls back
        to env KDM_CHROMEDRIVER_PATH. container from env KDM_CONTAINER (truthy).
        headless from env KDM_HEADLESS. log_level from KDM_LOG_LEVEL.
        JSON schema (new, miner-specific — does NOT reuse legacy config.json keys):
          { "channels": [ {"url": "...", "minutes": 120, "category_id": null}, ... ],
            "headless": false, "force_160p": true, "mute": true,
            "offline_grace_checks": 2, "loop_forever": true,
            "poll_offline_seconds": 60, "auto_campaigns": false }
        A bare string channel entry ('https://kick.com/foo') is allowed and means
        minutes = default_minutes (env KDM_DEFAULT_MINUTES or 120).
        Missing/!exists config.json => channels from env KDM_CHANNELS (comma or
        newline separated 'url' or 'url=minutes'). Never raises; logs and returns
        sensible defaults."""

    @property
    def cookies_dir(self) -> str: ...   # {data_dir}/cookies
    @property
    def chrome_data_dir(self) -> str: ... # {data_dir}/chrome_data
```

### `miner/auth.py` (Runtime)
```python
def cookie_file(cookies_dir: str, domain: str = "kick.com") -> str: ...

def load_cookies(cookies_dir: str, domain: str = "kick.com") -> list[dict]:
    """Read cookie json file; [] if missing/unreadable."""

def save_cookies(browser: "Browser", cookies_dir: str, domain: str = "kick.com") -> None:
    """Persist browser.get_cookies() to the domain file."""

def apply_cookies(browser: "Browser", cookies_dir: str, domain: str = "kick.com") -> bool:
    """browser must already be on the domain origin. add_cookies(load_cookies()),
    return True if any cookies were applied."""

def session_bearer(cookies: list[dict]) -> str | None:
    """Return the 'session_token' cookie value (bearer for drops endpoints) or None."""
```

### `miner/scheduler.py` (Runtime)
```python
class Scheduler:
    def __init__(self, config: "MinerConfig", log) -> None: ...

    def run(self, stop_event) -> None:
        """1. Browser(...).start(); navigate kick.com; apply_cookies; reload so
              the session is authenticated; compute bearer = session_bearer(...).
           2. Build the work queue from config.channels (+ campaign-derived live
              channels if auto_campaigns).
           3. For each item: call watch_channel(...) for target minutes.
              - OFFLINE: try a live alternative from the same campaign (if any
                via KickClient); else sleep poll_offline_seconds and move on.
              - WRONG_CATEGORY / COMPLETED / ERROR: move to next.
              - STOPPED: clean up and return.
           4. If loop_forever, restart the queue; else return when drained.
           Reuse the SAME Browser instance for the whole run (call ensure_alive()
           between items). Always Browser.quit() on exit."""

    def stop(self) -> None:
        """Set the internal stop flag (also honored via the stop_event arg)."""
```

### `miner/runner.py` (Runtime)
```python
def main() -> int:
    """Configure logging (get_logger), load MinerConfig, install SIGINT/SIGTERM
    handlers that set a threading.Event, construct Scheduler, call run(event).
    Return process exit code. runminer.py at repo root is just:
        from miner.runner import main
        import sys; sys.exit(main())"""
```

## Integration / quality bar
- Pure stdlib + selenium + undetected_chromedriver. No new heavy deps.
- Every public method documented above must exist with that exact name/signature.
- No function may raise across a module boundary for an *expected* failure
  (network, offline, missing cookies) — log and return None/[]/ERROR instead.
- Engine code must `import` without a browser present (no Chrome on the dev box),
  i.e. `python -c "import miner.browser, miner.kick, miner.watcher"` must succeed.
- Likewise `python -c "import miner.config, miner.auth, miner.scheduler, miner.runner"`.
- Keep functions readable and match the surrounding terse style.
