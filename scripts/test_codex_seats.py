#!/usr/bin/env python3
"""코덱스 좌석 고르기 시험 (2026-11-27).

실행: python3 scripts/test_codex_seats.py   (네트워크 없음 · 임시 폴더만 씀 · 1초 안팎)
배경: 게임 맥은 기본 ~/.codex 에 로그인이 없고 협찬 좌석 폴더(~/.codex-seats/game1~3)에만 있어, 브랜치 엔진을
      codex 로 바꾸는 것만으로는 돌지 않았다. 협찬 좌석은 한도를 넘기면 워크스페이스 주인 크레딧이 깎일 수 있어
      「90% 넘은 좌석은 돌리지 않는다」를 데몬이 지킨다.
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 실제 ~/.claude-daemon 을 건드리지 않도록 임시 설정 폴더로 띄운다
_TMP = tempfile.mkdtemp(prefix="pv-codex-seats-test.")
sys.argv = [sys.argv[0], "--config-dir", _TMP]

from daemon import codex_seats as cs  # noqa: E402
from daemon import sessions  # noqa: E402

FAILS = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def fresh(seats=("game1", "game2", "game3"), logged=("game1",), seat_map=None):
    """임시 좌석 폴더를 새로 만들고 모듈 상태를 비운다."""
    base = Path(tempfile.mkdtemp(prefix="seats.", dir=_TMP))
    for name in seats:
        (base / name).mkdir()
        if name in logged:
            (base / name / "auth.json").write_text("{}")
    cs.SEATS_DIR = base
    cs.STATE_PATH = base / "state.json"
    cs._state = None
    cs._running.clear()
    cs._map_cache.update(sig=None, data=None)
    if seat_map is not None:
        (base / cs.MAP_NAME).write_text(seat_map if isinstance(seat_map, str) else json.dumps(seat_map))
    return base


NOW = 1_800_000_000.0
LATER = NOW + 6 * 86400


def limits(pct, reset=LATER, blocked=None):
    return {"session_pct": pct, "session_reset": iso(reset) if reset else None, "week_pct": None, "week_reset": None,
            "blocked_reason": blocked}


print("좌석 지도가 없거나 깨졌으면 지금과 같은 동작(None)")
fresh()
check(cs.choose("branch:1", "game-develop", None, now=NOW) is None, "seats.json 없음 → None")
fresh(seat_map="{not json")
check(cs.choose("branch:1", "game-develop", None, now=NOW) is None, "깨진 seats.json → None (예외 없음)")
fresh(seat_map={"pool": [], "assign": {}})
check(cs.choose("branch:1", "game-develop", None, now=NOW) is None, "빈 지도 → None")
fresh(seat_map={"assign": {"ox-plan": "game1"}})
check(cs.choose("branch:1", "game-develop", None, now=NOW) is None, "pool 없고 내 담당 고정도 없음 → None")
fresh(seat_map=["game1"])
check(cs.choose("branch:1", None, None, now=NOW) is None, "최상위가 객체가 아님 → None")

print("pool — 로그인된 좌석만, 로그인되면 그때부터")
base = fresh(seat_map={"pool": ["game1", "game2", "game3"]})
pick = cs.choose("branch:1", "game-develop", None, now=NOW)
check(pick == {"seat": "game1", "home": str(base / "game1")}, "로그인된 game1 하나만 고른다")
(base / "game2" / "auth.json").write_text("{}")
cs.begin("game1")
check(cs.choose("branch:2", "game-develop", None, now=NOW)["seat"] == "game2", "game1 이 도는 중이면 새 세션은 방금 로그인된 game2 로")
check(cs.choose("branch:1", "game-develop", "game1", now=NOW)["seat"] == "game1", "쓰던 좌석이 쓸 만하면 그대로(세션 유지)")
cs.end("game1")
cs.end("game1")
check(cs._running.get("game1") == 0, "end 를 더 불러도 음수가 되지 않는다")

print("사용률 — 기준(기본 90%) 이상이면 돌리지 않는다")
base = fresh(logged=("game1", "game2"), seat_map={"pool": ["game1", "game2"]})
cs.record("game1", limits(92), now=NOW)
check(cs.choose("branch:1", None, None, now=NOW)["seat"] == "game2", "92% 좌석은 건너뛰고 다음 좌석")
check(cs.choose("branch:1", None, "game1", now=NOW)["seat"] == "game2", "쓰던 좌석이 기준을 넘으면 다른 좌석으로 옮긴다")
cs.record("game2", limits(90), now=NOW)
refused = cs.choose("branch:1", None, None, now=NOW)
check("refuse" in refused and "92%" in refused["refuse"] and "game2: 90%" in refused["refuse"], "둘 다 넘으면 돌리지 않고 이유를 답한다")
check(cs.choose("branch:1", None, None, now=LATER + 1)["seat"] == "game1", "창이 새로 시작된 뒤에는 옛 사용률을 믿지 않는다")
base = fresh(logged=("game1", "game2"), seat_map={"pool": ["game1", "game2"]})
cs.record("game1", limits(30), now=NOW)
cs.record("game2", limits(10), now=NOW)
check(cs.choose("branch:9", None, None, now=NOW)["seat"] == "game2", "도는 턴 수가 같으면 사용률 낮은 좌석")
check(json.loads((base / "state.json").read_text())["game2"]["windows"][0][0] == 10, "좌석별 한도를 파일에 적는다")
cs._state = None
check(cs.choose("branch:9", None, None, now=NOW)["seat"] == "game2", "데몬이 다시 떠도 적어 둔 한도로 고른다")

fresh(logged=("game1",), seat_map={"pool": ["game1"], "max_pct": 50})
cs.record("game1", limits(55), now=NOW)
check("refuse" in cs.choose("branch:1", None, None, now=NOW), "max_pct 를 지도에서 낮출 수 있다")
fresh(logged=("game1",), seat_map={"pool": ["game1"], "max_pct": "many"})
cs.record("game1", limits(89), now=NOW)
check(cs.choose("branch:1", None, None, now=NOW)["seat"] == "game1", "이상한 max_pct 는 기본 90 으로")

print("적어 둔 한도 — 옛 값이 새 값을 덮지 않는다")
fresh(logged=("game1", "game2"), seat_map={"pool": ["game1", "game2"]})
cs.record("game1", limits(92), now=NOW)
cs.record("game1", limits(40), now=NOW + 5)
check(cs.choose("branch:1", None, None, now=NOW + 10)["seat"] == "game2", "같은 창의 더 낮은 값(죽은 세션의 옛 기록)은 92% 를 내리지 못한다")
cs.record("game1", {"session_pct": None, "session_reset": None, "week_pct": None, "week_reset": None, "blocked_reason": None}, now=NOW + 6)
check(cs.choose("branch:1", None, None, now=NOW + 10)["seat"] == "game2", "아무것도 못 읽은 턴은 아는 값을 지우지 못한다")
cs.record("game1", limits(95, reset=LATER - 86400), now=NOW + 7)
check(cs._state["game1"]["windows"][0][0] == 92, "더 옛 창의 값은 버린다")
cs.record("game1", limits(3, reset=LATER + 7 * 86400), now=NOW + 8)
check(cs.choose("branch:1", None, "game1", now=NOW + 10)["seat"] == "game1", "리셋 시각이 뒤로 간 새 창의 값은 받아들인다")
cs.record("game1", limits(96, reset=LATER + 7 * 86400), now=NOW + 9)
check(cs.unusable_now("game2") is None and "로그인 없음" in (cs.unusable_now("game3") or ""), "돌리기 직전 재확인(unusable_now)")
check(not (cs.STATE_PATH.with_name(cs.STATE_PATH.name + ".tmp")).exists(), "상태 파일은 임시 파일로 쓴 뒤 바꿔치기한다")

print("막힘 — 풀리는 시각까지, 모르면 1시간")
fresh(logged=("game1", "game2"), seat_map={"pool": ["game1", "game2"]})
cs.record("game1", limits(None, reset=None, blocked="workspace_owner_credits_depleted"), now=NOW)
check(cs.choose("branch:1", None, "game1", now=NOW + 60)["seat"] == "game2", "막힌 좌석은 건너뛴다")
check(cs.choose("branch:1", None, "game1", now=NOW + cs.BLOCK_FALLBACK_SEC + 1)["seat"] == "game1", "시각을 모르면 1시간 뒤 다시 써 본다")
cs.record("game1", limits(100, reset=NOW + 7200, blocked="rate_limit_reached"), now=NOW)
check(cs.choose("branch:1", None, "game1", now=NOW + 3700)["seat"] == "game2", "리셋 시각을 알면 그때까지 막는다")
check(cs.choose("branch:1", None, "game1", now=NOW + 7201)["seat"] == "game1", "리셋이 지나면 다시 쓴다")

print("담당 고정(assign)")
base = fresh(logged=("game1", "game2"), seat_map={"pool": ["game1"], "assign": {"branch:7": "game2", "ox-plan": "game2", "x": "../etc"}})
check(cs.choose("branch:7", "game-develop", None, now=NOW)["seat"] == "game2", "브랜치 키로 고정")
check(cs.choose("branch:8", "ox-plan", None, now=NOW)["seat"] == "game2", "부모 프로젝트 키로 고정")
check(cs.choose("branch:9", "game-develop", None, now=NOW)["seat"] == "game1", "고정이 없으면 pool")
refused = cs.choose("x", None, None, now=NOW)
check("refuse" in refused and "폴더 없음" in refused["refuse"], "잘못 적힌 고정 좌석은 pool 로 흘리지 않고 돌리지 않는다(경로 탈출 없음)")
cs.record("game2", limits(95), now=NOW)
refused = cs.choose("branch:7", "game-develop", None, now=NOW)
check("refuse" in refused and "game2" in refused["refuse"], "고정 좌석이 기준을 넘으면 다른 좌석으로 넘기지 않고 돌리지 않는다")
fresh(logged=(), seat_map={"assign": {"branch:7": "game3"}})
check("로그인 없음" in cs.choose("branch:7", None, None, now=NOW)["refuse"], "고정 좌석에 로그인이 없으면 이유를 답한다")
fresh(seats=("game1",), logged=("game1",), seat_map={"pool": ["ghost", "game1"]})
check(cs.choose("branch:1", None, None, now=NOW)["seat"] == "game1", "폴더가 없는 좌석은 건너뛴다")

print("폴더 판별")
base = fresh(logged=("game1",), seat_map={"pool": ["game1"]})
link = base.parent / "default-home"
if link.is_symlink():
    link.unlink()
os.symlink(base / "game1", link)
check(cs.same_home(str(link), str(base / "game1")), "기본 폴더가 좌석으로 가는 링크면 같은 폴더")
check(not cs.same_home(str(base / "game2"), str(base / "game1")), "다른 좌석은 다른 폴더")
check(not cs.same_home(None, str(base / "game1")), "None 은 같지 않다")
check(cs.seat_of_home(str(link)) == "game1", "링크된 기본 폴더 → 좌석 이름")
check(cs.seat_of_home(_TMP) is None and cs.seat_of_home(None) is None, "좌석 폴더가 아니면 None")
check(cs.home_of("../game1") is None and cs.home_of("") is None and cs.home_of(None) is None, "이상한 이름은 폴더를 주지 않는다")

print("record — 이상한 입력")
fresh(logged=("game1",), seat_map={"pool": ["game1"]})
cs.record(None, limits(99), now=NOW)
cs.record("game1", None, now=NOW)
check(cs.choose("branch:1", None, None, now=NOW)["seat"] == "game1", "좌석 없음 · 한도 없음은 무시")
cs.record("game1", {"session_pct": True, "session_reset": "not-a-date", "week_pct": "x", "week_reset": None}, now=NOW)
check(cs.choose("branch:1", None, None, now=NOW)["seat"] == "game1", "숫자가 아닌 값 · 깨진 시각은 모르는 것으로")

print("세션에 좌석을 같이 적는다")
sessions.CODEX_SESSIONS_PATH = Path(_TMP) / "codex_sessions.json"
sessions._codex_sessions.clear()
sessions.update_codex_session("branch:1", "sid-a", seat="game1")
check(sessions.get_codex_session_seat("branch:1") == "game1", "좌석으로 돈 세션은 좌석을 기억한다")
check(json.loads(sessions.CODEX_SESSIONS_PATH.read_text())["branch:1"]["seat"] == "game1", "파일에도 남는다")
sessions.update_codex_session("branch:1", "sid-b")
check(sessions.get_codex_session_seat("branch:1") is None, "기본 폴더로 돈 턴 뒤에는 좌석 표시를 지운다")
check(sessions.get_codex_session_seat("nope") is None, "없는 세션은 None")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: " + " / ".join(FAILS))
    sys.exit(1)
print("all ok")
