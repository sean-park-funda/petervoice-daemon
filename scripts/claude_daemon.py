#!/usr/bin/env python3
"""
Claude Daemon — Peter Voice ↔ Claude Code CLI bridge
Single worker polling thread. Sessions are keyed by project:task.

This is a thin entry point. All logic lives in the daemon/ package.
"""

import json
import os
import sys
import signal

import daemon.globals as g
from daemon.globals import config, shutdown_event, logger
from daemon.config import setup_logging, load_config, acquire_pid_lock, release_pid_lock, cleanup_stale_state, ensure_default_projects
from daemon.sessions import load_sessions, load_codex_sessions, _process_pending_resets
from daemon.tasks import load_tasks
from daemon.prompts import ensure_template
from daemon.queue import load_queue
from daemon.supabase import resolve_user_id, check_force_restart, clear_force_restart, check_bot_response_exists
from daemon.worker import Worker
from daemon.health import SessionHealthChecker
from daemon.syncers.secrets import SecretsSyncer
from daemon.syncers.skills import SkillsSyncer

from daemon.syncers.auto_updater import AutoUpdater
from daemon.summon import SummonManager
from daemon.heartbeat import HeartbeatThread
from daemon.manager.thread import ManagerThread
from daemon.manager.http_server import start_manager_http_server
from daemon.api import api_request


def handle_signal(signum, frame):
    sig_name = signal.Signals(signum).name
    logger.info(f"Received {sig_name}, shutting down...")
    shutdown_event.set()


def _recover_after_restart(worker):
    """재시작 후 복구: 트리거 프로젝트에 완료 알림, 중단된 작업 재처리."""
    from daemon.utils import _read_json
    from daemon.api import api_request

    api_key = config.get("api_key", "")
    notified_projects = set()

    # 1. 재시작 트리거 확인 (어떤 프로젝트가 재시작을 지시했는지)
    trigger = _read_json(g.RESTART_TRIGGER_PATH, {})
    if trigger:
        try:
            g.RESTART_TRIGGER_PATH.unlink()
        except OSError:
            pass
        trigger_project = trigger.get("project")
        if trigger_project:
            api_request(api_key, "POST", "/api/bot/reply", {
                "text": "데몬 재시작 완료.",
                "project": trigger_project,
                "is_final": True,
            })
            notified_projects.add(trigger_project)
            logger.info(f"[recovery] Notified restart trigger project: {trigger_project}")

    # 2. 중단된 메시지 확인 및 재처리
    pending_queue = load_queue()
    if pending_queue:
        logger.info(f"Recovering {len(pending_queue)} queued message(s) from previous run")
        for msg in pending_queue:
            msg_id = msg.get("id")
            project = msg.get("project", "unknown")

            # 이미 응답이 전달됐는지 확인
            has_response = check_bot_response_exists(msg_id)

            if has_response:
                # 응답이 이미 있으면 알림만
                if project not in notified_projects:
                    api_request(api_key, "POST", "/api/bot/reply", {
                        "text": "데몬이 재시작됐습니다. 이전 응답은 정상 전달됐습니다.",
                        "project": project,
                        "is_final": True,
                    })
                    notified_projects.add(project)
                    logger.info(f"[recovery] {project}: response already delivered, notified user")
                from daemon.queue import dequeue_message
                dequeue_message(msg_id)
            else:
                # 응답이 없으면 재처리
                if project not in notified_projects:
                    api_request(api_key, "POST", "/api/bot/reply", {
                        "text": "데몬이 재시작됐습니다. 이전 작업을 다시 처리합니다.",
                        "project": project,
                        "is_final": True,
                    })
                    notified_projects.add(project)
                    logger.info(f"[recovery] {project}: response missing, reprocessing")
                with worker._spawned_lock:
                    worker._spawned_ids.add(msg_id)
                worker._executor.submit(worker._process_message_safe, msg)


def _find_cloudflared() -> str | None:
    """cloudflared 바이너리 경로 반환. 없으면 None."""
    import shutil
    for p in ["cloudflared", "/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"]:
        found = shutil.which(p)
        if found:
            return found
        if os.path.isfile(p):
            return p
    return None


def _ensure_cloudflared() -> str | None:
    """cloudflared가 없으면 자동 설치. 바이너리 경로 반환, 실패 시 None."""
    import shutil, subprocess, platform

    path = _find_cloudflared()
    if path:
        return path

    logger.info("[tunnel] cloudflared not found, installing...")
    try:
        if platform.system() == "Darwin":
            brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
            result = subprocess.run(
                [brew, "install", "cloudflared"],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                logger.info("[tunnel] cloudflared installed via brew")
                return _find_cloudflared()
            # brew 실패 시 직접 다운로드
            logger.warning("[tunnel] brew install failed, trying direct download")
            arch = "arm64" if platform.machine() == "arm64" else "amd64"
            url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-{arch}.tgz"
            subprocess.run(
                ["curl", "-sL", url, "-o", "/tmp/cloudflared.tgz"],
                timeout=120, check=True
            )
            subprocess.run(
                ["tar", "-xzf", "/tmp/cloudflared.tgz", "-C", "/usr/local/bin/"],
                timeout=30, check=True
            )
            logger.info("[tunnel] cloudflared installed via direct download")
            return _find_cloudflared() or "/usr/local/bin/cloudflared"
        else:
            logger.error("[tunnel] Auto-install only supported on macOS")
            return None
    except Exception as e:
        logger.error(f"[tunnel] Failed to install cloudflared: {e}")
        return None


def _save_config_fields(**fields):
    """config.json에 필드를 추가/업데이트하고 메모리에도 반영."""
    from daemon.globals import CONFIG_PATH
    with open(CONFIG_PATH) as f:
        data = json.load(f)
    data.update(fields)
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    config.update(fields)


def _fetch_tunnel_token_from_cf(tunnel_id: str) -> str | None:
    """Cloudflare API에서 기존 터널의 토큰을 직접 가져온다."""
    import urllib.request
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not cf_token:
        return None
    try:
        # 계정 ID 조회
        req = urllib.request.Request(
            "https://api.cloudflare.com/client/v4/accounts",
            headers={"Authorization": f"Bearer {cf_token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            accounts = json.loads(resp.read())
        account_id = accounts.get("result", [{}])[0].get("id")
        if not account_id:
            return None
        # 터널 토큰 조회
        req = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token",
            headers={"Authorization": f"Bearer {cf_token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("success"):
            return data["result"]
    except Exception as e:
        logger.warning(f"[tunnel] Failed to fetch token from Cloudflare API: {e}")
    return None


def _ensure_tunnel(api_key: str, username: str, cloudflared_path: str) -> str | None:
    """Cloudflare Tunnel이 설정되어 있는지 확인하고, 없으면 자동 생성.
    tunnel_id를 반환. 실패 시 None."""
    import subprocess
    from pathlib import Path

    tunnel_id = config.get("cloudflare_tunnel_id", "")
    tunnel_token = config.get("cloudflare_tunnel_token", "")

    # --- 1. 터널이 없으면 서버 API로 생성 ---
    if not tunnel_id or not tunnel_token:
        # 터널 ID는 있지만 토큰만 없는 경우: Cloudflare API에서 직접 가져오기
        if tunnel_id and not tunnel_token:
            tunnel_token = _fetch_tunnel_token_from_cf(tunnel_id)
            if tunnel_token:
                _save_config_fields(cloudflare_tunnel_token=tunnel_token)
                logger.info(f"[tunnel] Token recovered from Cloudflare API for {tunnel_id[:8]}...")

        if not tunnel_id or not tunnel_token:
            logger.info(f"[tunnel] No tunnel configured, creating via API for user '{username}'...")
            result = api_request(api_key, "POST", "/api/tunnel/create", body={"username": username})
            if not result or not result.get("tunnelId"):
                logger.error(f"[tunnel] Failed to create tunnel: {result}")
                return None
            tunnel_id = result["tunnelId"]
            tunnel_token = result["tunnelToken"]
            _save_config_fields(
                cloudflare_tunnel_id=tunnel_id,
                cloudflare_tunnel_token=tunnel_token,
            )
            logger.info(f"[tunnel] Created tunnel: pv-{username} ({tunnel_id[:8]}...)")

    # --- 2. cloudflared 프로세스 확인/시작 ---
    cf_running = False
    try:
        result = subprocess.run(
            ["pgrep", "-f", "cloudflared.*tunnel.*run"],
            capture_output=True, text=True
        )
        cf_running = result.returncode == 0
    except Exception:
        pass

    if not cf_running:
        logger.info("[tunnel] cloudflared not running, starting as launchd service...")
        plist_label = "com.cloudflare.cloudflared"
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{plist_label}.plist"

        import plistlib
        plist = {
            "Label": plist_label,
            "ProgramArguments": [
                cloudflared_path,
                "tunnel", "--no-autoupdate", "--protocol", "http2",
                "run", "--token", tunnel_token,
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "StandardOutPath": str(Path.home() / ".claude-daemon" / "cloudflared-stdout.log"),
            "StandardErrorPath": str(Path.home() / ".claude-daemon" / "cloudflared-stderr.log"),
        }

        # 기존 plist 언로드
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
        logger.info(f"[tunnel] cloudflared launchd service started")
    else:
        logger.info("[tunnel] cloudflared already running")

    return tunnel_id


def _ensure_dns_route(api_key: str, username: str, tunnel_id: str):
    """Home Portal용 DNS + ingress 라우트가 등록되었는지 확인/생성."""
    username_slug = username.lower().replace("_", "-")
    username_slug = "".join(c for c in username_slug if c.isalnum() or c == "-")
    hostname = f"{username_slug}.peter-voice.site"

    # 서버 API로 DNS + ingress 등록 (멱등 — 이미 있으면 업데이트)
    result = api_request(api_key, "POST", "/api/tunnel/add-route", body={
        "username": username_slug,
        "project": "",  # 빈 프로젝트 = Home Portal
        "port": 3000,
        "tunnelId": tunnel_id,
    })
    if result and result.get("url"):
        logger.info(f"[tunnel] DNS route ensured: {result['url']}")
    else:
        logger.warning(f"[tunnel] DNS route registration result: {result}")

    return f"https://{hostname}"


# --- cloudflared 좀비 터널 자가치유 상태 (모듈 전역) ---
_tunnel_unreachable_streak = 0          # 연속 외부 도달 실패 횟수
_tunnel_kick_times: list[float] = []    # 최근 kickstart 타임스탬프(time.time), backoff용
_TUNNEL_FAIL_THRESHOLD = 2              # 연속 N회 실패 시 조치(플래핑 방지)
_TUNNEL_KICK_MAX_PER_HOUR = 2           # 시간당 최대 kickstart 횟수


def _tunnel_url_from_config() -> str | None:
    """config의 username으로 자기 Home Portal 터널 URL을 계산.

    _ensure_dns_route()의 슬러그 규칙과 동일해야 함.
    """
    username = config.get("username")
    if not username:
        return None
    slug = username.lower().replace("_", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    if not slug:
        return None
    return f"https://{slug}.peter-voice.site/"


def _http_status(url: str, timeout: float = 8.0) -> tuple[int | None, str]:
    """URL에 HEAD 요청(실패 시 GET 폴백). (status_code, note) 반환.

    status_code=None 이면 연결 실패/timeout (엣지 미도달).
    Cloudflare 엣지는 터널이 죽어도 응답은 하며 상태코드 530/1033 등으로 알림.
    """
    import urllib.request
    import urllib.error

    def _try(method: str):
        req = urllib.request.Request(url, method=method)
        req.add_header("User-Agent", "petervoice-daemon-healthcheck/1")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, method

    for method in ("HEAD", "GET"):
        try:
            status, m = _try(method)
            return status, f"{m} {status}"
        except urllib.error.HTTPError as e:
            # 4xx/5xx도 엣지가 응답한 것 — status는 유효
            return e.code, f"{method} HTTP {e.code}"
        except urllib.error.URLError as e:
            # HEAD가 막히면 GET 재시도, 그 외엔 연결 실패로 처리
            if method == "HEAD":
                continue
            return None, f"URLError {e.reason}"
        except Exception as e:  # noqa: BLE001
            if method == "HEAD":
                continue
            return None, f"{type(e).__name__}: {e}"
    return None, "unreachable"


def _tunnel_reachable(status: int | None) -> bool:
    """엣지가 터널에 도달했는지 판정.

    정상: 2xx/3xx/401/403 등 실제 오리진 응답(홈포탈 인증요구 401 포함).
    비정상: 530/1033류(Cloudflare 터널 미연결), 502/504, 또는 연결 실패(None).
    """
    if status is None:
        return False
    # Cloudflare 터널/오리진 다운 시그널
    if status in (502, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530):
        return False
    # 그 외 HTTP 응답은 엣지→오리진 경로가 살아있다는 뜻(4xx 인증/권한 포함)
    return True


def _kickstart_cloudflared() -> bool:
    """launchctl kickstart로 cloudflared(우리 label만) 재시작. 성공 여부 반환."""
    import subprocess
    uid = os.getuid()
    target = f"gui/{uid}/com.cloudflare.cloudflared"
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return True
        logger.warning(f"[tunnel] kickstart failed rc={result.returncode}: {result.stderr.strip()}")
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[tunnel] kickstart error: {e}")
        return False


def _check_cloudflared_health():
    """2단계 헬스체크로 cloudflared 좀비 터널을 감지·자가치유.

    1) pgrep: 프로세스가 죽었으면 _ensure_home_portal()로 복구(기존 동작).
    2) 프로세스는 살아있지만 엣지 미연결('좀비')이면 launchctl kickstart로 재시작.
       - 연속 실패 임계치/시간당 backoff로 플래핑·무한루프 방지.
    """
    global _tunnel_unreachable_streak

    if not config.get("home_portal_enabled", True):
        return
    if sys.platform != "darwin":  # macOS 전용 (Windows 등 제외)
        return

    import subprocess

    # --- 1단계: 프로세스 생존 ---
    try:
        result = subprocess.run(
            ["pgrep", "-f", "cloudflared.*tunnel.*run"],
            capture_output=True, text=True
        )
    except Exception:
        return
    if result.returncode != 0:
        logger.warning("[tunnel] cloudflared not running, recovering...")
        _tunnel_unreachable_streak = 0
        _ensure_home_portal()
        return

    # --- 2단계: 외부 도달성(좀비 감지) ---
    tunnel_url = _tunnel_url_from_config()
    if not tunnel_url:
        return  # URL 미상 → 도달성 체크 스킵

    status, note = _http_status(tunnel_url)
    if _tunnel_reachable(status):
        if _tunnel_unreachable_streak:
            logger.info(f"[tunnel] external reachability recovered ({note})")
        _tunnel_unreachable_streak = 0
        return

    # 외부 실패 — 로컬 홈포탈(3000) 다운과 구분
    local_status, _ = _http_status("http://127.0.0.1:3000/", timeout=4.0)
    if local_status is None:
        # 홈포탈 자체가 안 뜸 → 터널 문제 아님. 프로세스 복구 경로로.
        logger.warning(
            f"[tunnel] external failed ({note}) but localhost:3000 down "
            f"— home portal issue, not tunnel; running _ensure_home_portal()"
        )
        _tunnel_unreachable_streak = 0
        _ensure_home_portal()
        return

    _tunnel_unreachable_streak += 1
    logger.warning(
        f"[tunnel] external reachability failed "
        f"({_tunnel_unreachable_streak} consecutive): {note} (local 3000 OK)"
    )
    if _tunnel_unreachable_streak < _TUNNEL_FAIL_THRESHOLD:
        return  # 아직 임계치 미만 — 일시적 드롭 오탐 방지

    # --- backoff: 시간당 최대 N회 ---
    import time
    now = time.time()
    _tunnel_kick_times[:] = [t for t in _tunnel_kick_times if now - t < 3600]
    if len(_tunnel_kick_times) >= _TUNNEL_KICK_MAX_PER_HOUR:
        logger.warning(
            f"[tunnel] zombie detected but kickstart backoff active "
            f"({len(_tunnel_kick_times)}/{_TUNNEL_KICK_MAX_PER_HOUR} this hour) — skipping"
        )
        return

    logger.warning(
        f"[tunnel] zombie tunnel confirmed ({note}) -> kickstarting cloudflared"
    )
    if _kickstart_cloudflared():
        _tunnel_kick_times.append(now)
        _tunnel_unreachable_streak = 0
        logger.info("[tunnel] cloudflared kickstarted; reachability will re-check next cycle")
    else:
        # kickstart 실패 시 전체 프로비저닝 경로로 폴백
        logger.warning("[tunnel] kickstart failed, falling back to _ensure_home_portal()")
        _ensure_home_portal()
        _tunnel_unreachable_streak = 0


def _ensure_home_portal():
    """Home Portal + Cloudflare Tunnel 전체 자동 프로비저닝.

    1. cloudflared 설치 확인/자동 설치
    2. 터널 없으면 서버 API로 자동 생성 + config 저장
    3. cloudflared 서비스 실행 확인/시작
    4. Home Portal 웹서버 실행 확인/시작
    5. DNS + ingress 라우트 등록
    6. tunnel_url 서버에 등록
    """
    if not config.get("home_portal_enabled", True):
        logger.info("[home-portal] Disabled via config")
        return

    api_key = config.get("api_key", "")
    if not api_key:
        return

    try:
        # 1. cloudflared 설치 확인
        cloudflared_path = _ensure_cloudflared()
        if not cloudflared_path:
            logger.error("[home-portal] cloudflared required but installation failed")
            return

        # 2. username 조회
        me = api_request(api_key, "GET", "/api/bot/me")
        if not me or not me.get("username"):
            logger.warning("[home-portal] Could not resolve username from /api/bot/me")
            return
        username = me["username"]

        # 3. 터널 확인/생성 + cloudflared 서비스 시작
        tunnel_id = _ensure_tunnel(api_key, username, cloudflared_path)
        if not tunnel_id:
            logger.error("[home-portal] Tunnel setup failed")
            return

        # 4. Home Portal launchd 시작 (이미 실행 중이면 스킵)
        import subprocess
        from pathlib import Path
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.petervoice.home-portal.plist"
        portal_running = False
        if plist_path.exists():
            try:
                result = subprocess.run(
                    ["launchctl", "list", "com.petervoice.home-portal"],
                    capture_output=True, text=True
                )
                portal_running = result.returncode == 0
            except Exception:
                pass

        if not portal_running:
            from daemon.site_manager import start_home_portal
            result = start_home_portal(username=username)
            if result.get("error"):
                logger.error(f"[home-portal] Failed to start: {result['error']}")
                return
            logger.info(f"[home-portal] Started: {result.get('url')}")
        else:
            logger.info("[home-portal] Already running")

        # 5. DNS + ingress 라우트 등록
        tunnel_url = _ensure_dns_route(api_key, username, tunnel_id)

        # 6. tunnel_url 서버에 등록
        api_request(api_key, "PATCH", "/api/bot/status", body={"tunnel_url": tunnel_url})
        logger.info(f"[home-portal] Registered tunnel_url: {tunnel_url}")

    except Exception as e:
        logger.error(f"[home-portal] Error: {e}")


def _get_git_version():
    """Return short commit hash and date, or 'unknown'."""
    try:
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h %ai"],
            cwd=repo, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _sanitize_config():
    """Remove direct Supabase keys from customer configs.

    Agents must use PeterVoice API only — service_role keys bypass RLS
    and allow cross-user data access.
    """
    from daemon.globals import CONFIG_PATH
    remove_keys = ["supabase_url", "supabase_key"]
    removed = [k for k in remove_keys if k in config]
    if not removed:
        return
    for k in removed:
        del config[k]
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    logger.info(f"[security] Removed direct DB keys from config: {removed}")


def _migrate_manager_config():
    """Backfill a default manager block so /do works on every customer.

    /do delegates to the manager thread (g._manager_instance), which only
    starts when config.manager.enabled is true. Installers historically wrote
    configs without a manager block, so /do was inert for most customers.

    Idempotent: only adds the block when the "manager" key is ABSENT. Never
    touches an existing manager config — respects Sean's manual settings and
    any account explicitly provisioned with enabled:false (e.g. add-account).

    projects MUST be [] (empty list): _get_target_projects() treats an explicit
    empty list as "autonomous sweep disabled" (0 Claude calls when idle) while
    /do deep tasks still run. Omitting projects would fall back to the session
    sweep and start autonomous Claude calls for every customer.
    """
    from daemon.globals import CONFIG_PATH
    if "manager" in config:
        return
    config["manager"] = {
        "enabled": True,
        "interval_minutes": 60,
        "projects": [],
    }
    # Dump the full in-memory config (same pattern as _sanitize_config) so no
    # existing fields or customer-specific keys are lost.
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    logger.info("[migrate] Added default manager block (enabled, projects=[]) to config")


def main():
    setup_logging()
    logger.info("=" * 60)
    version = _get_git_version()
    logger.info(f"Claude Daemon starting... (version: {version})")

    cleanup_stale_state()

    pid_file = acquire_pid_lock()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        load_config()
        _sanitize_config()
        _migrate_manager_config()
        load_sessions()
        load_codex_sessions()
        load_tasks()
        ensure_template()

        if not config.get("api_key"):
            logger.error("No api_key configured in config.json")
            sys.exit(1)

        logger.info(f"API URL: {config['api_url']}")
        logger.info(f"Bot: {config.get('bot_name', '?')}")
        from daemon.globals import CLAUDE_CMD, CODEX_CMD
        logger.info(f"Claude CLI: {CLAUDE_CMD}")
        logger.info(f"Codex CLI: {CODEX_CMD}")

        uid = resolve_user_id()
        if uid:
            logger.info(f"User ID: {uid}")
        else:
            logger.warning("Could not resolve user_id from api_key — force_restart/project queries may fail")

        # Ensure default projects (sysadmin, manager) exist
        ensure_default_projects()

        # Initialize encryption key (creates if not exists)
        from daemon.encryption import get_encryptor
        get_encryptor()

        # Start syncer threads
        SecretsSyncer().start()
        SkillsSyncer().start()
        # DocsSyncer removed — docs are now served via Home Portal API directly

        AutoUpdater().start()
        SummonManager().start()

        worker = Worker()
        worker.start()

        # Session health checker
        session_health_config = config.get("session_health", {})
        if session_health_config.get("enabled", True):
            health_checker = SessionHealthChecker()
            if session_health_config.get("interval_hours"):
                health_checker.HEALTH_CHECK_INTERVAL = session_health_config["interval_hours"] * 3600
            health_checker.start()
            logger.info(f"Session health checker started (interval={health_checker.HEALTH_CHECK_INTERVAL // 3600}h)")

        # Heartbeat thread
        HeartbeatThread().start()
        logger.info("HeartbeatThread started")

        # Manager thread
        mgr_config = config.get("manager", {})
        if mgr_config.get("enabled", False):
            manager_thread = ManagerThread()
            g._manager_instance = manager_thread
            manager_thread.start()
            logger.info(f"Manager thread started (interval={mgr_config.get('interval_minutes', 60)}m)")
            try:
                start_manager_http_server(port=mgr_config.get("status_port", 7777))
            except Exception as e:
                logger.warning(f"Manager status API failed to start: {e}")

        # Recover after restart: notify users and reprocess pending messages
        _recover_after_restart(worker)

        # Ensure Home Portal is running + register tunnel URL
        _ensure_home_portal()

        # Main loop: watchdog + force_restart polling
        user_id = resolve_user_id()
        watchdog_tick = 0
        while not shutdown_event.is_set():
            shutdown_event.wait(1)
            if shutdown_event.is_set():
                break
            watchdog_tick += 1

            if watchdog_tick % 10 == 0:
                if not worker.is_alive():
                    logger.error("Worker thread died! Restarting daemon...")
                    g.restart_requested = True
                    shutdown_event.set()
                    break

            if watchdog_tick % 10 == 0:
                try:
                    _process_pending_resets()
                except Exception as e:
                    logger.warning(f"pending_resets check error: {e}")

            if watchdog_tick % 60 == 0:
                try:
                    _check_cloudflared_health()
                except Exception as e:
                    logger.warning(f"cloudflared health check error: {e}")

            if watchdog_tick % 30 == 0 and user_id is not None:
                try:
                    if check_force_restart(user_id):
                        logger.info("Force restart requested via web UI")
                        clear_force_restart(user_id)
                        g.restart_requested = True
                        shutdown_event.set()
                        break
                except Exception as e:
                    logger.warning(f"force_restart check error: {e}")

            # 설정 UI 재로그인 브리지 (5초 주기) — 채팅 플로우와 같은 엔진 공유
            if watchdog_tick % 5 == 0 and user_id is not None:
                try:
                    from daemon.relogin_settings import poll_relogin
                    poll_relogin(user_id)
                except Exception as e:
                    logger.warning(f"relogin poll error: {e}")

        worker._executor.shutdown(wait=False, cancel_futures=True)
        worker.join(timeout=5)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
        shutdown_event.set()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        if g.restart_requested:
            logger.info("Claude Daemon restarting (exit 1 for launchd)...")
        else:
            logger.info("Claude Daemon stopped")
        release_pid_lock(pid_file)
        if g.restart_requested:
            sys.exit(1)


if __name__ == "__main__":
    main()
