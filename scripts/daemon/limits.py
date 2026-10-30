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
import shlex
import subprocess
import tempfile
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


# ── AGENTS.md 폴백 차단 (2026-09-19 사고) ─────────────────────────────────────
# Claude Code 2.1.27x 부터 내장 플러그인 agents-md@builtin 이 "cwd~루트 체인에 CLAUDE.md 가
# 하나도 없으면 AGENTS.md 를 프로젝트 지시문으로 읽는다"(기본값 claude-md-or-agents-md).
# 데몬은 Codex 실행 때마다 프로젝트 폴더에 AGENTS.md(역할 프롬프트+이전 세션 맥락 스냅샷)를
# 쓰므로, 같은 폴더·하위 폴더의 **Claude 세션에 다른 브랜치의 신원과 낡은 규칙이 주입**된다.
# 작업 디렉터리가 홈인 프로젝트가 Codex 로 돌면 그 맥의 전 프로젝트로 번진다(실제로 발생).
# 이 옵션은 user 설정·--settings·managed 설정에서만 읽힌다(프로젝트 settings.json 은 안 읽힘).
_AGENTS_MD_PLUGIN = "agents-md@builtin"
_AGENTS_MD_MODE = "claude-md"           # CLAUDE.md 만 읽는다
_AGENTS_MD_RECHECK_SEC = 6 * 3600       # 확정 후에도 주기 재확인 (CLI 가 설정을 다시 쓰며 키를 떨굴 수 있다)
_AGENTS_MD_RETRY_SEC = 600              # 일시적 실패는 10분 뒤 재시도 — 한 번 실패로 보호가 영구히 꺼지면 안 된다
_agents_md_next_check: dict[str, float] = {}
_agents_md_locks: dict[str, threading.Lock] = {}
_agents_md_guard = threading.Lock()     # 위 두 dict 만 보호한다. 파일 IO 는 폴더별 락에서.


def _agents_md_apply(base: str) -> tuple[bool, bool]:
    """(고쳤는가, 확정인가). 확정=False 면 호출자가 짧은 주기로 재시도한다."""
    path = os.path.join(base, "settings.json")
    if os.path.islink(path):
        # dotfiles 동기화 등으로 심링크면 링크를 일반 파일로 바꿔치기하지 않고 실제 파일을 고친다
        path = os.path.realpath(path)
    raw: str | None = None
    settings: dict = {}
    mode = 0o600
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            loaded = json.loads(raw) if raw.strip() else {}
        except ValueError as e:
            # 깨진 JSON 은 절대 덮어쓰지 않는다(유저 설정 보호가 우선). 유저가 고치면 다음 주기에 붙는다.
            logger.warning(f"[agents-md] {path} JSON 파싱 실패 — 건드리지 않음: {e}")
            return (False, False)
        if not isinstance(loaded, dict):
            logger.warning(f"[agents-md] {path} 최상위가 객체가 아님 — 건드리지 않음")
            return (False, True)
        settings = loaded
        mode = os.stat(path).st_mode & 0o777
    plugin_configs = settings.setdefault("pluginConfigs", {})
    if not isinstance(plugin_configs, dict):
        return (False, True)
    entry = plugin_configs.setdefault(_AGENTS_MD_PLUGIN, {})
    if not isinstance(entry, dict):
        return (False, True)
    options = entry.setdefault("options", {})
    if not isinstance(options, dict) or options.get("instructionFiles") is not None:
        return (False, True)  # 이미 있음(우리가 넣었거나 유저가 직접 정함) — 존중
    options["instructionFiles"] = _AGENTS_MD_MODE

    target_dir = os.path.dirname(path)
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        # 읽은 뒤 CLI 등 다른 주체가 파일을 바꿨으면 그 변경을 덮어쓰지 않는다 → 다음 주기에 재시도
        current: str | None = None
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                current = f.read()
        if current != raw:
            logger.info(f"[agents-md] {path} 가 처리 중 변경됨 — 이번엔 쓰지 않고 재시도")
            os.unlink(tmp)
            return (False, False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    logger.info(f"[agents-md] AGENTS.md 폴백 차단 옵션 추가: {path}")
    return (True, True)


def ensure_agents_md_ignored(config_dir: str | None = None) -> bool:
    """해당 Claude 설정 폴더의 settings.json 에 AGENTS.md 폴백 차단 옵션을 보장한다.

    - 유저가 instructionFiles 를 이미 정해 뒀으면 건드리지 않는다. 깨진 JSON 도 덮어쓰지 않는다.
    - 원자적 교체. 확정되면 6시간마다, 일시적 실패면 10분 뒤 다시 본다.
    - **어떤 예외도 밖으로 내지 않는다** — build_claude_env 가 죽으면 메시지 처리 전체가 멈춘다.
    반환: 이번 호출에서 파일을 고쳤으면 True.
    """
    try:
        base = os.path.expanduser(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
        now = time.time()
        with _agents_md_guard:
            if now < _agents_md_next_check.get(base, 0.0):
                return False
            # 선점: 같은 폴더를 여러 워커가 동시에 파지 않게 먼저 재시도 시각을 밀어 둔다
            _agents_md_next_check[base] = now + _AGENTS_MD_RETRY_SEC
            lock = _agents_md_locks.setdefault(base, threading.Lock())
        with lock:
            changed, settled = _agents_md_apply(base)
        with _agents_md_guard:
            _agents_md_next_check[base] = time.time() + (_AGENTS_MD_RECHECK_SEC if settled else _AGENTS_MD_RETRY_SEC)
        return changed
    except Exception as e:  # noqa: BLE001 — 여기서 새는 예외는 전 세션을 죽인다
        try:
            logger.warning(f"[agents-md] 설정 보장 실패({config_dir or 'default'}): {e!r} — 세션은 그대로 진행")
        except Exception:
            pass
        return False


# ── 계정 자리(seat) — 설정 한 줄로 계정 교체 (2026-10-14, Sean 맥미니) ──────────────
# config.json 에 "claude_seat": "<이름>" 이 있으면 **모든 계정 라벨(default·secondary…)** 을 그 자리의
# 설정 폴더로 실행한다. 자리 목록: "claude_seats": {"<이름>": {"config_dir": "~/.claude-seats/x"}}
# (config_dir 이 "" 면 기본 자리 ~/.claude). 교체에 재시작이 필요 없도록 config.json 을 mtime 이
# 바뀔 때마다 다시 읽는다. 두 키가 없는 머신(고객 전부)은 기존 동작 그대로다.
# 세션 키는 계정 라벨이라 자리를 바꿔도 세션이 리셋되지 않고, 자리 폴더의 projects/ 는
# ~/.claude/projects 로 심링크돼 있어(peter-voice scripts/claude-seat.py prepare) --resume 이 이어진다.
# 기준 문서: peter-voice docs/plans/2026-10-14-claude-account-seats.md
_seat_state: dict = {"mtime": None, "seat": None, "seats": {}, "hook": None}
_seat_lock = threading.Lock()
_seat_warned: set[str] = set()


def _live_seat_settings() -> tuple[str | None, dict]:
    path = Path(DAEMON_DIR) / "config.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None, {}
    with _seat_lock:
        if mtime != _seat_state["mtime"]:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                seat = data.get("claude_seat") or None
                seats = data.get("claude_seats") if isinstance(data.get("claude_seats"), dict) else {}
                if seat != _seat_state["seat"]:
                    logger.info(f"[seat] 계정 자리 {_seat_state['seat'] or '(기본)'} → {seat or '(기본)'}")
                _seat_state.update(mtime=mtime, seat=seat, seats=seats, hook=data.get("claude_seat_limit_hook") or None)
            except Exception as e:  # noqa: BLE001 — 쓰는 도중의 반쪽 JSON 일 수 있다: 직전 값 유지, 다음 호출에 재시도
                logger.warning(f"[seat] config.json 읽기 실패 — 직전 자리 유지: {e!r}")
        return _seat_state["seat"], _seat_state["seats"]


def active_seat() -> str | None:
    """지금 적용 중인 자리 이름 (설정이 없거나 모르는 이름이면 None)."""
    seat, seats = _live_seat_settings()
    return seat if seat and seat in seats else None


def seat_config_dir(account_name: str | None = "default", config: dict | None = None) -> str | None:
    """계정 라벨 → claude 를 띄울 CLAUDE_CONFIG_DIR (None = 기본 ~/.claude).

    계정→폴더 해석은 반드시 이 함수 하나로 한다 — 본 턴·칸반·세션 요약·재로그인·사용량 측정이
    서로 다른 폴더를 보면 "로그인은 A, 측정은 B" 같은 엇갈림이 생긴다.
    """
    seat, seats = _live_seat_settings()
    if seat:
        spec = seats.get(seat)
        if spec is not None:
            d = spec.get("config_dir") if isinstance(spec, dict) else spec
            # 키체인 항목 이름이 이 문자열의 해시라서 로그인 때와 똑같은 모양(절대경로, 끝 / 없음)으로 맞춘다
            return os.path.expanduser(d).rstrip("/") if d else None
        if seat not in _seat_warned:
            _seat_warned.add(seat)
            logger.warning(f"[seat] claude_seat={seat!r} 가 claude_seats 에 없음 — 무시하고 계정 설정대로 실행")
    name = account_name or "default"
    if name == "default":
        return None
    if config is None:
        from daemon.globals import config
    return (config.get("accounts", {}).get(name) or {}).get("config_dir") or None


# 자동 교체 (2026-10-14 Sean 사전 승인 「A」): 지금 자리가 한도에 막히면 config.json 의
# "claude_seat_limit_hook" 명령(Sean 맥: 사용량 보드 --auto-switch)을 즉시 돌려 다음 자리로 옮긴다.
# 판단(어디로 갈지)은 그 명령이 한다 — 데몬은 부르고, 자리가 바뀌었는지만 본다. 키가 없으면 아무 일도 없다.
_seat_hook_lock = threading.Lock()
_seat_hook_last: dict[str, float] = {}
SEAT_HOOK_TIMEOUT = 120
SEAT_HOOK_MIN_GAP = 120   # 같은 자리로 2분 안에 다시 부르지 않는다 (교체할 곳이 없을 때 턴마다 도는 것 방지)


def run_seat_limit_hook(limited_seat: str | None) -> str | None:
    """한도에 걸린 자리에서 자동 교체를 시도한다. 자리가 바뀌었으면 새 자리 이름, 아니면 None.

    동시에 여러 턴이 한도에 걸려도 훅은 한 번만 돈다 — 나머지는 락에서 기다렸다가 바뀐 자리를 받아 간다.
    """
    _live_seat_settings()  # config.json 이 바뀌었으면 훅 설정도 다시 읽는다
    hook = _seat_state.get("hook")
    if not hook or not limited_seat:
        return None
    with _seat_hook_lock:
        current = active_seat()
        if current and current != limited_seat:
            return current  # 다른 턴이 이미 옮겼다
        now = time.time()
        if now - _seat_hook_last.get(limited_seat, 0.0) < SEAT_HOOK_MIN_GAP:
            return None
        _seat_hook_last[limited_seat] = now
        logger.warning(f"[seat] 자리 {limited_seat} 한도 — 자동 교체 훅 실행")
        try:
            r = subprocess.run(shlex.split(hook) + ["--limited-seat", limited_seat],
                               capture_output=True, text=True, timeout=SEAT_HOOK_TIMEOUT)
            tail = (r.stdout or "").strip().splitlines()[-1:] or (r.stderr or "").strip().splitlines()[-1:]
            logger.info(f"[seat] 훅 종료 rc={r.returncode} {tail[0][:200] if tail else ''}")
        except Exception as e:  # noqa: BLE001 — 훅 실패는 기존 쿨다운 경로로 넘어간다
            logger.warning(f"[seat] 자동 교체 훅 실패: {e!r}")
        new = active_seat()
        return new if new and new != limited_seat else None


def build_claude_env(config: dict, account_config_dir: str | None = None) -> dict[str, str]:
    """claude CLI 를 띄울 때 쓰는 공용 env.

    claude 를 실행하는 지점이 5곳(본 턴·rewriter·bp·kanban·sessions)이라 각자 env 를 만들면
    한 곳만 고쳐도 나머지로 샌다. 반드시 이 함수를 거칠 것.
    account_config_dir 를 안 주면 기본 계정 — 계정 자리(claude_seat)가 걸려 있으면 그 자리로 간다.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["LANG"] = "en_US.UTF-8"
    env.update(limit_env(config))
    if account_config_dir is None:
        account_config_dir = seat_config_dir("default", config)
    if account_config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(account_config_dir)
    # 이 세션이 실제로 쓸 설정 폴더에 AGENTS.md 폴백 차단을 보장 (폴더당 1회, 실패 무해)
    ensure_agents_md_ignored(env.get("CLAUDE_CONFIG_DIR"))
    # claude.ai 커넥터(Gmail·Drive·Docs·Notion·Slack MCP) 자동 로딩 끄기 — 2026-10-07 Sean 승인(vibement #1 이관, KI-027).
    # claude -p 비대화형에선 OAuth 가 안 돼 매 세션 "인증하라" 리마인더만 생기고 담당자가 유저에게 그대로 옮겼다.
    # 실업무 연동은 피터보이스 스킬(env 토큰 주입)이 맡는다. Claude Code 2.1.292 공식 스위치.
    # 되돌리려면 config.json 에 "claudeai_connectors": true (대화형 CLI 는 이 env 를 안 거치므로 영향 없음).
    if not config.get("claudeai_connectors", False):
        env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
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
#   ② "You've hit your session limit · resets 4:10am (Asia/Seoul)"  (CLI 2.1.312 부터 "5-hour limit")
#      → 계정 전체(5시간 창). 모델을 바꿔도 소용없다 → 리셋까지 스폰 자체를 멈춘다.
#
# 2026-08-16~18 실사고: 두 문구 모두 어느 에러 분기에도 안 걸려 하트비트가 조용히 죽었고
# (evolution 2사이클 유실), 08-18 02:18~02:36 에는 릴레이 백로그가 세션 한도에 부딪혀
# 18분간 201회를 헛스폰했다. 계정 한도는 공유 자원이라 한 프로젝트의 폭주가 다른 프로젝트의
# 자율 루프를 죽인다 → 감지는 프로젝트별이지만 쿨다운은 **데몬 전역**이어야 한다.
_MODEL_LIMIT_RE = re.compile(r"reached your ([A-Za-z0-9 .\-]{1,30}?) limit", re.I)
# CLI 2.1.312(2026-10-29)부터 5시간 창 문구가 "session limit" → "5-hour limit" 로 바뀌었다(라벨 표 five_hour/seven_day/
# seven_day_opus/…: "5-hour limit"·"weekly limit"·"Opus limit"·"Sonnet limit"·"Fable limit"·"usage credit limit").
# 옛 `[a-z]+` 는 숫자·하이픈을 못 받아 5시간 한도를 통째로 놓쳤다(evolution 10-29 통보). 라벨은 선택이다 —
# 기업 프록시가 429 에 representative-claim 을 붙이면 클라이언트가 "You've hit your limit" 한 줄로 바꿔 쓴다.
_SESSION_LIMIT_RE = re.compile(r"hit your(?: ([a-z0-9][a-z0-9 .\-]{0,24}?))? limit", re.I)
# 같은 틀로 오는 모델별 주간 한도("hit your Opus limit")는 세션 한도가 아니다 — 다른 모델은 살아 있으니 모델 한도로 분류한다.
_MODEL_LABEL_RE = re.compile(r"^(opus|sonnet|haiku|fable|mythos)\b", re.I)
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

# 이 아래면 "세션 한도"라는 판단이 틀린 것으로 본다(한도면 100% 근처여야 한다).
USAGE_CLEAR_PCT = 90

# 경로별 캐시 — 계정마다 파일이 다르므로 단일 슬롯으로 두면 계정 간에 값이 서로 덮인다.
_email_cache: dict[str, dict] = {}


def claude_account_email(config_dir: str | None = None) -> str | None:
    """해당 계정의 Claude Code 로그인 이메일 (oauthAccount.emailAddress).

    계정마다 CLAUDE_CONFIG_DIR 이 다르고 설정 파일도 그 안에 따로 있다. config_dir 를
    무시하고 기본 계정 파일만 읽으면, secondary 슬롯에 기본 계정 이메일이 기록돼
    "기본 계정 재로그인"이 secondary 쿨다운을 풀어버린다(= 진짜 막힌 계정으로 헛스폰).

    파일이 수 MB까지 커지므로 mtime 이 변할 때만 다시 파싱한다. 호출부도 쿨다운이
    실제로 걸려 있을 때만 부른다 — 평상시 스폰 경로에는 부담이 없다.
    """
    path = (Path(os.path.expanduser(config_dir)) / ".claude.json"
            if config_dir else Path(os.path.expanduser("~/.claude.json")))
    slot = _email_cache.setdefault(str(path), {"mtime": 0.0, "email": None})
    try:
        mtime = path.stat().st_mtime
        if mtime != slot["mtime"]:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
            slot["email"] = (data.get("oauthAccount") or {}).get("emailAddress") or None
            slot["mtime"] = mtime
        return slot["email"]
    except Exception:
        return None


def _slot(account: str) -> dict:
    return _cooldowns.setdefault(
        account or "default",
        {"until": 0.0, "next_probe": 0.0, "reason": "", "resets": "", "email": None},
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
        label = (sm.group(1) or "").strip()
        kind = "model" if _MODEL_LABEL_RE.match(label) else "session"
        return {"kind": kind, "label": label, "resets": resets}
    return None


def start_account_cooldown(reason: str, resets: str = "", account: str = "default",
                           config_dir: str | None = None) -> float:
    """계정 한도 쿨다운 시작. 쿨다운 종료 epoch 반환."""
    ts = _parse_reset_ts(resets)
    now = time.time()
    until = ts if (ts and ts > now) else now + COOLDOWN_DEFAULT_SEC
    until = min(until, now + COOLDOWN_MAX_SEC)
    with _cooldown_lock:
        slot = _slot(account)
        if until > slot["until"]:
            slot.update({"until": until, "reason": reason, "resets": resets,
                         "next_probe": now + COOLDOWN_PROBE_INTERVAL,
                         # 한도에 걸린 게 "어느 계정"이었는지. 이게 바뀌면 = 재로그인.
                         "email": claude_account_email(config_dir)})
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
            slot.update({"until": 0.0, "next_probe": 0.0, "reason": "", "resets": "", "email": None})


def clear_account_cooldown_if_stale(session_pct: int | None) -> None:
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
    with _cooldown_lock:
        active = bool(_slot("default")["until"])
    if not active:
        return
    if session_pct is not None and session_pct < USAGE_CLEAR_PCT:
        logger.info(f"[limits] 세션 사용률 {session_pct}% (여유 있음) — 쿨다운 오판으로 보고 해제")
        clear_account_cooldown("default")


def account_cooldown_notice(account: str = "default", config_dir: str | None = None) -> str | None:
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
                slot.update({"until": 0.0, "next_probe": 0.0, "reason": "", "resets": "", "email": None})
            return None
        started_with = slot["email"]

    # 재로그인 즉시 복구: 한도에 걸렸던 계정과 지금 계정이 다르면 옛 한도는 무관하다.
    # 쿨다운 키는 config 라벨("default")이라 계정이 바뀌어도 그대로이므로, 여기서 실제
    # 계정을 확인하지 않으면 옛 계정의 리셋 시각까지 채팅이 계속 막힌다(2026-08-20 실사고).
    # 파일 읽기는 쿨다운이 걸려 있는 동안에만 일어나고 mtime 캐시가 걸려 있다.
    if started_with:
        current = claude_account_email(config_dir)
        if current and current != started_with:
            logger.info(f"[limits] 계정 변경 감지 ({started_with} → {current}) — 쿨다운 즉시 해제")
            clear_account_cooldown(account)
            return None

    with _cooldown_lock:
        slot = _slot(account)
        until = slot["until"]
        if not until or now >= until:
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


# 한도 안내문 끝 한 줄 (2026-10-31 문구 정정 묶음 B-4, 맥·클라우드 공통 — cloud_daemon.py 의 같은 이름 상수와 문장을 맞출 것)
USAGE_LIMIT_UPGRADE_TIP = "자주 닿으시면 Claude Max(월 $100부터)로 올리시면 한도가 5~20배가 됩니다."


def usage_limit_notice(limit: dict, fallback_note: str = "") -> str:
    """한도로 턴이 끝났을 때 사용자에게 보낼 안내. 첫 문장은 고정 앞머리(집계용), 끝 줄은 구독 상향 안내."""
    resets = f" (리셋: {limit['resets']})" if limit.get("resets") else ""
    if limit["kind"] == "model":
        head = f"⚠️ {limit['label']} 모델 사용 한도에 도달했습니다{resets}."
        body = fallback_note or "다른 모델로 전환하거나 한도 리셋을 기다려야 합니다 (설정 > 모델)."
    else:
        head = f"⏸ Claude 계정 세션 한도에 도달했습니다{resets}."
        body = "리셋 전까지는 모델을 바꿔도 실행되지 않습니다. 리셋 후 다시 요청해주세요."
    return f"{head}\n{body}\n{USAGE_LIMIT_UPGRADE_TIP}"
