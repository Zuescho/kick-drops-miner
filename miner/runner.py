"""Entrypoint: configure logging, load config, install signal handlers, run the
scheduler. ``runminer.py`` at the repo root is a thin shim over main()."""
import signal
import threading

from .config import MinerConfig
from .log import configure_logging, get_logger
from .scheduler import Scheduler


def main() -> int:
    # Configure logging FIRST so even config-load lines honor the level and the
    # noisy-library quieting is in effect before any driver work.
    configure_logging()
    cfg = MinerConfig.load()
    configure_logging(cfg.log_level)
    log = get_logger("miner")

    log.info(
        "KickDropsMiner (headless) starting: %d channel(s), headless=%s, "
        "data_dir=%s, container=%s, auto_campaigns=%s, loop_forever=%s",
        len(cfg.channels), cfg.headless, cfg.data_dir, cfg.container,
        cfg.auto_campaigns, cfg.loop_forever,
    )
    for ch in cfg.channels:
        mins = "indefinite" if ch.minutes <= 0 else f"{ch.minutes}m"
        log.info("  channel %s (%s%s)", ch.url, mins,
                 f", category {ch.category_id}" if ch.category_id else "")

    stop_event = threading.Event()
    scheduler = Scheduler(cfg, log)
    signals_seen = 0

    def _handle_signal(signum, _frame):
        nonlocal signals_seen
        signals_seen += 1
        if signals_seen >= 2:
            # Second SIGINT/SIGTERM: operator is impatient. Raise so the run
            # unwinds at once; run()'s finally still quits Chrome exactly once.
            log.warning("received signal %s again -> forcing exit", signum)
            raise KeyboardInterrupt
        log.info("received signal %s -> shutting down (repeat to force)", signum)
        stop_event.set()
        scheduler.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError, AttributeError):
            # e.g. not in the main thread, or signal unavailable on platform.
            pass

    try:
        scheduler.run(stop_event)
    except KeyboardInterrupt:
        log.info("interrupted")
        scheduler.stop()
    except Exception as e:
        log.exception("fatal: %s", e)
        return 1
    return 0
