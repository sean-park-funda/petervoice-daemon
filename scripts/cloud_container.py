"""T3: 유저당 rootful podman 컨테이너 런타임.

cloud_daemon.py 의 systemd-run 경로와 병행. config.container.enabled + 유저 opt-in 시에만 활성.
설계/PoC: peter-voice/docs/plans/2026-07-24-t3-per-user-containers.md

핵심:
- rootful podman(`sudo podman`) — rootless 는 메모리 상한 미강제라 rootful 채택
- 컨테이너 홈 /srv/pv/cusers/<id> (ubuntu 소유, 컨테이너 경계로 격리) 를 /home/agent 로 마운트
- 리소스 상한은 컨테이너 생성 시 지정(--memory/--cpus/--pids-limit)
- 첫 사용 시 지연 기동(run/start), 유휴 자동 정지
"""

import os
import subprocess
import threading
import time
import json
import re
import pty
import select
import signal
from pathlib import Path

_config = {}
_logger = None
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()
_last_used: dict[int, float] = {}


def init(config: dict, logger):
    global _config, _logger
    _config = config
    _logger = logger


def _cfg() -> dict:
    return _config.get("container", {})


def enabled_for(user_id: int) -> bool:
    c = _cfg()
    if not c.get("enabled"):
        return False
    users = c.get("users")  # None/누락 = 전체, 리스트 = opt-in
    return users is None or user_id in users


def home_root() -> Path:
    # systemd 모드와 같은 경로를 쓴다 (웹 projects.directory/포탈/프록시가 이 경로 기준).
    # 모드 차이는 소유권뿐: systemd=pv<id>, container=agent uid(10000)
    return Path(_cfg().get("home_root", _config.get("users_root", "/srv/pv/users")))


def home(user_id: int) -> Path:
    return home_root() / str(user_id)


def name(user_id: int) -> str:
    return f"pv-u{user_id}"


def image() -> str:
    return _cfg().get("image", "localhost/pv-base:latest")


def _podman(*args, timeout=60, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", "podman", *args],
                          capture_output=True, text=True, timeout=timeout, check=check)


def _lock(user_id: int) -> threading.Lock:
    with _locks_guard:
        if user_id not in _locks:
            _locks[user_id] = threading.Lock()
        return _locks[user_id]


AGENT_UID = "10000"  # 컨테이너 내 agent 유저 uid (rootful: 호스트 uid 와 동일 매핑)


_home_ready: set[int] = set()


def ensure_home(user_id: int):
    """컨테이너 홈 준비 (idempotent). 홈 전체를 agent uid(10000) 소유로 전환하고,
    docs 포탈(ubuntu)은 **workspace 에만** ACL 접근 — claude 토큰 폴더는 ubuntu 도 못 읽게 유지."""
    if user_id in _home_ready:
        return
    h = home(user_id)
    subprocess.run(["sudo", "-n", "mkdir", "-p",
                    str(h / "workspace" / "general" / "docs"), str(h / "claude")],
                   capture_output=True)
    subprocess.run(["sudo", "-n", "chown", "-R", f"{AGENT_UID}:{AGENT_UID}", str(h)],
                   capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "700", str(h)], capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "700", str(h / "claude")], capture_output=True)
    # 루트에 ubuntu traverse + workspace 에만 ubuntu rwx (포탈용)
    subprocess.run(["sudo", "-n", "setfacl", "-m", "u:ubuntu:--x", str(h)], capture_output=True)
    subprocess.run(["sudo", "-n", "setfacl", "-R", "-m", "u:ubuntu:rwx",
                    "-m", "d:u:ubuntu:rwx", str(h / "workspace")], capture_output=True)
    _home_ready.add(user_id)


def container_state(user_id: int) -> str:
    """running | exited | none"""
    r = _podman("inspect", name(user_id), "--format", "{{.State.Status}}", timeout=15)
    if r.returncode != 0:
        return "none"
    return (r.stdout or "").strip() or "none"


def ensure_running(user_id: int) -> bool:
    """컨테이너가 없으면 run, 멈춰있으면 start. idempotent."""
    with _lock(user_id):
        _last_used[user_id] = time.time()
        st = container_state(user_id)
        if st == "running":
            return True
        ensure_home(user_id)
        if st == "exited":
            r = _podman("start", name(user_id), timeout=60)
            return r.returncode == 0
        # 신규 생성
        c = _cfg()
        h = str(home(user_id))
        shared_skills = _config.get("shared_skills_dir", "/srv/pv/shared/skills")
        args = [
            "run", "-d", "--name", name(user_id),
            "--user=agent",
            "--memory=" + c.get("memory", "3g"),
            "--memory-swap=" + c.get("memory", "3g"),
            "--cpus=" + str(c.get("cpus", 1.5)),
            "--pids-limit=" + str(c.get("pids_limit", 256)),
            "--restart=no",
            "-v", f"{h}:/home/agent:rw",
            "-v", f"{shared_skills}:{shared_skills}:ro",
            "-e", "HOME=/home/agent",
            "-e", "CLAUDE_CONFIG_DIR=/home/agent/claude",
            image(), "sleep", "infinity",
        ]
        r = _podman(*args, timeout=120)
        if r.returncode != 0:
            if _logger:
                _logger.error(f"container run failed user={user_id}: {r.stderr[:200]}")
            return False
        return True


def has_credentials(user_id: int) -> bool:
    if not ensure_running(user_id):
        return False
    r = _podman("exec", name(user_id), "test", "-f", "/home/agent/claude/.credentials.json",
                timeout=15)
    return r.returncode == 0


def _envfile(user_id: int, env: dict) -> str:
    d = home_root() / ".turnenv"
    subprocess.run(["sudo", "-n", "mkdir", "-p", str(d)], capture_output=True)
    p = d / f"{name(user_id)}-{os.getpid()}-{int(time.time()*1000)%100000}.env"
    lines = [f"{k}={str(v).replace(chr(10), ' ')}" for k, v in env.items()]
    # ubuntu 소유 600 으로 작성 (sudo 로 podman 이 읽음)
    content = "\n".join(lines) + "\n"
    subprocess.run(["sudo", "-n", "tee", str(p)], input=content, capture_output=True, text=True)
    subprocess.run(["sudo", "-n", "chmod", "600", str(p)], capture_output=True)
    return str(p)


def exec_claude_turn(user_id: int, project: str, prompt: str,
                     session_id: str | None, secrets: dict,
                     system_prompt_path: str | None) -> tuple[int, str, str]:
    """컨테이너 안에서 claude 한 턴. returns (rc, stdout, stderr)."""
    if not ensure_running(user_id):
        return (1, "", "container start failed")
    _last_used[user_id] = time.time()
    ws = f"/home/agent/workspace/{project if re.fullmatch(r'[a-z0-9_-]{1,60}', project or '') else 'general'}"
    _podman("exec", name(user_id), "mkdir", "-p", f"{ws}/docs", timeout=15)

    cmd = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
    if system_prompt_path:
        cmd += ["--append-system-prompt-file", system_prompt_path]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += ["--", prompt]

    env = dict(secrets)
    ef = _envfile(user_id, env) if env else None
    exec_args = ["exec", "-w", ws]
    if ef:
        exec_args += ["--env-file", ef]
    exec_args += [name(user_id), *cmd]

    timeout = int(_cfg().get("turn_timeout_sec", 1800))
    try:
        r = _podman(*exec_args, timeout=timeout + 30)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        # 하드 타임아웃: 컨테이너 재시작으로 내부 프로세스 전부 종료
        _podman("restart", name(user_id), timeout=30)
        rc, out, err = 124, "", "timeout"
    finally:
        if ef:
            subprocess.run(["sudo", "-n", "rm", "-f", ef], capture_output=True)
    return rc, out, err


def sysprompt_path_in_home(user_id: int, content: str) -> str | None:
    """시스템 프롬프트 파일을 컨테이너 홈에 기록 → 컨테이너 내부 경로 반환."""
    try:
        host_p = home(user_id) / "workspace" / ".cloud-system-prompt.md"
        subprocess.run(["sudo", "-n", "tee", str(host_p)], input=content,
                       capture_output=True, text=True)
        # 컨테이너 내 claude(agent 10000)가 읽어야 함
        subprocess.run(["sudo", "-n", "chown", f"{AGENT_UID}:{AGENT_UID}", str(host_p)],
                       capture_output=True)
        return "/home/agent/workspace/.cloud-system-prompt.md"
    except Exception:
        return None


# ── 로그인 (컨테이너 안 claude auth login, pty) ──────────────────

_URL_RE = re.compile(r"https?://[^\s\x00-\x1f\"']+")


def login_start(user_id: int, url_timeout: int = 60) -> tuple[int | None, int | None, str | None]:
    """`podman exec -i claude auth login` 을 pty 로 띄워 URL 캡처.
    returns (pid, fd, url) — 이후 코드 주입은 login_submit."""
    if not ensure_running(user_id):
        return (None, None, None)
    argv = ["sudo", "-n", "podman", "exec", "-i", name(user_id),
            "claude", "auth", "login", "--claudeai"]
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
    buf = ""
    deadline = time.time() + url_timeout
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 1.0)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", chunk.decode(errors="replace"))
        for m in _URL_RE.finditer(buf):
            cand = m.group(0).rstrip(").,")
            if ("oauth" in cand.lower() or "authorize" in cand.lower()) and \
               m.end() < len(buf) and "redirect_uri=" in cand.lower():
                return (pid, fd, cand)
        low = buf.lower()
        if any(mk in low for mk in ("choose", "theme", "press enter")):
            try:
                os.write(fd, b"\r")
            except OSError:
                pass
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return (None, None, None)


def login_submit(pid: int, fd: int, code: str) -> bool:
    try:
        os.write(fd, (code.strip() + "\n").encode())
    except OSError:
        return False
    deadline = time.time() + 60
    while time.time() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
            try:
                os.close(fd)
            except OSError:
                pass
            return ok
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                os.read(fd, 4096)
            except OSError:
                pass
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return False


# ── 유휴 정리 ────────────────────────────────────────────────────

def reap_idle():
    """유휴(마지막 사용 후 idle_stop_sec 경과) 컨테이너 정지."""
    idle = int(_cfg().get("idle_stop_sec", 900))
    now = time.time()
    r = _podman("ps", "--format", "{{.Names}}", timeout=15)
    if r.returncode != 0:
        return
    for nm in (r.stdout or "").split():
        m = re.fullmatch(r"pv-u(\d+)", nm.strip())
        if not m:
            continue
        uid = int(m.group(1))
        last = _last_used.get(uid, 0)
        if now - last > idle:
            _podman("stop", "-t", "5", nm, timeout=30)
            if _logger:
                _logger.info(f"idle container stopped: {nm}")
