#!/usr/bin/env python3
"""강제(force_every) 하트비트 회차도 계정 쿨다운 중이면 미룬다 (2026-11-23).
전에는 강제 회차가 쿨다운 검사를 건너뛰어 본 세션을 깨웠고, 쿨다운 안내만 받은 채 그 회차가 소비됐다.
실행: python3 scripts/test_heartbeat_forced_cooldown.py
"""
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import queue  # noqa: F401,E402  (scripts/daemon/queue.py 가림 방지)
import logging.handlers  # noqa: F401,E402
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from daemon import heartbeat as hb  # noqa: E402
from daemon import scout  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS" if ok else "FAIL"), name, detail if not ok else "")
    fails += 0 if ok else 1


def run(forced: bool, cooldown: bool):
    """_process_task 한 번. 반환: (태스크 갱신 목록, 주입된 메시지 수, 정찰 시작 수)."""
    updates, injected, scouts = [], [], []
    t = object.__new__(hb.HeartbeatThread)
    t._pending = {}
    t._scouting = set()
    t._scouting_lock = threading.Lock()
    t._check_heartbeat_md = lambda project: None
    t._update_task = lambda tid, body: updates.append(body)
    t._heartbeat_filename = lambda project: "HEARTBEAT.md"
    t._heartbeat_size_hint = lambda project, name: ""
    t._read_heartbeat_text = lambda project, name: "- [ ] 할 일"
    t._scout_then_inject = lambda *a, **k: scouts.append(a)
    hb.inject_system_message = lambda project, msg, prefix="": (injected.append(msg) or (777, "ts"))
    scout.enabled = lambda: True
    scout.should_force_main = lambda run_count, force_every: forced
    scout.account_in_cooldown = lambda project: cooldown
    task = {"id": "t1", "project": "hb-test-proj", "interval_min": 1440, "run_count": 3, "max_runs": 365,
            "active_hours": None, "warning": None, "next_run_at": datetime.now(timezone.utc).isoformat()}
    t._process_task(task)
    for th in threading.enumerate():
        if th.name == "scout-hb-test-proj":
            th.join(timeout=5)
    return updates, len(injected), len(scouts)


u, inj, sc = run(forced=True, cooldown=True)
check("강제 + 쿨다운 → 본 세션을 깨우지 않는다", inj == 0 and sc == 0, (u, inj, sc))
check("강제 + 쿨다운 → next_run_at 만 미루고 run_count 는 그대로", len(u) == 1 and set(u[0]) == {"next_run_at"}, u)
if u:
    delay_min = (datetime.fromisoformat(u[0]["next_run_at"]) - datetime.now(timezone.utc)).total_seconds() / 60
    check("미루는 폭은 busy 와 같이 짧다(일일 태스크가 하루를 건너뛰지 않음)", 0 < delay_min <= 6, delay_min)

u, inj, sc = run(forced=False, cooldown=True)
check("정찰 회차 + 쿨다운 → 그대로 미룸(기존 동작)", inj == 0 and sc == 0 and len(u) == 1 and set(u[0]) == {"next_run_at"}, (u, inj, sc))

u, inj, sc = run(forced=True, cooldown=False)
check("강제 + 쿨다운 아님 → 본 세션 주입(기존 동작)", inj == 1 and sc == 0, (u, inj, sc))
check("주입한 회차는 run_count 가 오른다", any(x.get("run_count") == 4 for x in u), u)

u, inj, sc = run(forced=False, cooldown=False)
check("정찰 회차 + 쿨다운 아님 → 정찰만(기존 동작)", inj == 0 and sc == 1, (u, inj, sc))

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
