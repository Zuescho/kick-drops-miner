"""One long-lived Chrome/Chromium for the headless miner.

The ``Browser`` owns a single ``undetected_chromedriver`` instance with a
PERSISTENT user-data-dir (keeps Cloudflare clearance) and reuses it for every
API fetch AND for watching. It auto-recreates a dead driver and never spawns a
new Chrome per task.

Heavy imports (selenium / undetected_chromedriver) are deferred into the
methods that need them so this module imports cleanly on a box without a
browser installed.
"""
import os
import re
import shutil
import subprocess


# ---------------------------------------------------------------------------
# Chrome detection (ported from core/browser.py, proven on Windows + Linux)
# ---------------------------------------------------------------------------

def _chrome_executable_candidates():
    """Yield likely Chrome/Chromium executables in preference order."""
    seen = set()
    candidates = []

    if os.name == "nt":
        for env_name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
                )

    for command in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(command)
        if path:
            candidates.append(path)

    for path in candidates:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.exists(path):
            yield path


def _parse_major_version(version_text):
    match = re.search(r"(\d+)\.", version_text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _chrome_version_from_registry():
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None

    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Google\Chrome\BLBeacon"),
    )
    for root, key_name in keys:
        try:
            with winreg.OpenKey(root, key_name) as key:
                version, _ = winreg.QueryValueEx(key, "version")
                major = _parse_major_version(str(version))
                if major:
                    return major
        except Exception:
            continue
    return None


def _chrome_version_from_executable(path):
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
    except Exception:
        return None
    return _parse_major_version((proc.stdout or "") + " " + (proc.stderr or ""))


def _detect_chrome():
    """Return (major_version, executable_path) for installed Chrome when possible."""
    executable = next(_chrome_executable_candidates(), None)

    if os.name == "nt":
        major = _chrome_version_from_registry()
        if major:
            return major, executable

    for path in ([executable] if executable else []):
        major = _chrome_version_from_executable(path)
        if major:
            return major, path

    return None, executable


def _writable_chromedriver_path(driver_path, log=None):
    """undetected_chromedriver patches the chromedriver binary IN PLACE. If the
    configured path is read-only (e.g. baked into a container image), copy it to
    a writable temp location and return that. Returns the path to use, or the
    original on any failure."""
    if not driver_path or not os.path.isfile(driver_path):
        return driver_path
    try:
        if os.access(driver_path, os.W_OK) and os.access(
            os.path.dirname(driver_path) or ".", os.W_OK
        ):
            return driver_path
        import tempfile
        dst = os.path.join(
            tempfile.gettempdir(), "kdm_" + os.path.basename(driver_path)
        )
        shutil.copy2(driver_path, dst)
        try:
            os.chmod(dst, 0o755)
        except Exception:
            pass
        if log:
            log.info("Patched a writable chromedriver copy at %s", dst)
        return dst
    except Exception as exc:
        if log:
            log.warning("Could not create writable chromedriver copy: %s", exc)
        return driver_path


class Browser:
    """Owns ONE long-lived Chrome/Chromium, reused for every API fetch and every
    watch. Persistent user-data-dir keeps Cloudflare clearance. Auto-recreates a
    dead driver. Built on undetected_chromedriver (kept: it bypasses Kick's
    Cloudflare; plain selenium gets blocked)."""

    KICK_URL = "https://kick.com"

    def __init__(self, *, headless, chromedriver_path, user_data_dir,
                 container, log):
        self._headless = bool(headless)
        self._chromedriver_path = chromedriver_path
        self._user_data_dir = user_data_dir
        self._container = bool(container)
        self._log = log
        self._driver = None

    # -- lifecycle ---------------------------------------------------------

    def _clear_singleton_locks(self):
        """Remove stale Chrome ``Singleton*`` locks in the user-data-dir so a
        reused profile doesn't refuse to launch."""
        try:
            for name in os.listdir(self._user_data_dir):
                if name.startswith("Singleton"):
                    p = os.path.join(self._user_data_dir, name)
                    try:
                        if os.path.isdir(p) and not os.path.islink(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            os.remove(p)
                    except Exception:
                        pass
        except Exception:
            pass

    def _build_driver(self):
        """Create and return a fresh undetected_chromedriver Chrome."""
        import undetected_chromedriver as uc

        os.makedirs(self._user_data_dir, exist_ok=True)
        self._clear_singleton_locks()

        opts = uc.ChromeOptions()
        if self._headless:
            try:
                opts.add_argument("--headless=new")
            except Exception:
                opts.add_argument("--headless")
            opts.add_argument("--disable-gpu")
        else:
            opts.add_argument("--window-size=1280,800")

        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--log-level=3")
        opts.add_argument("--silent")

        # In a container (no GPU; headful under Xvfb) disable the GPU so Chrome
        # falls back to SwiftShader software rendering. Do NOT also pass
        # --disable-software-rasterizer or Chrome has no renderer and dies.
        if self._container or os.environ.get("KDM_CONTAINER"):
            opts.add_argument("--disable-gpu")

        opts.add_argument(f"--user-data-dir={self._user_data_dir}")

        chrome_major, chrome_executable = _detect_chrome()
        driver_kwargs = {"options": opts, "version_main": chrome_major}
        if chrome_executable:
            driver_kwargs["browser_executable_path"] = chrome_executable

        drv_path = _writable_chromedriver_path(self._chromedriver_path, self._log)
        if drv_path and os.path.isfile(drv_path):
            driver_kwargs["driver_executable_path"] = drv_path

        return uc.Chrome(**driver_kwargs)

    def start(self):
        """Create the driver and navigate to https://kick.com (so in-page
        fetch() runs from the kick.com origin). Clears stale Singleton* locks
        first. Safe to call once at startup."""
        if self._driver is not None:
            return
        self._driver = self._build_driver()
        try:
            self._driver.get(self.KICK_URL)
        except Exception as exc:
            if self._log:
                self._log.warning("Initial navigation to kick.com failed: %s", exc)

    def ensure_alive(self):
        """If the driver is dead/unreachable, quit() and recreate it, then
        re-navigate to kick.com. Callers may invoke before important
        operations."""
        if self._driver is None:
            self.start()
            return
        try:
            # Probe — touching these properties raises if the driver is gone.
            _ = self._driver.current_url
            _ = self._driver.title
            return
        except Exception as exc:
            if self._log:
                self._log.warning("Driver unreachable (%s); recreating.", exc)
        self.quit()
        self._driver = self._build_driver()
        try:
            self._driver.get(self.KICK_URL)
        except Exception as exc:
            if self._log:
                self._log.warning("Re-navigation to kick.com failed: %s", exc)

    def get(self, url):
        """Navigate the single tab. Calls ensure_alive() first."""
        self.ensure_alive()
        try:
            self._driver.get(url)
        except Exception as exc:
            if self._log:
                self._log.warning("Navigation to %s failed: %s", url, exc)

    # -- in-page fetch -----------------------------------------------------

    def fetch_json(self, url, *, bearer=None, timeout=12.0):
        """Run an in-page ``fetch(url, {credentials:'include'})`` from the
        current page origin (must be kick.com), optionally with
        ``Authorization: Bearer``. Returns parsed JSON dict, or None on network
        error / non-JSON / blocked. Uses execute_async_script. Never raises."""
        import json

        self.ensure_alive()
        script = """
        const cb = arguments[arguments.length - 1];
        const url = arguments[0];
        const bearer = arguments[1];
        const headers = { 'Accept': 'application/json' };
        if (bearer) { headers['Authorization'] = 'Bearer ' + bearer; }
        fetch(url, { method: 'GET', credentials: 'include', cache: 'no-store', headers: headers })
          .then(r => r.text())
          .then(t => cb(t))
          .catch(e => cb(JSON.stringify({ __fetch_error: String(e) })));
        """
        try:
            try:
                self._driver.set_script_timeout(timeout)
            except Exception:
                pass
            text = self._driver.execute_async_script(script, url, bearer)
        except Exception as exc:
            if self._log:
                self._log.debug("fetch_json transport error for %s: %s", url, exc)
            return None

        if not text or not isinstance(text, str):
            return None
        low = text.lower()
        if "blocked by security policy" in low:
            if self._log:
                self._log.debug("fetch_json blocked for %s", url)
            return None
        try:
            data = json.loads(text)
        except Exception:
            if self._log:
                self._log.debug("fetch_json non-JSON for %s: %s", url, text[:200])
            return None
        if isinstance(data, dict) and (data.get("__fetch_error") or data.get("error")):
            if self._log:
                self._log.debug(
                    "fetch_json error payload for %s: %s",
                    url,
                    data.get("__fetch_error") or data.get("error"),
                )
            # An "error" key from the API itself may still be a valid dict; but a
            # transport __fetch_error means no data.
            if data.get("__fetch_error"):
                return None
        return data if isinstance(data, dict) else None

    # -- raw script bridges ------------------------------------------------

    def execute_script(self, script, *args):
        self.ensure_alive()
        return self._driver.execute_script(script, *args)

    def execute_async_script(self, script, *args):
        self.ensure_alive()
        return self._driver.execute_async_script(script, *args)

    # -- cookies / storage -------------------------------------------------

    def add_cookies(self, cookies):
        """Add each cookie (dropping null 'expiry'); swallow per-cookie errors."""
        if not cookies:
            return
        self.ensure_alive()
        for c in cookies:
            try:
                c = dict(c)
                if c.get("expiry") is None:
                    c.pop("expiry", None)
                self._driver.add_cookie(c)
            except Exception:
                pass

    def get_cookies(self):
        try:
            self.ensure_alive()
            return self._driver.get_cookies()
        except Exception:
            return []

    def set_session_storage(self, key, value):
        """Best-effort sessionStorage.setItem; used for stream_quality='160'."""
        try:
            self.ensure_alive()
            self._driver.execute_script(
                "try { sessionStorage.setItem(arguments[0], arguments[1]); } catch(e) {}",
                key,
                value,
            )
        except Exception as exc:
            if self._log:
                self._log.debug("set_session_storage(%s) failed: %s", key, exc)

    # -- teardown ----------------------------------------------------------

    def quit(self):
        """Quit the driver, swallow errors. Safe to call multiple times."""
        drv = self._driver
        self._driver = None
        if drv is None:
            return
        try:
            drv.quit()
        except Exception:
            pass

    @property
    def driver(self):
        """The underlying selenium driver (or None before start())."""
        return self._driver
