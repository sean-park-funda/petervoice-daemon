#!/usr/bin/env python3
"""HEARTBEAT 파일 크기 규칙 시험 (2026-11-03) — 30KB 이상이면 warning 과 메시지 힌트, 미만이면 둘 다 없음.
실행: python3 scripts/test_heartbeat_size.py
"""
import sys
from pathlib import Path

import queue  # noqa: F401,E402  (scripts/daemon/queue.py 가림 방지)
import logging.handlers  # noqa: F401,E402
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from daemon.heartbeat import HEARTBEAT_WARN_BYTES, HEARTBEAT_TARGET_BYTES, heartbeat_size_warning, heartbeat_size_hint  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS" if ok else "FAIL"), name, detail if not ok else "")
    fails += 0 if ok else 1


check("thresholds 30KB/10KB", HEARTBEAT_WARN_BYTES == 30_000 and HEARTBEAT_TARGET_BYTES == 10_000)
check("14KB(peter-voice 자기 파일) → warning 없음", heartbeat_size_warning(14_979, "HEARTBEAT.md") is None)
check("29,999 → 없음", heartbeat_size_warning(29_999, "HEARTBEAT.md") is None and heartbeat_size_hint(29_999, "HEARTBEAT.md") == "")
w = heartbeat_size_warning(30_000, "HEARTBEAT.md")
check("30,000 → warning", w is not None and "너무 큼" in w and "30KB" in w and "docs/ops/" in w, w)
w = heartbeat_size_warning(318_608, "HEARTBEAT-branch-950.md")
check("319KB → warning 에 파일명·크기", "HEARTBEAT-branch-950.md 318KB" in w, w)
h = heartbeat_size_hint(318_608, "HEARTBEAT-branch-950.md")
check("hint 는 두 줄 띄우고 시작", h.startswith("\n\n(주의: HEARTBEAT-branch-950.md 이 318KB"), h[:60])
check("hint 에 목표·옮기기·지우지 말 것", "10KB" in h and "docs/ops/" in h and "지우지 말고" in h)
check("warning 은 메시지 skip 조건 문구를 안 품음", "파일 없음" not in w and "비어 있음" not in w)
print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
