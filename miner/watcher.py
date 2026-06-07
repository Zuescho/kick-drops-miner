"""Watch a single Kick channel and accrue REAL live wall-clock time.

Unlike the legacy worker (which did ``elapsed += 1`` per loop tick), this
accumulates only the monotonic-clock delta during intervals when the stream is
actually live AND recently confirmed. It polls live status infrequently
(jittered, with backoff when rate-limited) to dodge limits, but checks
``stop_event`` and calls ``on_tick`` at ~1s cadence. Never raises: all
exceptions become WatchResult(ERROR, ...).

Long-run hardening:
- Time is only accrued while the last live confirmation is recent (an API
  blackout / Cloudflare challenge stops the clock instead of banking fake time).
- A player watchdog checks ``video.currentTime`` is advancing; a stalled player
  is re-played and, if still stuck, the channel page is reloaded.
- ``stream_quality='160'`` is re-applied before EVERY navigation (incl. the
  recovery reload and any time the driver was recreated and lost the channel).
"""
import random
import time
from dataclasses import dataclass


# reasons
COMPLETED = "completed"          # reached target_seconds
OFFLINE = "offline"              # went offline past grace
WRONG_CATEGORY = "wrong_category"
STOPPED = "stopped"              # stop_event set
ERROR = "error"                  # unrecoverable

# tuning
_POLL_MIN = 10.0                 # base live-poll interval (s), jittered
_POLL_MAX = 15.0
_POLL_BACKOFF_MAX = 120.0        # cap when rate-limited / status unknown
_UNCONFIRMED_LIVE_LIMIT = 90.0   # stop accruing after this long w/o a live confirm
_STALL_LIMIT = 25.0              # video.currentTime frozen this long (live) -> recover
_INIT_WAIT = 5.0                 # let the player initialize after navigation


@dataclass
class WatchResult:
    reason: str                  # one of the constants above
    watched_seconds: int         # REAL live seconds accrued this call


def _player_js(mute):
    """JS that mutes (optional) + plays the <video> and RETURNS its state so the
    caller can detect a stalled/missing player."""
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


def _navigate(browser, url, force_160p, mute, log=None):
    """Navigate to a channel, re-applying 160p first (sessionStorage is
    per-origin and is wiped by a driver recreate, so it must be set every time)."""
    try:
        if force_160p:
            browser.set_session_storage("stream_quality", "160")
        browser.get(url)
    except Exception as exc:
        if log:
            log.warning("navigation to %s failed: %s", url, exc)
    try:
        browser.execute_script(_player_js(mute))
    except Exception:
        pass


def watch_channel(browser, kick, channel_url, *, target_seconds, stop_event,
                  required_category_id=None, offline_grace_checks=2,
                  force_160p=True, mute=True, on_tick=None, log=None):
    """Navigate ``browser`` to channel_url and accrue REAL watched time while the
    channel is live AND recently confirmed live (monotonic clock). Polls live
    status / category every ~10-15s (jittered, backing off when blocked); after
    ``offline_grace_checks`` consecutive offline checks returns OFFLINE; on a
    definite category mismatch returns WRONG_CATEGORY. Returns COMPLETED at
    target_seconds (<=0 => watch until offline/stopped). Checks stop_event >=1/s
    -> STOPPED. on_tick(secs, live) called ~1/sec. Never raises -> ERROR."""
    watched = 0.0           # accumulated real live seconds
    slug = None
    try:
        from .kick import channel_slug_from_url
        slug = channel_slug_from_url(channel_url)
    except Exception:
        pass

    def _result(reason):
        return WatchResult(reason, int(watched))

    try:
        _navigate(browser, channel_url, force_160p, mute, log)

        # Let the page/player initialize (honoring stop).
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
                blocked = getattr(browser, "last_blocked", False)
                if status is True:
                    offline_streak = 0
                    live = True
                    last_confirmed_live = now
                    poll_interval = random.uniform(_POLL_MIN, _POLL_MAX)
                elif status is False:
                    offline_streak += 1
                    if live_since is not None:
                        watched += now - live_since
                        live_since = None
                    live = False
                    poll_interval = random.uniform(_POLL_MIN, _POLL_MAX)
                else:
                    # Unknown (network/parse/rate-limited): keep the prior live
                    # belief but back off so we don't hammer a blocked endpoint.
                    if blocked:
                        poll_interval = min(poll_interval * 1.7, _POLL_BACKOFF_MAX)
                        if log:
                            log.debug("rate-limited polling %s; backing off to %.0fs",
                                      slug or channel_url, poll_interval)

                if (not live and offline_grace_checks
                        and offline_streak >= offline_grace_checks):
                    if log:
                        log.info("Channel %s offline (%d checks); stopping watch.",
                                 slug or channel_url, offline_streak)
                    return _result(OFFLINE)

                # Category enforcement (only when confirmed live; checks all cats).
                if required_category_id and status is True:
                    present = kick.required_category_present(
                        channel_url, required_category_id)
                    if present is False:
                        if log:
                            log.info("Channel %s left required category %s; stopping.",
                                     slug or channel_url, required_category_id)
                        if live_since is not None:
                            watched += now - live_since
                            live_since = None
                        return _result(WRONG_CATEGORY)

                next_poll = now + poll_interval

            # --- decide whether we should be accruing right now ---
            # Only count time while live AND confirmed live recently; an API
            # blackout longer than the limit pauses the clock (no fake time).
            confirmed_recent = (now - last_confirmed_live) <= _UNCONFIRMED_LIVE_LIMIT
            accrue = live and confirmed_recent

            if accrue and live_since is None:
                live_since = now
            elif not accrue and live_since is not None:
                watched += now - live_since
                live_since = None

            accrued = watched + ((now - live_since) if live_since is not None else 0.0)

            if target_seconds > 0 and accrued >= target_seconds:
                watched = accrued
                if log:
                    log.info("Channel %s reached target %ds.",
                             slug or channel_url, target_seconds)
                return _result(COMPLETED)

            # --- ~1s cadence: player health + watchdog + on_tick ---
            if now - last_tick >= 1.0:
                last_tick = now
                if live:
                    self_healed = _tend_player(
                        browser, kick, channel_url, slug, mute, force_160p,
                        now, last_ct, last_progress_at, log)
                    last_ct, last_progress_at, reloaded = self_healed
                    if reloaded:
                        # A reload navigated away/back; reset interval reference so
                        # we don't count the reload gap, and re-confirm shortly.
                        if live_since is not None:
                            watched += now - live_since
                            live_since = None
                if on_tick:
                    try:
                        on_tick(int(accrued), live)
                    except Exception:
                        pass

            _sleep_checking_stop(1.0, stop_event)

    except Exception as exc:
        if log:
            log.warning("watch_channel error for %s: %s", slug or channel_url, exc)
        return _result(ERROR)


def _tend_player(browser, kick, channel_url, slug, mute, force_160p,
                 now, last_ct, last_progress_at, log):
    """Keep the player muted/playing and watch that currentTime advances. If the
    driver was recreated (current page is no longer the channel), or the player
    has been frozen past _STALL_LIMIT, reload the channel. Returns
    (last_ct, last_progress_at, reloaded)."""
    reloaded = False
    # If the driver was recreated mid-watch it lands on kick.com, not the
    # channel — detect via the URL and re-navigate.
    try:
        cur = (browser.driver.current_url or "") if browser.driver else ""
    except Exception:
        cur = ""
    if slug and slug not in cur:
        if log:
            log.info("player not on %s (url=%s); re-navigating.", slug, cur[:80])
        _navigate(browser, channel_url, force_160p, mute, log)
        return None, now, True

    try:
        state = browser.execute_script(_player_js(mute))
    except Exception:
        state = None

    if not isinstance(state, dict) or not state.get("hasVideo"):
        # No <video> yet/anymore; give it _STALL_LIMIT before a reload.
        if now - last_progress_at > _STALL_LIMIT:
            if log:
                log.warning("no live player on %s; reloading.", slug or channel_url)
            _navigate(browser, channel_url, force_160p, mute, log)
            return None, now, True
        return last_ct, last_progress_at, False

    ct = state.get("currentTime")
    if isinstance(ct, (int, float)):
        if last_ct is None or ct > last_ct + 0.1:
            last_progress_at = now            # progress observed
        last_ct = ct
    if now - last_progress_at > _STALL_LIMIT:
        if log:
            log.warning("player stalled on %s (%.0fs no progress); reloading.",
                        slug or channel_url, now - last_progress_at)
        _navigate(browser, channel_url, force_160p, mute, log)
        return None, now, True
    return last_ct, last_progress_at, False


def _sleep_checking_stop(seconds, stop_event):
    """Sleep up to ``seconds`` but wake immediately if stop_event is set."""
    try:
        stop_event.wait(timeout=seconds)
    except Exception:
        time.sleep(seconds)
