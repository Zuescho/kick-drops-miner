"""Watch a single Kick channel and accrue REAL live wall-clock time.

Unlike the legacy worker (which did ``elapsed += 1`` per loop tick), this
accumulates only the monotonic-clock delta during intervals when the stream is
actually live. It polls live status infrequently (jittered) to dodge rate
limits, but checks ``stop_event`` and calls ``on_tick`` at ~1s cadence. Never
raises: all exceptions become WatchResult(ERROR, ...).
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


@dataclass
class WatchResult:
    reason: str                  # one of the constants above
    watched_seconds: int         # REAL live seconds accrued this call


# JS that keeps the <video> muted (optional) and playing.
def _player_state_js(mute):
    muted = "true" if mute else "false"
    volume = "0" if mute else "1"
    return f"""
    (function(){{
      try {{
        var v = document.querySelector('video');
        if (v) {{
          try {{ v.muted = {muted}; v.volume = {volume}; }} catch(e) {{}}
          if (v.paused) {{ try {{ v.play(); }} catch(e) {{}} }}
        }}
      }} catch(e) {{}}
    }})();
    """


def watch_channel(browser, kick, channel_url, *, target_seconds, stop_event,
                  required_category_id=None, offline_grace_checks=2,
                  force_160p=True, mute=True, on_tick=None, log=None):
    """Navigate ``browser`` to channel_url and accrue REAL watched time while
    the channel is live (monotonic clock; count only live intervals). Polls live
    status / category every ~10-15s (jittered); after ``offline_grace_checks``
    consecutive offline checks returns OFFLINE; on a definite category mismatch
    returns WRONG_CATEGORY. Sets stream_quality='160' via session storage BEFORE
    navigating. Returns COMPLETED at target_seconds (<=0 => watch until
    offline/stopped). Checks stop_event >=1/sec -> STOPPED. on_tick(secs, live)
    called ~1/sec. Never raises -> ERROR on any exception."""
    watched = 0.0  # accumulated real live seconds

    def _result(reason):
        return WatchResult(reason, int(watched))

    try:
        browser.ensure_alive()

        if force_160p:
            # Must be set BEFORE navigation to take effect for this stream load.
            browser.set_session_storage("stream_quality", "160")

        browser.get(channel_url)

        # Let the page/player initialize.
        _sleep_checking_stop(5.0, stop_event)
        if stop_event.is_set():
            return _result(STOPPED)

        try:
            browser.execute_script(_player_state_js(mute))
        except Exception:
            pass

        offline_streak = 0
        live = True              # assume live until first poll says otherwise
        last_tick = 0.0          # monotonic of last on_tick / player nudge
        next_poll = time.monotonic()  # poll immediately on first iteration
        live_since = None        # monotonic when current live interval began

        while True:
            now = time.monotonic()

            # Stop check (at least once per second).
            if stop_event.is_set():
                if live_since is not None:
                    watched += now - live_since
                    live_since = None
                return _result(STOPPED)

            # Periodic live/category poll.
            if now >= next_poll:
                status = kick.is_live(channel_url)
                if status is None:
                    # Unknown (network/parse) — keep previous assumption, don't
                    # penalize the offline streak.
                    pass
                else:
                    if status:
                        offline_streak = 0
                        if not live:
                            live = True
                    else:
                        offline_streak += 1
                        if live and live_since is not None:
                            # Close the live interval at poll time.
                            watched += now - live_since
                            live_since = None
                        live = False

                if (not live and offline_grace_checks
                        and offline_streak >= offline_grace_checks):
                    if log:
                        log.info(
                            "Channel %s offline (%d checks); stopping watch.",
                            channel_url, offline_streak,
                        )
                    return _result(OFFLINE)

                # Category enforcement (only meaningful while live).
                if required_category_id and live:
                    cat = kick.current_category_id(channel_url)
                    if cat is not None and cat != required_category_id:
                        if log:
                            log.info(
                                "Channel %s switched category %s -> %s; stopping.",
                                channel_url, required_category_id, cat,
                            )
                        if live_since is not None:
                            watched += now - live_since
                            live_since = None
                        return _result(WRONG_CATEGORY)

                # Schedule next poll with jitter.
                next_poll = now + random.uniform(10.0, 15.0)

            # Track live interval start.
            if live and live_since is None:
                live_since = now

            # Compute accrued seconds including the open interval for callbacks.
            accrued = watched + ((now - live_since) if live_since is not None else 0.0)

            # Completion check.
            if target_seconds > 0 and accrued >= target_seconds:
                watched = accrued
                if log:
                    log.info(
                        "Channel %s reached target %ds.", channel_url, target_seconds
                    )
                return _result(COMPLETED)

            # ~1s cadence work: keep player healthy + on_tick.
            if now - last_tick >= 1.0:
                last_tick = now
                if live:
                    try:
                        browser.execute_script(_player_state_js(mute))
                    except Exception:
                        pass
                if on_tick:
                    try:
                        on_tick(int(accrued), live)
                    except Exception:
                        pass

            _sleep_checking_stop(1.0, stop_event)

    except Exception as exc:
        if log:
            log.warning("watch_channel error for %s: %s", channel_url, exc)
        return _result(ERROR)


def _sleep_checking_stop(seconds, stop_event):
    """Sleep up to ``seconds`` but wake immediately if stop_event is set."""
    try:
        # threading.Event.wait returns True once set; bounded by timeout.
        stop_event.wait(timeout=seconds)
    except Exception:
        time.sleep(seconds)
