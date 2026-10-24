"""
로컬 퍼블리싱 매니저 — Next.js 프로젝트를 빌드하고 launchd로 서빙.
Cloudflare Tunnel 라우팅은 서버 API를 통해 처리.
"""

import json
import os
import plistlib
import re
import signal
import socket
import subprocess
import shutil
import time
from pathlib import Path

from daemon.globals import config, logger
from daemon.api import api_request

SITES_DIR = Path.home() / ".petervoice-sites"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PREFIX = "com.petervoice.site."
PORT_MIN = 3001
PORT_MAX = 3099
SITE_DOMAIN = "peter-voice.site"


def _resolve_username(username: str = None) -> str:
    """username fallback: 명시적 값 → config username → customer → 'user'
    bot_name은 에이전트 이름이라 유저 고유값이 아니므로 사용하지 않음."""
    if username:
        return username
    return config.get("username") or config.get("customer") or "user"


def _sites_state_path() -> Path:
    return SITES_DIR / "sites.json"


def _load_sites() -> dict:
    """저장된 사이트 목록 로드"""
    path = _sites_state_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_sites(sites: dict):
    SITES_DIR.mkdir(parents=True, exist_ok=True)
    _sites_state_path().write_text(json.dumps(sites, indent=2))


def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _allocate_port() -> int:
    """사용하지 않는 포트 할당"""
    sites = _load_sites()
    used_ports = {s["port"] for s in sites.values() if "port" in s}

    for port in range(PORT_MIN, PORT_MAX + 1):
        if port not in used_ports and not _is_port_in_use(port):
            return port

    raise RuntimeError(f"사용 가능한 포트 없음 ({PORT_MIN}-{PORT_MAX})")


def _plist_label(project_id: str) -> str:
    slug = project_id.lower().replace(" ", "-")
    return f"{PLIST_PREFIX}{slug}"


def _plist_path(project_id: str) -> Path:
    return PLIST_DIR / f"{_plist_label(project_id)}.plist"


# ── 옛 인스턴스 정리 (2026-10-24 ox-plan 실측: 재빌드 뒤 옛 next-server 가 남아 몇 분간 옛 화면을 줬다) ──
# launchd 잡의 메인 프로세스는 `npx`(npm exec)이고 실제 서버(next-server 등)는 그 자식이다. kickstart -k·bootout 은
# 메인만 확실히 끝내고, 자식 서버는 SIGTERM 을 받아 **리스너만 닫은 채**(LISTEN 없음·ppid 1) cloudflared 가 물고 있던
# keep-alive 연결로 옛 빌드를 계속 응답한다. 그래서 재시작 전에 옛 프로세스를 적어 두고, 새 인스턴스가 포트를 잡은 뒤 옛 것을 끝낸다.

# macOS 기본 PATH(launchd·데몬)엔 /usr/sbin 이 없어 `lsof` 가 안 잡힌다 — 절대 경로로 고정
LSOF = shutil.which("lsof") or "/usr/sbin/lsof"


def _job_main_pid(label: str):
    """실행 중인 launchd 잡의 메인 pid (없으면 None)"""
    try:
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    m = re.search(r"^\s*pid = (\d+)", r.stdout, re.M)
    return int(m.group(1)) if m else None


def _process_tree(root: int) -> set:
    """root 와 그 자손 pid 전부 (ps 한 번)"""
    try:
        r = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=10)
    except Exception:
        return {root}
    children: dict = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        children.setdefault(int(parts[1]), []).append(int(parts[0]))
    out, stack = set(), [root]
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.add(pid)
        stack.extend(children.get(pid, []))
    return out


def _listening_pids(port: int) -> set:
    """이 포트를 LISTEN 하는 pid 들"""
    try:
        r = subprocess.run([LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True, timeout=10)
    except Exception:
        return set()
    return {int(x) for x in r.stdout.split() if x.isdigit()}


def _site_pids_by_cwd(project_dir: str) -> set:
    """작업 디렉터리가 project_dir 인 next-server 프로세스 — 이전 재시작이 남긴 고아(잡 트리 밖, ppid 1)를 잡는다.
    ps 제목이 `next-server` 인 것만 고른다(같은 폴더의 에이전트 셸·dev 서버·빌드를 건드리지 않으려고). lsof 의 COMMAND 는
    실행 파일명(`node`)이라 제목으로 못 거르므로, 후보 pid 를 ps 로 먼저 고르고 그 pid 들의 cwd 만 lsof 로 본다."""
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return set()
    cands = []
    for line in ps.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].startswith("next-server"):
            cands.append(int(parts[0]))
    if not cands:
        return set()
    try:
        r = subprocess.run([LSOF, "-a", "-p", ",".join(map(str, cands)), "-d", "cwd", "-Fpn"], capture_output=True, text=True, timeout=20)
    except Exception:
        return set()
    target = os.path.realpath(project_dir)
    out, pid = set(), None
    for line in r.stdout.splitlines():
        if line[:1] == "p":
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line[:1] == "n" and pid:
            try:
                if os.path.realpath(line[1:]) == target:
                    out.add(pid)
            except Exception:
                pass
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_processes(pids, grace_sec: float = 5.0) -> int:
    """SIGTERM → grace → SIGKILL. 돌려주는 값은 신호를 보낸 프로세스 수."""
    targets = [p for p in pids if p > 1 and p != os.getpid() and _alive(p)]
    for p in targets:
        try:
            os.kill(p, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + grace_sec
    while time.time() < deadline and any(_alive(p) for p in targets):
        time.sleep(0.2)
    for p in targets:
        if _alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except Exception:
                pass
    return len(targets)


def _snapshot_site_pids(label: str, port: int, project_dir: str) -> set:
    """재시작 직전, 옛 인스턴스로 볼 pid 들 = 잡 프로세스 트리 + 포트 리스너 + 같은 폴더의 next-server 고아"""
    pids = set()
    main = _job_main_pid(label)
    if main:
        pids |= _process_tree(main)
    pids |= _listening_pids(port)
    pids |= _site_pids_by_cwd(project_dir)
    pids.discard(os.getpid())
    return pids


def _reap_old_instance(label: str, port: int, old_pids: set, wait_sec: float = 30.0) -> dict:
    """새 인스턴스가 포트를 잡을 때까지(최대 wait_sec) 기다린 뒤 옛 pid 를 끝낸다.
    새 인스턴스가 안 떠도 옛 것은 끝낸다 — 리스너를 닫은 옛 서버는 어차피 새 요청을 못 받고, 옛 서버가 포트를 쥐고 있던 경우라면
    끝내야 KeepAlive 가 새 인스턴스를 띄울 수 있다."""
    new_main, new_tree = None, set()
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        new_main = _job_main_pid(label)
        if new_main and new_main not in old_pids:
            new_tree = _process_tree(new_main)
            if _listening_pids(port) & new_tree:
                break
        time.sleep(0.5)
    stale = {p for p in old_pids if p not in new_tree}
    reaped = _kill_processes(stale)
    listening = bool(_listening_pids(port) & new_tree) if new_tree else False
    if reaped:
        logger.info(f"[site_manager] {label}: reaped {reaped} old process(es) after restart (new pid {new_main}, listening={listening})")
    if not listening:
        logger.warning(f"[site_manager] {label}: new instance is not listening on :{port} yet — check launchd logs")
    return {"new_pid": new_main if new_main not in old_pids else None, "listening": listening, "reaped": reaped}


def _find_npm_or_npx() -> str:
    """npm/npx 경로 찾기 (Homebrew node)"""
    for cmd in ["npx", "/opt/homebrew/bin/npx", "/usr/local/bin/npx"]:
        if shutil.which(cmd):
            return shutil.which(cmd)
    return "npx"


def _ensure_serve_bin() -> list[str]:
    """정적 사이트 서버(`serve`) 실행 명령. 가능하면 설치된 바이너리를 직접 쓴다.

    예전에는 plist 에 `npx serve` 를 그대로 박았다. npx 는 **기동할 때마다** 패키지를
    확인하느라 npm 레지스트리에 접속하는데, launchd 는 KeepAlive 로 죽으면 다시 띄운다.
    네트워크가 흔들려 기동이 실패하는 구간에서는 이 둘이 맞물려 재시작 폭주가 되고,
    맥의 임시 포트를 계속 태운다(2026-09-20 고객 맥 포트 고갈 사고의 용의선상).
    설치된 바이너리를 쓰면 기동에 네트워크가 아예 필요 없다."""
    for cand in ("serve", "/opt/homebrew/bin/serve", "/usr/local/bin/serve"):
        found = shutil.which(cand)
        if found:
            return [found]
    # 없으면 퍼블리싱 시점에 한 번만 설치한다 (서비스 기동 경로에서는 설치하지 않는다)
    try:
        subprocess.run(["npm", "install", "-g", "serve"], capture_output=True, timeout=180)
        found = shutil.which("serve") or shutil.which("/opt/homebrew/bin/serve")
        if found:
            return [found]
    except Exception:  # noqa: BLE001
        pass
    return [_find_npm_or_npx(), "serve"]   # 최후 폴백 — 기존 동작


def _detect_framework(project_dir: str) -> str:
    """프로젝트 프레임워크 감지"""
    pkg_path = Path(project_dir) / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs"
            if "vite" in deps:
                return "vite"
        except Exception:
            pass

    # 정적 사이트 (index.html만 있는 경우)
    if (Path(project_dir) / "index.html").exists():
        return "static"

    return "unknown"


def _build_project(project_dir: str, framework: str) -> bool:
    """프로젝트 빌드"""
    npx = _find_npm_or_npx()
    env = {**os.environ, "NODE_ENV": "production"}

    if framework == "nextjs":
        # npm install + next build
        logger.info(f"[site_manager] Building Next.js: {project_dir}")
        result = subprocess.run(
            ["npm", "install", "--production=false"],
            cwd=project_dir, env=env, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            logger.error(f"[site_manager] npm install failed: {result.stderr[:500]}")
            return False

        result = subprocess.run(
            [npx, "next", "build"],
            cwd=project_dir, env=env, capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            logger.error(f"[site_manager] next build failed: {result.stderr[:500]}")
            return False
        return True

    elif framework == "vite":
        logger.info(f"[site_manager] Building Vite: {project_dir}")
        result = subprocess.run(
            ["npm", "install"], cwd=project_dir, env=env,
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            return False
        result = subprocess.run(
            [npx, "vite", "build"], cwd=project_dir, env=env,
            capture_output=True, text=True, timeout=300
        )
        return result.returncode == 0

    elif framework == "static":
        return True  # 빌드 불필요

    logger.error(f"[site_manager] Unknown framework: {framework}")
    return False


def _create_launchd_plist(project_id: str, project_dir: str, port: int, framework: str):
    """launchd plist 생성 및 로드"""
    label = _plist_label(project_id)
    log_dir = SITES_DIR / project_id / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    npx = _find_npm_or_npx()

    if framework == "nextjs":
        program_args = [npx, "next", "start", "-p", str(port)]
    elif framework == "vite":
        program_args = [npx, "vite", "preview", "--port", str(port), "--host"]
    elif framework == "static":
        program_args = _ensure_serve_bin() + ["-l", str(port), "-s", "."]
    else:
        program_args = [npx, "next", "start", "-p", str(port)]

    # .env.local 읽어서 환경변수에 포함
    env_vars = {"NODE_ENV": "production", "PORT": str(port)}
    env_path = Path(project_dir) / ".env.local"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()

    # PATH 포함
    env_vars["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

    plist = {
        "Label": label,
        "ProgramArguments": program_args,
        "WorkingDirectory": project_dir,
        "EnvironmentVariables": env_vars,
        "RunAtLoad": True,
        "KeepAlive": True,
        # 기동이 계속 실패하는 사이트가 10초마다 되살아나면 재시작 폭주가 된다.
        # 30초면 사람이 체감하는 복구 속도는 그대로면서 폭주는 못 만든다.
        "ThrottleInterval": 30,
        "StandardOutPath": str(log_dir / "stdout.log"),
        "StandardErrorPath": str(log_dir / "stderr.log"),
    }

    plist_path = _plist_path(project_id)

    # 재퍼블리시면 옛 인스턴스(잡 트리·포트 리스너·같은 폴더 고아)를 적어 둔다 — bootout 은 메인 프로세스만 확실히 끝낸다
    old_pids = _snapshot_site_pids(label, port, project_dir)

    # 기존 plist가 있으면 먼저 언로드
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True
        )

    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True
    )
    logger.info(f"[site_manager] launchd loaded: {label} on port {port}")
    return _reap_old_instance(label, port, old_pids)


def _unload_launchd(project_id: str, port: int = None, project_dir: str = None):
    """launchd plist 언로드 및 삭제 (+ 남은 서버 프로세스 정리 — 언퍼블리시 뒤에도 옛 서버가 keep-alive 로 응답하지 않게)"""
    plist_path = _plist_path(project_id)
    label = _plist_label(project_id)
    old_pids = set()
    main = _job_main_pid(label)
    if main:
        old_pids |= _process_tree(main)
    if port:
        old_pids |= _listening_pids(port)
    if project_dir:
        old_pids |= _site_pids_by_cwd(project_dir)
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True
        )
        plist_path.unlink()
        logger.info(f"[site_manager] launchd unloaded: {label}")
    reaped = _kill_processes(old_pids)
    if reaped:
        logger.info(f"[site_manager] {label}: reaped {reaped} leftover process(es) after unload")


def publish(project_id: str, project_dir: str, username: str = None) -> dict:
    """
    프로젝트를 로컬에서 빌드 & 서빙 + Cloudflare 라우팅 등록.
    Returns: { url, port, hostname } on success, { error } on failure.
    """
    username = _resolve_username(username)

    # 프레임워크 감지
    framework = _detect_framework(project_dir)
    if framework == "unknown":
        return {"error": f"지원하지 않는 프로젝트 형식: {project_dir}"}

    # 기존 사이트 확인
    sites = _load_sites()
    existing = sites.get(project_id)

    if existing and existing.get("status") == "running":
        port = existing["port"]
    else:
        port = _allocate_port()

    # 빌드
    if not _build_project(project_dir, framework):
        return {"error": "빌드 실패. 로그를 확인하세요."}

    # launchd 서비스 시작
    _create_launchd_plist(project_id, project_dir, port, framework)

    # 서버 API로 Cloudflare 라우팅 등록
    tunnel_id = config.get("cloudflare_tunnel_id", "")
    api_key = config.get("api_key", "")
    hostname = f"{username}-{project_id}".lower().replace(" ", "-")
    hostname = "".join(c for c in hostname if c.isalnum() or c == "-")
    full_hostname = f"{hostname}.{SITE_DOMAIN}"

    route_result = None
    if tunnel_id and api_key:
        route_result = api_request(
            api_key, "POST", "/api/tunnel/add-route",
            body={
                "username": username,
                "project": project_id,
                "port": port,
                "tunnelId": tunnel_id,
            }
        )
        if not route_result:
            logger.warning("[site_manager] Cloudflare 라우팅 등록 실패 — 로컬 서빙만 동작")

    url = f"https://{full_hostname}"
    if route_result and route_result.get("url"):
        url = route_result["url"]

    # 상태 저장
    sites[project_id] = {
        "port": port,
        "project_dir": project_dir,
        "framework": framework,
        "hostname": full_hostname,
        "url": url,
        "status": "running",
        "username": username,
    }
    _save_sites(sites)

    logger.info(f"[site_manager] Published {project_id} → {url} (:{port})")
    return {"url": url, "port": port, "hostname": full_hostname}


def unpublish(project_id: str, username: str = None) -> dict:
    """사이트 중지 + Cloudflare 라우팅 제거"""
    username = _resolve_username(username)

    sites = _load_sites()
    site = sites.get(project_id)
    if not site:
        return {"error": f"퍼블리시된 사이트 없음: {project_id}"}

    # launchd 언로드 (+ 남은 서버 프로세스 정리)
    _unload_launchd(project_id, port=site.get("port"), project_dir=site.get("project_dir"))

    # Cloudflare 라우팅 제거
    tunnel_id = config.get("cloudflare_tunnel_id", "")
    api_key = config.get("api_key", "")
    if tunnel_id and api_key:
        api_request(
            api_key, "DELETE", "/api/tunnel/remove-route",
            body={
                "username": username,
                "project": project_id,
                "tunnelId": tunnel_id,
            }
        )

    site["status"] = "stopped"
    _save_sites(sites)

    logger.info(f"[site_manager] Unpublished {project_id}")
    return {"ok": True, "message": f"{project_id} 언퍼블리시 완료"}


def rebuild(project_id: str) -> dict:
    """코드 변경 후 재빌드 & 재시작"""
    sites = _load_sites()
    site = sites.get(project_id)
    if not site:
        return {"error": f"퍼블리시된 사이트 없음: {project_id}"}

    project_dir = site["project_dir"]
    framework = site["framework"]
    port = site["port"]

    # 재빌드
    if not _build_project(project_dir, framework):
        return {"error": "빌드 실패"}

    # launchd 재시작 — kickstart -k 는 메인(npx)만 끝내므로, 옛 인스턴스를 적어 뒀다가 새 인스턴스가 포트를 잡은 뒤 정리한다
    label = _plist_label(project_id)
    old_pids = _snapshot_site_pids(label, port, project_dir)
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
        capture_output=True
    )
    restart = _reap_old_instance(label, port, old_pids)

    logger.info(f"[site_manager] Rebuilt {project_id} on :{port} (new pid {restart['new_pid']}, reaped {restart['reaped']})")
    return {"ok": True, "port": port, "url": site.get("url", ""), "restart": restart}


def status() -> list:
    """모든 퍼블리시된 사이트 상태"""
    sites = _load_sites()
    result = []
    for pid, site in sites.items():
        port = site.get("port", 0)
        is_running = _is_port_in_use(port) if port else False
        result.append({
            "project_id": pid,
            "port": port,
            "url": site.get("url", ""),
            "hostname": site.get("hostname", ""),
            "framework": site.get("framework", ""),
            "status": "running" if is_running else "stopped",
        })
    return result


# ─── Home Portal ──────────────────────────────────────

HOME_PORTAL_PORT = 3000
HOME_PORTAL_LABEL = "com.petervoice.home-portal"


def start_home_portal(username: str = None) -> dict:
    """홈 포탈 웹서버 시작 + Cloudflare 라우팅 등록.
    username: 유저 고유 아이디 (DB username). 터널 hostname에 사용."""
    username = _resolve_username(username)
    # username은 이미 [a-z0-9_] 형식이지만 안전하게 정리
    username_slug = username.lower().replace(" ", "-").replace("_", "-")
    username_slug = "".join(c for c in username_slug if c.isalnum() or c == "-")

    # home-portal.js 경로
    portal_script = Path(__file__).resolve().parent.parent / "home-portal.js"
    if not portal_script.exists():
        return {"error": f"home-portal.js not found: {portal_script}"}

    node_path = shutil.which("node") or "/opt/homebrew/bin/node"

    # launchd plist 생성
    log_dir = SITES_DIR / "_home-portal" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": HOME_PORTAL_LABEL,
        "ProgramArguments": [node_path, str(portal_script), "--port", str(HOME_PORTAL_PORT)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        "StandardOutPath": str(log_dir / "stdout.log"),
        "StandardErrorPath": str(log_dir / "stderr.log"),
    }

    plist_path = PLIST_DIR / f"{HOME_PORTAL_LABEL}.plist"

    # 기존 plist가 있으면 먼저 언로드
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True
        )

    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True
    )

    # Cloudflare 라우팅 등록
    tunnel_id = config.get("cloudflare_tunnel_id", "")
    api_key = config.get("api_key", "")
    hostname = f"{username_slug}.{SITE_DOMAIN}"

    if tunnel_id and api_key:
        api_request(
            api_key, "POST", "/api/tunnel/add-route",
            body={
                "username": username_slug,
                "project": "",  # 빈 프로젝트 = 홈 포탈
                "port": HOME_PORTAL_PORT,
                "tunnelId": tunnel_id,
            }
        )

    url = f"https://{hostname}"
    logger.info(f"[site_manager] Home portal started → {url} (:{HOME_PORTAL_PORT})")
    return {"url": url, "port": HOME_PORTAL_PORT, "hostname": hostname}


def stop_home_portal() -> dict:
    """홈 포탈 중지"""
    plist_path = PLIST_DIR / f"{HOME_PORTAL_LABEL}.plist"
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True
        )
        plist_path.unlink()

    logger.info("[site_manager] Home portal stopped")
    return {"ok": True, "message": "홈 포탈 중지 완료"}
