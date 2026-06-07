"""Scheduler: build one long-lived Browser, log in with cookies, build a work
queue from config (+ campaign-derived live channels), and rotate through it
calling watch_channel(). Survives a single channel/driver failure and keeps
going; clean shutdown via stop_event / stop()."""
import threading
import time
from dataclasses import dataclass

from .auth import apply_cookies, load_cookies, save_cookies, session_bearer
from .browser import Browser
from .kick import KickClient, channel_slug_from_url
from .config import Channel, MinerConfig
from . import watcher
from .watcher import watch_channel

KICK_HOME = "https://kick.com"

_CAMPAIGNS_TTL = 300.0      # cache campaigns this long (avoid hammering the API)
_SESSION_TTL = 1800.0       # re-read cookies / bearer every 30 min
_PROGRESS_TTL = 1800.0      # log drops progress every 30 min
_BACKOFF_CAP = 600.0        # max idle-cycle sleep


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
        # long-run state
        self._campaigns_cache: list | None = None
        self._campaigns_at = 0.0
        self._last_session_refresh = 0.0
        self._last_progress_log = 0.0
        self._error_counts: dict[str, int] = {}
        self._idle_cycles = 0
        self._cycle_productive = False

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

            self.kick = KickClient(self.browser, self.log)
            # Prefer the LIVE browser cookie jar (reflects any silent re-auth)
            # and persist it so a restart survives a rotated token.
            self._refresh_session(stop_event, initial=True)

            # Main loop: build queue, drain it, optionally repeat.
            while not self._stopping(stop_event):
                self._refresh_session(stop_event)          # honors _SESSION_TTL
                self._log_progress()                       # honors _PROGRESS_TTL
                # Recycle Chrome between cycles to cap renderer-memory growth.
                try:
                    self.browser.maybe_recycle(cfg.driver_recycle_hours * 3600)
                except Exception as e:
                    self.log.debug("maybe_recycle failed: %s", e)

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
                self._cycle_productive = False
                for item in queue:
                    if self._stopping(stop_event):
                        break
                    self._process_item(item, stop_event)

                if self._stopping(stop_event):
                    break
                if not cfg.loop_forever:
                    self.log.info("queue drained; loop_forever disabled -> exiting")
                    break
                # If a whole cycle did no productive watching (everything offline),
                # back off exponentially instead of re-enumerating every 60s.
                if not self._cycle_productive:
                    self._idle_cycles += 1
                    backoff = min(
                        cfg.poll_offline_seconds * (2 ** min(self._idle_cycles, 4)),
                        _BACKOFF_CAP,
                    )
                    self.log.info("idle cycle %d; sleeping %ds before rebuild",
                                  self._idle_cycles, int(backoff))
                    self._sleep(backoff, stop_event)
                else:
                    self._idle_cycles = 0
        except Exception as e:
            self.log.exception("scheduler crashed: %s", e)
        finally:
            try:
                self.browser.quit()
            except Exception:
                pass
            self.log.info("scheduler stopped")

    # --- session / progress maintenance (also called mid-watch via on_tick) ---
    def _refresh_session(self, stop_event, *, initial=False) -> None:
        """Re-read cookies from the LIVE browser, recompute the drops bearer, and
        persist them if changed so a restart survives a rotated session_token.
        Throttled to _SESSION_TTL unless initial. Never raises."""
        now = time.monotonic()
        if not initial and now - self._last_session_refresh < _SESSION_TTL:
            return
        self._last_session_refresh = now
        try:
            live = self.browser.get_cookies() if self.browser else []
        except Exception:
            live = []
        bearer = session_bearer(live) or session_bearer(
            load_cookies(self.config.cookies_dir))
        changed = bearer != self.bearer
        self.bearer = bearer
        if bearer:
            if initial:
                self.log.info("authenticated session bearer present")
            elif changed:
                self.log.info("session bearer refreshed")
        else:
            self.log.warning(
                "no session_token -> drops endpoints anonymous; re-seed %s",
                self.config.cookies_dir)
        if live and (initial or changed):
            try:
                save_cookies(self.browser, self.config.cookies_dir)
            except Exception as e:
                self.log.debug("save_cookies failed: %s", e)

    def _log_progress(self) -> None:
        """Periodically log drops progress so the operator can SEE drops are
        crediting (and catch a stale bearer). Throttled to _PROGRESS_TTL."""
        if not self.config.progress_log or not self.kick:
            return
        now = time.monotonic()
        if now - self._last_progress_log < _PROGRESS_TTL:
            return
        self._last_progress_log = now
        try:
            rows = self.kick.fetch_progress(self.bearer)
        except Exception:
            rows = []
        if not rows:
            self.log.info("drops progress: none reported "
                          "(no active campaign progress, or bearer stale)")
            return
        for row in rows[:8]:
            if not isinstance(row, dict):
                continue
            name = row.get("name") or row.get("id")
            units = row.get("progress_units") or row.get("progress") or 0
            status = row.get("status", "?")
            self.log.info("drops progress: %s -> %s (%s)", name, units, status)

    def _make_on_tick(self, slug, stop_event):
        """Build the watch heartbeat callback. Logs liveness every
        heartbeat_seconds and runs session/progress maintenance on their TTLs so
        a multi-hour indefinite watch is neither silent nor session-stale."""
        state = {"beat": time.monotonic()}

        def cb(accrued, live):
            now = time.monotonic()
            if now - state["beat"] >= self.config.heartbeat_seconds:
                state["beat"] = now
                self.log.info("still watching %s: %dm live%s", slug, accrued // 60,
                              "" if live else " (currently offline)")
                # Safe mid-watch: neither navigates the player away.
                self._refresh_session(stop_event)
                self._log_progress()

        return cb

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

    def _cached_campaigns(self) -> list:
        """fetch_campaigns with a short TTL cache so repeated all-offline cycles
        don't re-enumerate the campaign API every minute."""
        now = time.monotonic()
        if (self._campaigns_cache is not None
                and now - self._campaigns_at < _CAMPAIGNS_TTL):
            return self._campaigns_cache
        camps = self.kick.fetch_campaigns(self.bearer) if self.kick else []
        self._campaigns_cache = camps
        self._campaigns_at = now
        return camps

    def _campaign_items(self) -> list[_Item]:
        """Enqueue one live channel per active campaign (with siblings for
        offline-fallback). Best-effort; returns [] on any failure."""
        out: list[_Item] = []
        # Bounded rotation slice so one 24/7 streamer can't pin the whole queue.
        slice_min = self.config.campaign_minutes or self.config.default_minutes
        campaigns = self._cached_campaigns()
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
                channel=Channel(url=picked, minutes=slice_min, category_id=cat_id),
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
        ch = item.channel
        try:
            self.browser.maybe_recycle(self.config.driver_recycle_hours * 3600)
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

        result = self._watch(ch.url, target_seconds, item.category_id, stop_event, slug)
        if result is None:
            return

        reason = result.reason
        self.log.info(
            "%s -> %s (%ds watched)", slug, reason, result.watched_seconds
        )
        self._note_result(ch.url, result, slug)

        if reason == watcher.STOPPED:
            return
        if reason == watcher.OFFLINE:
            self._handle_offline(item, stop_event)
        # COMPLETED / WRONG_CATEGORY / ERROR -> advance (caller loops on)

    def _note_result(self, url, result, slug) -> None:
        """Mark the cycle productive if any time was watched, and escalate a
        channel that keeps erroring (so a permanently-broken URL is visible)."""
        if result.watched_seconds > 0 or result.reason == watcher.COMPLETED:
            self._cycle_productive = True
        if result.reason == watcher.ERROR:
            n = self._error_counts.get(url, 0) + 1
            self._error_counts[url] = n
            if n >= 3:
                self.log.warning(
                    "%s has errored %d times in a row; check the URL/auth", slug, n)
        else:
            self._error_counts.pop(url, None)

    def _watch(self, url, target_seconds, category_id, stop_event, slug=None):
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
                on_tick=self._make_on_tick(slug or url, stop_event),
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
            result = self._watch(alt, target_seconds, item.category_id, stop_event, slug)
            if result is None:
                return
            self.log.info(
                "%s -> %s (%ds watched)", slug, result.reason, result.watched_seconds
            )
            self._note_result(alt, result, slug)
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
        # Honor the FULL timeout (the watcher relies on this for its ~1s loop
        # cadence) while still waking promptly when either event is set.
        end = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if end is not None:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self._a.wait(min(0.2, remaining))
            else:
                self._a.wait(0.2)
        return self.is_set()
