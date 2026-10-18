"""Run the local workbench with project virtualenvs and no container runtime."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".local"
PID_FILE = STATE / "local-run.pid"
SCRIPT = str(Path(__file__).resolve())
os.chdir(ROOT)
STATE.mkdir(mode=0o700, exist_ok=True)
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import dotenv_values

for name, value in dotenv_values(ROOT / "config/saas.env").items():
    if value is not None:
        os.environ[name] = value
# macOS exposes /var through a root-owned symlink. The trusted updater-state
# reader intentionally refuses symlink traversal; use its canonical system
# directory without resolving any application-controlled symlinks.
if sys.platform == "darwin" and Path("/var").resolve() == Path("/private/var"):
    for name, value in list(os.environ.items()):
        if name.startswith("SAAS_UPDATE") and value.startswith("/var/"):
            os.environ[name] = "/private" + value

from update_maintenance import read_maintenance

API_PORT = int(os.environ.get("SAAS_DEV_API_PORT", "8096"))
WEB_PORT = int(os.environ.get("SAAS_DEV_WEB_PORT", "4173"))
URL = f"http://127.0.0.1:{WEB_PORT}/xianyu-saas/"
LOGIN_ONLY = os.environ.get("SAAS_LOCAL_LOGIN_ONLY", "0").strip().lower() in {
    "1", "true", "yes",
}
OBSERVE_ONLY = os.environ.get("SAAS_LOCAL_OBSERVE_ONLY", "0").strip().lower() in {
    "1", "true", "yes",
} or LOGIN_ONLY


def running_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
        if pid <= 1:
            return None
        command = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return pid if f"{SCRIPT} run" in command else None
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def ready():
    if read_maintenance()["active"]:
        return False
    try:
        with urllib.request.urlopen(URL + "api/ready", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def run():
    lock = (STATE / "local-run.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    PID_FILE.write_text(str(os.getpid()) + "\n")
    PID_FILE.chmod(0o600)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    children, logs = [], []
    try:
        from ai_customer_service import ensure_development_master_key

        if not os.environ.get("SAAS_AI_MASTER_KEY"):
            os.environ["SAAS_AI_MASTER_KEY"] = ensure_development_master_key(
                STATE / "ai-master-key", STATE / "tenants",
            )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "backend")
        env["SAAS_DEV_API_ORIGIN"] = f"http://127.0.0.1:{API_PORT}"
        env["SAAS_DEV_WEB_PORT"] = str(WEB_PORT)
        env["PYTHONUNBUFFERED"] = "1"
        if OBSERVE_ONLY:
            env["SAAS_RESTORE_WORKERS"] = "0"
            env["SAAS_SELF_UPDATE_ENABLED"] = "0"
        if LOGIN_ONLY:
            env["SAAS_LOCAL_LOGIN_ONLY"] = "1"
        python = str(ROOT / "backend/.venv/bin/python")
        services = {
            "api": [python, "-m", "uvicorn", "app:app", "--app-dir", str(ROOT / "backend"),
                    "--host", "127.0.0.1", "--port", str(API_PORT)],
            "consumer": [python, "-m", "job_consumer"],
            "web": ["node", str(ROOT / "scripts/dev-server.mjs")],
        }
        if OBSERVE_ONLY:
            services.pop("consumer")
            if LOGIN_ONLY:
                print("本地登录专用启动：仅 API 和网页；消费者、店铺自动恢复、登录完成后的 Worker 及自更新已关闭。", flush=True)
            else:
                print("本地观察启动：仅 API 和网页；消费者、店铺自动恢复及自更新已关闭。", flush=True)
        log_dir = STATE / "local-logs"
        log_dir.mkdir(mode=0o700, exist_ok=True)
        for name, command in services.items():
            log = (log_dir / f"{name}.log").open("a")
            logs.append(log)
            children.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True))
        print(f"本地工作台：{URL}", flush=True)
        while not stop.wait(0.5):
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(f"服务退出，状态码 {child.returncode}；日志：{log_dir}")
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for log in logs:
            log.close()
        PID_FILE.unlink(missing_ok=True)


def start():
    maintenance = read_maintenance()
    if maintenance["active"]:
        raise SystemExit(f"维护状态阻止本地启动：{maintenance.get('error_code') or maintenance.get('phase')}")
    if running_pid():
        print(f"已在运行：{URL}")
        return
    for port in (API_PORT, WEB_PORT):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise SystemExit(f"本机端口 {port} 已被占用，请先停止占用服务。")
    with (STATE / "local.log").open("a") as log:
        child = subprocess.Popen([sys.executable, SCRIPT, "run"], cwd=ROOT, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):
        if child.poll() is not None:
            raise SystemExit(f"启动失败，请查看 {STATE / 'local.log'} 和 {STATE / 'local-logs'}")
        if ready():
            print(f"本地服务已启动：{URL}\n停止：./local.command stop\n状态：./local.command status")
            return
        time.sleep(0.25)
    child.terminate()
    raise SystemExit(f"启动超时，请查看 {STATE / 'local-logs'}")


def stop():
    pid = running_pid()
    if pid is None:
        print("本地服务未运行")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if running_pid() is None:
            print("本地服务已停止，数据已保留")
            return
        time.sleep(0.25)
    raise SystemExit(f"服务仍在退出，请查看 {STATE / 'local.log'}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "run":
        run()
    elif action == "start":
        start()
    elif action == "stop":
        stop()
    elif action == "status":
        print(f"运行中：{URL}" if running_pid() and ready() else "本地服务未就绪")
    else:
        raise SystemExit("用法：./local.command [start|stop|status]")
