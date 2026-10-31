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
  session_max_context_tokens (250000) — 마지막 호출의 프리필 토큰(입력+캐시 쓰기+캐시 읽기). 2026-10-31 추가:
      파일 크기보다 문맥 토큰이 비용을 정한다 — 946 은 메시지 20개·9.7MB 인데 문맥 45만이었다(큰 파일 읽기).
  session_summary_max_failures (3) — 컨텍스트 저장이 이 횟수 연속 실패하면 저장 없이 리셋한다. 2026-10-31 추가:
      대화가 없는 옛 세션 5개가 매 사이클 요약 실패→보존→재시도를 반복해 2시간마다 LLM 호출 5회를 헛썼다.
  session_reset_max_per_cycle (20) — LLM 요약 호출 상한/사이클
  임계는 0/음수면 해당 조건 비활성.
"""

from datetime import datetime
from pathlib import Path
import threading

import json

import daemon.globals as g
from daemon.globals import (
    SESSION_MANAGER_PROJECT, config, sessions_lock, shutdown_event, logger,
)
from daemon.sessions import save_session_context, reset_session

# 세션 키 → 연속 컨텍스트 저장 실패 횟수 (데몬 수명 동안)
_save_failures: dict[str, int] = {}


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


def _session_jsonl_path(session_id: str):
    if not session_id:
        return None
    try:
        base = Path.home() / ".claude" / "projects"
        if base.is_dir():
            for p in base.glob(f"*/{session_id}.jsonl"):
                return p
    except Exception:
        pass
    return None


def context_tokens_from_tail(data: bytes) -> int:
    """전사 꼬리(bytes)에서 마지막 assistant 메시지의 프리필 토큰을 읽는다 — input + cache_creation + cache_read.
    이 값이 다음 호출이 다시 읽을 문맥의 크기다(= 빈 하트비트 1회 비용의 거의 전부)."""
    best = 0
    for raw in reversed(data.split(b"\n")):
        if b'"usage"' not in raw or b'"assistant"' not in raw:
            continue
        try:
            e = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if e.get("type") != "assistant":
            continue
        u = (e.get("message") or {}).get("usage") or {}
        tot = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
        if tot:
            return tot
    return best


def session_context_tokens(session_id: str, tail_bytes: int = 512 * 1024) -> int:
    """세션의 마지막 호출 문맥 토큰. 못 찾으면 0. 큰 전사라도 꼬리만 읽는다."""
    p = _session_jsonl_path(session_id)
    if not p:
        return 0
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            return context_tokens_from_tail(f.read())
    except Exception:
        return 0


def thresholds() -> dict:
    return {
        "max_life": config.get("session_max_lifetime_hours", 48),
        "max_msgs": config.get("session_max_messages", 500),
        "max_mb": config.get("session_max_jsonl_mb", 10),
        "max_ctx": config.get("session_max_context_tokens", 250000),
        "stale_msgs": config.get("session_stale_message_count", 3),
        "per_cycle": config.get("session_reset_max_per_cycle", 20),
        "max_failures": config.get("session_summary_max_failures", 3),
    }


def oversize_reasons(info: dict, th: dict) -> list[str]:
    """임계 초과 사유 목록. 비어 있으면 정상."""
    reasons = []
    if th["max_life"] and th["max_life"] > 0 and info.get("lifetime_hours", 0) >= th["max_life"]:
        reasons.append(f"lifetime {info['lifetime_hours']}h>={th['max_life']}h")
    if th["max_msgs"] and th["max_msgs"] > 0 and info.get("message_count", 0) >= th["max_msgs"]:
        reasons.append(f"msgs {info['message_count']}>={th['max_msgs']}")
    # 크기 임계: 메시지 수가 적어도 턴당 내용이 무거우면(대량 도구출력·문서추출) 여기서만 잡힌다.
    if th["max_mb"] and th["max_mb"] > 0 and info.get("jsonl_mb", 0) >= th["max_mb"]:
        reasons.append(f"transcript {info['jsonl_mb']}MB>={th['max_mb']}MB")
    # 문맥 토큰: 프리필 비용의 직접 원인. 파일 크기(10MB)에 못 미쳐도 문맥 45만이 될 수 있다(2026-10-31 실측 946·954).
    if th["max_ctx"] and th["max_ctx"] > 0 and info.get("context_tokens", 0) >= th["max_ctx"]:
        reasons.append(f"context {info['context_tokens'] // 1000}k>={int(th['max_ctx']) // 1000}k")
    return reasons


def severity(info: dict, th: dict) -> float:
    r = 0.0
    for key, lim in (("lifetime_hours", th["max_life"]), ("message_count", th["max_msgs"]),
                     ("jsonl_mb", th["max_mb"]), ("context_tokens", th["max_ctx"])):
        if lim and lim > 0:
            r = max(r, float(info.get(key, 0) or 0) / float(lim))
    return r


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
            "context_tokens": session_context_tokens(sid),
        })
    return infos


def auto_reset_oversized_sessions(infos: list[dict]) -> dict:
    """임계 초과 세션을 저장 후 리셋. 처리 통계를 반환."""
    th = thresholds()
    stale_msgs = th["stale_msgs"]
    per_cycle = th["per_cycle"]
    max_failures = th["max_failures"]

    candidates = []
    for info in infos:
        reasons = oversize_reasons(info, th)
        if reasons:
            candidates.append((info, "; ".join(reasons)))

    stats = {"oversized": len(candidates), "reset": 0, "summarized": 0, "stale": 0, "deferred": 0, "gave_up": 0}
    if not candidates:
        return stats

    # 임계 대비 초과 비율이 큰 것부터(오래됨/큼/문맥 중 더 심한 쪽 기준) → 지연·비용 유발 세션 우선.
    candidates.sort(key=lambda c: severity(c[0], th), reverse=True)
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
            _save_failures.pop(key, None)
            reset_session(project, reason=f"auto-reset ({reason})", key_override=key)
            stats["reset"] += 1
            logger.info(f"[auto-reset] done: {project}")
        else:
            _save_failures[key] = _save_failures.get(key, 0) + 1
            if max_failures and max_failures > 0 and _save_failures[key] >= max_failures:
                # 저장할 대화 자체가 없는 세션(지워진 프로젝트·옛 팀 리드 키)은 영원히 실패한다 → 그만 붙잡는다
                reset_session(project, reason=f"auto-reset after {_save_failures[key]} failed context saves ({reason})",
                              key_override=key)
                _save_failures.pop(key, None)
                stats["reset"] += 1
                stats["gave_up"] += 1
                logger.warning(f"[auto-reset] {project} — context save failed {max_failures}x in a row; reset without summary")
            else:
                logger.warning(
                    f"[auto-reset] skipped {project} — context save failed ({_save_failures[key]}/{max_failures}); keeping session "
                    f"to avoid losing context, will retry next cycle"
                )

    logger.info(
        f"[auto-reset] cycle done: reset {stats['reset']} "
        f"(summarized {stats['summarized']}, stale-no-summary {stats['stale']}, gave-up {stats['gave_up']}), "
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
