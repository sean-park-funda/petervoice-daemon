"""자율 에이전트 폭주 상한 (2026-07-29)

데몬은 --dangerously-skip-permissions 로 사람 확인 없이 돈다. 바깥 통제(동시 턴 수·타임아웃)는
있었지만 "턴 하나가 안에서 얼마나 커지나"에는 상한이 없었다 — 서브에이전트가 또 서브에이전트를
낳으면 지수적으로 불어난다.

여기 값은 **폭주만 막는** 수준이다. 정상 작업을 끊지 않는 것이 더 중요하므로 넉넉하게 잡고,
config.json 으로 언제든 풀 수 있게 한다(고객이 걸리면 코드 배포 없이 대응).

설계 메모: MAX_TURNS 는 일부러 걸지 않는다 — claude_hard_timeout_sec 가 이미 같은 역할을 하고,
둘 다 걸면 긴 정상 작업이 예고 없이 끊긴다.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from daemon.globals import DAEMON_DIR, logger

# 환경변수 이름 → config.json 키. 값은 고객 배포 기본값(사내는 config.json 에서 더 조인다).
LIMIT_KEYS: dict[str, tuple[str, int]] = {
    # 재귀 깊이 — 유일한 지수적 폭발 경로라 여기만은 사내·고객 동일하게 조인다
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": ("max_subagent_depth", 2),
    "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": ("max_subagents_per_session", 100),
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": ("max_concurrent_subagents", 8),
    "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": ("max_web_searches_per_session", 60),
}

HITS_FILE = Path(DAEMON_DIR) / "limit-hits.jsonl"

# CLI 가 상한에 걸렸을 때 내보내는 문구(2.1.220 바이너리에서 확인).
# 조용히 죽지 않고 에이전트가 읽을 수 있는 메시지로 돌아온다 → 데몬도 같은 문자열로 감지한다.
CAP_MESSAGES: list[tuple[str, str]] = [
    ("Subagent nesting limit reached", "서브에이전트 중첩 깊이"),
    ("Subagent spawn limit reached", "서브에이전트 개수"),
    ("Concurrent subagent limit reached", "동시 서브에이전트 수"),
    ("Web search limit reached", "웹검색 횟수"),
]


def limit_env(config: dict) -> dict[str, str]:
    """상한 환경변수만 담은 dict. config.json 값이 있으면 그것을 쓴다.

    값을 0 이하로 두면 그 항목은 해제된다(특정 고객을 풀어줄 때).
    """
    env: dict[str, str] = {}
    for var, (cfg_key, default) in LIMIT_KEYS.items():
        try:
            value = int(config.get(cfg_key, default))
        except (TypeError, ValueError):
            logger.warning(f"[limits] config.{cfg_key} 값이 정수가 아님 — 기본값 {default} 사용")
            value = default
        if value > 0:
            env[var] = str(value)
    return env


def build_claude_env(config: dict, account_config_dir: str | None = None) -> dict[str, str]:
    """claude CLI 를 띄울 때 쓰는 공용 env.

    claude 를 실행하는 지점이 5곳(본 턴·rewriter·bp·kanban·sessions)이라 각자 env 를 만들면
    한 곳만 고쳐도 나머지로 샌다. 반드시 이 함수를 거칠 것.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["LANG"] = "en_US.UTF-8"
    env.update(limit_env(config))
    if account_config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(account_config_dir)
    return env


def detect_cap_hit(line: str) -> str | None:
    """스트림 한 줄에서 상한 도달 문구를 찾는다. 걸렸으면 사람이 읽을 이름을 돌려준다."""
    for needle, label in CAP_MESSAGES:
        if needle in line:
            return label
    return None


def record_cap_hit(project: str, label: str, config: dict) -> None:
    """상한 도달을 파일에 적립한다.

    별도 계측 파이프라인을 만들지 않은 이유가 이것이다 — 실제로 걸린 순간만 남기면
    "값이 빡빡한가"를 나중에 이 파일만 보고 판단할 수 있다.
    """
    try:
        HITS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HITS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "project": project,
                "limit": label,
                "values": limit_env(config),
            }, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"[limits] 기록 실패: {e}")


def cap_notice(label: str) -> str:
    """채팅에 띄울 안내 — 로그가 아니라 사람 말로."""
    return (
        f"⚠️ 안전장치가 작동했습니다 — {label} 상한에 도달해 일부 작업이 중단됐습니다.\n"
        f"결과가 불완전할 수 있습니다. 반복되면 상한을 조정할 수 있으니 알려주세요."
    )


# ══ 계정 사용 한도 (모델별 / 세션창) ══════════════════════════════
# 위의 CAP_* 는 "우리가 건 상한"이고, 여기는 "Anthropic 계정 쿼터"다. 처방이 서로 다르다.
#
#   ① "You've reached your Fable 5 limit. Switch to another model, ..."
#      → 모델별 주간 쿼터. **다른 모델은 살아있다** → 폴백 모델로 1회 재시도하면 살아난다.
#   ② "You've hit your session limit · resets 4:10am (Asia/Seoul)"
#      → 계정 전체(5시간 창). 모델을 바꿔도 소용없다 → 리셋까지 스폰 자체를 멈춘다.
#
# 2026-08-16~18 실사고: 두 문구 모두 어느 에러 분기에도 안 걸려 하트비트가 조용히 죽었고
# (evolution 2사이클 유실), 08-18 02:18~02:36 에는 릴레이 백로그가 세션 한도에 부딪혀
# 18분간 201회를 헛스폰했다. 계정 한도는 공유 자원이라 한 프로젝트의 폭주가 다른 프로젝트의
# 자율 루프를 죽인다 → 감지는 프로젝트별이지만 쿨다운은 **데몬 전역**이어야 한다.
_MODEL_LIMIT_RE = re.compile(r"reached your ([A-Za-z0-9 .\-]{1,30}?) limit", re.I)
_SESSION_LIMIT_RE = re.compile(r"hit your ([a-z]+) limit", re.I)
_RESET_RE = re.compile(r"resets\s+([^·\n]{1,40})", re.I)

# 쿨다운 중에도 이 간격으로 한 번은 진짜로 찔러본다 — 리셋 시각 파싱이 틀렸거나 한도가 일찍
# 풀렸을 때 스스로 복구하기 위한 카나리아. (없으면 오판 한 번이 몇 시간을 먹는다)
COOLDOWN_PROBE_INTERVAL = 600
COOLDOWN_DEFAULT_SEC = 1800      # 리셋 시각을 못 읽었을 때
COOLDOWN_MAX_SEC = 6 * 3600      # 파싱이 틀려도 이 이상은 절대 멈추지 않는다

# 한도는 **계정** 단위 자원이다. 멀티계정(config.accounts)에서 A계정이 막혔다고 B계정까지
# 세우면 안 되므로 계정 이름으로 나눠 건다.
_cooldown_lock = threading.Lock()
_cooldowns: dict[str, dict] = {}

# 주기 /usage 가 알려주는 기본 계정 이메일. 바뀌면 = 재로그인 → 옛 쿨다운은 무효.
_last_account_email: str | None = None
# 이 아래면 "세션 한도"라는 판단이 틀린 것으로 본다(한도면 100% 근처여야 한다).
USAGE_CLEAR_PCT = 90


def _slot(account: str) -> dict:
    return _cooldowns.setdefault(
        account or "default", {"until": 0.0, "next_probe": 0.0, "reason": "", "resets": ""}
    )


def _parse_reset_ts(reset_text: str) -> float | None:
    """"4:10am (Asia/Seoul)" → 다음 4:10 의 epoch. 못 읽으면 None.

    메시지의 타임존은 CLI 가 로컬 기준으로 찍어주므로 머신 로컬시로 해석한다.
    """
    if not reset_text:
        return None
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", reset_text, re.I)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "p":
        hour += 12
    now = datetime.now()
    target = now.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def detect_usage_limit(text: str) -> dict | None:
    """CLI 에러 원문에서 계정 한도를 판별한다. 아니면 None."""
    if not text:
        return None
    resets = ""
    rm = _RESET_RE.search(text)
    if rm:
        resets = rm.group(1).strip()
    mm = _MODEL_LIMIT_RE.search(text)
    if mm:
        return {"kind": "model", "label": mm.group(1).strip(), "resets": resets}
    sm = _SESSION_LIMIT_RE.search(text)
    if sm:
        return {"kind": "session", "label": sm.group(1).strip(), "resets": resets}
    return None


def start_account_cooldown(reason: str, resets: str = "", account: str = "default") -> float:
    """계정 한도 쿨다운 시작. 쿨다운 종료 epoch 반환."""
    ts = _parse_reset_ts(resets)
    now = time.time()
    until = ts if (ts and ts > now) else now + COOLDOWN_DEFAULT_SEC
    until = min(until, now + COOLDOWN_MAX_SEC)
    with _cooldown_lock:
        slot = _slot(account)
        if until > slot["until"]:
            slot.update({"until": until, "reason": reason, "resets": resets,
                         "next_probe": now + COOLDOWN_PROBE_INTERVAL})
        else:
            until = slot["until"]
    logger.warning(
        f"[limits] 계정 한도 쿨다운 시작 (account={account}) — {reason}"
        f"{f' (resets {resets})' if resets else ''}, {int((until - now) / 60)}분간 CLI 스폰 중단"
    )
    return until


def clear_account_cooldown(account: str = "default") -> None:
    """턴이 정상 완료되면 호출 — 한도가 풀렸다는 뜻이므로 즉시 해제."""
    with _cooldown_lock:
        slot = _slot(account)
        if slot["until"]:
            logger.info(f"[limits] 계정 한도 쿨다운 해제 (account={account}, 정상 응답 확인)")
            slot.update({"until": 0.0, "next_probe": 0.0, "reason": "", "resets": ""})


def clear_account_cooldown_if_stale(email: str | None, session_pct: int | None) -> None:
    """주기 /usage 수집이 부를 것 — 쿨다운이 이미 무의미해졌으면 스스로 푼다.

    쿨다운은 데몬 메모리에만 있고 해제 경로가 "턴이 정상 완료됐을 때" 하나뿐이었다.
    그래서 한도에 걸린 계정에서 **다른 계정으로 재로그인**하면, 배너(/usage)는 새 계정을
    정상 표시하는데 채팅은 옛 계정의 리셋 시각까지 계속 막혀 있었다 — 쿨다운 키가 실제
    Anthropic 계정이 아니라 config 라벨("default")이라 계정이 바뀐 걸 알 수도 없다.
    (2026-08-20 실사고: 20:32~20:44, 카나리아 프로브가 풀 때까지 12분간 전 프로젝트 차단)

    두 가지 신호로 푼다:
      ① 계정 이메일이 바뀌었다 → 옛 계정의 한도는 이제 무관하다.
      ② 세션 사용률에 여유가 있다 → 한도라는 판단 자체가 틀렸다(리셋 시각 오파싱 포함).

    **"default" 슬롯만** 건드린다. /usage 는 계정 config dir 지정 없이 도므로 기본 계정의
    상태만 말해준다 — 이걸로 다른 계정의 쿨다운까지 풀면 진짜 막힌 계정을 헛스폰시킨다.
    """
    global _last_account_email
    with _cooldown_lock:
        prev, _last_account_email = _last_account_email, email or _last_account_email
        active = bool(_slot("default")["until"])
    if not active:
        return
    if email and prev and email != prev:
        logger.info(f"[limits] 계정 변경 감지 ({prev} → {email}) — 쿨다운 해제")
        clear_account_cooldown("default")
        return
    if session_pct is not None and session_pct < USAGE_CLEAR_PCT:
        logger.info(f"[limits] 세션 사용률 {session_pct}% (여유 있음) — 쿨다운 오판으로 보고 해제")
        clear_account_cooldown("default")


def account_cooldown_notice(account: str = "default") -> str | None:
    """쿨다운 중이면 사용자에게 보낼 안내를 돌려준다(=CLI 스폰 생략).

    None 이면 실행해도 된다. 쿨다운 중이라도 COOLDOWN_PROBE_INTERVAL 마다 한 번은
    None 을 돌려줘 실제로 찔러보게 한다(조기 복구).
    """
    now = time.time()
    with _cooldown_lock:
        slot = _slot(account)
        until = slot["until"]
        if not until or now >= until:
            if until:
                slot.update({"until": 0.0, "next_probe": 0.0, "reason": "", "resets": ""})
            return None
        if now >= slot["next_probe"]:
            slot["next_probe"] = now + COOLDOWN_PROBE_INTERVAL
            return None
        reason, resets = slot["reason"], slot["resets"]
    mins = max(1, int((until - now) / 60))
    tail = f" (리셋: {resets})" if resets else ""
    return (
        f"⏸ Claude 계정 사용 한도에 걸려 있습니다 — {reason}{tail}.\n"
        f"약 {mins}분 뒤 자동으로 재개됩니다. 이 메시지는 처리되지 않았으니 그때 다시 요청해주세요."
    )


def usage_limit_notice(limit: dict, fallback_note: str = "") -> str:
    """한도로 턴이 끝났을 때 사용자에게 보낼 안내."""
    resets = f" (리셋: {limit['resets']})" if limit.get("resets") else ""
    if limit["kind"] == "model":
        head = f"⚠️ {limit['label']} 모델 사용 한도에 도달했습니다{resets}."
        body = fallback_note or "다른 모델로 전환하거나 한도 리셋을 기다려야 합니다 (설정 > 모델)."
    else:
        head = f"⏸ Claude 계정 세션 한도에 도달했습니다{resets}."
        body = "리셋 전까지는 모델을 바꿔도 실행되지 않습니다. 리셋 후 다시 요청해주세요."
    return f"{head}\n{body}"
