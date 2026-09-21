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
from daemon.autoreset import SessionAutoResetThread
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
                # 같은 세션을 --resume 하므로 완료된 작업의 반복(이중 부작용)을 막는 안내를 붙인다.
                # 큐에는 원본 msg가 남아있어(id 기준 dedupe) 프리픽스가 중첩되지 않는다.
                msg = dict(msg)
                msg["text"] = (
                    "[안내: 직전 턴이 데몬 재시작으로 중단되었습니다. "
                    "이미 완료된 작업은 반복하지 말고, 중단 지점부터 이어서 진행한 뒤 결과를 보고하세요.]\n\n"
                    + msg.get("text", "")
                )
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


# 연결기 launchd 라벨: 데몬 설치(_ensure_tunnel) / 온보딩 데몬 설치(portal_start)
_CLOUDFLARED_LABELS = ("com.cloudflare.cloudflared", "com.petervoice.cloudflared")


def _tunnel_id_from_token(token: str) -> str | None:
    """터널 토큰(base64 JSON {"a","t","s"})에서 터널 ID(t)를 꺼낸다. 실패 시 None."""
    import base64
    try:
        raw = token.replace("-", "+").replace("_", "/")  # URL-safe 인코딩도 허용
        data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
        return data.get("t") if isinstance(data, dict) else None
    except Exception:
        return None


def _cloudflared_args_target_tunnel(args: list[str], tunnel_id: str, tunnel_token: str) -> bool:
    """cloudflared 인자 목록이 이 터널(config 의 id/token)의 연결기인지."""
    if tunnel_token and tunnel_token in args:
        return True
    if tunnel_id and any(tunnel_id in a for a in args):
        return True
    for i, a in enumerate(args):
        tok = None
        if a == "--token" and i + 1 < len(args):
            tok = args[i + 1]
        elif a.startswith("--token="):
            tok = a.split("=", 1)[1]
        if tok and tunnel_id and _tunnel_id_from_token(tok) == tunnel_id:
            return True
    return False


def _tunnel_connector_running(tunnel_id: str, tunnel_token: str) -> bool:
    """이 터널의 cloudflared 연결기가 떠 있는지.

    `pgrep -f "cloudflared.*tunnel.*run"` 은 (1) 다른 터널의 연결기 (2) 그 문구를 인자로
    가진 아무 프로세스(프롬프트에 코드가 담긴 claude 세션 등)까지 잡아, 우리 터널에 연결기가
    없는데도 "already running" 으로 오판했다 (2026-09-17 user 94: 온보딩이 띄운 터널 A 연결기를
    보고 데몬이 만든 터널 B 를 방치 → 530). 실행 파일이 cloudflared 이고 인자가 이 터널을
    가리키는 프로세스만 센다. config 에 터널이 없으면 아무 cloudflared 연결기나 인정한다.
    """
    import subprocess
    try:
        r = subprocess.run(["ps", "-axww", "-o", "command="],
                           capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0
    except Exception:
        ok = False
    if not ok:  # ps 불가 — 예전 방식으로 (다른 터널·문구만 담은 프로세스도 잡는 부정확한 판별)
        logger.warning("[tunnel] ps failed, falling back to legacy pgrep connector check (imprecise)")
        try:
            return subprocess.run(["pgrep", "-f", "cloudflared.*tunnel.*run"],
                                  capture_output=True, text=True).returncode == 0
        except Exception:
            return False
    for line in r.stdout.splitlines():
        args = line.split()
        if not args or os.path.basename(args[0]) != "cloudflared":
            continue
        if "tunnel" not in args or "run" not in args:
            continue
        if not tunnel_id and not tunnel_token:
            return True
        if _cloudflared_args_target_tunnel(args, tunnel_id, tunnel_token):
            return True
    return False


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

    # --- 2. 이 터널의 cloudflared 연결기 확인/시작 ---
    cf_running = _tunnel_connector_running(tunnel_id, tunnel_token)

    if not cf_running:
        logger.info(f"[tunnel] cloudflared connector for {tunnel_id[:8]}... not running, starting as launchd service...")
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

# --- 로컬 자원 고갈 방어 (hoon 2026-07-16 포트고갈 사고) ---
# 포트 고갈(TIME_WAIT 폭증)로 프로브가 errno49(EADDRNOTAVAIL)로 실패하면, 이는
# 원격 터널/홈포탈 고장이 아니라 로컬 자원 고갈이다. 그런데 기존 헬스체크는 이를
# "홈포탈 다운"으로 오판하고 _ensure_home_portal()로 복구 → Cloudflare API 에 새
# 연결을 더 만들어 포트를 더 소진 → 악순환(자가치유가 기름을 붓는다).
# → 자원 고갈로 판별되면 복구를 트리거하지 않고 경고+지수백오프만 한다.
_RESOURCE_EXHAUSTION_MARKERS = (
    "can't assign requested address", "cannot assign requested address",
    "errno 49", "eaddrnotavail",
    "cannot allocate memory", "errno 12", "enomem",
    "too many open files", "errno 24", "emfile",
)
_tunnel_exhaustion_streak = 0            # 연속 자원고갈 감지 횟수
_EXHAUSTION_CHAT_AFTER = 3               # 이 횟수부터 고객 채팅에 안내(일시적 오탐 배제)
_EXHAUSTION_CHAT_COOLDOWN = 6 * 3600     # 같은 사고로 채팅을 도배하지 않는다
_last_exhaustion_chat_ts = 0.0           # 마지막 고객 안내 시각
_last_recover_ts = 0.0                   # 마지막 _ensure_home_portal 복구 시각
_recover_streak = 0                      # 연속 복구 시도 횟수 (지수 백오프용)
_RECOVER_BACKOFF_BASE = 20               # 백오프 시작 간격(초): 20 → 40 → 80 … 최대 300

# --- 서킷브레이커 (터널 불통이 장시간 지속 시 재시도 중단) ---
# 진짜 터널 불통(좀비/홈포탈다운, 자원고갈 제외)이 복구 없이 오래 지속되면, 60초마다
# 프로브+시간당 kickstart를 무한히 반복하는 건 자원 낭비이고 kickstart로 못 고치는
# 상황(엣지 장애·설정 손상 등 사람 개입 필요)일 가능성이 높다. 일정 시간(1h) 넘게
# 복구 실패가 이어지면 OPEN 으로 전환해 능동 조치를 멈추고 알림만 남긴다. 이후
# 주기적(30m)으로 half-open 프로브 1회만 시도해 스스로 회복됐는지 확인한다.
_CB_OPEN_AFTER = 3600                    # 연속 불통 이 시간(초) 넘으면 CLOSED→OPEN
_CB_HALFOPEN_EVERY = 1800               # OPEN 상태에서 half-open 프로브 간격(초)
_cb_state = "closed"                     # closed | open | half_open
_cb_fail_since = 0.0                     # 현재 연속 불통 시작 시각(건강 시 0)
_cb_opened_ts = 0.0                      # OPEN 진입 시각(로깅용)
_cb_last_probe_ts = 0.0                  # OPEN 상태 마지막 half-open 프로브 시각


def _looks_like_resource_exhaustion(note: str | None) -> bool:
    """프로브 실패 사유가 로컬 자원 고갈(포트/메모리/FD)인지 판별."""
    if not note:
        return False
    low = note.lower()
    return any(m in low for m in _RESOURCE_EXHAUSTION_MARKERS)


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

    ⚠️ 계약: 실패 시 note 에는 OSError 의 원문(예: "[Errno 49] Can't assign requested
    address")이 그대로 담긴다. _looks_like_resource_exhaustion() 이 이 문자열의 errno
    토큰으로 로컬 자원고갈을 판별하므로, note 포맷을 바꾸면 그 가드가 깨진다.
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


def _cloudflared_label() -> str:
    """이 터널의 연결기를 띄우는 launchd 라벨. 온보딩 설치분은 라벨이 달라
    com.cloudflare.cloudflared 만 kickstart 하면 "Could not find service" 로 복구가 실패했다.
    plist 인자가 이 터널을 가리키는 라벨을 고르고, 없으면 데몬 기본 라벨."""
    import plistlib
    from pathlib import Path
    tunnel_id = config.get("cloudflare_tunnel_id", "")
    tunnel_token = config.get("cloudflare_tunnel_token", "")
    if tunnel_id or tunnel_token:
        for label in _CLOUDFLARED_LABELS:
            path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            try:
                with open(path, "rb") as f:
                    args = plistlib.load(f).get("ProgramArguments") or []
            except Exception:
                continue
            if _cloudflared_args_target_tunnel([str(a) for a in args], tunnel_id, tunnel_token):
                return label
    return _CLOUDFLARED_LABELS[0]


def _kickstart_cloudflared() -> bool:
    """launchctl kickstart로 cloudflared(우리 label만) 재시작. 성공 여부 반환."""
    import subprocess
    uid = os.getuid()
    target = f"gui/{uid}/{_cloudflared_label()}"
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


def _recover_home_portal(reason: str) -> bool:
    """_ensure_home_portal() 을 지수 백오프로 호출. 스톰(고정 20s 재시도)로 인한
    자원 소진을 막는다. 실제 호출 시 True, 백오프로 스킵 시 False."""
    global _last_recover_ts, _recover_streak
    import time
    now = time.time()
    interval = min(300, _RECOVER_BACKOFF_BASE * (2 ** _recover_streak))
    if _last_recover_ts and now - _last_recover_ts < interval:
        logger.warning(
            f"[tunnel] recovery backoff active ({reason}); "
            f"skipping _ensure_home_portal (next in {interval - (now - _last_recover_ts):.0f}s, streak={_recover_streak})"
        )
        return False
    logger.warning(f"[tunnel] recovery trigger #{_recover_streak + 1} ({reason}) -> _ensure_home_portal()")
    _last_recover_ts = now
    _recover_streak += 1
    _ensure_home_portal()
    return True


def _report_tunnel_health(state: str, note: str = ""):
    """서킷브레이커 상태를 서버에 능동 보고(로그만으로는 인지 누락 위험).

    기존 PATCH /api/bot/status 채널 재사용(전 고객 공통, 고객별 api_key). OPEN 은
    '진짜 다운도 복구를 멈춘' 상태이므로 system-admin 이 어드민/고객관리 뷰에서
    인지할 수 있게 tunnel_health 를 실어 보낸다. 실패해도 헬스체크는 계속.
    """
    api_key = config.get("api_key", "")
    if not api_key:
        return
    try:
        api_request(api_key, "PATCH", "/api/bot/status",
                    body={"tunnel_health": state, "tunnel_health_note": note[:200]})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[tunnel][circuit] health report failed: {e}")


def _count_time_wait() -> int | None:
    """TIME_WAIT 소켓 수 — 경보에 실어 보낼 근거 숫자. 실패해도 경보 자체는 진행한다."""
    import subprocess
    for netstat in ("/usr/sbin/netstat", "netstat"):
        try:
            r = subprocess.run([netstat, "-an", "-p", "tcp"],
                               capture_output=True, text=True, timeout=5)
        except Exception:  # noqa: BLE001
            continue
        if r.returncode == 0:
            return sum(1 for ln in r.stdout.splitlines() if "TIME_WAIT" in ln)
    return None


def _notify_exhaustion(note: str, streak: int):
    """로컬 자원 고갈을 서버(관리자)와 고객에게 알린다.

    감지 자체는 hoon 사고(2026-07-16) 때 넣었지만 로그만 남겼다. 그 결과 2026-09-20
    고객 맥에서 730회 감지되는 동안 서버·관리자·고객 누구도 몰랐고 31시간 불통이
    그대로 방치됐다(재로그인 시도만 7회 실패). 감지했으면 반드시 말해야 한다.
    → 서버에는 감지 로그와 같은 주기로, 고객 채팅에는 확정 후 1회만(쿨다운).
    """
    global _last_exhaustion_chat_ts
    import time
    tw = _count_time_wait()
    detail = (note or "unknown")[:120]
    if tw is not None:
        detail = f"{detail} | TIME_WAIT={tw}"
    _report_tunnel_health("exhausted", detail)

    if streak < _EXHAUSTION_CHAT_AFTER:
        return
    now = time.time()
    if now - _last_exhaustion_chat_ts < _EXHAUSTION_CHAT_COOLDOWN:
        return
    api_key = config.get("api_key", "")
    if not api_key:
        return
    tw_line = f"\n- 지금 잠겨 있는 통신 회선: **{tw:,}개**" if tw is not None else ""
    text = (
        "\u26a0\ufe0f **이 맥의 통신 회선(포트)이 모두 소진됐습니다.**\n\n"
        "밖으로 새 연결을 맺지 못하는 상태라 답변이 계속 실패할 수 있습니다. "
        "로그인 문제가 아니라 회선 문제라서 재로그인으로는 해결되지 않습니다."
        f"{tw_line}\n\n"
        "**맥을 한 번 재시동해 주세요.** 애플 메뉴 > 재시동을 누르시면 됩니다. "
        "1~2분 뒤부터 정상으로 돌아옵니다.\n\n"
        "재시동이 어려우시면 이 창에 말씀해 주세요 — 임시 조치를 안내해 드리겠습니다."
    )
    try:
        api_request(api_key, "POST", "/api/bot/reply",
                    {"text": text, "project": "general", "is_final": True})
        _last_exhaustion_chat_ts = now
        logger.warning("[tunnel] exhaustion notice sent to customer chat")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[tunnel] exhaustion chat notice failed: {e}")


def _check_cloudflared_health():
    """2단계 헬스체크로 cloudflared 좀비 터널을 감지·자가치유.

    1) 이 터널의 연결기 프로세스가 없으면 _ensure_home_portal()로 복구.
    2) 프로세스는 살아있지만 엣지 미연결('좀비')이면 launchctl kickstart로 재시작.
       - 연속 실패 임계치/시간당 backoff로 플래핑·무한루프 방지.
    """
    global _tunnel_unreachable_streak, _tunnel_exhaustion_streak, _recover_streak
    global _last_exhaustion_chat_ts
    global _cb_state, _cb_fail_since, _cb_opened_ts, _cb_last_probe_ts

    if not config.get("home_portal_enabled", True):
        return
    if config.get("portal_shared"):
        return  # 공유 맥: 터널·포탈은 소유자 데몬이 관리 — 테넌트는 헬스체크/복구 안 함
    if sys.platform != "darwin":  # macOS 전용 (Windows 등 제외)
        return

    # --- 1단계: 이 터널의 연결기 생존 ---
    if not _tunnel_connector_running(config.get("cloudflare_tunnel_id", ""),
                                     config.get("cloudflare_tunnel_token", "")):
        logger.warning("[tunnel] cloudflared connector for this tunnel not running, recovering...")
        _tunnel_unreachable_streak = 0
        _recover_home_portal("cloudflared process dead")
        return

    # --- 2단계: 외부 도달성(좀비 감지) ---
    tunnel_url = _tunnel_url_from_config()
    if not tunnel_url:
        return  # URL 미상 → 도달성 체크 스킵

    import time
    now = time.time()

    # --- 서킷브레이커 게이트: OPEN 이면 프로브도 30분에 1회(half-open)만 ---
    if _cb_state == "open":
        if now - _cb_last_probe_ts < _CB_HALFOPEN_EVERY:
            return  # OPEN 유지 — 조용히 스킵(프로브/kickstart 없음)
        _cb_state = "half_open"
        _cb_last_probe_ts = now
        logger.warning(
            f"[tunnel][circuit] HALF-OPEN trial probe "
            f"(OPEN for {(now - _cb_opened_ts) / 60:.0f}m)"
        )

    status, note = _http_status(tunnel_url)
    if _tunnel_reachable(status):
        if _cb_state != "closed":
            down_min = (now - _cb_fail_since) / 60 if _cb_fail_since else 0
            logger.warning(
                f"[tunnel][circuit] CLOSED — tunnel recovered after "
                f"~{down_min:.0f}m down ({note})"
            )
            _report_tunnel_health("closed", note)  # OPEN 해제 서버 통지
        elif _tunnel_unreachable_streak or _tunnel_exhaustion_streak:
            logger.info(f"[tunnel] external reachability recovered ({note})")
            if _tunnel_exhaustion_streak:
                # 고갈 경보를 올렸으면 해제도 올려야 관리자 화면이 계속 빨갛게 남지 않는다
                _report_tunnel_health("closed", f"recovered from exhaustion ({note})")
                _last_exhaustion_chat_ts = 0.0
        _cb_state = "closed"
        _cb_fail_since = 0.0
        _tunnel_unreachable_streak = 0
        _tunnel_exhaustion_streak = 0
        _recover_streak = 0   # 건강 회복 → 백오프 리셋
        return

    # 외부 실패가 로컬 자원 고갈(errno49 등)이면 터널/홈포탈 문제가 아님.
    # 복구를 트리거하면 새 연결로 자원을 더 소진해 악화되므로 손대지 않는다 (hoon 사고).
    if _looks_like_resource_exhaustion(note):
        _tunnel_exhaustion_streak += 1
        if _tunnel_exhaustion_streak <= 3 or _tunnel_exhaustion_streak % 10 == 0:
            logger.warning(
                f"[tunnel] LOCAL RESOURCE EXHAUSTION detected ({note}), "
                f"streak={_tunnel_exhaustion_streak} — skipping recovery (would amplify). "
                f"Likely ephemeral-port/FD exhaustion; needs local remediation."
            )
            _notify_exhaustion(note, _tunnel_exhaustion_streak)
        return
    _tunnel_exhaustion_streak = 0

    # --- 서킷브레이커: 진짜 불통(좀비/홈포탈다운) 연속 지속 시간 추적 ---
    if not _cb_fail_since:
        _cb_fail_since = now
    down_for = now - _cb_fail_since
    if _cb_state == "closed" and down_for >= _CB_OPEN_AFTER:
        # 1시간 넘게 복구 실패 → 능동 조치 중단(OPEN). kickstart로 못 고치는 상황.
        _cb_state = "open"
        _cb_opened_ts = now
        _cb_last_probe_ts = now
        logger.error(
            f"[tunnel][circuit] OPEN — tunnel unreachable ~{down_for / 60:.0f}m "
            f"despite recovery attempts; pausing active remediation "
            f"(half-open probe every {_CB_HALFOPEN_EVERY // 60}m). "
            f"Manual attention likely needed. last={note}"
        )
        _report_tunnel_health("open", note)  # 서버에도 능동 알림
        return
    if _cb_state == "half_open":
        # half-open 프로브가 여전히 실패 → OPEN 유지, 쿨다운 재시작(30m 대기)
        _cb_state = "open"
        _cb_last_probe_ts = now
        logger.warning(f"[tunnel][circuit] half-open trial still failing ({note}) — back to OPEN")
        return

    # 외부 실패 — 로컬 홈포탈(3000) 다운과 구분
    local_status, local_note = _http_status("http://127.0.0.1:3000/", timeout=4.0)
    if local_status is None:
        # 로컬 프로브도 자원 고갈로 실패한 것이면 홈포탈 문제가 아니다 → 복구 금지(악화 방지)
        if _looks_like_resource_exhaustion(local_note):
            _tunnel_exhaustion_streak += 1
            logger.warning(
                f"[tunnel] local probe failed by resource exhaustion ({local_note}) "
                f"— NOT triggering recovery (would amplify)"
            )
            if _tunnel_exhaustion_streak <= 3 or _tunnel_exhaustion_streak % 10 == 0:
                _notify_exhaustion(local_note, _tunnel_exhaustion_streak)
            return
        # 진짜 홈포탈 다운 → 프로세스 복구 경로로(지수 백오프)
        logger.warning(
            f"[tunnel] external failed ({note}) but localhost:3000 down ({local_note}) "
            f"— home portal issue, not tunnel"
        )
        _tunnel_unreachable_streak = 0
        _recover_home_portal("home portal down")
        return

    _tunnel_unreachable_streak += 1
    logger.warning(
        f"[tunnel] external reachability failed "
        f"({_tunnel_unreachable_streak} consecutive): {note} (local 3000 OK)"
    )
    if _tunnel_unreachable_streak < _TUNNEL_FAIL_THRESHOLD:
        return  # 아직 임계치 미만 — 일시적 드롭 오탐 방지

    # --- backoff: 시간당 최대 N회 ---
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
        # kickstart 실패 시 전체 프로비저닝 경로로 폴백 (지수 백오프 경유)
        logger.warning("[tunnel] kickstart failed, falling back to recovery")
        _recover_home_portal("kickstart failed")
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

    # 공유 맥 모드: 이 머신의 포탈·터널은 소유자(관리) 데몬이 소유한다.
    # 테넌트 데몬은 포탈/cloudflared 를 띄우지 않고, 소유자 터널(shared_tunnel_id)에
    # 자기 호스트명 ingress + DNS 만 등록하고 tunnel_url 을 서버에 알린다.
    # (포탈은 PV_MULTI_USER=1 레지스트리로 이 유저를 서빙 — home-portal.js MULTI_MODE)
    if config.get("portal_shared"):
        try:
            me = api_request(api_key, "GET", "/api/bot/me")
            if not me or not me.get("username"):
                logger.warning("[home-portal] (shared) Could not resolve username")
                return
            username = me["username"]
            shared_tunnel_id = config.get("shared_tunnel_id", "")
            if not shared_tunnel_id:
                logger.error("[home-portal] (shared) portal_shared=true 인데 shared_tunnel_id 가 없음")
                return
            tunnel_url = _ensure_dns_route(api_key, username, shared_tunnel_id)
            api_request(api_key, "PATCH", "/api/bot/status", body={"tunnel_url": tunnel_url})
            logger.info(f"[home-portal] (shared) Registered tunnel_url: {tunnel_url}")
        except Exception as e:
            logger.error(f"[home-portal] (shared) Error: {e}")
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


def _ensure_launchd_exit_timeout():
    """launchd SIGKILL grace(ExitTimeOut)를 드레인 상한보다 길게 보정.

    현재 로드된 job의 grace는 기본 5초라 launchctl stop 시 드레인이 불가능하다.
    plist 수정은 다음 로드(재부팅 또는 bootout/bootstrap)부터 적용된다.
    적용 전까지 launchctl stop 경로는 짧은 grace로 SIGKILL되지만, 그 경우에도
    메시지가 큐에 남아 자동재개되므로 동작은 올바르다(드레인만 못 할 뿐).
    AutoUpdater//restart/웹 강제재시작은 자체 exit 경로라 launchd grace와 무관하게
    항상 드레인된다.
    """
    if sys.platform != "darwin":
        return
    try:
        import plistlib
        from pathlib import Path
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.petervoice.claude-daemon.plist"
        if not plist_path.exists():
            return
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        desired = int(config.get("shutdown_drain_sec", 90)) + 30
        current = plist.get("ExitTimeOut")
        if isinstance(current, int) and current >= desired:
            return
        plist["ExitTimeOut"] = desired
        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)
        logger.info(f"[migrate] Set ExitTimeOut={desired} in daemon plist (applies on next launchd load)")
    except Exception as e:
        logger.warning(f"[migrate] Could not update daemon plist ExitTimeOut: {e}")


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
        _ensure_launchd_exit_timeout()
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

        # Session auto-reset (deterministic, no LLM report) — independent of session_health
        # so it can run while the LLM-reporting health checker stays disabled.
        auto_reset_config = config.get("session_auto_reset", {})
        if auto_reset_config.get("enabled", True):
            SessionAutoResetThread(
                interval_hours=auto_reset_config.get("interval_hours", 2),
                initial_delay_sec=auto_reset_config.get("initial_delay_sec", 300),
            ).start()

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

            # 사용량 즉시 새로고침 요청(배너 버튼) — 10초 주기 감지
            if watchdog_tick % 10 == 0 and user_id is not None:
                try:
                    from daemon.supabase import check_usage_refresh, clear_usage_refresh
                    if check_usage_refresh(user_id):
                        logger.info("[usage] on-demand refresh requested")
                        clear_usage_refresh()
                        from daemon.heartbeat import collect_and_store_usage
                        collect_and_store_usage()
                except Exception as e:
                    logger.warning(f"[usage] refresh check error: {e}")

            # 설정 UI 재로그인 브리지 (5초 주기) — 채팅 플로우와 같은 엔진 공유
            if watchdog_tick % 5 == 0 and user_id is not None:
                try:
                    from daemon.relogin_settings import poll_relogin
                    poll_relogin(user_id)
                except Exception as e:
                    logger.warning(f"relogin poll error: {e}")

        # 드레인 종료: 새 작업 수령은 이미 중단됨(worker 루프 종료 + pending future 취소).
        # 진행 중인 claude/codex 턴은 완주를 기다린다. 각 턴은 claude_runner가
        # shutdown_drain_sec 상한에서 스스로 kill + 센티널 반환하므로(큐 보존 → 자동재개)
        # 여기서는 그보다 약간 긴 상한으로 in-flight가 0이 되기를 기다린다.
        # 유휴 상태(in-flight 0)면 대기 없이 즉시 종료된다.
        import time as _time
        worker._executor.shutdown(wait=False, cancel_futures=True)
        _drain_sec = config.get("shutdown_drain_sec", 90)
        _deadline = _time.time() + _drain_sec + 15
        _inflight = worker.inflight_count()
        if _inflight:
            logger.info(f"Draining {_inflight} in-flight turn(s) before exit (up to {_drain_sec}s)...")
        while worker.inflight_count() > 0 and _time.time() < _deadline:
            _time.sleep(0.5)
        _remaining = worker.inflight_count()
        if _remaining:
            logger.warning(f"Drain window elapsed with {_remaining} turn(s) still running — exiting; queued messages will auto-resume")
        elif _inflight:
            logger.info("Drain complete — all in-flight turns finished")
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
