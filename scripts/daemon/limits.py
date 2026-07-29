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
import time
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
