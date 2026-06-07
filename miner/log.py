"""Structured stdout logging for the headless miner (Docker-friendly).

stdlib logging only. Level comes from env ``KDM_LOG_LEVEL`` (default INFO).
Configured once from runner.main(); get_logger(name) is safe to call anywhere."""
import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_configured = False


def configure_logging(level: str | None = None) -> None:
    """Install a single stdout handler on the root logger. Idempotent."""
    global _configured
    if level is None:
        level = os.environ.get("KDM_LOG_LEVEL", "INFO")
    lvl = logging.getLevelName(str(level).upper())
    if not isinstance(lvl, int):
        lvl = logging.INFO

    root = logging.getLogger()
    root.setLevel(lvl)
    if not _configured:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
        _configured = True
    else:
        for h in root.handlers:
            h.setLevel(lvl)

    # Quiet chatty plumbing: undetected_chromedriver/selenium talk to chromedriver
    # over urllib3, which logs WARNING "Retrying ... Read timed out" spam on every
    # slow command. A genuinely dead driver is already surfaced by the Browser's
    # own WARNING, so keep these at ERROR (set KDM_QUIET_LIBS=0 to opt out).
    if str(os.environ.get("KDM_QUIET_LIBS", "1")).lower() not in ("0", "false", "no"):
        for noisy in ("urllib3", "urllib3.connectionpool", "selenium",
                      "undetected_chromedriver", "websockets"):
            logging.getLogger(noisy).setLevel(logging.ERROR)


def get_logger(name: str) -> logging.Logger:
    """Return a logger; ensure logging is configured at least once."""
    if not _configured:
        configure_logging()
    return logging.getLogger(name)
