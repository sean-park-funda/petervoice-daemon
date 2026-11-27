"""코덱스 좌석 고르기 — 로그인 폴더(CODEX_HOME)가 여러 개인 맥에서 턴마다 어느 좌석으로 돌릴지 정한다 (2026-11-27).

좌석 지도 `~/.codex-seats/seats.json` 이 없는 맥(고객 맥 전부)은 아무것도 바뀌지 않는다 — choose() 가 None 을 돌려주고
코덱스는 지금처럼 CODEX_HOME 환경변수 또는 ~/.codex 로 돈다.

seats.json (고칠 때마다 다시 읽는다 — 데몬 재시작이 필요 없다):
  {
    "pool":    ["game1", "game2", "game3"],                    # 따로 정하지 않은 코덱스 담당이 나눠 쓰는 좌석 (순서 = 동점일 때 우선)
    "assign":  {"branch:1500": "game2", "ox-plan": "game3"},   # 담당 고정 (브랜치 키 → 부모 프로젝트 키 순으로 찾는다)
    "max_pct": 90                                              # 어느 창이든 이만큼 쓴 좌석은 돌리지 않는다 (기본 90)
  }
좌석 이름 = ~/.codex-seats/<이름> 폴더. 로그인(auth.json)이 없는 좌석은 건너뛴다 — 나중에 로그인되면 그때부터 쓴다.

고르는 규칙
  1. assign 에 있으면 그 좌석만. 못 쓰는 상태면 다른 좌석으로 넘기지 않고 돌리지 않는다(고정한 이유가 있을 것이므로).
  2. 아니면 pool 에서: 그 세션이 쓰던 좌석이 쓸 만하면 그대로 — 코덱스 세션 기록은 좌석 폴더 안에 있어 좌석을 옮기면 이어 갈 수 없다.
     아니면 지금 도는 턴이 가장 적은 좌석 → 마지막으로 본 사용률이 낮은 좌석 → pool 순서.
  3. 쓸 좌석이 없으면 돌리지 않고 이유를 답한다. 협찬 좌석은 한도를 넘기면 막히는 대신 워크스페이스 주인의 크레딧이 깎일 수 있다.

사용률은 데몬이 따로 묻지 않는다 — 코덱스가 턴마다 세션 기록에 남기는 값을 턴이 끝날 때 읽어 좌석별로 적어 둔다(record).
그래서 다른 기계가 같은 좌석을 쓰면 이 맥의 다음 턴이 끝날 때까지 모른다(한 턴만큼 넘을 수 있다 — max_pct 를 100 보다 낮게 두는 이유).
"""

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from daemon.globals import DAEMON_DIR, logger

SEATS_DIR = Path.home() / ".codex-seats"
MAP_NAME = "seats.json"
STATE_PATH = DAEMON_DIR / "codex_seat_state.json"
DEFAULT_MAX_PCT = 90
BLOCK_FALLBACK_SEC = 3600  # 막혔는데 풀리는 시각을 모르면 이만큼 쉰 뒤 다시 써 본다

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_lock = threading.Lock()
_map_cache: dict = {"sig": None, "data": None}
_running: dict[str, int] = {}
_state: dict | None = None


# ─── 좌석 폴더 ────────────────────────────────────────────────

def home_of(seat: str | None) -> str | None:
    """좌석 이름 → 그 좌석의 CODEX_HOME. 이름이 이상하거나 폴더가 없으면 None."""
    if not seat or not isinstance(seat, str) or not _NAME_RE.match(seat):
        return None
    path = SEATS_DIR / seat
    return str(path) if path.is_dir() else None


def logged_in(seat: str) -> bool:
    home = home_of(seat)
    return bool(home) and os.path.isfile(os.path.join(home, "auth.json"))


def seat_of_home(path: str | None) -> str | None:
    """이 경로가 좌석 폴더(또는 좌석 폴더로 가는 링크)면 그 좌석 이름."""
    if not path:
        return None
    real = Path(os.path.realpath(path))
    if real.parent == Path(os.path.realpath(SEATS_DIR)) and home_of(real.name):
        return real.name
    return None


def same_home(a: str | None, b: str | None) -> bool:
    """두 경로가 같은 폴더인가 (기본 ~/.codex 가 좌석 폴더로 가는 링크인 맥이 있다)."""
    if not a or not b:
        return False
    return os.path.realpath(a) == os.path.realpath(b)


# ─── 좌석 지도 ────────────────────────────────────────────────

def load_map() -> dict | None:
    """seats.json 을 읽어 정리한 값. 파일이 없거나 깨졌거나 비어 있으면 None(= 좌석 기능 꺼짐)."""
    path = SEATS_DIR / MAP_NAME
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    with _lock:
        if _map_cache["sig"] == sig:
            return _map_cache["data"]
    data = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top level is not an object")
        pool = [s for s in (raw.get("pool") or []) if isinstance(s, str) and _NAME_RE.match(s)]
        # 고정 좌석 이름은 여기서 거르지 않는다 — 오타를 조용히 버리면 그 담당이 pool 로 흘러가 「고정」이 깨진다(쓸 때 why_unusable 이 거절한다)
        assign = {str(k): v for k, v in (raw.get("assign") or {}).items() if isinstance(v, str) and v}
        max_pct = raw.get("max_pct", DEFAULT_MAX_PCT)
        if not isinstance(max_pct, (int, float)) or isinstance(max_pct, bool) or not 0 < max_pct <= 100:
            max_pct = DEFAULT_MAX_PCT
        if pool or assign:
            data = {"pool": pool, "assign": assign, "max_pct": max_pct}
    except Exception as e:
        logger.warning(f"[codex-seat] {path} unreadable ({e}) — running codex on the default home")
    with _lock:
        _map_cache["sig"], _map_cache["data"] = sig, data
    return data


# ─── 좌석별 마지막으로 본 한도 ──────────────────────────────────

def _load_state() -> dict:
    global _state
    if _state is None:
        try:
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            _state = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _state = {}
    return _state


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _num(value) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _merge_window(old, new, now: float) -> list:
    """같은 창이면 사용률은 줄지 않는다 — 큰 값을 남긴다. 창이 새로 시작됐으면 새 값."""
    old_pct, old_reset = (list(old or []) + [None, None])[:2]
    new_pct, new_reset = (list(new or []) + [None, None])[:2]
    old_at, new_at = _epoch(old_reset), _epoch(new_reset)
    if _num(old_pct) is None or (old_at is not None and old_at <= now):
        return [new_pct, new_reset]                     # 적어 둔 것이 없거나 그 창은 이미 끝났다
    if _num(new_pct) is None:
        return [old_pct, old_reset]                     # 이번에 못 읽었다 — 아는 값을 지우지 않는다
    if old_at is not None and new_at is not None and new_at > old_at + 300:
        return [new_pct, new_reset]                     # 리셋 시각이 뒤로 갔다 = 새 창
    if old_at is not None and new_at is not None and new_at < old_at - 300:
        return [old_pct, old_reset]                     # 이번 값이 더 옛 창의 것이다(죽은 세션의 기록 등)
    return [max(_num(old_pct), _num(new_pct)), new_reset or old_reset]


def record(seat: str | None, limits: dict | None, now: float | None = None) -> None:
    """턴이 끝난 뒤 읽은 한도(_read_codex_usage 의 limits)를 그 좌석 것으로 적어 둔다.

    한 좌석을 여러 세션이 쓰고, 턴이 일찍 깨지면 그 세션 기록의 마지막 값은 옛것일 수 있다 — 통째로 덮어쓰면
    92% 로 적어 둔 좌석이 옛 40% 로 되돌아가 기준이 풀린다. 그래서 창별로 합친다(_merge_window)."""
    if not seat or not isinstance(limits, dict):
        return
    now = now if now is not None else time.time()
    fresh = [[limits.get("session_pct"), limits.get("session_reset")],
             [limits.get("week_pct"), limits.get("week_reset")]]
    blocked = limits.get("blocked_reason")
    if not blocked and all(_num(w[0]) is None for w in fresh):
        return  # 읽은 것이 없다 — 아는 값을 지우지 않는다
    with _lock:
        state = _load_state()
        old = (state.get(seat) or {}).get("windows") or [None, None]
        state[seat] = {
            "windows": [_merge_window(old[i] if i < len(old) else None, fresh[i], now) for i in range(2)],
            "blocked_reason": blocked,
            "at": now,
        }
        try:
            tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
        except Exception as e:
            logger.warning(f"[codex-seat] state save failed: {e}")


def _known(seat: str, now: float) -> tuple[float | None, float | None]:
    """(지금도 유효한 사용률 중 가장 큰 값, 막힘이 풀리는 시각) — 모르면 None."""
    with _lock:
        entry = dict((_load_state().get(seat) or {}))
    pct, resets = None, []
    for window in entry.get("windows") or []:
        value, reset = (list(window) + [None, None])[:2]
        reset_at = _epoch(reset)
        if reset_at is not None and reset_at <= now:
            continue  # 그 창은 이미 새로 시작됐다 — 옛 사용률을 믿지 않는다
        if reset_at is not None:
            resets.append(reset_at)
        if _num(value) is not None:
            pct = value if pct is None else max(pct, value)
    blocked_until = None
    if entry.get("blocked_reason"):
        until = min(resets) if resets else (entry.get("at") or 0) + BLOCK_FALLBACK_SEC
        if until > now:
            blocked_until = until
    return pct, blocked_until


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")


def why_unusable(seat: str, max_pct: float, now: float) -> str | None:
    """이 좌석을 지금 못 쓰는 이유 한 마디. 쓸 수 있으면 None."""
    if not home_of(seat):
        return "폴더 없음"
    if not logged_in(seat):
        return "로그인 없음"
    pct, blocked_until = _known(seat, now)
    if blocked_until:
        return f"한도에 막힘({_when(blocked_until)} 까지)"
    if pct is not None and pct >= max_pct:
        return f"{pct:g}% 사용(기준 {max_pct:g}%)"
    return None


def unusable_now(seat: str) -> str | None:
    """고른 뒤 실제로 돌리기 직전에 한 번 더 본다 — 지금 지도 기준으로 못 쓰게 됐으면 이유, 아니면 None."""
    seat_map = load_map()
    return why_unusable(seat, (seat_map or {}).get("max_pct", DEFAULT_MAX_PCT), time.time())


# ─── 고르기 ──────────────────────────────────────────────────

def begin(seat: str | None) -> None:
    if seat:
        with _lock:
            _running[seat] = _running.get(seat, 0) + 1


def end(seat: str | None) -> None:
    if seat:
        with _lock:
            _running[seat] = max(0, _running.get(seat, 0) - 1)


def choose(project: str, parent: str | None, session_seat: str | None, now: float | None = None) -> dict | None:
    """이 턴을 돌릴 좌석.

    None                         좌석 기능이 꺼져 있거나 이 담당에 해당 없음 → 기본 폴더로 (지금과 같은 동작)
    {"seat": 이름, "home": 경로}   그 좌석으로
    {"refuse": 안내문}            쓸 좌석이 없음 → 돌리지 말고 안내문을 답한다
    """
    seat_map = load_map()
    if not seat_map:
        return None
    now = now if now is not None else time.time()
    max_pct = seat_map["max_pct"]

    fixed = seat_map["assign"].get(project) or (seat_map["assign"].get(parent) if parent else None)
    if fixed:
        reason = why_unusable(fixed, max_pct, now)
        if reason:
            return {"refuse": f"코덱스 좌석 {fixed} 을(를) 지금 쓸 수 없어 이 턴을 돌리지 않았습니다 — {reason}. "
                              f"이 담당은 그 좌석으로 고정돼 있습니다(~/.codex-seats/{MAP_NAME})."}
        return {"seat": fixed, "home": home_of(fixed)}

    pool = seat_map["pool"]
    if not pool:
        return None
    reasons = {seat: why_unusable(seat, max_pct, now) for seat in pool}
    usable = [seat for seat in pool if not reasons[seat]]
    if not usable:
        detail = " · ".join(f"{seat}: {reasons[seat]}" for seat in pool)
        return {"refuse": f"쓸 수 있는 코덱스 좌석이 없어 이 턴을 돌리지 않았습니다 — {detail}. "
                          f"한도가 풀리거나 좌석이 추가되면 같은 메시지를 다시 보내 주세요."}
    if session_seat in usable:
        chosen = session_seat
    else:
        with _lock:
            busy = dict(_running)
        chosen = min(usable, key=lambda s: (busy.get(s, 0), _known(s, now)[0] or 0, pool.index(s)))
    return {"seat": chosen, "home": home_of(chosen)}
