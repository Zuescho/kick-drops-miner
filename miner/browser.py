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
import time


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

    # Bound every page-load / async-script so a hung navigation returns instead
    # of blocking forever (the selenium HTTP client to chromedriver would
    # otherwise read-timeout at 120s and *retry the navigation*). Page-load
    # timeout must be comfortably under that client timeout.
    PAGE_LOAD_TIMEOUT = 45
    SCRIPT_TIMEOUT = 30

    def __init__(self, *, headless, chromedriver_path, user_data_dir,
                 container, log):
        self._headless = bool(headless)
        self._chromedriver_path = chromedriver_path
        self._user_data_dir = user_data_dir
        self._container = bool(container)
        self._log = log
        self._driver = None
        self._created_at = 0.0          # monotonic time the driver was built
        self._last_blocked = False      # last fetch_json hit 429/403/Cloudflare

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
        # Cap the on-disk media/HTTP cache so a multi-day run can't grow the
        # profile without bound.
        opts.add_argument("--disk-cache-size=104857600")  # 100 MB

        chrome_major, chrome_executable = _detect_chrome()
        if chrome_major is None and (self._container or os.environ.get("KDM_CONTAINER")):
            # With a supplied driver_executable_path uc won't download, but a
            # missing version can still trigger a noisy patch/retry — surface it.
            if self._log:
                self._log.warning(
                    "could not detect Chrome major version; relying on the "
                    "supplied chromedriver (set KDM_CHROME_VERSION if needed)")
            env_major = _parse_major_version(os.environ.get("KDM_CHROME_VERSION", ""))
            if env_major:
                chrome_major = env_major
        driver_kwargs = {"options": opts, "version_main": chrome_major}
        if chrome_executable:
            driver_kwargs["browser_executable_path"] = chrome_executable

        drv_path = _writable_chromedriver_path(self._chromedriver_path, self._log)
        if drv_path and os.path.isfile(drv_path):
            driver_kwargs["driver_executable_path"] = drv_path

        drv = uc.Chrome(**driver_kwargs)
        # Finite timeouts so a stalled load/script can never wedge the loop.
        try:
            drv.set_page_load_timeout(self.PAGE_LOAD_TIMEOUT)
            drv.set_script_timeout(self.SCRIPT_TIMEOUT)
        except Exception:
            pass
        self._created_at = time.monotonic()
        return drv

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
        """If the driver is dead/unreachable/crashed, quit() and recreate it,
        then re-navigate to kick.com. Callers may invoke before important
        operations."""
        if self._driver is None:
            self.start()
            return
        try:
            # Probe — touching these raises if the session is gone, and the
            # tiny script round-trips the *renderer*, catching an "Aw, Snap"
            # crashed tab that still answers current_url from the browser proc.
            # Bound the script call so a wedged renderer can't stall callers.
            _ = self._driver.current_url
            try:
                self._driver.set_script_timeout(5)
            except Exception:
                pass
            ok = self._driver.execute_script("return 1;") == 1
            try:
                self._driver.set_script_timeout(self.SCRIPT_TIMEOUT)
            except Exception:
                pass
            if not ok:
                raise RuntimeError("renderer probe returned unexpected value")
            return
        except Exception as exc:
            if self._log:
                self._log.warning("Driver unreachable (%s); recreating.", exc)
        self._recreate()

    def _recreate(self):
        """Tear down and rebuild the driver, landing back on kick.com."""
        self.quit()
        self._driver = self._build_driver()
        try:
            self._driver.get(self.KICK_URL)
        except Exception as exc:
            if self._log:
                self._log.warning("Re-navigation to kick.com failed: %s", exc)

    def maybe_recycle(self, max_age_seconds):
        """Proactively recycle the driver once it exceeds max_age_seconds. This
        is the most reliable defense against cumulative renderer-memory / leaked
        DOM growth over a multi-day run. No-op if max_age_seconds <= 0."""
        if max_age_seconds and max_age_seconds > 0 and self._driver is not None:
            age = time.monotonic() - (self._created_at or 0.0)
            if age >= max_age_seconds:
                if self._log:
                    self._log.info("recycling browser after %ds uptime", int(age))
                self._recreate()

    def get(self, url):
        """Navigate the single tab. Calls ensure_alive() first. On a page-load
        timeout, stop the partial load and continue (the page is usually still
        usable) rather than treating it as fatal."""
        self.ensure_alive()
        try:
            self._driver.get(url)
        except Exception as exc:
            name = type(exc).__name__
            if self._log:
                self._log.warning("Navigation to %s failed (%s)", url, name)
            # Abort the stuck load so the tab is responsive again.
            try:
                self._driver.execute_script("window.stop();")
            except Exception:
                pass

    # -- in-page fetch -----------------------------------------------------

    # Markers that mean Cloudflare/WAF returned an HTML challenge, not JSON.
    _CLOUDFLARE_MARKERS = (
        "just a moment",
        "cf-chl",
        "attention required",
        "blocked by security policy",
        "/cdn-cgi/challenge",
    )

    @property
    def last_blocked(self):
        """True if the most recent fetch_json hit a 429/403/503 or a Cloudflare
        challenge (vs. a genuine answer). Callers use this to back off."""
        return self._last_blocked

    def fetch_json(self, url, *, bearer=None, timeout=12.0):
        """Run an in-page ``fetch(url, {credentials:'include'})`` from the
        current page origin (must be kick.com), optionally with
        ``Authorization: Bearer``. Returns the parsed JSON dict, or None on
        network error / non-JSON / rate-limit / Cloudflare block. Sets
        ``last_blocked`` so a 429/403/503/challenge is distinguishable from a
        real "channel offline" answer. Uses execute_async_script. Never raises."""
        import json

        self._last_blocked = False
        self.ensure_alive()
        # Capture BOTH the HTTP status and the body so a 429/403 JSON error body
        # can't be mistaken for valid data, and an AbortController bounds the
        # request so a hung fetch is actively cancelled (not leaked).
        script = """
        const cb = arguments[arguments.length - 1];
        const url = arguments[0];
        const bearer = arguments[1];
        const ms = arguments[2];
        const headers = { 'Accept': 'application/json' };
        if (bearer) { headers['Authorization'] = 'Bearer ' + bearer; }
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), ms);
        fetch(url, { method: 'GET', credentials: 'include', cache: 'no-store',
                     headers: headers, signal: ctrl.signal })
          .then(r => r.text().then(t => { clearTimeout(timer);
                 cb(JSON.stringify({ __status: r.status, __body: t })); }))
          .catch(e => { clearTimeout(timer);
                 cb(JSON.stringify({ __fetch_error: String(e) })); });
        """
        try:
            try:
                self._driver.set_script_timeout(timeout + 2)
            except Exception:
                pass
            # Tell the in-page fetch to self-abort slightly before selenium would.
            text = self._driver.execute_async_script(
                script, url, bearer, int(timeout * 1000)
            )
        except Exception as exc:
            if self._log:
                self._log.debug("fetch_json transport error for %s: %s", url, exc)
            return None
        finally:
            # Restore the default script timeout (don't leak the per-fetch one
            # onto later execute_script/ensure_alive probes).
            try:
                self._driver.set_script_timeout(self.SCRIPT_TIMEOUT)
            except Exception:
                pass

        if not text or not isinstance(text, str):
            return None
        try:
            envelope = json.loads(text)
        except Exception:
            return None
        if not isinstance(envelope, dict):
            return None
        if envelope.get("__fetch_error"):
            if self._log:
                self._log.debug(
                    "fetch_json transport error for %s: %s", url, envelope["__fetch_error"]
                )
            return None

        status = envelope.get("__status")
        body = envelope.get("__body") or ""
        low = body.lower()
        if status in (401, 403, 429, 503) or any(m in low for m in self._CLOUDFLARE_MARKERS):
            self._last_blocked = True
            if self._log:
                self._log.debug("fetch_json blocked (status=%s) for %s", status, url)
            return None
        try:
            data = json.loads(body)
        except Exception:
            if self._log:
                self._log.debug("fetch_json non-JSON for %s: %s", url, body[:200])
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
                # Selenium/Chrome only accept sameSite Strict/Lax/None; a value
                # exported from another tool (e.g. "no_restriction"/"unspecified")
                # makes add_cookie reject the WHOLE cookie — which could be the
                # session_token. Normalize, or drop the field entirely.
                same = c.get("sameSite")
                if same is not None:
                    mapped = {"no_restriction": "None", "unspecified": None,
                              "lax": "Lax", "strict": "Strict", "none": "None"}
                    norm = mapped.get(str(same).lower(), same)
                    if norm in ("Strict", "Lax", "None"):
                        c["sameSite"] = norm
                    else:
                        c.pop("sameSite", None)
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
