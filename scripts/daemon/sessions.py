"""Session management: load/save sessions, handle resets, session summaries."""

import os
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import daemon.globals as g
from daemon.globals import (
    config, sessions_lock, shutdown_event, logger,
    DAEMON_DIR, SESSIONS_PATH, PENDING_RESETS_PATH,
    CLAUDE_CMD, IS_WINDOWS,
)
from daemon.supabase import resolve_user_id, get_project_dir, _fetch_project_settings, _fetch_recent_conversation
from daemon.api import api_request
from daemon.utils import _strip_ansi


def session_key(project: str, task: str = "default") -> str:
    return f"{project}:{task}"


def get_session_id(project: str, task: str = "default") -> str | None:
    key = session_key(project, task)
    with sessions_lock:
        return g.sessions.get(key, {}).get("session_id")


def update_session(project: str, session_id: str, task: str = "default", account: str = "default"):
    key = session_key(project, task)
    now = datetime.now().isoformat()
    with sessions_lock:
        sess = g.sessions.setdefault(key, {"created_at": now, "message_count": 0})
        sess["session_id"] = session_id
        sess["last_used"] = now
        sess["message_count"] = sess.get("message_count", 0) + 1
        if account and account != "default":
            sess["account"] = account
    save_sessions()


def clear_session(project: str, task: str = "default"):
    key = session_key(project, task)
    with sessions_lock:
        g.sessions.pop(key, None)
    save_sessions()


def load_sessions():
    try:
        if SESSIONS_PATH.exists():
            data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                g.sessions = data
                logger.info(f"Sessions loaded: {len(data)} active")
                return
    except Exception as e:
        logger.warning(f"Failed to load sessions: {e}")
    g.sessions = {}


def save_sessions():
    try:
        with sessions_lock:
            data = dict(g.sessions)
        SESSIONS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to save sessions: {e}")


def reset_session(project: str, reason: str = ""):
    key = session_key(project)
    with sessions_lock:
        removed = g.sessions.pop(key, None)
    if removed:
        save_sessions()
        logger.info(f"Session reset for {project}" + (f" ({reason})" if reason else ""))


def _process_pending_resets():
    if not PENDING_RESETS_PATH.exists():
        return
    try:
        projects = json.loads(PENDING_RESETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        PENDING_RESETS_PATH.unlink(missing_ok=True)
        return
    if not projects:
        PENDING_RESETS_PATH.unlink(missing_ok=True)
        return
    for p in projects:
        project = p if isinstance(p, str) else p.get("project", "")
        if project:
            reset_session(project, reason="pending reset from previous run")
    PENDING_RESETS_PATH.unlink(missing_ok=True)
    logger.info(f"[pending-reset] Processed {len(projects)} resets")


def _save_session_summary(project: str, summary: str):
    """Save session summary via API."""
    api_key = config.get("api_key", "")
    if not api_key:
        return
    result = api_request(api_key, "PATCH", "/api/bot/session-summary", body={
        "project": project,
        "summary": summary,
    }, timeout=10)
    if result and result.get("ok"):
        logger.info(f"Session summary saved for {project} ({len(summary)} chars)")
    else:
        logger.error(f"Failed to save session summary for {project}")


def save_session_context(project: str) -> bool:
    """Save session context before reset: ask Claude to summarize, then store."""
    key = session_key(project)
    with sessions_lock:
        sess = g.sessions.get(key)
    if not sess or not sess.get("session_id"):
        conv = _fetch_recent_conversation(project, limit=10)
        if conv:
            fallback = f"[자동 요약 — 이전 세션 요약 불가, 최근 대화 원본]\n\n{conv}"
            _save_session_summary(project, fallback)
            logger.info(f"Session context fallback saved for {project}")
            return True
        return False

    sid = sess["session_id"]
    project_dir = get_project_dir(project)
    summary_prompt = (
        "세션이 곧 리셋됩니다. 다음 세션에서 이어갈 수 있도록, "
        "지금까지의 핵심 맥락을 요약해주세요:\n"
        "1. 현재 진행 중인 작업과 상태\n"
        "2. 최근 내린 주요 결정사항\n"
        "3. 아직 완료되지 않은 과제\n"
        "4. 중요한 컨텍스트 (에러, 제약사항 등)\n"
        "간결하게 500자 이내로 작성해주세요."
    )

    proj_settings = _fetch_project_settings(project)
    account_name = proj_settings.get("account") or "default"
    accounts = config.get("accounts", {})
    account_config_dir = accounts.get(account_name, {}).get("config_dir") if account_name != "default" else None

    cmd = [CLAUDE_CMD, "-p", "--output-format", "stream-json", "--resume", sid, "--", summary_prompt]
    claude_env = {
        **{k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        "LANG": "en_US.UTF-8",
    }
    if account_config_dir:
        claude_env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(account_config_dir)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=project_dir, env=claude_env, shell=IS_WINDOWS,
        )
        summary_text = ""
        for line in proc.stdout:
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            if etype == "assistant" and "message" in event:
                for block in event["message"].get("content", []):
                    if block.get("type") == "text":
                        summary_text += block.get("text", "")
            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    summary_text += delta.get("text", "")
            elif etype == "result":
                if event.get("result") and not summary_text.strip():
                    summary_text = event["result"]
        proc.wait(timeout=60)
        summary_text = _strip_ansi(summary_text).strip()

        if summary_text and len(summary_text) > 20:
            _save_session_summary(project, summary_text)
            return True
        else:
            logger.warning(f"Summary too short for {project}, using fallback")
    except Exception as e:
        logger.error(f"Failed to get session summary for {project}: {e}")

    conv = _fetch_recent_conversation(project, limit=10)
    if conv:
        fallback = f"[자동 요약 — AI 요약 실패, 최근 대화 원본]\n\n{conv}"
        _save_session_summary(project, fallback)
        return True
    return False


def _build_session_context_prompt(project: str) -> str:
    """Build a session context prompt from saved summary."""
    summary = _fetch_session_summary(project)
    if not summary or len(summary.strip()) < 20:
        return ""
    return (
        "# 이전 세션 컨텍스트\n"
        "아래는 이전 세션에서 저장된 맥락입니다. 필요 시 참고하세요.\n\n"
        f"{summary.strip()}"
    )


def _fetch_session_summary(project: str) -> str | None:
    """Fetch session_summary via API."""
    api_key = config.get("api_key", "")
    if not api_key:
        return None
    import urllib.parse
    result = api_request(api_key, "GET", f"/api/bot/session-summary?project={urllib.parse.quote(project)}", timeout=5)
    if result:
        return result.get("summary")
    return None
