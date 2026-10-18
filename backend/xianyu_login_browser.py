"""Ephemeral official login window, operated by the authenticated human.

The page and every verification callback stay in one browser context. Only
rendered images and bounded human input cross the API boundary; cookies never
reach the workbench. No CAPTCHA recognition or fingerprint modifications.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import queue
import threading
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


LOGGER = logging.getLogger(__name__)
_CAPACITY = threading.BoundedSemaphore(1)
_DOMAINS = ("goofish.com", "taobao.com", "alicdn.com", "alipay.com", "mmstat.com", "aliyun.com", "alibaba.com")
_WIDTH, _HEIGHT = 900, 560
_OFFICIAL_LOGIN_PAGE = (
    "https://passport.goofish.com/mini_login.htm?lang=zh_cn&appName=xianyu"
    "&appEntrance=web&styleType=vertical&notLoadSsoView=false&notKeepLogin=false"
    "&isMobile=false&qrCodeFirst=false&stie=77"
)
_PLACEHOLDER = b'<svg xmlns="http://www.w3.org/2000/svg" width="360" height="420" viewBox="0 0 360 420"><rect width="360" height="420" fill="white"/><text x="180" y="210" text-anchor="middle" font-size="16" fill="#555">\xe6\xad\xa3\xe5\x9c\xa8\xe8\xbd\xbd\xe5\x85\xa5\xe9\x97\xb2\xe9\xb1\xbc\xe5\xae\x98\xe6\x96\xb9\xe7\x99\xbb\xe5\xbd\x95\xe9\xa1\xb5</text></svg>'


class LoginBrowserBusy(RuntimeError):
    pass


def _allowed_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = str(parsed.hostname or "").lower()
        return parsed.scheme == "https" and parsed.port in (None, 443) and any(
            host == domain or host.endswith("." + domain) for domain in _DOMAINS
        ) and parsed.username is None and parsed.password is None
    except ValueError:
        return False


def _account_ready(cookies: list[dict]) -> bool:
    now = time.time()
    applicable = {
        str(cookie.get("name") or ""): str(cookie.get("value") or "")
        for cookie in cookies
        if str(cookie.get("domain") or "").lstrip(".") == "goofish.com"
        and str(cookie.get("path") or "/") == "/"
        and (float(cookie.get("expires") or -1) < 0 or float(cookie["expires"]) > now)
    }
    return applicable.get("unb", "").isdigit() and bool(applicable.get("cookie2"))


class OfficialLoginBrowser:
    """All Playwright objects belong to this one bounded background thread."""

    def __init__(self, seconds: float = 600, before_open=None, after_open=None):
        if not _CAPACITY.acquire(timeout=1):
            raise LoginBrowserBusy("login_capacity")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._commands = queue.Queue(maxsize=8)
        self._deadline = time.monotonic() + max(30, min(float(seconds), 600))
        self._before_open, self._after_open = before_open, after_open
        self._state = "waiting"
        self._error = ""
        self._svg = _PLACEHOLDER
        self._revision = 0
        self._image_digest = ""
        self._clip = {"x": 0, "y": 0, "width": _WIDTH, "height": _HEIGHT}
        self._cookies = []
        self._frames = {}
        self._user_agent = ""
        self._thread = threading.Thread(target=self._run, name="xianyu-official-login", daemon=True)
        try:
            self._thread.start()
        except Exception:
            _CAPACITY.release()
            raise

    def snapshot(self, *, credentials: bool = False) -> dict:
        with self._lock:
            result = {"status": self._state, "error": self._error, "svg": self._svg, "revision": self._revision}
            if credentials:
                result.update(cookies=[dict(cookie) for cookie in self._cookies], user_agent=self._user_agent)
            return result

    def close(self):
        # Cancellation never waits for a navigation while holding the manager lock.
        self._stop.set()
        with self._lock:
            self._cookies.clear()
            self._svg = _PLACEHOLDER
            self._frames.clear()
        while True:
            try:
                self._commands.get_nowait()
            except queue.Empty:
                break

    def action(self, revision: int, points: list[tuple[float, float]]):
        with self._lock:
            frame = self._frames.get(revision)
            if self._stop.is_set() or frame is None or time.monotonic() - frame[2] > 15:
                raise LoginBrowserBusy("login_busy")
            clip, url, _ = frame
        try:
            self._commands.put_nowait((url, dict(clip), list(points)))
        except queue.Full:
            raise LoginBrowserBusy("login_busy") from None

    def _render(self, page):
        clip = {"x": 0, "y": 0, "width": _WIDTH, "height": _HEIGHT}
        # Keep the official parent alive. Crop only the picture, never move or
        # reconstruct its iframe or intercept verification messages.
        for element in page.locator("iframe").all()[:8]:
            source = element.get_attribute("src", timeout=500) or ""
            host = urlsplit(source).hostname or ""
            if host != "passport.goofish.com":
                continue
            box = element.bounding_box(timeout=500)
            if box and box["width"] >= 200 and box["height"] >= 150:
                x, y = max(0, box["x"]), max(0, box["y"])
                width, height = min(box["width"], _WIDTH - x), min(box["height"], _HEIGHT - y)
                if width >= 200 and height >= 150:
                    clip = {"x": x, "y": y, "width": width, "height": height}
                    break
        content = page.screenshot(type="png", clip=clip, timeout=2500)
        mime = "image/png"
        if len(content) > 320 * 1024:
            content = page.screenshot(type="jpeg", quality=75, clip=clip, timeout=2500)
            mime = "image/jpeg"
        if not content or len(content) > 360 * 1024:
            raise RuntimeError("bounded_image_failed")
        digest = hashlib.sha256(content).hexdigest()
        width, height = round(clip["width"]), round(clip["height"])
        encoded = base64.b64encode(content).decode("ascii")
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
               f'<image width="{width}" height="{height}" href="data:{mime};base64,{encoded}"/></svg>').encode("ascii")
        with self._lock:
            if digest != self._image_digest:
                self._revision += 1
                self._image_digest = digest
                self._svg = svg
            self._clip = clip
            self._frames[self._revision] = (dict(clip), page.url, time.monotonic())
            for revision in list(self._frames):
                if revision < self._revision - 10:
                    self._frames.pop(revision, None)

    def _human_input(self, page):
        try:
            url, clip, points = self._commands.get_nowait()
        except queue.Empty:
            return
        if page.url != url:
            return
        positions = [(clip["x"] + x * clip["width"], clip["y"] + y * clip["height"]) for x, y in points]
        if len(positions) == 1:
            page.mouse.click(*positions[0])
            return
        page.mouse.move(*positions[0])
        page.mouse.down()
        try:
            for position in positions[1:]:
                page.mouse.move(*position)
                page.wait_for_timeout(20)
        finally:
            page.mouse.up()

    def _run(self):
        playwright = browser = context = None
        runtime_directory = None
        opening = False
        stage = "launch"
        try:
            from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeout

            runtime_directory = tempfile.TemporaryDirectory(prefix="xianyu-login-")
            runtime = Path(runtime_directory.name)
            for folder in (runtime / "config", runtime / "cache"):
                folder.mkdir(mode=0o700)
            browser_environment = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "LANG": "zh_CN.UTF-8", "TZ": "Asia/Shanghai",
                "XDG_CONFIG_HOME": str(runtime / "config"),
                "XDG_CACHE_HOME": str(runtime / "cache"),
            }
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                channel="chromium", headless=True,
                args=["--renderer-process-limit=2"],
                env=browser_environment,
            )
            context = browser.new_context(
                viewport={"width": _WIDTH, "height": _HEIGHT}, device_scale_factor=1,
                locale="zh-CN", timezone_id="Asia/Shanghai", accept_downloads=False,
                service_workers="block",
            )
            context.set_default_timeout(2000)
            context.route("**/*", lambda route: route.continue_() if _allowed_url(route.request.url) else route.abort())
            context.route_web_socket("**/*", lambda socket: socket.close())
            page = context.new_page()
            self._user_agent = page.evaluate("navigator.userAgent")
            if self._before_open is not None:
                self._before_open()
                opening = True
            try:
                stage = "open"
                try:
                    page.goto(_OFFICIAL_LOGIN_PAGE, wait_until="domcontentloaded", timeout=25000)
                except PlaywrightTimeout:
                    if not _allowed_url(page.url):
                        raise
            finally:
                if opening and self._after_open is not None:
                    self._after_open()
                    opening = False
            last_picture = 0.0
            render_failures = 0
            verification = False
            while not self._stop.is_set() and time.monotonic() < self._deadline:
                if page.is_closed():
                    raise RuntimeError("official_window_closed")
                # Official links may open a verification tab in the same
                # isolated context. The human sees and operates that tab too.
                pages = [candidate for candidate in context.pages if not candidate.is_closed() and _allowed_url(candidate.url)]
                if pages:
                    page = pages[-1]
                stage = "input"
                self._human_input(page)
                stage = "cookies"
                cookies = context.cookies()
                ready = _account_ready(cookies)
                for frame in page.frames[:10]:
                    target = urlsplit(frame.url)
                    if target.hostname == "passport.goofish.com" and (
                        target.path.startswith("/iv/") or target.path.startswith("/newlogin/safe/")
                    ):
                        verification = True
                state = "confirmed" if ready else "verification_required" if verification else "waiting"
                with self._lock:
                    if self._stop.is_set():
                        break
                    self._cookies = cookies
                    self._state = state
                if not ready and time.monotonic() - last_picture >= 1.5:
                    stage = "render"
                    try:
                        self._render(page)
                        render_failures = 0
                    except PlaywrightError:
                        render_failures += 1
                        if render_failures >= 8:
                            raise
                    last_picture = time.monotonic()
                page.wait_for_timeout(150)
            if not self._stop.is_set():
                with self._lock:
                    self._state = "expired"
        except Exception as exc:
            # Playwright errors contain full URLs and query tickets. Never
            # log their messages, page HTML, screenshots or cookie contents.
            LOGGER.warning("qr_official_browser_failed: stage=%s type=%s", stage, type(exc).__name__)
            with self._lock:
                self._state = "error"
                self._error = "verification_browser_unavailable"
        finally:
            if opening and self._after_open is not None:
                try:
                    self._after_open()
                except Exception:
                    pass
            for resource in (context, browser):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            if runtime_directory is not None:
                try:
                    runtime_directory.cleanup()
                except OSError:
                    pass
            with self._lock:
                self._cookies.clear()
            _CAPACITY.release()
