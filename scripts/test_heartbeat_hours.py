#!/usr/bin/env python3
"""하트비트 active_hours 파싱·창 판정 시험 (2026-11-03).

회귀 대상: "07-24" 처럼 분이 없는 값을 옛 파서가 예외 → return True(항상 켜짐)로 처리해 밤 정지가 사흘간 효과 0 이었던 결함.
실행: python3 scripts/test_heartbeat_hours.py
"""
import sys
from datetime import datetime
from pathlib import Path

# 표준 라이브러리 queue/logging 을 먼저 올린다 — scripts/daemon/queue.py 가 sys.path 앞에 오면 urllib3 import 가 깨진다
import queue  # noqa: F401,E402
import logging.handlers  # noqa: F401,E402
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from daemon.heartbeat import HeartbeatThread, parse_active_hours  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS" if ok else "FAIL"), name, detail if not ok else "")
    fails += 0 if ok else 1


P = parse_active_hours
# ── 파싱 ──
check("canonical HH:MM-HH:MM", P("07:00-23:59") == (420, 1439), P("07:00-23:59"))
check("HH-HH (login-manager 10/31 값)", P("07-24") == (420, 1440), P("07-24"))
check("H-H", P("6-24") == (360, 1440))
check("mixed 7:30~22", P("7:30~22") == (450, 1320))
check("en dash", P("07:00–23:59") == (420, 1439))
check("24:00 end = 1440", P("00:00-24:00") == (0, 1440))
check("24 start → 0", P("24-07") == (0, 420))
check("spaces", P(" 09:00 - 18:00 ") == (540, 1080))
check("wrap 22:00-06:00", P("22:00-06:00") == (1320, 360))
for bad in ("07:0-23:59", "25:00-26:00", "07:60-08:00", "24:30-07:00", "07", "-", "", None, 12, "abc", "7am-10pm", "07:00-23:59-01:00"):
    check(f"reject {bad!r}", P(bad) is None, P(bad))

# ── 창 판정 (now 주입) ──
IN = HeartbeatThread._in_active_hours


def t(h, m, s=30):
    return datetime(2026, 11, 3, h, m, s)


check("07-24 at 03:00 → 밖 (옛 파서는 True)", IN("07-24", t(3, 0)) is False)
check("07-24 at 07:00 → 안", IN("07-24", t(7, 0)) is True)
check("07-24 at 23:59 → 안", IN("07-24", t(23, 59)) is True)
check("07:00-23:59 at 23:59:59 → 안 (분 단위 포함)", IN("07:00-23:59", t(23, 59, 59)) is True)
check("07:00-23:59 at 06:59:59 → 밖", IN("07:00-23:59", t(6, 59, 59)) is False)
check("07:00-23:59 at 00:00 → 밖", IN("07:00-23:59", t(0, 0, 0)) is False)
check("wrap 22-06 at 23:00 안", IN("22:00-06:00", t(23, 0)) is True)
check("wrap 22-06 at 03:00 안", IN("22:00-06:00", t(3, 0)) is True)
check("wrap 22-06 at 06:00:59 안", IN("22:00-06:00", t(6, 0, 59)) is True)
check("wrap 22-06 at 12:00 밖", IN("22:00-06:00", t(12, 0)) is False)
check("narrow 05:00-06:00 at 05:59 안", IN("05:00-06:00", t(5, 59)) is True)
check("narrow 05:00-06:00 at 06:01 밖", IN("05:00-06:00", t(6, 1)) is False)
check("00:00-24:00 always", all(IN("00:00-24:00", t(h, 0)) for h in range(24)))
check("bad format → True(항상 켜짐, 경고 로그)", IN("abc", t(3, 0)) is True)

# ── 다음 창 시작 ──
NW = HeartbeatThread._next_window_start


def local(dt):
    return dt.astimezone().replace(tzinfo=None) if dt else None


check("next window 07-24 at 03:00 → 오늘 07:00", local(NW("07-24", t(3, 0))) == datetime(2026, 11, 3, 7, 0))
check("next window 07:00-23:59 at 23:59:59 → 내일 07:00", local(NW("07:00-23:59", t(23, 59, 59))) == datetime(2026, 11, 4, 7, 0))
check("next window 07:30~22 at 07:00 → 오늘 07:30", local(NW("07:30~22", t(7, 0))) == datetime(2026, 11, 3, 7, 30))
check("next window wrap 22-06 at 12:00 → 오늘 22:00", local(NW("22:00-06:00", t(12, 0))) == datetime(2026, 11, 3, 22, 0))
check("next window bad → None", NW("abc", t(3, 0)) is None)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
