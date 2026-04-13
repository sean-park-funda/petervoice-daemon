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

    _SESSION_MANAGER_PROMPT = """\
# Session Lifecycle Manager

당신은 피터보이스 데몬의 **세션 관리자**입니다.

## 역할
1. **세션 건강 관리**: 정기 리포트를 받고, 리셋이 필요한 세션을 Sean에게 제안
2. **Stall Detection**: 에이전트가 응답해야 하는데 못 하고 있으면 릴레이로 깨움

## DB 조회 방법

```bash
SUPABASE_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c['supabase_url'])")
SUPABASE_KEY=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c['supabase_key'])")

# 특정 프로젝트의 최근 메시지 조회
curl -s "${SUPABASE_URL}/rest/v1/messages?project=eq.{프로젝트명}&user_id=eq.1&order=created_at.desc&limit=15&select=type,text,created_at" \\
  -H "apikey: ${SUPABASE_KEY}" -H "Authorization: Bearer ${SUPABASE_KEY}"
```

## 세션 건강 ([정기 세션 점검 리포트] 수신 시)

- 눈에 띄는 세션은 DB에서 직접 조회해서 상세 분석
- 리셋 제안 시: "**[제안] {프로젝트명}** 리셋을 제안합니다. 근거: {이유}"
- 정상이면: 간단히 "모든 세션 정상"

### 리셋 실행 (승인 후)
```bash
python3 -c "
import json
projects = ['project1']
with open('$HOME/.claude-daemon/pending_resets.json', 'w') as f:
    json.dump(projects, f)
"
```

### TTL 초과 세션
TTL(24시간) 초과 세션은 승인 없이 즉시 리셋. 리셋 후 보고.

## Stall Detection ([stall-check 리포트] 수신 시)

### 깨워야 하는 경우
- "잠시만요", "확인해드릴게요" 등 미완료 약속 후 30분 이상 침묵
- 타임아웃/에러로 끊긴 흔적
- 백그라운드 작업 시작 후 완료 보고 없음

### 깨우지 않아야 하는 경우
- 자연스럽게 끝난 대화 ("감사합니다", "다 됐어", 결과 보고 후 침묵)
- 유저가 의도적 보류 ("나중에", "내일")
- 유저 메시지가 마지막

### 깨우는 방법
```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://peter-voice.vercel.app'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")

curl -X POST "$API_URL/api/relay/message" \\
  -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \\
  -d '{"from_project": "session-manager", "to_project": "대상", "text": "[stall-check] 맥락 + 이어서 할 작업"}'
```

### 응답 규칙 (토큰 절약)
- 깨울 대상 없으면: **"없음"** 한 마디로 끝
- 깨울 대상 있으면: nudge 후 한 줄 보고
- 같은 대상에 연속 nudge 금지

## 원칙
- 기계적 임계값이 아니라 맥락으로 판단
- session-manager 자체 세션은 리셋하지 말 것
"""

    def _ensure_session_manager(self) -> bool:
        """Ensure session-manager project exists. Auto-create if missing. Cached."""
        if self._has_session_manager is not None:
            return self._has_session_manager

        result = api_request(self.api_key, "GET", "/api/projects", timeout=5)
        if result and "projects" in result:
            exists = any(
                p.get("id") == SESSION_MANAGER_PROJECT for p in result["projects"]
            )
        else:
            self._has_session_manager = False
            return False

        if exists:
            self._has_session_manager = True
            return True

        # Auto-create session-manager project
        logger.info(f"[session-health] Creating '{SESSION_MANAGER_PROJECT}' project...")

        # 1. Create project
        create_result = api_request(self.api_key, "POST", "/api/projects", body={
            "id": SESSION_MANAGER_PROJECT,
            "name": "Session Manager",
        }, timeout=10)

        if not create_result or "error" in str(create_result).lower():
            logger.error(f"[session-health] Failed to create project: {create_result}")
            self._has_session_manager = False
            return False

        # 2. Set model to haiku
        api_request(self.api_key, "PUT", "/api/projects", body={
            "id": SESSION_MANAGER_PROJECT,
            "model": "haiku",
        }, timeout=5)

        # 3. Set prompt
        api_request(self.api_key, "PUT", "/api/prompts", body={
            "project": SESSION_MANAGER_PROJECT,
            "content": self._SESSION_MANAGER_PROMPT,
        }, timeout=5)

        logger.info(f"[session-health] '{SESSION_MANAGER_PROJECT}' project created with haiku model")
        self._has_session_manager = True
        return True

    def _check_sessions(self):
        infos = self._get_all_sessions_info()
        if not infos:
            logger.info("[session-health] No active sessions to check")
            return

        self._presave_expiring_sessions(infos)

        if not self._ensure_session_manager():
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
        if not self._ensure_session_manager():
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
