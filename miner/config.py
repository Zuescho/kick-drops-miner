"""Miner configuration: env overlay on ``{data_dir}/config.json``.

New miner-specific JSON schema (does NOT reuse the legacy config.json keys).
``MinerConfig.load()`` never raises — it logs and falls back to sane defaults."""
import json
import os
from dataclasses import dataclass, field

from .log import get_logger

log = get_logger("miner.config")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return _truthy(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        log.warning("invalid int for %s=%r; using %d", name, raw, default)
        return default


def _as_int(value, default: int) -> int:
    """Coerce a JSON scalar to int without ever raising (load() must not crash
    on a malformed config.json — that would be a boot crash-loop)."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        log.warning("invalid int %r in config.json; using %d", value, default)
        return default


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
    offline_grace_checks: int = 4      # ~1min grace so a BRB blip isn't abandoned
    loop_forever: bool = True          # after finishing the queue, start over
    poll_offline_seconds: int = 60     # offline & no live alt -> wait this long
    auto_campaigns: bool = False       # also enqueue live channels from campaigns
    log_level: str = "INFO"
    default_minutes: int = 120         # per-channel target when unspecified
    campaign_minutes: int = 0          # rotation slice for auto-campaign items
                                       # (0 => fall back to default_minutes)
    driver_recycle_hours: float = 6.0  # proactively recycle Chrome past this age
    heartbeat_seconds: int = 300       # log "still watching" every N seconds
    progress_log: bool = True          # periodically log drops progress

    @property
    def cookies_dir(self) -> str:
        return os.path.join(self.data_dir, "cookies")

    @property
    def chrome_data_dir(self) -> str:
        return os.path.join(self.data_dir, "chrome_data")

    @classmethod
    def load(cls) -> "MinerConfig":
        """Build config from {data_dir}/config.json overlaid with env vars.
        Never raises; logs and returns sensible defaults on any failure."""
        cfg = cls()

        # --- data_dir / container / driver / log level from env ---
        cfg.data_dir = os.environ.get("KDM_DATA_DIR") or "."
        cfg.log_level = os.environ.get("KDM_LOG_LEVEL", cfg.log_level)
        cfg.container = _env_bool("KDM_CONTAINER", cfg.container)
        cfg.chromedriver_path = os.environ.get("KDM_CHROMEDRIVER_PATH")

        default_minutes = _env_int("KDM_DEFAULT_MINUTES", 120)
        cfg.default_minutes = default_minutes

        # --- JSON file overlay ---
        data: dict = {}
        cfg_path = os.path.join(cfg.data_dir, "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    log.warning("config.json is not a JSON object; ignoring")
            except Exception as e:
                log.warning("failed to read %s: %s; using defaults", cfg_path, e)

        # Scalar settings from JSON (env still wins below for a few of these).
        cfg.headless = bool(data.get("headless", cfg.headless))
        cfg.force_160p = bool(data.get("force_160p", cfg.force_160p))
        cfg.mute = bool(data.get("mute", cfg.mute))
        cfg.offline_grace_checks = _as_int(
            data.get("offline_grace_checks"), cfg.offline_grace_checks
        )
        cfg.loop_forever = bool(data.get("loop_forever", cfg.loop_forever))
        cfg.poll_offline_seconds = _as_int(
            data.get("poll_offline_seconds"), cfg.poll_offline_seconds
        )
        cfg.auto_campaigns = bool(data.get("auto_campaigns", cfg.auto_campaigns))
        cfg.campaign_minutes = _as_int(
            data.get("campaign_minutes"), cfg.campaign_minutes
        )
        cfg.heartbeat_seconds = _as_int(
            data.get("heartbeat_seconds"), cfg.heartbeat_seconds
        )
        cfg.progress_log = bool(data.get("progress_log", cfg.progress_log))
        try:
            cfg.driver_recycle_hours = float(
                data.get("driver_recycle_hours", cfg.driver_recycle_hours)
            )
        except (ValueError, TypeError):
            log.warning("invalid driver_recycle_hours; using %s", cfg.driver_recycle_hours)
        if data.get("chromedriver_path"):
            cfg.chromedriver_path = data.get("chromedriver_path")

        # --- channels from JSON, else from KDM_CHANNELS env ---
        cfg.channels = cls._parse_channels(data.get("channels"), default_minutes)
        if not cfg.channels:
            cfg.channels = cls._channels_from_env(default_minutes)

        # --- env overrides (env wins over JSON for these toggles) ---
        if os.environ.get("KDM_HEADLESS") is not None:
            cfg.headless = _truthy(os.environ.get("KDM_HEADLESS"))
        if os.environ.get("KDM_LOOP_FOREVER") is not None:
            cfg.loop_forever = _truthy(os.environ.get("KDM_LOOP_FOREVER"))
        if os.environ.get("KDM_AUTO_CAMPAIGNS") is not None:
            cfg.auto_campaigns = _truthy(os.environ.get("KDM_AUTO_CAMPAIGNS"))
        if os.environ.get("KDM_FORCE_160P") is not None:
            cfg.force_160p = _truthy(os.environ.get("KDM_FORCE_160P"))
        if os.environ.get("KDM_MUTE") is not None:
            cfg.mute = _truthy(os.environ.get("KDM_MUTE"))
        cfg.poll_offline_seconds = _env_int(
            "KDM_POLL_OFFLINE_SECONDS", cfg.poll_offline_seconds
        )
        cfg.offline_grace_checks = _env_int(
            "KDM_OFFLINE_GRACE_CHECKS", cfg.offline_grace_checks
        )

        return cfg

    # --- helpers ---
    @staticmethod
    def _parse_channels(raw, default_minutes: int) -> list[Channel]:
        """Parse the JSON 'channels' list. Accepts dicts or bare url strings.
        Skips malformed entries; never raises."""
        out: list[Channel] = []
        if not isinstance(raw, list):
            return out
        for entry in raw:
            try:
                if isinstance(entry, str):
                    url = entry.strip()
                    if url:
                        out.append(Channel(url=url, minutes=default_minutes))
                elif isinstance(entry, dict):
                    url = str(entry.get("url", "")).strip()
                    if not url:
                        continue
                    minutes = entry.get("minutes", default_minutes)
                    try:
                        minutes = int(minutes)
                    except (ValueError, TypeError):
                        minutes = default_minutes
                    cat = entry.get("category_id")
                    if cat is not None:
                        try:
                            cat = int(cat)
                        except (ValueError, TypeError):
                            cat = None
                    out.append(Channel(url=url, minutes=minutes, category_id=cat))
            except Exception as e:
                log.warning("skipping malformed channel entry %r: %s", entry, e)
        return out

    @staticmethod
    def _channels_from_env(default_minutes: int) -> list[Channel]:
        """KDM_CHANNELS: comma- or newline-separated 'url' or 'url=minutes'."""
        raw = os.environ.get("KDM_CHANNELS")
        if not raw:
            return []
        out: list[Channel] = []
        parts = [p.strip() for chunk in raw.split("\n") for p in chunk.split(",")]
        for part in parts:
            if not part:
                continue
            url, minutes = part, default_minutes
            if "=" in part:
                url, _, m = part.partition("=")
                url = url.strip()
                try:
                    minutes = int(m.strip())
                except (ValueError, TypeError):
                    minutes = default_minutes
            if url:
                out.append(Channel(url=url, minutes=minutes))
        return out
