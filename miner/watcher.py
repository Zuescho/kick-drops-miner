"""Watch a single Kick channel and accrue REAL playing wall-clock time.

Unlike the legacy worker (which did ``elapsed += 1`` per loop tick), this
accumulates only the monotonic-clock delta during intervals when the stream is
live, recently confirmed live, AND the ``<video>`` is actually progressing
(``currentTime`` advancing). That last gate is what makes "watched_seconds"
mean "the thing Kick credits as watch time", not merely "the tab was open".

Long-run hardening:
- An API/Cloudflare blackout (live status unknown > 90s) pauses the clock.
- A stalled or autoplay-blocked player pauses the clock and, past a grace
  window, the channel page is reloaded; after too many failed recoveries the
  watch returns OFFLINE so the scheduler rotates on.
- ``stream_quality='160'`` and a Page-Visibility spoof are (re)applied before
  EVERY navigation (incl. recovery reloads and after a driver recreate).
"""
import random
import time
from dataclasses import dataclass


# reasons
COMPLETED = "completed"          # reached target_seconds of real playing time
OFFLINE = "offline"              # went offline past grace / player unrecoverable
WRONG_CATEGORY = "wrong_category"
STOPPED = "stopped"              # stop_event set
ERROR = "error"                  # unrecoverable

# tuning
_POLL_MIN = 10.0                 # base live-poll interval (s), jittered
_POLL_MAX = 15.0
_POLL_BACKOFF_MAX = 120.0        # cap when rate-limited / status unknown
_UNCONFIRMED_LIVE_LIMIT = 90.0   # stop accruing after this long w/o a live confirm
_STALL_LIMIT = 25.0              # video.currentTime frozen this long -> reload
_MAX_RELOADS = 5                 # give up (OFFLINE) after this many failed recoveries
_INIT_WAIT = 5.0                 # let the player initialize after navigation


@dataclass
class WatchResult:
    reason: str                  # one of the constants above
    watched_seconds: int         # REAL playing seconds accrued this call


def _player_js(mute):
    """JS that mutes (optional) + plays the <video> and RETURNS its state so the
    caller can detect a stalled/missing/paused player."""
    muted = "true" if mute else "false"
    volume = "0" if mute else "1"
    return f"""
    try {{
      var v = document.querySelector('video');
      if (!v) return {{hasVideo: false}};
      try {{ v.muted = {muted}; v.volume = {volume}; }} catch(e) {{}}
      if (v.paused) {{ try {{ v.play(); }} catch(e) {{}} }}
      return {{hasVideo: true, currentTime: v.currentTime,
               paused: v.paused, readyState: v.readyState}};
    }} catch(e) {{ return {{hasVideo: false, error: String(e)}}; }}
    """


# Make Kick believe the (headless/Xvfb) tab is focused & visible — some drop
# systems gate crediting on document.visibilityState === 'visible'.
_VISIBILITY_SPOOF_JS = """
try {
  Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
  Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
  document.hasFocus = function () { return true; };
  document.dispatchEvent(new Event('visibilitychange'));
} catch (e) {}
"""


def _path_slug(url):
    """First path segment of a URL ('https://kick.com/foo?x' -> 'foo')."""
    try:
        from urllib.parse import urlparse
        return (urlparse(url).path or "").strip("/").split("/")[0] or None
    except Exception:
        return None


def _navigate(browser, url, force_160p, mute, log=None):
    """Navigate to a channel, re-applying 160p + the visibility spoof first
    (sessionStorage/overrides are per-document and wiped by a driver recreate)."""
    try:
        if force_160p:
            browser.set_session_storage("stream_quality", "160")
        browser.get(url)
    except Exception as exc:
        if log:
            log.warning("navigation to %s failed: %s", url, exc)
    for js in (_VISIBILITY_SPOOF_JS, _player_js(mute)):
        try:
            browser.execute_script(js)
        except Exception:
            pass


def watch_channel(browser, kick, channel_url, *, target_seconds, stop_event,
                  required_category_id=None, offline_grace_checks=2,
                  force_160p=True, mute=True, on_tick=None, log=None):
    """Navigate ``browser`` to channel_url and accrue REAL playing time while the
    channel is live, confirmed live within 90s, AND the video is progressing.
    Polls live/category every ~10-15s (jittered, backing off when blocked);
    after ``offline_grace_checks`` consecutive offline checks returns OFFLINE; on
    a definite category mismatch returns WRONG_CATEGORY; after too many failed
    player recoveries returns OFFLINE. Returns COMPLETED at target_seconds
    (<=0 => watch until offline/stopped). Checks stop_event >=1/s -> STOPPED.
    on_tick(secs, live) called ~1/sec. Never raises -> ERROR."""
    watched = 0.0                 # accumulated real playing seconds
    slug = _path_slug(channel_url) or channel_url

    def _result(reason):
        return WatchResult(reason, int(watched))

    try:
        _navigate(browser, channel_url, force_160p, mute, log)
        _sleep_checking_stop(_INIT_WAIT, stop_event)
        if stop_event.is_set():
            return _result(STOPPED)

        offline_streak = 0
        live = False                      # do NOT assume live; first poll decides
        live_since = None                 # monotonic when current accrual interval began
        last_confirmed_live = time.monotonic()
        last_tick = 0.0
        poll_interval = random.uniform(_POLL_MIN, _POLL_MAX)
        next_poll = time.monotonic()      # poll immediately on first iteration

        # player-progress watchdog state
        last_ct = None
        last_progress_at = time.monotonic()
        playing = False                   # currentTime advanced on the last tick
        reloads = 0

        while True:
            now = time.monotonic()

            if stop_event.is_set():
                if live_since is not None:
                    watched += now - live_since
                    live_since = None
                return _result(STOPPED)

            # --- periodic live / category poll ---
            if now >= next_poll:
                status = kick.is_live(channel_url)
                blocked = bool(getattr(browser, "last_blocked", False))
                if status is True:
                    offline_streak = 0
                    live = True
                    last_confirmed_live = now
                    poll_interval = random.uniform(_POLL_MIN, _POLL_MAX)
                elif status is False:
                    offline_streak += 1
                    live = False
                    poll_interval = random.uniform(_POLL_MIN, _POLL_MAX)
                elif blocked:
                    poll_interval = min(poll_interval * 1.7, _POLL_BACKOFF_MAX)

                # Stop the clock the moment we believe we're offline.
                if not live and live_since is not None:
                    watched += now - live_since
                    live_since = None

                if (not live and offline_grace_checks
                        and offline_streak >= offline_grace_checks):
                    if log:
                        log.info("Channel %s offline (%d checks); stopping watch.",
                                 slug, offline_streak)
                    return _result(OFFLINE)

                if required_category_id and status is True:
                    present = kick.required_category_present(
                        channel_url, required_category_id)
                    if present is False:
                        if log:
                            log.info("Channel %s left required category %s; stopping.",
                                     slug, required_category_id)
                        if live_since is not None:
                            watched += now - live_since
                            live_since = None
                        return _result(WRONG_CATEGORY)

                next_poll = now + poll_interval
                if stop_event.is_set():   # the poll above can take seconds
                    if live_since is not None:
                        watched += now - live_since
                    return _result(STOPPED)

            # --- ~1s cadence: tend the player, then decide accrual ---
            if now - last_tick >= 1.0:
                last_tick = now
                if live:
                    last_ct, last_progress_at, playing, reloaded = _tend_player(
                        browser, channel_url, slug, mute, force_160p,
                        now, last_ct, last_progress_at, log)
                    if reloaded:
                        reloads += 1
                        playing = False
                        if live_since is not None:
                            watched += now - live_since   # don't count the reload gap
                            live_since = None
                        if reloads >= _MAX_RELOADS:
                            if log:
                                log.warning("Channel %s player unrecoverable after %d "
                                            "reloads; rotating.", slug, reloads)
                            return _result(OFFLINE)
                else:
                    playing = False

            # Accrue only while live AND confirmed-recent AND actually playing.
            confirmed_recent = (now - last_confirmed_live) <= _UNCONFIRMED_LIVE_LIMIT
            accrue = live and confirmed_recent and playing
            if accrue and live_since is None:
                live_since = now
            elif not accrue and live_since is not None:
                watched += now - live_since
                live_since = None

            accrued = watched + ((now - live_since) if live_since is not None else 0.0)

            if target_seconds > 0 and accrued >= target_seconds:
                watched = accrued
                if log:
                    log.info("Channel %s reached target %ds.", slug, target_seconds)
                return _result(COMPLETED)

            if on_tick:
                try:
                    on_tick(int(accrued), live)
                except Exception:
                    pass

            _sleep_checking_stop(1.0, stop_event)

    except Exception as exc:
        if log:
            log.warning("watch_channel error for %s: %s", slug, exc)
        return _result(ERROR)


def _tend_player(browser, channel_url, slug, mute, force_160p,
                 now, last_ct, last_progress_at, log):
    """Keep the player muted/playing and check currentTime is advancing. Reloads
    the channel if the driver was recreated (page no longer the channel) or the
    player has been frozen past _STALL_LIMIT. Returns
    (last_ct, last_progress_at, playing, reloaded)."""
    # Driver recreated mid-watch lands on kick.com — compare the PATH SEGMENT
    # (substring matching is fragile) and re-navigate if we drifted off-channel.
    try:
        cur = (browser.driver.current_url or "") if browser.driver else ""
    except Exception:
        cur = ""
    cur_slug = _path_slug(cur)
    if slug and cur_slug and cur_slug != slug:
        if log:
            log.info("player drifted to %s; re-navigating to %s.", cur_slug, slug)
        _navigate(browser, channel_url, force_160p, mute, log)
        return None, now, False, True

    try:
        state = browser.execute_script(_player_js(mute))
    except Exception:
        state = None

    if not isinstance(state, dict) or not state.get("hasVideo"):
        # No usable <video> (still loading / crashed). Reload past the grace.
        if now - last_progress_at > _STALL_LIMIT:
            if log:
                log.warning("no live player on %s; reloading.", slug)
            _navigate(browser, channel_url, force_160p, mute, log)
            return None, now, False, True
        return last_ct, last_progress_at, False, False

    ct = state.get("currentTime")
    playing = False
    if isinstance(ct, (int, float)):
        if last_ct is not None and ct > last_ct + 0.1:
            last_progress_at = now
            playing = True
        last_ct = ct
    if now - last_progress_at > _STALL_LIMIT:
        if log:
            log.warning("player stalled on %s (%.0fs no progress); reloading.",
                        slug, now - last_progress_at)
        _navigate(browser, channel_url, force_160p, mute, log)
        return None, now, False, True
    return last_ct, last_progress_at, playing, False


def _sleep_checking_stop(seconds, stop_event):
    """Sleep up to ``seconds`` but wake immediately if stop_event is set."""
    try:
        stop_event.wait(timeout=seconds)
    except Exception:
        time.sleep(seconds)
