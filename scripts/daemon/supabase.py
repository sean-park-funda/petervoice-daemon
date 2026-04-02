"""API proxy queries: user resolution, project settings, status checks.

All DB access goes through PeterVoice web API (/api/bot/*) — no direct Supabase.
"""

import os
import json
import time

import daemon.globals as g
from daemon.globals import config, logger
from daemon.api import api_request


def resolve_user_id() -> int | None:
    """api_key로 user_id를 조회한다. 시작 시 1회 호출."""
    if g._cached_user_id is not None:
        return g._cached_user_id
    api_key = config.get("api_key", "")
    if not api_key:
        return None
    result = api_request(api_key, "GET", "/api/bot/me", timeout=5)
    if result and "userId" in result:
        g._cached_user_id = result["userId"]
        return g._cached_user_id
    logger.warning("Failed to resolve user_id from api_key")
    return None


def _fetch_project_settings(project: str) -> dict:
    """projects 테이블에서 directory, chrome 등 설정 조회. 10초 캐시."""
    cached = g._project_settings_cache.get(project)
    if cached and time.time() - cached[0] < 10:
        return cached[1]
    api_key = config.get("api_key", "")
    if not api_key:
        return {}
    import urllib.parse
    result = api_request(api_key, "GET", f"/api/bot/project-settings?project={urllib.parse.quote(project)}", timeout=5)
    if result and isinstance(result, dict) and "error" not in result:
        g._project_settings_cache[project] = (time.time(), result)
        return result
    return {}


def _fetch_project_directory(project: str) -> str | None:
    """projects 테이블에서 directory 컬럼 조회."""
    settings = _fetch_project_settings(project)
    return settings.get("directory") or None


def get_project_dir(project: str) -> str:
    from daemon.globals import DAEMON_DIR
    # 1) API에서 directory 조회
    directory = _fetch_project_directory(project)
    if directory:
        os.makedirs(directory, exist_ok=True)
        return directory
    # 2) config.json 폴백
    dirs = config.get("project_dirs", {})
    if project in dirs:
        return dirs[project]
    # 3) 자동 디렉토리 생성
    auto_dir = str(DAEMON_DIR / "projects" / project)
    os.makedirs(auto_dir, exist_ok=True)
    return auto_dir


def check_bot_response_exists(msg_id: int) -> bool:
    """특정 user 메시지에 대한 bot 응답이 존재하는지 확인."""
    api_key = config.get("api_key", "")
    if not api_key:
        return False
    result = api_request(api_key, "GET", f"/api/bot/check-response?msg_id={msg_id}", timeout=5)
    if result:
        return result.get("exists", False)
    return False


def fetch_prompt_from_supabase(project: str, user_id_override: int | None = None) -> str | None:
    """프로젝트 프롬프트 내용을 가져온다."""
    api_key = config.get("api_key", "")
    if not api_key:
        return None
    import urllib.parse
    params = f"project={urllib.parse.quote(project)}"
    if user_id_override == 0:
        params += "&system=1"
    result = api_request(api_key, "GET", f"/api/bot/prompt?{params}", timeout=5)
    if result:
        return result.get("content")
    return None


def check_force_restart(user_id: int) -> bool:
    """user_status에서 force_restart 플래그 확인."""
    api_key = config.get("api_key", "")
    if not api_key:
        return False
    result = api_request(api_key, "GET", "/api/bot/status", timeout=5)
    if result:
        return result.get("force_restart", False)
    return False


def clear_force_restart(user_id: int):
    """user_status.force_restart = false 로 초기화."""
    api_key = config.get("api_key", "")
    if not api_key:
        return
    api_request(api_key, "PATCH", "/api/bot/status", body={"force_restart": False}, timeout=5)


def check_stop_requested(user_id: int) -> bool:
    """user_status에서 stop_requested 플래그 확인."""
    api_key = config.get("api_key", "")
    if not api_key:
        return False
    result = api_request(api_key, "GET", "/api/bot/status", timeout=5)
    if result:
        return result.get("stop_requested", False)
    return False


def clear_stop_requested(user_id: int):
    """user_status.stop_requested = false 로 초기화."""
    api_key = config.get("api_key", "")
    if not api_key:
        return
    api_request(api_key, "PATCH", "/api/bot/status", body={"stop_requested": False, "stop_requested_at": None}, timeout=5)


def _fetch_recent_conversation(project: str, limit: int = 10) -> str:
    """Fetch recent messages for a project to provide context."""
    api_key = config.get("api_key", "")
    if not api_key:
        return ""
    import urllib.parse
    result = api_request(api_key, "GET",
                         f"/api/bot/conversation?project={urllib.parse.quote(project)}&limit={limit}",
                         timeout=10)
    if not result or not result.get("messages"):
        return ""
    rows = result["messages"]
    rows.reverse()
    lines = []
    for r in rows:
        role = "유저" if r.get("type") == "user" else "클로드"
        text = r.get("text", "").strip()
        if text and not text.startswith("🔧"):
            if len(text) > 500:
                text = text[:500] + "..."
            lines.append(f"[{role}] {text}")
    return "\n".join(lines)
