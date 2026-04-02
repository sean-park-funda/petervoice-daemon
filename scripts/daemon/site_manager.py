"""
로컬 퍼블리싱 매니저 — Next.js 프로젝트를 빌드하고 launchd로 서빙.
Cloudflare Tunnel 라우팅은 서버 API를 통해 처리.
"""

import json
import os
import plistlib
import socket
import subprocess
import shutil
from pathlib import Path

from daemon.globals import config, logger
from daemon.api import api_request

SITES_DIR = Path.home() / ".petervoice-sites"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PREFIX = "com.petervoice.site."
PORT_MIN = 3001
PORT_MAX = 3099
SITE_DOMAIN = "peter-voice.site"


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


def _find_npm_or_npx() -> str:
    """npm/npx 경로 찾기 (Homebrew node)"""
    for cmd in ["npx", "/opt/homebrew/bin/npx", "/usr/local/bin/npx"]:
        if shutil.which(cmd):
            return shutil.which(cmd)
    return "npx"


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
        program_args = [npx, "serve", "-l", str(port), "-s", "."]
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
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_dir / "stdout.log"),
        "StandardErrorPath": str(log_dir / "stderr.log"),
    }

    plist_path = _plist_path(project_id)

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


def _unload_launchd(project_id: str):
    """launchd plist 언로드 및 삭제"""
    plist_path = _plist_path(project_id)
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True
        )
        plist_path.unlink()
        logger.info(f"[site_manager] launchd unloaded: {_plist_label(project_id)}")


def publish(project_id: str, project_dir: str, username: str = None) -> dict:
    """
    프로젝트를 로컬에서 빌드 & 서빙 + Cloudflare 라우팅 등록.
    Returns: { url, port, hostname } on success, { error } on failure.
    """
    if not username:
        username = config.get("bot_name", "user")

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
    if not username:
        username = config.get("bot_name", "user")

    sites = _load_sites()
    site = sites.get(project_id)
    if not site:
        return {"error": f"퍼블리시된 사이트 없음: {project_id}"}

    # launchd 언로드
    _unload_launchd(project_id)

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

    # launchd 재시작
    label = _plist_label(project_id)
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
        capture_output=True
    )

    logger.info(f"[site_manager] Rebuilt {project_id} on :{port}")
    return {"ok": True, "port": port, "url": site.get("url", "")}


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
    """홈 포탈 웹서버 시작 + Cloudflare 라우팅 등록"""
    if not username:
        username = config.get("bot_name", "user")
    username_slug = username.lower().replace(" ", "-")

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
