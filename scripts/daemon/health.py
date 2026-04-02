"""Session health checker thread."""

from datetime import datetime
import threading

import daemon.globals as g
from daemon.globals import (
    SESSION_MANAGER_PROJECT, config, sessions_lock, shutdown_event, logger,
)
from daemon.sessions import save_session_context, save_sessions
from daemon.supabase import _fetch_recent_conversation
from daemon.api import api_request


class SessionHealthChecker(threading.Thread):
    """Periodically checks session health via a dedicated 'session-manager' project."""

    CHECK_INTERVAL = 2 * 3600  # 2 hours

    def __init__(self):
        super().__init__(daemon=True, name="session-health-checker")
        self.api_key = config["api_key"]

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

    def _check_sessions(self):
        infos = self._get_all_sessions_info()
        if not infos:
            logger.info("[session-health] No active sessions to check")
            return

        self._presave_expiring_sessions(infos)

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

    def run(self):
        logger.info("[session-health] Health checker started (interval=2h)")
        shutdown_event.wait(1800)

        while not shutdown_event.is_set():
            try:
                self._check_sessions()
            except Exception as e:
                logger.error(f"[session-health] Check error: {e}")
            shutdown_event.wait(self.CHECK_INTERVAL)

        logger.info("[session-health] Health checker stopped")
