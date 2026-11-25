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
    RESUME_NOTICE, body_text, compile_priority_patterns, is_priority_auto,
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

    cur_auto, max_auto = [0], [0]   # 자동 턴 동시 실행 수 (부팅 완충 시나리오가 본다)

    def run_turn(msg):
        ids = turn_message_ids(msg)
        auto = is_automatic(msg)
        with lock:
            for i in ids:
                starts[i] = round(time.monotonic() - t0, 2)
            order.append((ids, msg))
            if auto:
                cur_auto[0] += 1
                max_auto[0] = max(max_auto[0], cur_auto[0])
        if gate_first and msg["id"] == first_id:
            gate.wait(10)
        time.sleep(msg.get("dur", 0.05))
        with lock:
            if auto:
                cur_auto[0] -= 1
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
    st["max_auto_concurrency"] = max_auto[0]
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

# 3b. 작업대 실무자 글(릴레이 길)은 같은 담당 대기열에서 하트비트·팀장 릴레이를 앞지르고, 묶기(켜 둠)에도 안 들어간다 (2026-11-16)
WB = "[relay from:axl-consulting branch:766]\n[작업대 대화] 1 정산서 검수 — 김현정 경영관리부 대리 (대화 #552) · 역할: 실무자"
s, order, *_ = scenario("workbench staff message jumps queue, not merged", 1, 0, [
    {"id": "r1", "project": "branch:741", "text": "[relay from:axl-consulting branch:708]\n팀장 지시 1"},
    {"id": "r2", "project": "branch:741", "text": "[relay from:axl-consulting branch:708]\n팀장 지시 2"},
    {"id": "hb", "project": "branch:741", "text": "[heartbeat] HEARTBEAT.md"},
    {"id": "wb", "project": "branch:741", "text": WB},
    {"id": "ts", "project": "branch:741", "text": "[relay from:axl-consulting branch:766]\n[시험 자리] [작업대 대화] 1 정산서 검수 — 시험"},
])
check(order == [["r1"], ["wb"], ["r2", "ts"], ["hb"]], f"order r1 → wb(human) → r2+ts merged → hb: {order}")

# 3c. 작업대 실무자 글은 사람 예비 칸을 쓴다 — 자동 턴이 칸을 다 채워도 바로 시작 (11/03 원인 ③ 재발 막기)
s, order, gate_t, *_ = scenario("workbench staff message uses the human reserve lane", 2, 1, [
    {"id": "X1", "project": "X", "text": "[heartbeat] x"},
    {"id": "Y1", "project": "Y", "text": "[relay from:q] y"},
    {"id": "W1", "project": "branch:741", "text": WB},
], merge_enabled=False)
check(s["W1"] < gate_t, f"W1 (workbench staff) started at {s['W1']}s though the auto lane was full")
check(s["Y1"] >= gate_t, f"Y1 (auto) waited for the auto lane ({s['Y1']}s)")

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
# 재시작 복구 머리말은 분류 전에 벗긴다 (2026-11-16 — 전에는 복구된 릴레이가 사람으로 분류됐다)
check(is_automatic({"text": RESUME_NOTICE + "[relay from:x] a"}), "resumed relay = automatic")
check(is_mergeable({"text": RESUME_NOTICE + "[relay from:x] a"}), "resumed relay = mergeable")
check(is_automatic({"text": RESUME_NOTICE + "[heartbeat] HEARTBEAT.md"}), "resumed heartbeat = automatic")
check(not is_automatic({"text": RESUME_NOTICE + "사람이 친 메시지"}), "resumed human message = human")
check(body_text({"text": RESUME_NOTICE + "  [relay from:x] a"}) == "[relay from:x] a", "body_text strips notice + spaces")
# 작업대 실무자 글은 릴레이 길로 와도 사람 메시지 (2026-11-16, axl 카드 839 E) — 표식·깨우기·중지·본문 중간 언급은 자동 그대로
WB = "[relay from:axl-consulting branch:766]\n[작업대 대화] 1 정산서 검수 — 김현정 경영관리부 대리 (대화 #552) · 역할: 실무자"
check(not is_automatic({"text": WB}), "workbench staff message = human")
check(not is_automatic({"text": RESUME_NOTICE + WB}), "resumed workbench staff message = human")
check(is_automatic({"text": "[relay from:axl-consulting branch:766]\n[시험 자리] [작업대 대화] 1 정산서 검수 — 시험"}), "workbench test seat = automatic")
check(is_automatic({"text": "[relay from:axl-consulting branch:766]\n(Sean 시험) [작업대 대화] 1 정산서 검수 — 대신 보기"}), "workbench proxy view = automatic")
check(is_automatic({"text": "[relay from:axl-consulting branch:766]\n[작업대 확인] 1 정산서 검수 · 역할: 실무자 — eta 지남"}), "workbench wake-up = automatic")
check(is_automatic({"text": "[relay from:axl-consulting branch:766]\n[작업 중지] 1 정산서 검수 — 김현정"}), "workbench stop = automatic")
check(is_automatic({"text": "[relay from:coo]\n팀장 지시 — 작업대 글 「[작업대 대화] …」 를 참고"}), "other relay mentioning the head mid-body = automatic")
check(is_automatic({"text": "[heartbeat] [작업대 대화] x"}), "heartbeat with the head = automatic")

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

# ── 부팅 완충 (2026-11-16) — 기계가 막 켜졌으면 자동 턴은 boot_auto_lanes 개씩, 사람은 바로. 따뜻한 기계엔 없음 ──
# 11. 막 켜진 기계(uptime 30초): 자동 턴 동시 1개, 사람 메시지는 자동 칸이 꽉 차도 바로
s, order, gate_t, turns, _, st = scenario("boot grace caps auto turns", 4, 0, [
    {"id": "X1", "project": "X", "text": "[heartbeat] x", "dur": 0.2},
    {"id": "Y1", "project": "Y", "text": RESUME_NOTICE + "[relay from:q] y", "dur": 0.2},   # 복구된 릴레이 = 자동
    {"id": "Z1", "project": "Z", "text": "[relay from:q] z", "dur": 0.2},
    {"id": "H1", "project": "H", "text": "사람 메시지"},
], merge_enabled=False, boot_grace_sec=30, boot_auto_lanes=1, uptime_fn=lambda: 30.0)
check(s["H1"] < gate_t, f"H1 (human) started at {s['H1']}s during boot grace")
check(st["max_auto_concurrency"] == 1, f"auto turns never overlapped during boot grace (max {st['max_auto_concurrency']})")
check(s["Y1"] >= gate_t and s["Z1"] >= gate_t, f"Y1/Z1 waited behind gated X1 ({s['Y1']}, {s['Z1']} ≥ {gate_t})")
check(st["boot_grace_left"] > 20, f"grace still armed after the run ({st['boot_grace_left']}s left)")

# 12. 따뜻한 기계(uptime 2시간): 완충 없음 → 자동 턴이 겹쳐 돈다
s, order, gate_t, turns, _, st = scenario("no boot grace on a warm machine", 4, 0, [
    {"id": "X1", "project": "X", "text": "[heartbeat] x", "dur": 0.2},
    {"id": "Y1", "project": "Y", "text": "[relay from:q] y", "dur": 0.2},
    {"id": "Z1", "project": "Z", "text": "[relay from:q] z", "dur": 0.2},
], merge_enabled=False, boot_grace_sec=30, boot_auto_lanes=1, uptime_fn=lambda: 7200.0)
check(st["max_auto_concurrency"] >= 2, f"auto turns overlapped without grace (max {st['max_auto_concurrency']})")
check(st["boot_grace_left"] == 0, "no grace armed on a warm machine")

# 13. 완충은 시간이 지나면 풀린다 · uptime 을 모르면 보수적으로 건다 (white-box)
d = TurnDispatcher(4, 0, run_turn=lambda m: None, submit=lambda f, it: None, boot_grace_sec=30, uptime_fn=lambda: 10.0)
d._arm_boot_grace()
check(d.boot_grace_left() > 25, f"armed on cold machine ({d.boot_grace_left()}s)")
d._boot_until = time.monotonic() - 1
check(d.boot_grace_left() == 0, "expired grace → no cap")
d2 = TurnDispatcher(4, 0, run_turn=lambda m: None, submit=lambda f, it: None, boot_grace_sec=30, uptime_fn=lambda: None)
d2._arm_boot_grace()
check(d2.boot_grace_left() > 25, "unknown uptime → grace armed (conservative)")
d3 = TurnDispatcher(4, 0, run_turn=lambda m: None, submit=lambda f, it: None, boot_grace_sec=0, uptime_fn=lambda: 10.0)
d3._arm_boot_grace()
check(d3.boot_grace_left() == 0, "boot_grace_sec=0 → disabled")

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

# ── 2026-11-25 급한 자동 메시지 먼저 (revenue-lab #946 카드 981 — 숨고 고객 알림이 자동 줄 25통 뒤에서 20~45분 대기) ──
PRIO = [r"^\[relay from:revenue-lab branch:(959|1085)\]\s*\[숨고 (고객 추가 메시지|진행 중 방)"]
CUST = "[relay from:revenue-lab branch:959]\n[숨고 고객 추가 메시지 · 자동] 인쇄물 디자인 · 홍길동 님"
ROOM = "[relay from:revenue-lab branch:959]\n[숨고 진행 중 방 · 담당 턴 10분째 · 자동] 인쇄물 디자인"

# 14. 분류기: 머리가 맞는 자동 메시지만 급함 — 사람 글·다른 발신·본문 중간 언급·틀린 식은 아님
pp = compile_priority_patterns(PRIO)
check(is_priority_auto({"text": CUST}, pp) and is_priority_auto({"text": ROOM}, pp), "customer alert / in-progress room = priority auto")
check(is_priority_auto({"text": RESUME_NOTICE + CUST}, pp), "resumed customer alert = priority auto")
check(not is_priority_auto({"text": "[relay from:revenue-lab branch:1085]\n[시각 알림 · 자동] 견적만 나간 방"}, pp), "other alert from the same sender = ordinary auto")
check(not is_priority_auto({"text": "[relay from:game-develop]\n[숨고 고객 추가 메시지 · 자동] 흉내"}, pp), "same head from another sender = ordinary auto")
check(not is_priority_auto({"text": "[relay from:coo]\n참고: [relay from:revenue-lab branch:959]\n[숨고 고객 추가 메시지 · 자동]"}, pp), "mention mid-body = ordinary auto")
check(not is_priority_auto({"text": "[숨고 고객 추가 메시지 · 자동] 사람이 붙여 넣은 글"}, pp), "human message never priority-auto (already first)")
check(not is_priority_auto({"text": CUST}, []) and not is_priority_auto({"text": CUST}, None), "no patterns → feature off")
check(len(compile_priority_patterns(["(unclosed", "", None, 3, r"^\[heartbeat\]"])) == 1, "bad regex / junk entries skipped, good one kept")
check(len(compile_priority_patterns(r"^\[relay from:x\]")) == 1 and compile_priority_patterns(None) == [], "string → one pattern, None → none")

# 15. 다른 담당끼리: 자동 칸 1개가 차 있어도, 풀리면 나중에 온 급한 알림이 먼저 온 자동 메시지들을 앞지른다. 사람은 여전히 맨 앞
s, order, gate_t, turns, _, st = scenario("priority auto jumps the auto queue", 1, 0, [
    {"id": "L1", "project": "L", "text": "[relay from:funda-rebuild]\n긴 작업"},
    {"id": "A1", "project": "A", "text": "[heartbeat] a"},
    {"id": "B1", "project": "B", "text": "[relay from:game-develop]\nb"},
    {"id": "C1", "project": "branch:1084", "text": CUST},
    {"id": "H1", "project": "H", "text": "사람이 친 메시지"},
], priority_patterns=PRIO)
check(order == [["L1"], ["H1"], ["C1"], ["A1"], ["B1"]], f"L1 (running) → human → customer alert → A1 → B1: {order}")
check(st["priority_started"] == 1 and st["priority_patterns"] == 1, f"stats count the priority start {st}")

# 16. 패턴이 없으면 예전 그대로 도착순
s, order, *_ = scenario("no patterns → plain FIFO", 1, 0, [
    {"id": "L1", "project": "L", "text": "[relay from:funda-rebuild]\n긴 작업"},
    {"id": "A1", "project": "A", "text": "[heartbeat] a"},
    {"id": "C1", "project": "branch:1084", "text": CUST},
])
check(order == [["L1"], ["A1"], ["C1"]], f"unchanged without config: {order}")

# 17. 같은 담당 안에서: 급한 알림이 하트비트를 앞지르고, 먼저 와 있던 릴레이는 같은 턴에 **도착순으로** 묶인다
s, order, gate_t, turns, _, st = scenario("priority within one project merges in arrival order", 1, 0, [
    {"id": "r0", "project": "branch:950", "text": "[relay from:revenue-lab branch:946]\n먼저 돌던 턴"},
    {"id": "hb", "project": "branch:950", "text": "[heartbeat] HEARTBEAT-branch-950.md"},
    {"id": "r1", "project": "branch:950", "text": "[relay from:revenue-lab branch:946]\n팀장 지시", "created_at": "2026-11-25T05:00:00+00:00"},
    {"id": "c1", "project": "branch:950", "text": CUST, "created_at": "2026-11-25T05:01:00+00:00"},
], priority_patterns=PRIO)
check(order == [["r0"], ["r1", "c1"], ["hb"]], f"r0 → (r1 + customer alert, arrival order) → heartbeat last: {order}")
mm = next((m for ids, m in turns if ids == ["r1", "c1"]), None)
check(mm is not None and mm["text"].index("팀장 지시") < mm["text"].index("숨고 고객 추가 메시지"), "merged body keeps arrival order (older relay first)")
check(mm is not None and mm["id"] == "r1" and [p["id"] for p in mm["merged_parts"]] == ["r1", "c1"], "merged base id = oldest (r1), merged_parts in arrival order")

# 18. 예비 칸 빌리기: 자동 칸(3-2=1)이 차 있어도 급한 알림은 사람 예비 칸 1개를 빌려 바로 시작, 보통 자동은 기다린다.
#     빌린 뒤에도 사람 칸 1개는 남아 사람 메시지가 바로 시작한다 (리뷰 11/25: 예비 칸을 다 빌려주면 사람이 긴 자동 턴 뒤에 선다)
s, order, gate_t, turns, _, st = scenario("priority auto borrows a reserve lane, one stays for humans", 3, 2, [
    {"id": "X1", "project": "X", "text": "[heartbeat] x"},
    {"id": "Z1", "project": "Z", "text": "[relay from:q]\nz"},
    {"id": "C1", "project": "branch:1084", "text": CUST, "dur": 0.4},
    {"id": "C2", "project": "branch:950", "text": CUST},
    {"id": "H1", "project": "H", "text": "사람이 친 메시지"},
], merge_enabled=False, priority_patterns=PRIO, priority_extra_lanes=2)
check(st["priority_extra"] == 1, f"extra lanes clamped to reserve-1 = 1 ({st['priority_extra']})")
check(s["C1"] < gate_t, f"customer alert started at {s['C1']}s while the auto lane was busy")
check(s["H1"] < gate_t, f"human H1 started at {s['H1']}s — one reserve lane was kept for humans")
check(s["Z1"] >= gate_t, f"ordinary auto Z1 still waited ({s['Z1']}s ≥ gate {gate_t}s)")
check(st["max_auto_concurrency"] == 2, f"auto concurrency peaked at cap+1 = 2 ({st['max_auto_concurrency']})")
check(st["priority_running"] == 0, f"priority counter released ({st['priority_running']})")

# 19. 빌리기 0(기본)이면 순서만 앞선다 — 자동 칸이 풀릴 때까지는 기다린다
s, order, gate_t, turns, _, st = scenario("no borrowing by default", 2, 1, [
    {"id": "X1", "project": "X", "text": "[heartbeat] x"},
    {"id": "Z1", "project": "Z", "text": "[relay from:q]\nz"},
    {"id": "C1", "project": "branch:1084", "text": CUST},
], merge_enabled=False, priority_patterns=PRIO)
check(s["C1"] >= gate_t, f"customer alert waited for the auto lane without borrowing ({s['C1']}s ≥ gate {gate_t}s)")
check(order == [["X1"], ["C1"], ["Z1"]], f"but went before Z1 once the lane freed: {order}")
check(st["max_auto_concurrency"] == 1, f"auto concurrency stayed at the auto cap 1 ({st['max_auto_concurrency']})")
d4 = TurnDispatcher(4, 1, run_turn=lambda m: None, submit=lambda f, it: None, priority_patterns=PRIO, priority_extra_lanes=9)
check(d4.priority_extra == 0, f"reserve 1 → nothing to lend (humans keep their lane): {d4.priority_extra}")
d4b = TurnDispatcher(12, 2, run_turn=lambda m: None, submit=lambda f, it: None, priority_patterns=PRIO, priority_extra_lanes=9)
check(d4b.priority_extra == 1 and d4b.priority_max_running == 5, f"12 lanes / reserve 2 → lend 1, at most 5 priority turns ({d4b.priority_extra}, {d4b.priority_max_running})")
d5 = TurnDispatcher(4, 2, run_turn=lambda m: None, submit=lambda f, it: None, priority_extra_lanes=2)
check(d5.priority_extra == 0, "extra lanes ignored without patterns")
d6 = TurnDispatcher(4, 2, run_turn=lambda m: None, submit=lambda f, it: None, priority_patterns=PRIO, priority_extra_lanes="x")
check(d6.priority_extra == 0, "junk extra-lanes value → 0, no crash")

# 19b. 급한 대우 동시 상한(자동 칸의 절반): 4칸·예비 0 → 2턴까지. 급한 턴 2개가 도는 동안 풀린 칸에서는 셋째 급한 알림도
#      보통 자동처럼 도착순으로 선다(먼저 온 하트비트 뒤) — 급한 턴이 끝나 자리가 나야 다시 앞줄 대우를 받는다
s, order, gate_t, turns, _, st = scenario("priority treatment capped at half the auto lanes", 4, 0, [
    {"id": "N0", "project": "N0", "text": "[relay from:q]\n게이트로 붙잡힌 보통 턴"},
    {"id": "C1", "project": "branch:1084", "text": CUST, "dur": 1.0},           # 급한 턴 1
    {"id": "C2", "project": "branch:950", "text": CUST, "dur": 1.0},            # 급한 턴 2 (상한 2 도달)
    {"id": "N1", "project": "N1", "text": "[relay from:q]\n보통", "dur": 1.0},  # 여기서 4칸이 다 찬다
    {"id": "HB", "project": "K", "text": "[heartbeat] 먼저 온 하트비트"},
    {"id": "C3", "project": "branch:1323", "text": CUST},                       # 급하지만 상한 초과 → 도착순
], merge_enabled=False, priority_patterns=PRIO)
check(order[4:] == [["HB"], ["C3"]], f"third alert queued FIFO behind the earlier heartbeat while 2 priority turns ran: {order}")
check(st["priority_started"] == 2, f"only two turns got priority treatment ({st['priority_started']})")
check(st["priority_running"] == 0, "priority counter back to 0")

# 19c. 대체: 급한 옛 메시지를 같은 키의 보통 메시지가 대체하면 급한 등급을 물려받는다
s, order, gate_t, turns, sup, st = scenario("superseding message inherits priority", 1, 0, [
    {"id": "L1", "project": "L", "text": "[relay from:funda-rebuild]\n긴 작업"},
    {"id": "A1", "project": "A", "text": "[heartbeat] a"},
    {"id": "c1", "project": "branch:1084", "text": CUST, "metadata": {"supersede": "room:9"}},
    {"id": "c2", "project": "branch:1084", "text": "[relay from:revenue-lab branch:946]\n같은 방 바로잡기", "metadata": {"supersede": "room:9"}},
], priority_patterns=PRIO)
check([(o["id"], n["id"]) for o, n in sup] == [("c1", "c2")], f"c1 superseded by c2: {sup}")
check(order == [["L1"], ["c2"], ["A1"]], f"c2 inherited priority and went before the heartbeat: {order}")

# 20. 부팅 완충 중에는 급한 알림도 자동 1칸 규칙을 따른다(빌리지 않음) — 순서만 앞선다
s, order, gate_t, turns, _, st = scenario("boot grace: priority does not borrow", 4, 1, [
    {"id": "X1", "project": "X", "text": "[heartbeat] x", "dur": 0.2},
    {"id": "Z1", "project": "Z", "text": "[relay from:q]\nz", "dur": 0.2},
    {"id": "C1", "project": "branch:1084", "text": CUST, "dur": 0.2},
], merge_enabled=False, priority_patterns=PRIO, priority_extra_lanes=1,
    boot_grace_sec=30, boot_auto_lanes=1, uptime_fn=lambda: 30.0)
check(st["max_auto_concurrency"] == 1, f"auto turns never overlapped during boot grace (max {st['max_auto_concurrency']})")
check(order == [["X1"], ["C1"], ["Z1"]], f"priority still first once the lane frees: {order}")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL PASS")
