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

# ── 브라우저 세션 인계 (CDP) ──
# 컨테이너 안 chromium CDP(9222)를 호스트 127.0.0.1:(BASE+uid) 로 퍼블리시한다.
# pv-portal 이 이 포트로 붙어 화면/입력을 중계 (docs/plans/2026-07-30-browser-session-handoff.md)
CDP_PORT_BASE = 19000
# 컨테이너 간 통신 차단 네트워크 (netavark isolate). CDP 가 컨테이너 안에서 0.0.0.0 바인딩이라
# 기본 브리지에선 다른 유저 컨테이너가 교차 접근할 수 있다 — isolate 네트워크로 차단.
ISOLATED_NETWORK = "pv-isolated"
_network_ready = False
_keepalive: dict[int, float] = {}  # user_id -> 이 시각까지 유휴정지 금지 (epoch)


def cdp_port(user_id: int) -> int:
    return CDP_PORT_BASE + int(user_id)


def keep_alive(user_id: int, until_ts: float):
    """활성 브라우저 인계 동안 reap_idle 이 컨테이너를 정지하지 않게 한다."""
    _keepalive[user_id] = max(_keepalive.get(user_id, 0), until_ts)


def _ensure_network() -> str | None:
    """isolate 네트워크 보장. 생성 실패(구버전 podman 등) 시 None → 기본 네트워크 사용."""
    global _network_ready
    if _network_ready:
        return ISOLATED_NETWORK
    r = _podman("network", "exists", ISOLATED_NETWORK, timeout=15)
    if r.returncode != 0:
        r = _podman("network", "create", "--opt", "isolate=true", ISOLATED_NETWORK, timeout=30)
        if r.returncode != 0:
            if _logger:
                _logger.warning(f"isolate network create failed, using default: {(r.stderr or '')[:200]}")
            return None
    _network_ready = True
    return ISOLATED_NETWORK


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


def _mem_to_bytes(spec: str) -> int:
    """"3g" / "2048m" → bytes. 파싱 실패 시 0."""
    m = re.fullmatch(r"(\d+)([kmg]?)", str(spec).strip().lower())
    if not m:
        return 0
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)]
    return int(m.group(1)) * mult


def _current_mem_bytes(user_id: int) -> int:
    r = _podman("inspect", name(user_id), "--format", "{{.HostConfig.Memory}}", timeout=15)
    try:
        return int((r.stdout or "").strip()) if r.returncode == 0 else 0
    except ValueError:
        return 0


def _target_mem(limits: dict | None) -> str:
    """티어 한도 → 이 컨테이너의 메모리 스펙. limits 없음(전용 호스트·구 웹) = 호스트 config.

    max_memory_mb: 호스트 물리 한계 보호 캡 (공용 large 7GB → 기본 6144).
    MAX(8GB) 유저도 첫 전용 풀 증설 전까지는 이 캡으로 태운다 (2026-08-04 Sean 승인)."""
    c = _cfg()
    if not limits or not limits.get("turnMemoryMb"):
        return str(c.get("memory", "3g"))
    cap = int(c.get("max_memory_mb", 6144))
    return f"{min(int(limits['turnMemoryMb']), cap)}m"


def ensure_running(user_id: int, limits: dict | None = None) -> bool:
    """컨테이너가 없으면 run, 멈춰있으면 start. idempotent.

    limits(로스터 티어 한도)가 오면 메모리를 티어값으로 적용한다. 이미 다른 상한으로
    떠 있으면: 정지 상태면 재생성(레이어 apt 설치물은 날아감 — 티어 변경 때 1회 비용),
    실행 중이면 다음 정지 후 기동 때 반영한다 (podman 3.x 에는 update 가 없다)."""
    with _lock(user_id):
        _last_used[user_id] = time.time()
        target_mem = _target_mem(limits)
        st = container_state(user_id)
        if st == "running":
            return True
        ensure_home(user_id)
        if st in ("stopping", "removing"):
            # podman 3.x 는 conmon 이 먼저 죽으면 컨테이너가 "stopping" 에서 멎는다.
            # 그대로 두면 아래 신규 생성으로 흘러 "name already in use" 로 실패하고,
            # start 도 "must be in Created or Stopped state" 로 거부된다.
            # cleanup → stop -t 0 이면 stopped 로 떨어져 다시 start 할 수 있다.
            # (컨테이너를 지우지 않는다 — 유저가 안에 설치한 것들이 날아가므로)
            _podman("container", "cleanup", name(user_id), timeout=30)
            _podman("stop", "-t", "0", name(user_id), timeout=30)
            st = container_state(user_id)
            if _logger:
                _logger.warning(f"container unwedged user={user_id} → {st}")
        if st == "paused":
            r = _podman("unpause", name(user_id), timeout=30)
            return r.returncode == 0
        if st != "none":
            # 티어 변경으로 메모리 상한이 달라졌으면, 정지 상태인 지금이 재생성 적기.
            # (홈은 마운트라 무손실 — 컨테이너 레이어의 apt 설치물만 날아간다)
            cur = _current_mem_bytes(user_id)
            want = _mem_to_bytes(target_mem)
            if want and cur and cur != want:
                if _logger:
                    _logger.info(f"container mem {cur >> 20}M → {want >> 20}M, "
                                 f"recreating user={user_id}")
                _podman("rm", "-f", name(user_id), timeout=45)
                st = "none"
        if st != "none":
            # exited/created/stopping 등 — 이름이 이미 존재하면 start 만 시도한다.
            # (여기서 신규 생성으로 넘어가면 이름 충돌로 반드시 실패한다)
            r = _podman("start", name(user_id), timeout=60)
            if r.returncode != 0 and _logger:
                _logger.error(f"container start failed user={user_id} state={st}: "
                              f"{(r.stderr or '')[:200]}")
            return r.returncode == 0
        # 신규 생성
        c = _cfg()
        h = str(home(user_id))
        shared_skills = _config.get("shared_skills_dir", "/srv/pv/shared/skills")
        args = [
            "run", "-d", "--name", name(user_id),
            "--user=agent",
            "--memory=" + target_mem,
            # 기본은 스왑 금지(memory-swap=memory). 전용 호스트는 memory_swap 을 크게 줘서
            # 상한 초과 시 즉사(OOM) 대신 스왑으로 버티게 할 수 있다.
            "--memory-swap=" + str(c.get("memory_swap", target_mem)),
            "--cpus=" + str(c.get("cpus", 1.5)),
            "--pids-limit=" + str(c.get("pids_limit", 256)),
            "--restart=no",
            # 브라우저 인계: 컨테이너 CDP → 호스트 localhost 전용 퍼블리시
            "-p", f"127.0.0.1:{cdp_port(user_id)}:9222",
            "-v", f"{h}:/home/agent:rw",
            "-v", f"{shared_skills}:{shared_skills}:ro",
            "-e", "HOME=/home/agent",
            "-e", "CLAUDE_CONFIG_DIR=/home/agent/claude",
        ]
        net = _ensure_network()
        if net:
            args += ["--network", net]
        args += [image(), "sleep", "infinity"]
        r = _podman(*args, timeout=120)
        if r.returncode != 0:
            if _logger:
                _logger.error(f"container run failed user={user_id}: {r.stderr[:200]}")
            return False
        return True


def _has_cdp_publish(user_id: int) -> bool:
    r = _podman("inspect", name(user_id), "--format",
                "{{json .HostConfig.PortBindings}}", timeout=15)
    if r.returncode != 0:
        return False
    return "9222" in (r.stdout or "")


def ensure_cdp(user_id: int) -> bool:
    """브라우저 인계용: CDP 포트 매핑이 있는 컨테이너 보장.

    포트 퍼블리시는 생성 시에만 가능하므로, 매핑 없는 기존 컨테이너는 재생성한다
    (홈은 마운트라 무손실 — 단 컨테이너 레이어의 apt 설치물은 날아간다).
    턴(claude) 실행 중이면 보류하고 다음 폴에서 재시도."""
    if not enabled_for(user_id):
        return False
    if not ensure_running(user_id):
        return False
    if _has_cdp_publish(user_id):
        return True
    with _lock(user_id):
        r = _podman("exec", name(user_id), "pgrep", "-x", "claude", timeout=15)
        if r.returncode == 0:
            if _logger:
                _logger.info(f"cdp recreate deferred (turn running) user={user_id}")
            return False
        if _logger:
            _logger.info(f"recreating container with cdp port user={user_id}")
        _podman("stop", "-t", "5", name(user_id), timeout=45)
        _podman("rm", "-f", name(user_id), timeout=45)
    return ensure_running(user_id)


BROWSER_START_SCRIPT = "browser-handoff/scripts/start-browser.sh"


def ensure_browser(user_id: int) -> bool:
    """헤드리스 chromium 이 컨테이너 안에서 CDP(9222)와 함께 떠 있게 보장.

    시작 스크립트는 공유 스킬(ro 마운트)에 있어 에이전트와 데몬이 같은 것을 쓴다."""
    if not ensure_cdp(user_id):
        return False
    _last_used[user_id] = time.time()
    # 스크립트가 chromium/브리지 각각 idempotent 하게 처리 (둘 다 떠 있으면 즉시 no-op)
    shared_skills = _config.get("shared_skills_dir", "/srv/pv/shared/skills")
    script = f"{shared_skills}/{BROWSER_START_SCRIPT}"
    r = _podman("exec", "--user=agent", name(user_id), "bash", script, timeout=60)
    if r.returncode != 0 and _logger:
        _logger.warning(f"browser start failed user={user_id}: {(r.stderr or r.stdout or '')[:200]}")
    return r.returncode == 0


def _home_has_credentials(user_id: int) -> bool:
    """컨테이너를 깨우지 않고 호스트에서 자격증명 유무만 판정.
    claude/ 는 agent(10000) 소유 700 이라 ubuntu 가 직접 stat 할 수 없어 sudo 로 확인한다
    (sudoers 에 이미 허용된 du 를 존재 확인용으로 쓴다 — 내용은 읽지 않는다)."""
    p = home(user_id) / "claude" / ".credentials.json"
    r = subprocess.run(["sudo", "-n", "du", "-sb", str(p)],
                       capture_output=True, timeout=15)
    return r.returncode == 0


def has_credentials(user_id: int) -> bool:
    """로그인 여부 확인. **컨테이너를 새로 띄우지 않는다.**
    여기서 ensure_running 을 하면 로그인조차 안 한 유저까지 컨테이너가 생성돼,
    전용 호스트(유휴 정지 없음)에서 빈 컨테이너가 상시 점유된다.
    컨테이너는 실제 턴(exec_claude_turn)이나 로그인(login_start) 때만 뜬다."""
    if container_state(user_id) != "running":
        return _home_has_credentials(user_id)
    r = _podman("exec", name(user_id), "test", "-f", "/home/agent/claude/.credentials.json",
                timeout=15)
    return r.returncode == 0


_skills_linked: set[int] = set()


def ensure_skills(user_id: int) -> None:
    """컨테이너 안 `$CLAUDE_CONFIG_DIR/skills` 에 번들 스킬을 **개별 심링크**로 건다.

    마운트만으로는 안 된다 — Claude Code 는 `CLAUDE_CONFIG_DIR/skills` 만 탐색하고,
    `/srv/pv/shared/skills` 는 그저 임의 경로다. 이게 빠져 있어서 컨테이너 유저는
    gmail·slack 등 번들 스킬을 하나도 못 썼다 (2026-07-29 실측으로 확인).

    개별 링크인 이유: 통짜로 걸면 유저가 자기 스킬을 같은 폴더에 설치할 수 없다.
    프로세스당 1회만 수행하고, 배포로 데몬이 재시작되면 다시 걸려 새 스킬이 반영된다."""
    if user_id in _skills_linked:
        return
    shared = _config.get("shared_skills_dir", "/srv/pv/shared/skills")
    script = (
        'd="${CLAUDE_CONFIG_DIR:-/home/agent/claude}/skills"; mkdir -p "$d"; '
        f'for s in {shared}/*/; do n=$(basename "$s"); '
        '[ -e "$d/$n" ] || ln -sfn "${s%/}" "$d/$n"; done'
    )
    r = _podman("exec", name(user_id), "sh", "-c", script, timeout=30)
    if r.returncode == 0:
        _skills_linked.add(user_id)
    elif _logger:
        _logger.warning(f"skills link failed user={user_id}: {(r.stderr or '')[:200]}")


def kill_turns(user_id: int) -> None:
    """컨테이너 안에서 도는 claude 턴을 정리한다.

    `podman exec` 는 클라이언트를 죽여도 **컨테이너 안 프로세스가 살아남는다**(실측 확인).
    데몬 종료 시 정리하지 않으면, 재전달된 메시지가 같은 세션에서 두 번 돌게 된다."""
    if container_state(user_id) != "running":
        return
    _podman("exec", name(user_id), "pkill", "-f", "^claude ", timeout=15)


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
                     system_prompt_path: str | None,
                     limits: dict | None = None) -> tuple[int, str, str]:
    """컨테이너 안에서 claude 한 턴. returns (rc, stdout, stderr).
    limits: 로스터 티어 한도 (메모리·턴 시간). None = 호스트 config 일괄값."""
    if not ensure_running(user_id, limits):
        return (1, "", "container start failed")
    ensure_skills(user_id)  # 번들 스킬 심링크 (프로세스당 1회)
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

    if limits and limits.get("turnTimeoutMin"):
        timeout = int(limits["turnTimeoutMin"]) * 60
    else:
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


def exec_claude_cmd(user_id: int, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """이미 떠 있는 컨테이너에서 짧은 claude 서브명령 실행 (조회용).
    컨테이너를 기동시키지 않는다 — 유휴 정지를 방해하지 않기 위함."""
    if container_state(user_id) != "running":
        return (1, "", "container not running")
    try:
        r = _podman("exec", "-w", "/home/agent/workspace", name(user_id),
                    "claude", *args, timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    return r.returncode, r.stdout, r.stderr


def exec_shell(user_id: int, workdir: str, command: str, env: dict,
               timeout_sec: int) -> tuple[int, str, str]:
    """컨테이너 안에서 셸 명령 하나 실행 (cron 배치용). 클로드 턴이 아니다.

    타임아웃은 **컨테이너 안 `timeout`** 으로 건다. podman exec 클라이언트만 죽이면
    안쪽 프로세스가 고아로 남아 다음 회차와 겹친다. 컨테이너 재시작은 하지 않는다 —
    같은 컨테이너에서 돌던 대화 턴까지 끊기기 때문.
    명령은 argv 가 아니라 env 로 넘긴다(따옴표 지옥 회피 + /proc cmdline 노출 최소화).
    """
    # 컨테이너 없는 호스트(예: 뉴넥스 내부 서버, container.enabled=false)에서는
    # 데몬 계정으로 직접 실행한다. 컨테이너 경로(/home/agent/...)는 호스트 경로로 번역.
    if not enabled_for(user_id):
        host_wd = workdir
        if workdir.startswith("/home/agent/"):
            host_wd = str(home(user_id) / workdir[len("/home/agent/"):])
        try:
            os.makedirs(host_wd, exist_ok=True)
            e = dict(os.environ)
            e.update(env)
            e["PV_CRON_CMD"] = command
            r = subprocess.run(
                ["bash", "-lc",
                 f'timeout -k 10 {int(timeout_sec)} bash -c "$PV_CRON_CMD"'],
                cwd=host_wd, env=e, capture_output=True, text=True,
                timeout=timeout_sec + 60)
            return (r.returncode, r.stdout, r.stderr)
        except subprocess.TimeoutExpired:
            return (124, "", "host timeout")
        except OSError as exc:
            return (1, "", f"host exec failed: {exc}")

    if not ensure_running(user_id):
        return (1, "", "container start failed")
    _last_used[user_id] = time.time()
    e = dict(env)
    e["PV_CRON_CMD"] = command
    e["PV_CRON_TIMEOUT"] = str(int(timeout_sec))
    ef = _envfile(user_id, e)
    args = ["exec", "-w", workdir, "--env-file", ef, name(user_id),
            "bash", "-lc", 'timeout -k 10 "$PV_CRON_TIMEOUT" bash -c "$PV_CRON_CMD"']
    try:
        r = _podman(*args, timeout=timeout_sec + 60)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        # 컨테이너 안 timeout 이 안 먹은 경우(드묾). 컨테이너는 건드리지 않는다.
        rc, out, err = 124, "", "runner-side timeout"
    finally:
        subprocess.run(["sudo", "-n", "rm", "-f", ef], capture_output=True)
    return rc, out, err


def sysprompt_path_in_home(user_id: int, content: str,
                           name: str = ".cloud-system-prompt.md") -> str | None:
    """시스템 프롬프트 파일을 컨테이너 홈에 기록 → 컨테이너 내부 경로 반환.
    name 을 프로젝트별로 다르게 주면 동시 턴 간 파일 경합을 피한다."""
    try:
        host_p = home(user_id) / "workspace" / name
        subprocess.run(["sudo", "-n", "tee", str(host_p)], input=content,
                       capture_output=True, text=True)
        # 컨테이너 내 claude(agent 10000)가 읽어야 함
        subprocess.run(["sudo", "-n", "chown", f"{AGENT_UID}:{AGENT_UID}", str(host_p)],
                       capture_output=True)
        return f"/home/agent/workspace/{name}"
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

def layer_size_bytes(user_id: int) -> int:
    """컨테이너 rw 레이어 크기(apt 설치물 등). 홈 du 에 안 잡히는 사용량 — 디스크 쿼터에 합산."""
    r = _podman("inspect", "--size", name(user_id), "--format", "{{.SizeRw}}", timeout=30)
    try:
        return int((r.stdout or "").strip()) if r.returncode == 0 else 0
    except ValueError:
        return 0


def reap_idle(skip=None):
    """유휴(마지막 사용 후 idle_stop_sec 경과) 컨테이너 정지.

    idle_stop_sec <= 0 이면 유휴 정지를 하지 않는다(상시 유지).
    전용 호스트에서 cron 배치·스크래핑 데몬을 상시 돌릴 때 필요.
    skip(uid) -> bool: True 인 유저는 정지하지 않는다 (MAX 티어 상시 유지).
    """
    idle = int(_cfg().get("idle_stop_sec", 900))
    if idle <= 0:
        return
    now = time.time()
    r = _podman("ps", "--format", "{{.Names}}", timeout=15)
    if r.returncode != 0:
        return
    for nm in (r.stdout or "").split():
        m = re.fullmatch(r"pv-u(\d+)", nm.strip())
        if not m:
            continue
        uid = int(m.group(1))
        if skip and skip(uid):
            continue  # 티어 상시 유지 (MAX)
        if now < _keepalive.get(uid, 0):
            continue  # 활성 브라우저 인계 — 유저가 원격 로그인 중일 수 있어 정지 금지
        last = _last_used.get(uid, 0)
        if now - last > idle:
            _podman("stop", "-t", "5", nm, timeout=30)
            # podman 3.x 는 여기서 "stopping" 에 걸려 다음 기동을 막는 일이 있다.
            # 그 자리에서 풀어둔다 (다음 턴이 느려지거나 실패하지 않게).
            if container_state(uid) == "stopping":
                _podman("container", "cleanup", nm, timeout=30)
                _podman("stop", "-t", "0", nm, timeout=30)
                if _logger:
                    _logger.warning(f"idle stop wedged, unwedged: {nm} → {container_state(uid)}")
            if _logger:
                _logger.info(f"idle container stopped: {nm}")
