#!/usr/bin/env python3
"""턴 디스패처 시험 (2026-11-03 · 2026-11-12 합치기·대체 추가) — 줄 선 메시지가 칸을 쥐지 않는지, 사람 메시지가 먼저 가는지,
담당당 한 턴인지, 쌓인 자동 메시지가 한 턴에 묶이는지, supersede 키가 옛 대기 메시지를 빼는지.

실행: python3 scripts/test_worker_dispatch.py   (네트워크 없음, 5초 안팎)
배경: 11/03 12:14 Sean 맥 12칸이 '실제 실행 3 + 잠금 대기 9' 로 차서 사람 메시지가 16분 대기.
      11/12 「A 가 맡아」→「A 말고 B」 릴레이가 턴제라 늦게 도착(부산진 중복 답 1건) → 합치기·대체.
시험 방식(11/12): 첫 메시지의 턴을 gate 로 붙잡아 두고 나머지를 넣은 뒤 gate 를 연다 — 머신 부하로 sleep 이 늘어나도
결과가 바뀌지 않는다(11/12 실측: load 3~7 에서 sleep(0.03) 이 0.15초가 돼 타이밍 기반 판정이 어긋났다).
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from daemon.dispatch import (  # noqa: E402
    TurnDispatcher, is_automatic, is_mergeable, merge_messages, supersede_key, turn_message_ids,
)

FAILS = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def scenario(title, max_workers, reserve, msgs, gate_first=True, **kw):
    """msgs 도착순. gate_first 면 첫 메시지의 턴을 붙잡아 둔 채 나머지를 전부 넣고 gate 를 연다.
    돌려주는 것: starts(id→시작 시각), order(턴 시작 순서대로 각 턴이 소비한 id 목록), gate_t, superseded, stats."""
    print(f"\n[{title}] lanes={max_workers} reserve={reserve} {kw or ''}")
    starts, order, superseded = {}, [], []
    lock, done, gate = threading.Lock(), threading.Event(), threading.Event()
    want = {m["id"] for m in msgs}
    first_id = msgs[0]["id"]
    t0 = time.monotonic()

    def seen():
        return {i for ids, _ in order for i in ids} | {o["id"] for o, _ in superseded}

    def run_turn(msg):
        ids = turn_message_ids(msg)
        with lock:
            for i in ids:
                starts[i] = round(time.monotonic() - t0, 2)
            order.append((ids, msg))
        if gate_first and msg["id"] == first_id:
            gate.wait(10)
        time.sleep(msg.get("dur", 0.05))
        with lock:
            if want <= seen():
                done.set()

    def on_superseded(old, new):
        with lock:
            superseded.append((old, new))
            if want <= seen():
                done.set()

    ex = ThreadPoolExecutor(max_workers=max_workers)
    d = TurnDispatcher(max_workers, reserve, run_turn=run_turn, submit=ex.submit, on_superseded=on_superseded, **kw)
    d.start()
    d.enqueue(msgs[0])
    if gate_first:
        for _ in range(500):  # 첫 턴이 실제로 시작할 때까지 (최대 5초)
            with lock:
                if first_id in starts:
                    break
            time.sleep(0.01)
        assert first_id in starts, "first turn did not start"
    for m in msgs[1:]:
        d.enqueue(m)
    time.sleep(0.15)  # 게이트 전에 시작할 수 있는 것(다른 담당·사람)은 이 사이에 시작한다
    gate_t = round(time.monotonic() - t0, 2)
    gate.set()
    assert done.wait(15), f"turns did not finish: order={[ids for ids, _ in order]}"
    time.sleep(0.1)
    ex.shutdown(wait=True)
    st = d.stats()
    check(st["running"] == 0 and st["pending"] == 0, f"drained cleanly {st}")
    print("  starts:", starts, "| gate opened at", gate_t)
    print("  turns:", [ids for ids, _ in order], "| superseded:", [(o["id"], n["id"]) for o, n in superseded])
    return starts, [ids for ids, _ in order], gate_t, order, superseded, st


# ── 스케줄링(칸·우선순위) — 합치기는 끄고 본다 (11/12 시나리오 7~10 에서 따로) ──
# 1. 같은 담당에 몰린 메시지가 칸을 쥐지 않는다: A1 이 도는 동안 A2~A4 가 줄 서도 B 는 즉시 시작해야 한다
s, order, gate_t, *_ = scenario("waiters hold no lane", 2, 0, [
    {"id": "A1", "project": "A", "text": "[relay from:x] a"},
    {"id": "A2", "project": "A", "text": "[relay from:x] a"},
    {"id": "A3", "project": "A", "text": "[relay from:x] a"},
    {"id": "A4", "project": "A", "text": "[relay from:x] a"},
    {"id": "B1", "project": "B", "text": "hello"},
], merge_enabled=False)
check(s["B1"] < gate_t, f"B1 started at {s['B1']}s while A1 held its lane and A2~A4 waited (old code: after A1)")
check(s["A1"] < s["A2"] < s["A3"] < s["A4"], "same-class FIFO within project A")
check(s["A2"] >= gate_t, f"A2 waited for A1 to finish ({s['A2']}s ≥ gate {gate_t}s) — one turn per project")

# 2. 사람 예비 칸: 자동 턴이 (lanes - reserve) 를 채워도 사람 메시지는 바로 시작한다
s, order, gate_t, *_ = scenario("human reserve lane", 2, 1, [
    {"id": "X1", "project": "X", "text": "[heartbeat] x"},
    {"id": "Y1", "project": "Y", "text": "[heartbeat] y"},
    {"id": "Z1", "project": "Z", "text": "[relay from:q] z"},
    {"id": "H1", "project": "H", "text": "사람이 친 메시지"},
], merge_enabled=False)
check(s["H1"] < gate_t, f"H1 (human) started at {s['H1']}s though the auto lane was full")
check(s["Y1"] >= gate_t, f"Y1 (auto) waited for the auto lane ({s['Y1']}s)")
check(s["Y1"] < s["Z1"], "auto FIFO kept (Y1 before Z1)")

# 3. 같은 담당 안에서도 사람 메시지가 자동 메시지를 앞지른다 (칸 1개)
s, order, *_ = scenario("human jumps queue within project", 1, 0, [
    {"id": "a1", "project": "P", "text": "[relay from:x] 1"},
    {"id": "a2", "project": "P", "text": "[relay from:x] 2"},
    {"id": "a3", "project": "P", "text": "[heartbeat] 3"},
    {"id": "h1", "project": "P", "text": "사람 메시지"},
], merge_enabled=False)
check(order == [["a1"], ["h1"], ["a2"], ["a3"]], f"order a1 → h1 → a2 → a3: {order}")

# 4. 사람끼리는 도착 순 (한 사람이 연달아 보낸 4건, 게이트 없이)
s, order, *_ = scenario("humans FIFO", 3, 1, [
    {"id": f"m{i}", "project": "jangsa", "text": f"요청 {i}", "dur": 0.2} for i in range(4)
], gate_first=False, merge_enabled=False)
check(s["m0"] <= s["m1"] <= s["m2"] <= s["m3"], "m0 → m1 → m2 → m3")

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
    check(w._dispatcher.merge_enabled and w._dispatcher.merge_max == 10 and w._dispatcher._on_superseded is not None,
          "worker wires merge (on, max 10) and the supersede callback")
    w.shutdown_executors()
except Exception as e:  # import 환경 문제는 실패로 친다 — 배포 전 반드시 통과
    check(False, f"worker smoke raised {type(e).__name__}: {e}")


# ── 2026-11-12 쌓인 자동 메시지 합치기 · 대체 ──
def relay(i, text="[relay from:x] body", **extra):
    m = {"id": i, "project": "P", "text": f"{text} {i}", "dur": 0.05, "created_at": "2026-11-12T03:21:00+00:00"}
    m.update(extra)
    return m


# 7. 첫 턴이 도는 동안 쌓인 릴레이는 다음 자동 턴에 묶인다 — 사람 메시지가 먼저 가고 절대 안 묶이며, 하트비트·첨부는 따로
s, order, gate_t, turns, _, st = scenario("merge queued auto msgs", 1, 0, [
    relay("r1"),
    relay("r2"),
    relay("r3", text="[외부 relay @u · message] body"),
    relay("hb", text="[heartbeat] HEARTBEAT.md 확인"),
    relay("r4", files=[{"name": "a.pdf"}]),
    relay("r5", text="[relay from:y] body"),
    relay("h1", text="사람이 친 메시지"),
])
check(order == [["r1"], ["h1"], ["r2", "r3", "r5"], ["hb"], ["r4"]],
      f"r1 alone (started first) → human h1 → relays r2+r3+r5 merged (skipping hb/file) → hb → r4: {order}")
merged_msg = next((m for ids, m in turns if ids == ["r2", "r3", "r5"]), None)
check(merged_msg is not None and merged_msg["id"] == "r2" and merged_msg["merged_count"] == 3, "merged msg keeps first id, counts 3")
if merged_msg:
    check("[자동 메시지 3건 묶음]" in merged_msg["text"] and "━━━ 1/3 · #r2 · 11/12 12:21 ━━━" in merged_msg["text"],
          "merged text has header + KST stamped separators")
    check(all(f"body {i}" in merged_msg["text"] for i in ("r2", "r3", "r5")), "merged text carries every body")
check(st["merged_turns"] == 1 and st["merged_msgs"] == 3, f"stats count the merge {st}")

# 8. 대체 키: 같은 담당 대기열의 같은 키 옛 메시지는 빠지고 콜백으로 처리완료, 다른 키는 그대로
s, order, gate_t, turns, sup, st = scenario("supersede drops queued msg", 1, 0, [
    relay("r1"),
    relay("r2", text="[relay from:959] 1084 가 맡아", metadata={"supersede": "room:241574666"}),
    relay("r3", text="[relay from:959] 1084 말고 1249", metadata={"supersede": "room:241574666"}),
    relay("r4", text="[relay from:959] 다른 방", metadata={"supersede": "room:7"}),
])
check([(o["id"], n["id"]) for o, n in sup] == [("r2", "r3")], f"r2 superseded by r3 via callback: {sup}")
check(order == [["r1"], ["r3", "r4"]], f"r2 never runs; r3+r4 merge: {order}")
check(st["superseded"] == 1, f"stats count supersede {st}")

# 9. 끄기 스위치: merge_enabled=False 면 전부 따로 돈다
s, order, *_ = scenario("merge disabled", 1, 0, [relay("r1"), relay("r2"), relay("r3")], merge_enabled=False)
check(order == [["r1"], ["r2"], ["r3"]], f"no merging when disabled: {order}")

# 10. 묶음 상한: merge_max=2 면 두 통씩
s, order, *_ = scenario("merge_max=2", 1, 0, [relay("r1"), relay("r2"), relay("r3"), relay("r4"), relay("r5")], merge_max=2)
check(order == [["r1"], ["r2", "r3"], ["r4", "r5"]], f"two per turn: {order}")

# 11. 분류기 · 도우미
check(is_mergeable({"text": "[relay from:a branch:9]\nx"}), "relay mergeable")
check(is_mergeable({"text": "[stall-check] x"}), "stall-check mergeable")
check(not is_mergeable({"text": "[heartbeat] x"}), "heartbeat not mergeable")
check(not is_mergeable({"text": "[매니저] x"}), "manager msg not mergeable")
check(not is_mergeable({"text": "[relay from:a] x", "files": [{"n": 1}]}), "relay with files not mergeable")
check(not is_mergeable({"text": "[relay from:a] x", "reply_to": [3]}), "relay with reply_to not mergeable")
check(not is_mergeable({"text": "[relay from:a] x", "encrypted": True}), "encrypted not mergeable")
check(not is_mergeable({"text": "사람 글"}), "human not mergeable")
check(supersede_key({"metadata": {"supersede": " room:1 "}}) == "room:1", "supersede key trimmed")
check(supersede_key({"metadata": {"relay": {}}}) is None and supersede_key({}) is None, "no key → None")
check(supersede_key({"metadata": {"supersede": 5}}) is None, "non-string key → None")
check(turn_message_ids({"id": 7}) == [7] and turn_message_ids({"id": 7, "merged_ids": [7, 8]}) == [7, 8], "turn_message_ids")
m = merge_messages([{"id": 1, "text": "[relay from:a] 하나", "created_at": "2026-11-12T03:21:00.12+00:00", "project": "P"},
                    {"id": 2, "text": "[relay from:b] 둘", "created_at": None, "project": "P"}])
check(m["merged_ids"] == [1, 2] and m["project"] == "P" and m["id"] == 1, "merge keeps first id/project")
check("━━━ 2/2 · #2 ━━━" in m["text"], "missing created_at → no time stamp, no crash")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL PASS")
