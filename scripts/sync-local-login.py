"""Use the local workbench to authorize Xianyu, then import its Cookie online.

The local project is used for the human login flow. Once the local workbench
has saved the verified Cookie under ``.local/tenants/``, this script reads that
single private file and sends it to the selected HTTPS workbench. The online
service validates the identity, replaces the session atomically, and obtains
its own access token.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import requests


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".local/login-sync/config.json"
DEFAULT_SERVER = "https://xianyu.sulian.org/xianyu-saas"
LOCAL_COMMAND = ROOT / "local.command"
LOCAL_URL = "http://127.0.0.1:4173/xianyu-saas/"
LOCAL_DB = ROOT / ".local/saas.db"
LOCAL_TENANTS = ROOT / ".local/tenants"
ACCOUNT_KEY = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
USER_ID = re.compile(r"^[0-9]{1,20}$")
FINGERPRINT = re.compile(r"^[a-f0-9]{16}$")


class SyncError(RuntimeError):
    """Messages are written here, never copied from network or Cookie data."""


def private_read(path: Path, maximum: int = 65536) -> str:
    """Read a private file without following the file itself as a symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise SyncError("本地登录文件不存在或无法读取，请先在本地工作台完成登录。") from error
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SyncError("凭据或配置文件必须属于当前用户且权限为 600。")
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise SyncError("文件超过允许长度。")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SyncError("本地登录文件编码无效。") from error


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
                        "identity_required": "本地登录同步缺少线上身份校验信息，请重新检查目标店铺。",
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


def _local_command(action: str, env=None, *, check=True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            [str(LOCAL_COMMAND), action], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyncError("本地工作台启动器执行失败，请查看 .local/local.log。") from error
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SyncError(detail[:240] or "本地工作台启动失败，请查看 .local/local.log。")
    return result


def _local_is_running() -> bool:
    result = _local_command("status", check=False)
    output = (result.stdout or "") + (result.stderr or "")
    return "运行中" in output


def _local_process_exists() -> bool:
    pid_file = ROOT / ".local/local-run.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        command = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False
    return f"{ROOT / 'scripts/local-run.py'} run" in command


def start_local_workbench(*, keep_existing: bool) -> bool:
    """Start local API/web in observe + login-only mode.

    Returns whether this invocation owns the local process and may stop it in
    ``finally``. A running process is restarted by default so an older
    invocation cannot silently run with Worker autostart enabled.
    """
    if _local_is_running() or _local_process_exists():
        if keep_existing:
            print(f"复用已运行的本地工作台：{LOCAL_URL}", flush=True)
            return False
        print("检测到本地工作台正在运行；为确保登录完成后不启动 Worker，先重启为登录专用模式。", flush=True)
        _local_command("stop")
    env = os.environ.copy()
    env["SAAS_LOCAL_OBSERVE_ONLY"] = "1"
    env["SAAS_LOCAL_LOGIN_ONLY"] = "1"
    result = _local_command("start", env=env)
    output = (result.stdout or "").strip()
    if output:
        print(output, flush=True)
    try:
        subprocess.run(["open", LOCAL_URL], cwd=ROOT, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        print(f"请手动打开本地工作台：{LOCAL_URL}", flush=True)
    return True


def stop_local_workbench():
    result = _local_command("stop", check=False)
    output = (result.stdout or result.stderr or "").strip()
    if output:
        print(output, flush=True)


def _read_only_local_db() -> sqlite3.Connection | None:
    if not LOCAL_DB.exists() or LOCAL_DB.is_symlink():
        return None
    try:
        return sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def local_user_ids(username: str, requested_id: str = "") -> list[str]:
    if requested_id:
        if not USER_ID.fullmatch(requested_id):
            raise SyncError("本地工作台用户 ID 无效。")
        return [requested_id]
    if username:
        connection = _read_only_local_db()
        if connection is None:
            raise SyncError("无法读取本地工作台用户，请先启动并登录本地项目。")
        try:
            rows = connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchall()
        except sqlite3.Error as error:
            raise SyncError("本地工作台用户信息不可读取。") from error
        finally:
            connection.close()
        result = [str(row[0]) for row in rows if USER_ID.fullmatch(str(row[0]))]
        if not result:
            raise SyncError("未找到指定的本地工作台用户。")
        return result
    if not LOCAL_TENANTS.exists() or LOCAL_TENANTS.is_symlink():
        return []
    return sorted(
        item.name for item in LOCAL_TENANTS.iterdir()
        if item.is_dir() and not item.is_symlink() and USER_ID.fullmatch(item.name)
    )


def _cookie_file_for(user_id: str, account_key: str) -> Path:
    if not USER_ID.fullmatch(user_id) or not ACCOUNT_KEY.fullmatch(account_key):
        raise SyncError("本地店铺或用户标识无效。")
    tenant = LOCAL_TENANTS / user_id
    path = tenant / "cookies.txt" if account_key == "default" else tenant / "accounts" / account_key / "cookies.txt"
    current = ROOT
    try:
        relative = path.relative_to(ROOT)
    except ValueError as error:
        raise SyncError("本地登录文件路径无效。") from error
    for component in relative.parts:
        current = current / component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise SyncError("本地登录文件路径不能包含符号链接。")
    return path


def local_cookie_files(account_key: str, username: str, user_id: str = "") -> list[tuple[str, Path]]:
    user_ids = local_user_ids(username, user_id)
    result: list[tuple[str, Path]] = []
    for user_id in user_ids:
        if account_key:
            path = _cookie_file_for(user_id, account_key)
            if path.exists():
                result.append((f"{user_id}/{account_key}", path))
            continue
        tenant = LOCAL_TENANTS / user_id
        candidates = []
        default = tenant / "cookies.txt"
        if default.exists():
            candidates.append(("default", default))
        accounts = tenant / "accounts"
        if accounts.exists() and not accounts.is_symlink():
            for item in sorted(accounts.iterdir(), key=lambda value: value.name):
                if item.is_dir() and not item.is_symlink() and ACCOUNT_KEY.fullmatch(item.name):
                    path = item / "cookies.txt"
                    if path.exists():
                        candidates.append((item.name, path))
        result.extend((f"{user_id}/{key}", path) for key, path in candidates)
    return result


def _cookie_parser():
    sys.path.insert(0, str(ROOT / "backend"))
    from shop_sync import account_ref, parse_cookie_header
    return parse_cookie_header, account_ref


def load_local_cookie(account_key: str, username: str, user_id: str = "") -> tuple[str, str, str]:
    parse_cookie_header, account_ref = _cookie_parser()
    candidates = local_cookie_files(account_key, username, user_id)
    valid: list[tuple[str, str, str]] = []
    for label, path in candidates:
        try:
            raw = private_read(path, 32768).strip()
            normalized, parsed = parse_cookie_header(raw)
            valid.append((label, normalized, account_ref(parsed)))
        except Exception:
            # Do not expose parser or Cookie details; a partial file is common
            # while the local QR flow is still finishing.
            continue
    if not valid:
        if account_key:
            raise SyncError(f"本地店铺 {account_key} 尚未保存完整闲鱼登录态；请在本地工作台完成扫码后重试。")
        raise SyncError("本地工作台尚未保存完整闲鱼登录态；请完成扫码并确认店铺已连接后重试。")
    if len(valid) > 1:
        labels = ", ".join(item[0] for item in valid[:8])
        raise SyncError(f"本地找到多个有效店铺登录态（{labels}），请使用 --local-account 指定来源。")
    return valid[0]


def capture_local_cookie(account_key: str, username: str, user_id: str, timeout: int) -> tuple[str, str, str]:
    print(f"本地工作台已打开：{LOCAL_URL}", flush=True)
    print("请在本地工作台登录管理员，选择本地店铺，点击“连接闲鱼”并完成扫码。", flush=True)
    print("看到本地店铺显示已连接后回到此终端；本地登录完成后不会启动 Worker。", flush=True)
    if sys.stdin.isatty():
        input("按回车读取本地已保存的登录态并同步线上：")
    deadline = time.monotonic() + max(30, min(timeout, 1800))
    last_error = None
    while time.monotonic() < deadline:
        try:
            return load_local_cookie(account_key, username, user_id)
        except SyncError as error:
            last_error = error
            if not sys.stdin.isatty():
                break
            time.sleep(1)
            continue
    raise last_error or SyncError("本地登录态读取超时，请确认本地工作台已完成连接。")


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
    parser = argparse.ArgumentParser(description="在本地工作台登录闲鱼，并将完整 Cookie 安全同步到指定线上店铺。")
    parser.add_argument("--server", help="HTTPS 工作台地址；默认使用已保存配置")
    parser.add_argument("--account", help="线上已有店铺 key；首次运行可交互选择")
    parser.add_argument("--username", help="线上工作台用户名；密码以隐藏输入读取")
    parser.add_argument("--session-file", type=Path, help="本次操作的临时工作台会话文件（权限 600）；不保存或输出内容")
    parser.add_argument("--cookie-file", type=Path, help="使用已有本地 Cookie 文件（权限 600），跳过本地工作台扫码")
    parser.add_argument("--local-account", help="本地工作台来源店铺 key；与线上店铺 key 可以不同")
    parser.add_argument("--local-username", help="本地工作台用户；仅用于选择本地 tenants 目录")
    parser.add_argument("--local-user-id", help="本地工作台用户 ID；多个本地用户时可直接指定")
    parser.add_argument("--timeout", type=int, default=600, help="等待本地登录态保存的秒数，30 至 1800")
    parser.add_argument("--keep-local", action="store_true", help="同步后保留本地工作台运行")
    parser.add_argument("--reuse-local", action="store_true", help="复用已经运行的本地工作台，不重启它")
    parser.add_argument("--no-resume", action="store_true", help="保留线上原有的运行意愿，不主动重新启用已暂停店铺")
    parser.add_argument("--allow-initial-bind", action="store_true", help="确认将本机闲鱼账号首次绑定到没有历史身份的线上店铺")
    parser.add_argument("--check", action="store_true", help="仅检查线上账号、店铺和导入能力，不读取或同步闲鱼 Cookie")
    args = parser.parse_args()
    try:
        settings = json.loads(private_read(CONFIG)) if CONFIG.exists() else {}
    except (ValueError, TypeError):
        raise SyncError("本地同步配置无效，请删除 .local/login-sync/config.json 后重试。") from None
    address = args.server or settings.get("server") or DEFAULT_SERVER
    username = args.username or settings.get("username") or "admin"
    local_account = args.local_account or settings.get("local_account") or ""
    local_username = args.local_username or settings.get("local_username") or ""
    local_user_id = args.local_user_id or settings.get("local_user_id") or ""
    print(f"线上工作台：{server_address(address)[0]}", flush=True)
    ensure_local_worker_stopped()
    workbench = OnlineWorkbench(address)
    cookie = ""
    local_started = False
    try:
        workbench.authenticate(username, args.session_file)
        account = select_account(workbench, args.account or settings.get("account") or "")
        key = account["key"]
        status = workbench.call("GET", "bot/status", account=key)
        if status.get("account", {}).get("key") != key or not status.get("local_login_import", {}).get("identity_check_required"):
            raise SyncError("线上版本尚不支持带账号校验的本地登录导入，请先更新对应功能。")
        expected = str(status.get("account", {}).get("identity_fingerprint") or "")
        import_capability = status.get("local_login_import") or {}
        initial_bind = False
        if expected and not FINGERPRINT.fullmatch(expected):
            raise SyncError("线上店铺身份信息无效，操作已停止。")
        if not expected:
            if import_capability.get("initial_bind_allowed") is not True:
                raise SyncError("线上店铺没有可核验的历史身份，未允许首次绑定。")
            initial_bind = True
        print(f"目标线上店铺：{account.get('name') or key} ({key})", flush=True)
        if args.check:
            suffix = "（允许显式首次绑定）" if initial_bind else ""
            print(f"线上认证、目标店铺及安全导入能力均可用{suffix}；未同步 Cookie、未启动店铺。", flush=True)
            return 0
        if initial_bind and not args.allow_initial_bind:
            if not sys.stdin.isatty():
                raise SyncError("首次绑定需要在交互终端确认，或使用 --allow-initial-bind。")
            answer = input("线上店铺没有历史闲鱼身份；本次会把本地登录账号绑定到该店铺。继续？[y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                raise SyncError("已取消首次绑定，线上店铺未变更。")
        if args.cookie_file:
            cookie = private_read(args.cookie_file, 32768).strip()
            source_label = "指定 Cookie 文件"
            source_ref = ""
        else:
            local_started = start_local_workbench(keep_existing=args.reuse_local)
            source_label, cookie, source_ref = capture_local_cookie(local_account, local_username, local_user_id, args.timeout)
            print(f"已读取本地店铺登录态：{source_label}", flush=True)
        parse_cookie_header, account_ref = _cookie_parser()
        cookie, parsed = parse_cookie_header(cookie)
        fingerprint = account_ref(parsed)
        if source_ref and source_ref != fingerprint:
            raise SyncError("本地登录态读取前后发生变化，请重新在本地工作台登录。")
        if expected and fingerprint != expected:
            raise SyncError("本地登录的是另一闲鱼账号，未上传 Cookie、未变更线上店铺。")
        print("本地登录完成，正在通过 HTTPS 验证并更新指定线上店铺……", flush=True)
        result = workbench.call("PUT", "bot/cookies", account=key, payload={
            "cookies": cookie, "expected_account_ref": fingerprint, "resume": not args.no_resume,
            "allow_initial_bind": initial_bind,
        })
        cookie = ""
        if result.get("account_key") != key or result.get("identity_fingerprint") != fingerprint or result.get("connected") is not True:
            raise SyncError("线上接管结果未能确认，请查看工作台；不会自动重试写入。")
        remember_settings({
            "server": workbench.address,
            "account": key,
            "username": username,
            "local_account": source_label.split("/", 1)[-1] if source_label != "指定 Cookie 文件" else local_account,
            "local_username": local_username,
            "local_user_id": source_label.split("/", 1)[0] if source_label != "指定 Cookie 文件" else local_user_id,
        })
        print(f"已同步：{result.get('shop_name') or account.get('name') or key}。线上将自行重新获取访问令牌。", flush=True)
        current = workbench.call("GET", "bot/status", account=key)
        phase = str(current.get("auth_phase") or "")
        print(f"线上进程：{'运行中' if current.get('running') else '未运行'}；登录阶段：{phase or '等待连接'}。", flush=True)
        if current.get("needs_human"):
            print("闲鱼仍要求线上环境验证，请按官方提示处理；Cookie 同步不能保证解除平台限制。", flush=True)
        return 0
    finally:
        cookie = ""
        if local_started and not args.keep_local:
            stop_local_workbench()
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
        print(f"同步未完成（{type(error).__name__}），凭据未输出，请检查本地工作台或网络。", file=sys.stderr)
        raise SystemExit(1)
