"""Session health checker thread with stall detection."""

from datetime import datetime
import time
import threading

import daemon.globals as g
from daemon.globals import (
    SESSION_MANAGER_PROJECT, config, sessions_lock, shutdown_event, logger,
)
from daemon.sessions import save_session_context, save_sessions
from daemon.supabase import _fetch_recent_conversation
from daemon.api import api_request


class SessionHealthChecker(threading.Thread):
    """Periodically checks session health and detects stalled conversations."""

    HEALTH_CHECK_INTERVAL = 2 * 3600  # 2 hours
    STALL_CHECK_INTERVAL = 1800       # 30 minutes

    def __init__(self):
        super().__init__(daemon=True, name="session-health-checker")
        self.api_key = config["api_key"]
        self._has_session_manager: bool | None = None  # lazy check

    def _get_all_sessions_info(self) -> list[dict]:
        infos = []
        with sessions_lock:
            for key, sess in g.sessions.items():
                parts = key.split(":", 1)
                project = parts[0] if parts else key
                if project == SESSION_MANAGER_PROJECT:
                    continue
                age_h = 0.0
                last_used = sess.get("last_used")
                if last_used:
                    age_h = (datetime.now() - datetime.fromisoformat(last_used)).total_seconds() / 3600
                created = sess.get("created_at", "")
                lifetime_h = 0.0
                if created:
                    try:
                        lifetime_h = (datetime.now() - datetime.fromisoformat(created)).total_seconds() / 3600
                    except Exception:
                        pass
                infos.append({
                    "project": project,
                    "key": key,
                    "session_id": sess.get("session_id", "")[:12],
                    "message_count": sess.get("message_count", 0),
                    "idle_hours": round(age_h, 1),
                    "lifetime_hours": round(lifetime_h, 1),
                })
        return infos

    def _presave_expiring_sessions(self, infos: list[dict]):
        ttl_hours = config.get("session_ttl_hours", 24)
        if ttl_hours <= 0:
            return
        threshold = ttl_hours * 0.75
        for info in infos:
            if info["idle_hours"] < threshold:
                continue
            project = info["project"]
            key = info["key"]
            with sessions_lock:
                sess = g.sessions.get(key, {})
                if sess.get("summary_saved"):
                    continue
            logger.info(f"[session-health] Pre-saving summary for {project} (idle {info['idle_hours']}h, TTL {ttl_hours}h)")
            try:
                saved = save_session_context(project)
                if saved:
                    with sessions_lock:
                        if key in g.sessions:
                            g.sessions[key]["summary_saved"] = True
                    save_sessions()
            except Exception as e:
                logger.error(f"[session-health] Pre-save failed for {project}: {e}")

    def _check_session_manager_exists(self) -> bool:
        """Check if session-manager project exists for this user. Cached after first check."""
        if self._has_session_manager is not None:
            return self._has_session_manager
        result = api_request(self.api_key, "GET", "/api/projects", timeout=5)
        if result and "projects" in result:
            self._has_session_manager = any(
                p.get("id") == SESSION_MANAGER_PROJECT for p in result["projects"]
            )
        else:
            self._has_session_manager = False
        if not self._has_session_manager:
            logger.info(f"[session-health] '{SESSION_MANAGER_PROJECT}' project not found — health/stall checks disabled")
        return self._has_session_manager

    def _check_sessions(self):
        infos = self._get_all_sessions_info()
        if not infos:
            logger.info("[session-health] No active sessions to check")
            return

        self._presave_expiring_sessions(infos)

        if not self._check_session_manager_exists():
            return

        session_lines = []
        for info in infos:
            project = info["project"]
            conv = _fetch_recent_conversation(project, limit=5)
            recent = conv[:400] if conv else "(최근 대화 없음)"
            session_lines.append(
                f"### {project}\n"
                f"- 메시지: {info['message_count']}개, 미사용: {info['idle_hours']}시간, 수명: {info['lifetime_hours']}시간\n"
                f"- 최근 대화:\n```\n{recent}\n```"
            )

        report = (
            f"[정기 세션 점검 리포트 — {datetime.now().strftime('%Y-%m-%d %H:%M')}]\n\n"
            f"현재 활성 세션 {len(infos)}개:\n\n"
            + "\n\n".join(session_lines)
            + "\n\n위 스니펫으로 1차 판단하고, 관심 가는 세션은 messages 테이블에서 더 많은 대화를 직접 조회해서 상세 분석해주세요."
        )

        api_key = config.get("api_key", "")
        if not api_key:
            logger.warning("[session-health] Cannot send report — missing api_key")
            return

        result = api_request(api_key, "POST", "/api/bot/message", body={
            "project": SESSION_MANAGER_PROJECT,
            "text": report,
            "type": "user",
            "subtype": "session_health_report",
            "processed": False,
        }, timeout=10)

        if result and result.get("id"):
            logger.info(f"[session-health] Report sent to {SESSION_MANAGER_PROJECT} ({len(infos)} sessions)")
        else:
            logger.error("[session-health] Failed to send report")

    def _check_stalls(self):
        """Collect conversation snippets and ask session-manager to judge stalls."""
        if not self._check_session_manager_exists():
            return
        infos = self._get_all_sessions_info()
        if not infos:
            return

        session_lines = []
        for info in infos:
            project = info["project"]
            conv = _fetch_recent_conversation(project, limit=5)
            recent = conv[:400] if conv else "(최근 대화 없음)"
            session_lines.append(
                f"### {project}\n"
                f"- 미사용: {info['idle_hours']}시간\n"
                f"- 최근 대화:\n```\n{recent}\n```"
            )

        report = (
            f"[stall-check 리포트 — {datetime.now().strftime('%Y-%m-%d %H:%M')}]\n\n"
            f"활성 세션 {len(infos)}개의 최근 대화입니다.\n"
            f"에이전트가 응답해야 하는데 못 하고 있는 경우가 있으면 릴레이로 깨워주세요.\n"
            f"없으면 '없음'으로 짧게 답하세요.\n\n"
            + "\n\n".join(session_lines)
        )

        api_key = config.get("api_key", "")
        if not api_key:
            return

        result = api_request(api_key, "POST", "/api/bot/message", body={
            "project": SESSION_MANAGER_PROJECT,
            "text": report,
            "type": "user",
            "subtype": "stall_check_report",
            "processed": False,
        }, timeout=10)

        if result and result.get("id"):
            logger.info(f"[stall-check] Report sent ({len(infos)} sessions)")
        else:
            logger.error("[stall-check] Failed to send report")

    def run(self):
        logger.info("[session-health] Started (health=2h, stall=30m)")
        shutdown_event.wait(1800)  # initial wait

        last_health = 0.0
        last_stall = 0.0

        while not shutdown_event.is_set():
            now = time.time()

            if now - last_health >= self.HEALTH_CHECK_INTERVAL:
                try:
                    self._check_sessions()
                except Exception as e:
                    logger.error(f"[session-health] Check error: {e}")
                last_health = now

            if now - last_stall >= self.STALL_CHECK_INTERVAL:
                try:
                    self._check_stalls()
                except Exception as e:
                    logger.error(f"[stall-check] Error: {e}")
                last_stall = now

            shutdown_event.wait(60)  # tick every 60s

        logger.info("[session-health] Stopped")
