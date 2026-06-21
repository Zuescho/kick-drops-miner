"""Cookie persistence + session_token -> drops bearer.

The miner authenticates with cookies exported from a logged-in browser (the
``session_token`` cookie doubles as the Bearer token for the drops endpoints).
Nothing here raises for an expected failure (missing/unreadable file)."""
import json
import os

from .log import get_logger

log = get_logger("miner.auth")


def cookie_file(cookies_dir: str, domain: str = "kick.com") -> str:
    """Path to the cookie JSON file for a domain (':' sanitized)."""
    safe = domain.replace(":", "_")
    return os.path.join(cookies_dir, f"{safe}.json")


def load_cookies(cookies_dir: str, domain: str = "kick.com") -> list[dict]:
    """Read the cookie json file; [] if missing/unreadable/not a list."""
    path = cookie_file(cookies_dir, domain)
    if not os.path.exists(path):
        log.info("no cookie file at %s", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("failed to read cookies %s: %s", path, e)
        return []
    if not isinstance(data, list):
        log.warning("cookie file %s is not a list; ignoring", path)
        return []
    return [c for c in data if isinstance(c, dict)]


def save_cookies(browser, cookies_dir: str, domain: str = "kick.com") -> None:
    """Persist browser.get_cookies() to the domain file. Best-effort."""
    try:
        cookies = browser.get_cookies()
    except Exception as e:
        log.warning("could not read cookies from browser: %s", e)
        return
    if not cookies:
        return
    try:
        os.makedirs(cookies_dir, exist_ok=True)
        path = cookie_file(cookies_dir, domain)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)
    except Exception as e:
        log.warning("failed to save cookies: %s", e)


def apply_cookies(browser, cookies_dir: str, domain: str = "kick.com") -> bool:
    """Add saved cookies to the browser (must already be on the domain origin).
    Returns True if any cookies were applied."""
    cookies = load_cookies(cookies_dir, domain)
    if not cookies:
        return False
    try:
        browser.add_cookies(cookies)
    except Exception as e:
        log.warning("failed to apply cookies: %s", e)
        return False
    log.info("applied %d cookie(s) for %s", len(cookies), domain)
    return True


def session_bearer(cookies: list[dict]) -> str | None:
    """Return the 'session_token' cookie value (drops Bearer) or None. Prefers a
    kick.com-domain cookie if several share the name."""
    fallback = None
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        if c.get("name") != "session_token":
            continue
        val = c.get("value")
        if not val:
            continue
        if "kick.com" in str(c.get("domain", "")):
            return val
        fallback = fallback or val
    return fallback
