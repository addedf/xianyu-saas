"""Authorize on this Mac and import the complete session into one online shop.

No local API, database, task consumer or Xianyu Worker is started. Credentials
stay in memory and are sent only to the selected HTTPS workbench. The server
must advertise the identity-checked import contract before capture begins.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from http.cookiejar import Cookie
from urllib.parse import urlsplit

import requests


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".local/login-sync/config.json"
DEFAULT_SERVER = "https://xianyu.sulian.org/xianyu-saas"
LOGIN_PAGE = (
    "https://passport.goofish.com/mini_login.htm?lang=zh_cn&appName=xianyu"
    "&appEntrance=web&styleType=vertical&notLoadSsoView=false&notKeepLogin=false"
    "&isMobile=false&qrCodeFirst=false&stie=77"
)
BOOTSTRAP_API = "mtop.taobao.idlehome.home.webpc.feed"
BOOTSTRAP_URL = f"https://h5api.m.goofish.com/h5/{BOOTSTRAP_API}/1.0/"
TARGET_HOST = "h5api.m.goofish.com"
COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
ACCOUNT_KEY = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
FINGERPRINT = re.compile(r"^[a-f0-9]{16}$")


class SyncError(RuntimeError):
    """Messages are written here, never copied from network/browser errors."""


def private_read(path: Path, maximum: int = 65536) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SyncError("凭据或配置文件必须属于当前用户且权限为 600。")
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise SyncError("文件超过允许长度。")
    return raw.decode("utf-8")


def remember_settings(settings: dict):
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CONFIG.parent.is_symlink() or CONFIG.is_symlink():
        raise SyncError("本地同步配置不能使用符号链接。")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=CONFIG.parent, delete=False, encoding="utf-8") as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(settings, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, CONFIG)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def server_address(value: str) -> tuple[str, str]:
    target = urlsplit(str(value).strip())
    if target.scheme != "https" or not target.hostname or target.username or target.password or target.query or target.fragment:
        raise SyncError("线上工作台地址必须是完整 HTTPS 地址，不能包含凭据、查询参数或片段。")
    if ".." in target.path or "\\" in target.path or "%" in target.path:
        raise SyncError("线上工作台路径无效。")
    origin = f"https://{target.netloc}"
    return origin + target.path.rstrip("/"), origin


class OnlineWorkbench:
    def __init__(self, address: str):
        self.address, self.origin = server_address(address)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "Accept": "application/json", "Origin": self.origin,
            "Referer": self.address + "/", "X-SaaS-Browser-Intent": "browser-write",
        })
        self.owns_login = False

    def call(self, method: str, path: str, *, account: str = "", payload=None) -> dict:
        headers = {"X-Shop-Account": account} if account else {}
        try:
            with self.session.request(
                method, self.address + "/api/" + path, headers=headers,
                json=payload, timeout=(10, 100), allow_redirects=False, stream=True,
            ) as response:
                chunks = bytearray()
                for chunk in response.iter_content(16384):
                    chunks.extend(chunk)
                    if len(chunks) > 1024 * 1024:
                        raise SyncError("线上响应超过允许长度，操作已停止。")
                if "application/json" not in response.headers.get("Content-Type", "").lower():
                    raise SyncError(f"线上网关暂时不可用（HTTP {response.status_code}），请稍后重试。")
                data = json.loads(chunks)
                if not response.ok:
                    detail = data.get("detail") if isinstance(data, dict) else None
                    code = str(detail.get("code") or "") if isinstance(detail, dict) else ""
                    known = {
                        "account_mismatch": "本地闲鱼账号与指定线上店铺不一致，未替换登录态。",
                        "account_identity_unknown": "线上原登录信息无法确认身份，请先确认店铺。",
                        "verification_required": "线上仍要求闲鱼官方安全验证；本次登录态未完成接管。",
                        "risk_control": "闲鱼仍限制服务器当前请求，Cookie 同步不能保证解除平台限制。",
                        "risk_cooldown": "线上处于请求保护冷却期，请稍后再同步。",
                        "cookie_expired": "闲鱼登录态已失效，请在本机重新登录。",
                        "cookie_invalid": "闲鱼登录态未通过线上身份验证，原登录态已保留。",
                        "sync_busy": "线上店铺正在处理另一项连接操作，请稍后重试。",
                        "sync_cooldown": "线上店铺暂时等待，请稍后重试。",
                        "login_failed": "工作台账号或密码错误。",
                    }
                    if response.status_code == 401:
                        raise SyncError("线上工作台未登录或凭据已过期，请重新输入工作台账号密码。")
                    raise SyncError(known.get(code, f"线上未完成操作（HTTP {response.status_code}），请查看工作台状态。"))
                if not isinstance(data, dict):
                    raise SyncError("线上响应格式不正确，操作已停止。")
                return data
        except (requests.RequestException, ValueError):
            raise SyncError("未能确认线上响应，请检查工作台连接状态后再重试。") from None

    def authenticate(self, username: str, session_file: Path | None):
        if session_file is not None:
            payload = json.loads(private_read(session_file))
            token = str(payload.get("session_token") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", token):
                raise SyncError("临时工作台会话文件无效。")
            self.session.headers["Authorization"] = "Bearer " + token
        else:
            if not sys.stdin.isatty():
                raise SyncError("请在终端运行并输入线上工作台密码。")
            password = getpass.getpass(f"线上工作台 {username} 的密码（不保存）：")
            try:
                self.call("POST", "auth/login", payload={"username": username, "password": password})
                self.owns_login = True
            finally:
                password = ""
        self.call("GET", "me")

    def close(self):
        if self.owns_login:
            try:
                self.call("POST", "auth/logout")
            except SyncError:
                pass
        self.session.cookies.clear()
        self.session.headers.pop("Authorization", None)
        self.session.close()


def ensure_local_worker_stopped():
    result = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, check=True)
    worker = str(ROOT / "worker/main.py")
    if any(worker in command for command in result.stdout.splitlines()):
        raise SyncError("检测到本项目的本地店铺 Worker。请先停用本地自动处理，再同步线上。")


def cookie_session(cookies: list[dict], user_agent: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": user_agent, "Origin": "https://www.goofish.com", "Referer": "https://www.goofish.com/"})
    if len(cookies) > 256:
        raise SyncError("官方登录信息超过允许数量。")
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if domain.lstrip(".") != "goofish.com" and not domain.lstrip(".").endswith(".goofish.com"):
            continue
        name, value = str(cookie.get("name") or ""), str(cookie.get("value") or "")
        if not COOKIE_NAME.fullmatch(name) or not value or len(value) > 4096 or not value.isascii():
            continue
        if any(ord(character) < 0x21 or ord(character) == 127 or character == ";" for character in value):
            continue
        expiry = cookie.get("expires")
        session.cookies.set_cookie(Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=str(cookie.get("path") or "/"), path_specified=True, secure=bool(cookie.get("secure")),
            expires=int(expiry) if expiry and expiry > 0 else None, discard=not bool(expiry and expiry > 0),
            comment=None, comment_url=None, rest={"HttpOnly": None} if cookie.get("httpOnly") else {}, rfc2109=False,
        ))
    return session


def cookie_header(session: requests.Session) -> str:
    selected = {}
    for cookie in session.cookies:
        domain = str(cookie.domain or "").lstrip(".")
        applies = TARGET_HOST == domain or (cookie.domain_specified and TARGET_HOST.endswith("." + domain))
        if not applies or cookie.is_expired() or not "/h5/".startswith(str(cookie.path or "/")):
            continue
        score = len(domain) + len(str(cookie.path or "/"))
        if cookie.name not in selected or score > selected[cookie.name][0]:
            selected[cookie.name] = (score, cookie.value)
    values = {name: value for name, (_, value) in selected.items()}
    if not values.get("unb", "").isdigit() or "_" not in values.get("_m_h5_tk", "") or not values.get("cookie2"):
        raise SyncError("官方登录信息尚不完整，请完成本机登录和安全验证后重试。")
    header = "; ".join(f"{name}={value}" for name, value in sorted(values.items()))
    if len(header.encode("ascii")) > 32768:
        raise SyncError("官方登录信息超过允许长度。")
    return header


def complete_cookie(cookies: list[dict], user_agent: str) -> str:
    session = cookie_session(cookies, user_agent)
    try:
        try:
            return cookie_header(session)
        except SyncError:
            # The official Passport window may not yet have initialized the
            # independent MTOP signing cookie. One public, unsigned call does.
            try:
                with session.post(BOOTSTRAP_URL, params={
                    "jsv": "2.7.2", "appKey": "34839810", "t": str(int(time.time() * 1000)), "sign": "",
                    "api": BOOTSTRAP_API, "v": "1.0", "type": "originaljson", "dataType": "json",
                }, data={"data": "{}"}, timeout=(8, 15), allow_redirects=False, stream=True) as response:
                    if not 200 <= response.status_code < 300:
                        raise SyncError("本机登录签名初始化失败，请稍后重试。")
                    size = 0
                    for chunk in response.iter_content(16384):
                        size += len(chunk)
                        if size > 256 * 1024:
                            raise SyncError("本机登录初始化响应过长。")
            except requests.RequestException:
                raise SyncError("本机无法初始化闲鱼登录签名，请检查网络后重试。") from None
            return cookie_header(session)
    finally:
        session.cookies.clear()
        session.close()


def capture_local_login(seconds: int) -> str:
    from playwright.sync_api import sync_playwright

    print("请在新打开的 Google Chrome 官方登录窗口扫码，并由本人完成安全验证。", flush=True)
    print("登录完成后自动同步；本地不启动店铺自动回复或发货。", flush=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel="chrome", headless=False)
        try:
            context = browser.new_context(viewport={"width": 1000, "height": 700}, locale="zh-CN")
            page = context.new_page()
            page.goto(LOGIN_PAGE, wait_until="domcontentloaded", timeout=30000)
            agent = page.evaluate("navigator.userAgent")
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if not context.pages:
                    raise SyncError("官方登录窗口已关闭，未同步登录态。")
                cookies = context.cookies()
                root_values = {item["name"]: item["value"] for item in cookies if item.get("domain", "").lstrip(".") == "goofish.com"}
                if root_values.get("unb", "").isdigit() and root_values.get("cookie2"):
                    return complete_cookie(cookies, agent)
                context.pages[0].wait_for_timeout(500)
            raise SyncError("本次本机登录已超时，未同步登录态。")
        finally:
            browser.close()


def select_account(workbench: OnlineWorkbench, requested: str) -> dict:
    accounts = [item for item in workbench.call("GET", "bot/accounts").get("accounts", []) if item.get("enabled")]
    if requested:
        chosen = next((item for item in accounts if item.get("key") == requested), None)
    elif len(accounts) == 1:
        chosen = accounts[0]
    elif sys.stdin.isatty():
        for index, item in enumerate(accounts, 1):
            print(f"{index}. {item.get('name') or item['key']} ({item['key']})")
        try:
            index = int(input("选择要同步的线上店铺序号：")) - 1
            chosen = accounts[index] if 0 <= index < len(accounts) else None
        except ValueError:
            chosen = None
    else:
        chosen = None
    if not chosen or not ACCOUNT_KEY.fullmatch(str(chosen.get("key") or "")):
        raise SyncError("未选择有效的线上店铺，请使用 --account 指定已启用店铺。")
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description="在本机登录闲鱼，并将完整 Cookie 安全同步到指定线上店铺。")
    parser.add_argument("--server", help="HTTPS 工作台地址；默认使用已保存配置")
    parser.add_argument("--account", help="线上已有店铺 key；首次运行可交互选择")
    parser.add_argument("--username", help="线上工作台用户名；密码以隐藏输入读取")
    parser.add_argument("--session-file", type=Path, help="本次操作的临时工作台会话文件（权限 600）；不保存或输出内容")
    parser.add_argument("--cookie-file", type=Path, help="使用已有本地 Cookie 文件（权限 600），替代重新扫码")
    parser.add_argument("--timeout", type=int, default=600, help="等待本人登录的秒数，60 至 600")
    parser.add_argument("--no-resume", action="store_true", help="保留线上原有的运行意愿，不主动重新启用已暂停店铺")
    parser.add_argument("--check", action="store_true", help="仅检查线上账号、店铺和导入能力，不读取或同步闲鱼 Cookie")
    args = parser.parse_args()
    settings = json.loads(private_read(CONFIG)) if CONFIG.exists() else {}
    address = args.server or settings.get("server") or DEFAULT_SERVER
    username = args.username or settings.get("username") or "admin"
    print(f"线上工作台：{server_address(address)[0]}", flush=True)
    ensure_local_worker_stopped()
    workbench = OnlineWorkbench(address)
    cookie = ""
    try:
        workbench.authenticate(username, args.session_file)
        account = select_account(workbench, args.account or settings.get("account") or "")
        key = account["key"]
        status = workbench.call("GET", "bot/status", account=key)
        if status.get("account", {}).get("key") != key or not status.get("local_login_import", {}).get("identity_check_required"):
            raise SyncError("线上版本尚不支持带账号校验的本地登录导入，请先更新对应功能。")
        expected = str(status.get("account", {}).get("identity_fingerprint") or "")
        if expected and not FINGERPRINT.fullmatch(expected):
            raise SyncError("线上店铺身份信息无效，操作已停止。")
        print(f"目标店铺：{account.get('name') or key} ({key})", flush=True)
        if args.check:
            print("线上认证、目标店铺及安全导入能力均可用；未同步 Cookie、未启动店铺。", flush=True)
            return 0
        cookie = private_read(args.cookie_file, 32768).strip() if args.cookie_file else capture_local_login(max(60, min(args.timeout, 600)))
        sys.path.insert(0, str(ROOT / "backend"))
        from shop_sync import parse_cookie_header, account_ref
        cookie, parsed = parse_cookie_header(cookie)
        fingerprint = account_ref(parsed)
        if expected and fingerprint != expected:
            raise SyncError("本机登录的是另一闲鱼账号，未上传 Cookie、未变更线上店铺。")
        print("本机登录完成，正在通过 HTTPS 验证并更新指定线上店铺……", flush=True)
        result = workbench.call("PUT", "bot/cookies", account=key, payload={
            "cookies": cookie, "expected_account_ref": fingerprint, "resume": not args.no_resume,
        })
        cookie = ""
        if result.get("account_key") != key or result.get("identity_fingerprint") != fingerprint or result.get("connected") is not True:
            raise SyncError("线上接管结果未能确认，请查看工作台；不会自动重试写入。")
        remember_settings({"server": workbench.address, "account": key, "username": username})
        print(f"已同步：{result.get('shop_name') or account.get('name') or key}。本地登录窗口已关闭。", flush=True)
        current = workbench.call("GET", "bot/status", account=key)
        phase = str(current.get("auth_phase") or "")
        print(f"线上进程：{'运行中' if current.get('running') else '未运行'}；登录阶段：{phase or '等待连接'}。", flush=True)
        if current.get("needs_human"):
            print("闲鱼仍要求线上环境验证，请按官方提示处理；Cookie 同步不能保证解除平台限制。", flush=True)
        return 0
    finally:
        cookie = ""
        workbench.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消；未输出或保存闲鱼 Cookie。", file=sys.stderr)
        raise SystemExit(130)
    except SyncError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f"同步未完成（{type(error).__name__}），凭据未输出，请检查网络或本机浏览器。", file=sys.stderr)
        raise SystemExit(1)
