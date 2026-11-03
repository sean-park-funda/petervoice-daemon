#!/usr/bin/env python3
"""턴 디스패처 시험 (2026-11-03) — 줄 선 메시지가 칸을 쥐지 않는지, 사람 메시지가 먼저 가는지, 담당당 한 턴인지.

실행: python3 scripts/test_worker_dispatch.py   (네트워크 없음, 5초 안팎)
배경: 11/03 12:14 Sean 맥 12칸이 '실제 실행 3 + 잠금 대기 9' 로 차서 사람 메시지가 16분 대기.
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from daemon.dispatch import TurnDispatcher, is_automatic  # noqa: E402

FAILS = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def scenario(title, max_workers, reserve, msgs):
    """msgs: [{id, project, text, dur}] 도착 순. 각 메시지의 시작 시각(초)과 동시 실행 최대치를 돌려준다."""
    print(f"\n[{title}] lanes={max_workers} reserve={reserve}")
    starts, lock, done = {}, threading.Lock(), threading.Event()
    running, peak = [0], [0]
    t0 = time.monotonic()

    def run_turn(msg):
        with lock:
            starts[msg["id"]] = round(time.monotonic() - t0, 2)
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(msg["dur"])
        with lock:
            running[0] -= 1
            if len(starts) == len(msgs) and running[0] == 0:
                done.set()

    ex = ThreadPoolExecutor(max_workers=max_workers)
    d = TurnDispatcher(max_workers, reserve, run_turn=run_turn, submit=ex.submit)
    d.start()
    for m in msgs:
        d.enqueue(m)
        time.sleep(0.03)  # 도착 순서 고정
    assert done.wait(15), "turns did not finish"
    ex.shutdown(wait=True)
    st = d.stats()
    check(st["running"] == 0 and st["pending"] == 0, f"drained cleanly {st}")
    check(peak[0] <= max_workers, f"never exceeded lanes (peak {peak[0]})")
    print("  starts:", starts)
    return starts


# 1. 같은 담당에 몰린 메시지가 칸을 쥐지 않는다: A 4건(각 0.6s) 뒤에 온 B 는 즉시 시작해야 한다
s = scenario("waiters hold no lane", 2, 0, [
    {"id": "A1", "project": "A", "text": "[relay from:x] a", "dur": 0.6},
    {"id": "A2", "project": "A", "text": "[relay from:x] a", "dur": 0.1},
    {"id": "A3", "project": "A", "text": "[relay from:x] a", "dur": 0.1},
    {"id": "A4", "project": "A", "text": "[relay from:x] a", "dur": 0.1},
    {"id": "B1", "project": "B", "text": "hello", "dur": 0.1},
])
check(s["B1"] < 0.4, f"B1 started at {s['B1']}s while A queue waited (old code: ≥0.6s)")
check(s["A1"] < s["A2"] < s["A3"] < s["A4"], "same-class FIFO within project A")
check(s["A2"] >= 0.55, f"A2 waited for A1 to finish ({s['A2']}s) — one turn per project")

# 2. 사람 예비 칸: 자동 턴이 (lanes - reserve) 를 채워도 사람 메시지는 바로 시작한다
s = scenario("human reserve lane", 2, 1, [
    {"id": "X1", "project": "X", "text": "[heartbeat] x", "dur": 0.6},
    {"id": "Y1", "project": "Y", "text": "[heartbeat] y", "dur": 0.1},
    {"id": "Z1", "project": "Z", "text": "[relay from:q] z", "dur": 0.1},
    {"id": "H1", "project": "H", "text": "사람이 친 메시지", "dur": 0.3},
])
check(s["H1"] < 0.3, f"H1 (human) started at {s['H1']}s though auto lane was full")
check(s["Y1"] >= 0.55, f"Y1 (auto) waited for the auto lane ({s['Y1']}s)")
check(s["Y1"] < s["Z1"], "auto FIFO kept (Y1 before Z1)")

# 3. 같은 담당 안에서도 사람 메시지가 자동 메시지를 앞지른다 (칸 1개)
s = scenario("human jumps queue within project", 1, 0, [
    {"id": "a1", "project": "P", "text": "[relay from:x] 1", "dur": 0.3},
    {"id": "a2", "project": "P", "text": "[relay from:x] 2", "dur": 0.1},
    {"id": "a3", "project": "P", "text": "[heartbeat] 3", "dur": 0.1},
    {"id": "h1", "project": "P", "text": "사람 메시지", "dur": 0.1},
])
check(s["a1"] < s["h1"] < s["a2"] < s["a3"], "order a1 → h1 → a2 → a3")

# 4. 사람끼리는 도착 순 (한 사람이 연달아 보낸 4건)
s = scenario("humans FIFO", 3, 1, [
    {"id": f"m{i}", "project": "jangsa", "text": f"요청 {i}", "dur": 0.1} for i in range(4)
])
check(s["m0"] < s["m1"] < s["m2"] < s["m3"], "m0 → m1 → m2 → m3")

# 5. 분류기
check(is_automatic({"text": "[relay from:revenue-lab branch:959]\n..."}), "relay = automatic")
check(is_automatic({"text": "[heartbeat] HEARTBEAT.md를 확인"}), "heartbeat = automatic")
check(not is_automatic({"text": "결정: A 로 간다"}), "decision answer = human")
check(not is_automatic({"text": "[음성 핸드오프 from:coo] 용건"}), "voice handoff = human")
check(not is_automatic({"text": ""}), "empty text (file only) = human")

# 6. Worker 통합 스모크 — 실제 Worker 가 디스패처로 턴을 돌리는지 (네트워크 호출은 전부 막는다)
print("\n[worker smoke]")
try:
    import daemon.globals as g
    g.config.update({"api_key": "test", "api_url": "http://127.0.0.1:9", "bot_name": "t", "max_concurrent": 3})
    from daemon.worker import Worker
    w = Worker()
    w.heartbeat = lambda *a, **k: None
    ran, ev = [], threading.Event()

    def fake_process(msg):
        ran.append(msg["id"])
        time.sleep(0.05)
        if len(ran) == 3:
            ev.set()
    w.process_message = fake_process
    w._dispatcher.start()
    w.submit({"id": 1, "project": "p", "text": "하나"})
    w.submit({"id": 2, "project": "p", "text": "[heartbeat] 둘"})
    w.submit({"id": 3, "project": "q", "text": "셋"})
    check(ev.wait(5), f"worker ran all 3 via dispatcher: {ran}")
    check(ran[:2] == [1, 3] or ran[:2] == [3, 1], f"humans (1,3) before heartbeat (2): {ran}")
    check(w.queue_stats()["pending"] == 0, "worker queue drained")
    w.shutdown_executors()
except Exception as e:  # import 환경 문제는 실패로 친다 — 배포 전 반드시 통과
    check(False, f"worker smoke raised {type(e).__name__}: {e}")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL PASS")
