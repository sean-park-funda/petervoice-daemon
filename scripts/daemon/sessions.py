"""Session management: load/save sessions, handle resets, session summaries."""

import os
import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import daemon.globals as g
from daemon.limits import build_claude_env
from daemon.globals import (
    config, sessions_lock, shutdown_event, logger,
    DAEMON_DIR, SESSIONS_PATH, PENDING_RESETS_PATH, CODEX_SESSIONS_PATH,
    CLAUDE_CMD, IS_WINDOWS,
)
from daemon.supabase import resolve_user_id, get_project_dir, _fetch_project_settings, _fetch_recent_conversation
from daemon.api import api_request
from daemon.utils import _strip_ansi


def session_key(project: str, task: str = "default") -> str:
    return f"{project}:{task}"


def team_session_key(project: str, member_key: str, branch_id: int | None = None) -> str:
    suffix = f"branch-{branch_id}" if branch_id else "main"
    return f"{project}:{member_key}:{suffix}"


def update_session_by_key(key: str, session_id: str, account: str = "default"):
    now = datetime.now().isoformat()
    with sessions_lock:
        sess = g.sessions.setdefault(key, {"created_at": now, "message_count": 0})
        sess["session_id"] = session_id
        sess["last_used"] = now
        sess["message_count"] = sess.get("message_count", 0) + 1
        if account and account != "default":
            sess["account"] = account
    save_sessions()


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


def reset_session(project: str, reason: str = "", key_override: str | None = None):
    key = key_override or session_key(project)
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

    # 슬라이딩 윈도우 단기기억(작업 A):
    # 최근 N턴은 다음 세션에 '원문 그대로' 붙일 것이므로, 요약에서는 그 이전만 다룬다.
    # recent_raw를 요약 프롬프트에 '제외 대상'으로 명시해 경계를 내용 기준으로 고정 →
    # 저장 시점에 [요약(N턴 이전) + 원문(최근 N턴)]이 확정되어 중복/누락 없음(경계 정합).
    recent_turns = config.get("session_recent_turns", 20)
    recent_raw = _fetch_recent_conversation(project, limit=recent_turns)
    summary_prompt = (
        "세션이 곧 리셋됩니다. 다음 세션에서 이어갈 수 있도록 지금까지의 핵심 맥락을 요약해주세요.\n"
        "단, 아래 '최근 대화'는 다음 세션에 원문 그대로 다시 제공되므로 **요약에서 제외**하고, "
        "그 이전 내용만 요약하세요 (최근 대화 내용을 다시 적지 마세요):\n"
        "1. 현재 진행 중인 작업과 상태\n"
        "2. 최근 내린 주요 결정사항\n"
        "3. 아직 완료되지 않은 과제\n"
        "4. 중요한 컨텍스트 (에러, 제약사항 등)\n"
        "간결하게 500자 이내로 작성해주세요.\n\n"
        "--- 최근 대화 (요약 제외 대상, 이미 다음 세션에 원문 제공됨) ---\n"
        f"{recent_raw or '(최근 대화 없음)'}"
    )

    proj_settings = _fetch_project_settings(project)
    account_name = proj_settings.get("account") or "default"
    accounts = config.get("accounts", {})
    account_config_dir = accounts.get(account_name, {}).get("config_dir") if account_name != "default" else None

    # --verbose 필수: CLI가 "When using --print, --output-format=stream-json requires --verbose"로
    # exit 1 하며 즉시 죽는다(1초). 이 인자가 없어 AI 요약이 항상 실패하고 폴백(원문)만
    # 저장되고 있었다 — 2026-07-27 실측으로 확인·수정.
    cmd = [CLAUDE_CMD, "-p", "--output-format", "stream-json", "--verbose",
           "--resume", sid, "--", summary_prompt]
    claude_env = build_claude_env(config, account_config_dir)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # stdin=DEVNULL: CLI가 stdin을 3초 기다리며 "no stdin data received" 경고를
            # 내고 그만큼 늦어진다. 요약은 인자로 프롬프트를 주므로 stdin이 필요 없다.
            stdin=subprocess.DEVNULL,
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
        # 19.5MB 세션 요약에 실측 25초. 더 큰 세션(30~70MB)도 있어 60초는 빡빡하므로
        # 기본 180초로 늘리고 config로 조정 가능하게 한다.
        proc.wait(timeout=config.get("session_summary_timeout_sec", 180))
        summary_text = _strip_ansi(summary_text).strip()

        if summary_text and len(summary_text) > 20:
            # 슬라이딩 윈도우: 요약(N턴 이전) + 최근 N턴 원문을 함께 저장.
            # 경계는 위 recent_raw로 이미 고정됨(요약에서 제외하도록 지시). 저장/주입 시점 재조회 없음.
            combined = summary_text
            if recent_raw:
                combined += f"\n\n## 최근 {recent_turns}턴 (원문)\n{recent_raw}"
            _save_session_summary(project, combined)
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


# ─── Codex session management ──────────────────────────────────

_codex_sessions: dict = {}
_codex_sessions_lock = threading.Lock()


def load_codex_sessions():
    global _codex_sessions
    try:
        if CODEX_SESSIONS_PATH.exists():
            data = json.loads(CODEX_SESSIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _codex_sessions = data
                logger.info(f"Codex sessions loaded: {len(data)} active")
                return
    except Exception as e:
        logger.warning(f"Failed to load codex sessions: {e}")
    _codex_sessions = {}


def _save_codex_sessions():
    try:
        with _codex_sessions_lock:
            data = dict(_codex_sessions)
        CODEX_SESSIONS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to save codex sessions: {e}")


def get_codex_session_id(project: str) -> str | None:
    with _codex_sessions_lock:
        return _codex_sessions.get(project, {}).get("session_id")


def update_codex_session(project: str, session_id: str):
    now = datetime.now().isoformat()
    with _codex_sessions_lock:
        sess = _codex_sessions.setdefault(project, {"created_at": now, "message_count": 0})
        sess["session_id"] = session_id
        sess["last_used"] = now
        sess["message_count"] = sess.get("message_count", 0) + 1
    _save_codex_sessions()


def reset_codex_session(project: str, reason: str = ""):
    with _codex_sessions_lock:
        removed = _codex_sessions.pop(project, None)
    if removed:
        _save_codex_sessions()
        logger.info(f"Codex session reset for {project}" + (f" ({reason})" if reason else ""))


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
