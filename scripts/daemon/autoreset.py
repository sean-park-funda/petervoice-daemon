"""세션 자동리셋 전용 스레드 (결정론적, LLM 리포트 없음).

왜 health.py와 분리했나:
  SessionHealthChecker는 세션매니저(LLM)에게 정기 리포트를 보내 불필요한 작업을 많이
  유발해서 Sean이 꺼둔 상태다(config `session_health.enabled=false`).
  반면 세션 자동리셋은 조용하고(로그만) 임계 초과 세션에만 요약을 부르므로,
  헬스체커와 독립적으로 켤 수 있어야 한다. 한 스위치에 묶으면 하나를 끄면 둘 다 꺼진다.

무엇을 하나:
  주기적으로 활성 세션의 수명/메시지수/transcript(.jsonl) 크기를 보고, 임계 초과 세션에
  save_session_context()(슬라이딩 윈도우 씨앗 저장) → reset_session()을 건다.
  맥락 보존 우선: 컨텍스트 저장이 성공했을 때만 리셋(실패 시 다음 주기 재시도).

스탬피드 방지:
  상한은 '처리 개수'가 아니라 'LLM 요약 호출 수'로 건다. 요약할 내용이 없는 스테일
  세션(메시지 <= stale 임계)은 요약을 건너뛰고 바로 리셋 → 상한을 소비하지 않는다.
  최초 배포 시 기존 적체가 한꺼번에 잡히는 것을 막으면서도 적체가 빠르게 빠진다.

config:
  session_auto_reset: {"enabled": true, "interval_hours": 2}
  session_max_lifetime_hours (48)  session_max_messages (500)
  session_max_jsonl_mb (10)        session_stale_message_count (3)
  session_reset_max_per_cycle (20) — LLM 요약 호출 상한/사이클
  임계는 0/음수면 해당 조건 비활성.
"""

from datetime import datetime
from pathlib import Path
import threading

import daemon.globals as g
from daemon.globals import (
    SESSION_MANAGER_PROJECT, config, sessions_lock, shutdown_event, logger,
)
from daemon.sessions import save_session_context, reset_session


def session_jsonl_mb(session_id: str) -> float:
    """세션 transcript(.jsonl) 크기를 MB로 반환. 못 찾으면 0.0.

    Claude Code는 세션 이력을 ~/.claude/projects/<slug>/<session_id>.jsonl에 두고
    --resume 시 매 턴 프리필한다 → 크기가 지연·토큰비용의 가장 직접적인 신호.
    프로젝트 slug을 몰라도 되도록 session_id 파일명으로 탐색한다.
    """
    if not session_id:
        return 0.0
    try:
        base = Path.home() / ".claude" / "projects"
        if not base.is_dir():
            return 0.0
        for p in base.glob(f"*/{session_id}.jsonl"):
            return round(p.stat().st_size / (1024 * 1024), 1)
    except Exception:
        pass
    return 0.0


def collect_sessions_info() -> list[dict]:
    """활성 세션의 수명/메시지수/transcript 크기 수집."""
    infos = []
    with sessions_lock:
        items = list(g.sessions.items())
    for key, sess in items:
        # key = "{project}:{task}" — project 자체가 콜론을 포함할 수 있다(예: "branch:135")
        project = key.rsplit(":", 1)[0] if ":" in key else key
        if project == SESSION_MANAGER_PROJECT:
            continue
        lifetime_h = 0.0
        created = sess.get("created_at", "")
        if created:
            try:
                lifetime_h = (datetime.now() - datetime.fromisoformat(created)).total_seconds() / 3600
            except Exception:
                pass
        sid = sess.get("session_id", "")
        infos.append({
            "project": project,
            "key": key,
            "message_count": sess.get("message_count", 0),
            "lifetime_hours": round(lifetime_h, 1),
            "jsonl_mb": session_jsonl_mb(sid),
        })
    return infos


def auto_reset_oversized_sessions(infos: list[dict]) -> dict:
    """임계 초과 세션을 저장 후 리셋. 처리 통계를 반환."""
    max_life = config.get("session_max_lifetime_hours", 48)
    max_msgs = config.get("session_max_messages", 500)
    max_mb = config.get("session_max_jsonl_mb", 10)
    stale_msgs = config.get("session_stale_message_count", 3)
    per_cycle = config.get("session_reset_max_per_cycle", 20)

    candidates = []
    for info in infos:
        reasons = []
        if max_life and max_life > 0 and info["lifetime_hours"] >= max_life:
            reasons.append(f"lifetime {info['lifetime_hours']}h>={max_life}h")
        if max_msgs and max_msgs > 0 and info["message_count"] >= max_msgs:
            reasons.append(f"msgs {info['message_count']}>={max_msgs}")
        # 크기 임계: 메시지 수가 적어도 턴당 내용이 무거우면(대량 도구출력·문서추출)
        # 여기서만 잡힌다. 프리필 비용의 직접 원인이라 지연 완화엔 가장 정확한 신호.
        if max_mb and max_mb > 0 and info["jsonl_mb"] >= max_mb:
            reasons.append(f"transcript {info['jsonl_mb']}MB>={max_mb}MB")
        if reasons:
            candidates.append((info, "; ".join(reasons)))

    stats = {"oversized": len(candidates), "reset": 0, "summarized": 0, "stale": 0, "deferred": 0}
    if not candidates:
        return stats

    # 임계 대비 초과 비율이 큰 것부터(오래됨/큼 중 더 심한 쪽 기준) → 지연 유발 세션 우선.
    def severity(info):
        r = 0.0
        if max_life and max_life > 0:
            r = max(r, info["lifetime_hours"] / max_life)
        if max_msgs and max_msgs > 0:
            r = max(r, info["message_count"] / max_msgs)
        if max_mb and max_mb > 0:
            r = max(r, info["jsonl_mb"] / max_mb)
        return r

    candidates.sort(key=lambda c: severity(c[0]), reverse=True)
    logger.info(f"[auto-reset] {len(candidates)} oversized session(s) (LLM summary cap {per_cycle}/cycle)")

    for info, reason in candidates:
        if shutdown_event.is_set():
            break
        project, key = info["project"], info["key"]
        is_stale = stale_msgs >= 0 and info["message_count"] <= stale_msgs

        if not is_stale and per_cycle and per_cycle > 0 and stats["summarized"] >= per_cycle:
            stats["deferred"] += 1
            continue  # 요약 상한 소진 → 다음 사이클 (스테일은 계속 처리)

        if is_stale:
            # 대화가 거의 없는 세션: 요약 생략(비용 0)하고 리셋만.
            reset_session(project, reason=f"auto-reset stale ({reason}, {info['message_count']}msg)",
                          key_override=key)
            stats["stale"] += 1
            stats["reset"] += 1
            continue

        logger.info(f"[auto-reset] {project} ({reason}) — saving context first")
        saved = False
        try:
            saved = save_session_context(project)
        except Exception as e:
            logger.error(f"[auto-reset] save_session_context failed for {project}: {e}")
        stats["summarized"] += 1  # 성공/실패 무관하게 LLM 호출 발생 → 상한 소비
        if saved:
            reset_session(project, reason=f"auto-reset ({reason})", key_override=key)
            stats["reset"] += 1
            logger.info(f"[auto-reset] done: {project}")
        else:
            logger.warning(
                f"[auto-reset] skipped {project} — context save failed; keeping session "
                f"to avoid losing context, will retry next cycle"
            )

    logger.info(
        f"[auto-reset] cycle done: reset {stats['reset']} "
        f"(summarized {stats['summarized']}, stale-no-summary {stats['stale']}), "
        f"deferred {stats['deferred']}"
    )
    return stats


class SessionAutoResetThread(threading.Thread):
    """주기적으로 오버사이즈 세션을 자동 리셋하는 경량 스레드."""

    def __init__(self, interval_hours: float = 2.0, initial_delay_sec: int = 300):
        super().__init__(daemon=True, name="session-auto-reset")
        self.interval = max(float(interval_hours), 0.1) * 3600
        self.initial_delay = max(int(initial_delay_sec), 0)

    def run(self):
        logger.info(
            f"[auto-reset] Thread started (interval={self.interval / 3600:.1f}h, "
            f"first run in {self.initial_delay}s)"
        )
        if shutdown_event.wait(self.initial_delay):
            return
        while not shutdown_event.is_set():
            try:
                infos = collect_sessions_info()
                if infos:
                    auto_reset_oversized_sessions(infos)
                else:
                    logger.info("[auto-reset] no active sessions")
            except Exception as e:
                logger.error(f"[auto-reset] cycle error: {e}")
            if shutdown_event.wait(self.interval):
                break
        logger.info("[auto-reset] Thread stopped")
