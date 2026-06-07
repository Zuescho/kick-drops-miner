"""Scheduler: build one long-lived Browser, log in with cookies, build a work
queue from config (+ campaign-derived live channels), and rotate through it
calling watch_channel(). Survives a single channel/driver failure and keeps
going; clean shutdown via stop_event / stop()."""
import threading
import time
from dataclasses import dataclass

from .auth import apply_cookies, load_cookies, session_bearer
from .browser import Browser
from .kick import KickClient, channel_slug_from_url
from .config import Channel, MinerConfig
from . import watcher
from .watcher import watch_channel

KICK_HOME = "https://kick.com"


@dataclass
class _Item:
    """A queue entry: one channel to watch, plus campaign context for fallback."""
    channel: Channel
    category_id: int | None = None
    campaign_channels: tuple = ()      # tuple[str] of sibling channel urls


class Scheduler:
    def __init__(self, config: MinerConfig, log) -> None:
        self.config = config
        self.log = log
        self._stop = threading.Event()
        self.browser: Browser | None = None
        self.kick: KickClient | None = None
        self.bearer: str | None = None

    def stop(self) -> None:
        """Set the internal stop flag (also honored via the stop_event arg)."""
        self._stop.set()

    def _stopping(self, stop_event) -> bool:
        return self._stop.is_set() or (stop_event is not None and stop_event.is_set())

    def run(self, stop_event) -> None:
        cfg = self.config
        self.browser = Browser(
            headless=cfg.headless,
            chromedriver_path=cfg.chromedriver_path,
            user_data_dir=cfg.chrome_data_dir,
            container=cfg.container,
            log=self.log,
        )
        try:
            self.browser.start()
            self.browser.get(KICK_HOME)

            # Authenticate: apply saved cookies, reload so the session sticks.
            applied = apply_cookies(self.browser, cfg.cookies_dir)
            if applied:
                self.browser.get(KICK_HOME)
            else:
                self.log.warning(
                    "no cookies applied — drops/auth endpoints will be anonymous; "
                    "seed %s with a logged-in session_token",
                    cfg.cookies_dir,
                )

            self.bearer = session_bearer(load_cookies(cfg.cookies_dir))
            if self.bearer:
                self.log.info("authenticated session bearer present")
            else:
                self.log.warning("no session_token cookie -> no drops bearer")

            self.kick = KickClient(self.browser, self.log)

            # Main loop: build queue, drain it, optionally repeat.
            while not self._stopping(stop_event):
                queue = self._build_queue()
                if not queue:
                    self.log.warning(
                        "work queue is empty; sleeping %ds", cfg.poll_offline_seconds
                    )
                    self._sleep(cfg.poll_offline_seconds, stop_event)
                    if not cfg.loop_forever:
                        break
                    continue

                self.log.info("queue has %d channel(s)", len(queue))
                for item in queue:
                    if self._stopping(stop_event):
                        break
                    self._process_item(item, stop_event)

                if self._stopping(stop_event):
                    break
                if not cfg.loop_forever:
                    self.log.info("queue drained; loop_forever disabled -> exiting")
                    break
        except Exception as e:
            self.log.exception("scheduler crashed: %s", e)
        finally:
            try:
                self.browser.quit()
            except Exception:
                pass
            self.log.info("scheduler stopped")

    # --- queue construction ---
    def _build_queue(self) -> list[_Item]:
        items: list[_Item] = []
        for ch in self.config.channels:
            items.append(_Item(channel=ch, category_id=ch.category_id))

        if self.config.auto_campaigns:
            try:
                items.extend(self._campaign_items())
            except Exception as e:
                self.log.warning("auto_campaigns failed: %s", e)
        return items

    def _campaign_items(self) -> list[_Item]:
        """Enqueue one live channel per active campaign (with siblings for
        offline-fallback). Best-effort; returns [] on any failure."""
        out: list[_Item] = []
        campaigns = self.kick.fetch_campaigns(self.bearer) if self.kick else []
        for camp in campaigns or []:
            status = str(camp.get("status", "")).lower()
            if status and status not in ("active", "live", "running"):
                continue
            cat_id = camp.get("category_id")
            chans = camp.get("channels") or []
            urls = [c.get("url") for c in chans if isinstance(c, dict) and c.get("url")]
            if not urls:
                continue
            siblings = tuple(urls)
            picked = self._first_live(urls)
            if not picked:
                # No configured channel live; let live_streamers_by_category pick.
                if cat_id and self.kick:
                    live = self.kick.live_streamers_by_category(cat_id, limit=24)
                    if live:
                        picked = live[0]
                        siblings = tuple(live)
            if not picked:
                continue
            out.append(_Item(
                channel=Channel(url=picked, minutes=0, category_id=cat_id),
                category_id=cat_id,
                campaign_channels=siblings,
            ))
            self.log.info("campaign %r -> watching %s", camp.get("name"), picked)
        return out

    def _first_live(self, urls) -> str | None:
        for url in urls:
            if self.kick and self.kick.is_live(url):
                return url
        return None

    # --- per-item processing ---
    def _process_item(self, item: _Item, stop_event) -> None:
        cfg = self.config
        ch = item.channel
        try:
            self.browser.ensure_alive()
        except Exception as e:
            self.log.warning("ensure_alive failed before %s: %s", ch.url, e)

        target_seconds = max(0, int(ch.minutes) * 60)
        slug = channel_slug_from_url(ch.url) or ch.url
        self.log.info(
            "watching %s for %s",
            slug,
            "indefinitely" if target_seconds <= 0 else f"{ch.minutes}m",
        )

        result = self._watch(ch.url, target_seconds, item.category_id, stop_event)
        if result is None:
            return

        reason = result.reason
        self.log.info(
            "%s -> %s (%ds watched)", slug, reason, result.watched_seconds
        )

        if reason == watcher.STOPPED:
            return
        if reason == watcher.OFFLINE:
            self._handle_offline(item, stop_event)
        # COMPLETED / WRONG_CATEGORY / ERROR -> advance (caller loops on)

    def _watch(self, url, target_seconds, category_id, stop_event):
        """Call watch_channel, converting any unexpected raise into None."""
        try:
            return watch_channel(
                self.browser,
                self.kick,
                url,
                target_seconds=target_seconds,
                stop_event=self._combined_stop(stop_event),
                required_category_id=category_id,
                offline_grace_checks=self.config.offline_grace_checks,
                force_160p=self.config.force_160p,
                mute=self.config.mute,
                log=self.log,
            )
        except Exception as e:
            self.log.warning("watch_channel raised for %s: %s", url, e)
            return None

    def _combined_stop(self, stop_event):
        """A single event watcher can poll that reflects both our stop and the
        external stop_event. Returns an object with .is_set()."""
        if stop_event is None:
            return self._stop
        return _EitherEvent(self._stop, stop_event)

    def _handle_offline(self, item: _Item, stop_event) -> None:
        """On OFFLINE: try a live alternative from the same campaign; else
        from the category; else sleep poll_offline_seconds and advance."""
        cfg = self.config
        alts = [u for u in item.campaign_channels if u != item.channel.url]

        # If we have a category but no/empty siblings, ask Kick for live ones.
        if item.category_id and self.kick:
            try:
                live = self.kick.live_streamers_by_category(item.category_id, limit=24)
                for u in live:
                    if u not in alts and u != item.channel.url:
                        alts.append(u)
            except Exception as e:
                self.log.warning("live_streamers_by_category failed: %s", e)

        for alt in alts:
            if self._stopping(stop_event):
                return
            try:
                if not self.kick or not self.kick.is_live(alt):
                    continue
            except Exception:
                continue
            slug = channel_slug_from_url(alt) or alt
            self.log.info("offline fallback -> %s", slug)
            target_seconds = max(0, int(item.channel.minutes) * 60)
            result = self._watch(alt, target_seconds, item.category_id, stop_event)
            if result is None:
                return
            self.log.info(
                "%s -> %s (%ds watched)", slug, result.reason, result.watched_seconds
            )
            if result.reason in (watcher.STOPPED, watcher.COMPLETED):
                return
            # else (OFFLINE/ERROR/WRONG_CATEGORY) try the next alternative

        self.log.info("no live alternative; sleeping %ds", cfg.poll_offline_seconds)
        self._sleep(cfg.poll_offline_seconds, stop_event)

    # --- utils ---
    def _sleep(self, seconds: float, stop_event) -> None:
        """Sleep in short slices so a stop is honored within ~0.5s."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stopping(stop_event):
                return
            time.sleep(min(0.5, end - time.monotonic()))


class _EitherEvent:
    """Lightweight .is_set() OR of two threading.Events (for watch_channel)."""
    def __init__(self, a, b):
        self._a, self._b = a, b

    def is_set(self) -> bool:
        return self._a.is_set() or self._b.is_set()

    def wait(self, timeout=None) -> bool:
        # watcher uses .is_set(); provide .wait() for completeness.
        if self.is_set():
            return True
        if timeout:
            time.sleep(min(timeout, 0.5))
        return self.is_set()
