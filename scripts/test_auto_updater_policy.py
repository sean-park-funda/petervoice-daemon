#!/usr/bin/env python3
"""CLI 업데이트 시점 정책 · 두 벌 플랫폼 패키지 감지 · 실행 파일 공백 재시도 시험 (2026-11-16).

실행: python3 scripts/test_auto_updater_policy.py   (네트워크 없음, 1초 안팎)
배경: 11/15 23:53 맥미니 커널 패닉 → 자동로그인 32초 뒤 업데이터가 npm 설치를 돌려 기계를 더 눌렀고,
      11/10~15 Claude CLI 2.1.325 설치가 5분마다 실패(두 벌 플랫폼 패키지)하며 실행 파일 공백으로 턴 16건이 Errno 2 로 죽었다.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from daemon.syncers.auto_updater import CliUpdatePolicy, AutoUpdater  # noqa: E402
from daemon.utils import popen_retry_missing_binary  # noqa: E402

FAILS = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


# ── 1. 시점 정책 ──
print("\n[CliUpdatePolicy]")
clk = Clock(0.0)
pol = CliUpdatePolicy(boot_grace_sec=900, max_defer_sec=6 * 3600, retry_sec=6 * 3600, clock=clk)
ok, why = pol.decide("2.1.325", inflight=0)
check(not ok and why == "boot grace", f"t=0: deferred during boot grace ({why})")
clk.t = 899
check(pol.decide("2.1.325", 0) == (False, "boot grace"), "t=899: still boot grace")
clk.t = 1000
ok, why = pol.decide("2.1.325", inflight=3)
check(not ok and "in flight" in why, f"t=1000, 3 turns running: deferred ({why})")
ok, why = pol.decide("2.1.325", inflight=0)
check(ok, "t=1000, idle: go")
# 실패 백오프
pol.note_failure("2.1.325")
check(pol.decide("2.1.325", 0) == (False, "failed earlier"), "same version right after a failure: skipped")
clk.t = 1000 + 6 * 3600 - 1
check(pol.decide("2.1.325", 0) == (False, "failed earlier"), "5h59m later: still skipped")
clk.t = 1000 + 6 * 3600 + 1
check(pol.decide("2.1.325", 0)[0], "6h later: retried")
clk.t = 2000
pol.note_failure("2.1.325")
ok, why = pol.decide("2.1.326", 0)
check(ok, f"a newer version resets the back-off ({why!r})")
# 최대 미룸: 바쁜 기계라도 6시간 넘게 미루지 않는다
pol2 = CliUpdatePolicy(boot_grace_sec=0, max_defer_sec=6 * 3600, retry_sec=6 * 3600, clock=clk)
clk.t = 10_000
check(pol2.decide("2.1.330", 5) == (False, "5 turn(s) in flight"), "busy: deferred")
clk.t = 10_000 + 6 * 3600 - 10
check(not pol2.decide("2.1.330", 5)[0], "busy 5h59m: still deferred")
clk.t = 10_000 + 6 * 3600 + 10
check(pol2.decide("2.1.330", 5)[0], "busy 6h+: installed anyway (max_defer)")
pol2.note_success()
clk.t = 20_000
check(pol2.decide("2.1.331", 5) == (False, "5 turn(s) in flight"), "after success the defer window restarts for the next version")
# 미룸 로그는 같은 사유로 한 번만
pol3 = CliUpdatePolicy(boot_grace_sec=0, clock=clk)
check(pol3.should_log_defer("2.1.330", "2 turn(s) in flight"), "first defer logged")
check(not pol3.should_log_defer("2.1.330", "7 turn(s) in flight"), "same reason (count differs) not logged again")
check(pol3.should_log_defer("2.1.330", "failed earlier"), "different reason logged")
check(pol3.should_log_defer("2.1.331", "failed earlier"), "different version logged")
# in_boot_grace
clk.t = 0
pol4 = CliUpdatePolicy(boot_grace_sec=900, clock=clk)
check(pol4.in_boot_grace(), "in boot grace at t=0")
clk.t = 901
check(not pol4.in_boot_grace(), "out of boot grace at t=901")

# ── 1b. 복구(repair) 판단 — 실행 파일이 없으면 턴 유무·6h 백오프와 무관하게, 부팅 완충과 30분 간격만 (2026-11-17) ──
print("\n[CliUpdatePolicy.decide_repair]")
clk5 = Clock(0.0)
pol5 = CliUpdatePolicy(boot_grace_sec=900, retry_sec=6 * 3600, repair_retry_sec=1800, clock=clk5)
check(pol5.decide_repair() == (False, "boot grace"), "t=0: repair waits for boot grace")
clk5.t = 901
pol5.note_failure("2.1.326")                      # 같은 버전 설치가 방금 실패했어도
check(pol5.decide_repair() == (True, ""), "after boot grace: repair goes even though the version failed earlier")
pol5.note_repair_attempt()
ok, why = pol5.decide_repair()
check(not ok and why.startswith("repair tried"), f"right after an attempt: deferred ({why})")
clk5.t = 901 + 1799
check(not pol5.decide_repair()[0], "29m59s later: still deferred")
clk5.t = 901 + 1800
check(pol5.decide_repair()[0], "30m later: retried")
pol5.note_repair_attempt(); pol5.note_success()
check(pol5.decide_repair()[0], "note_success clears the repair back-off")

# ── 2. 두 벌 플랫폼 패키지 감지 ──
print("\n[stray platform packages]")
up = AutoUpdater()
with tempfile.TemporaryDirectory() as tmp:
    scope = Path(tmp) / "@anthropic-ai"
    (scope / "claude-code").mkdir(parents=True)
    check(up._stray_platform_packages({}, root=tmp) == [], "only the main package → none")
    (scope / "claude-code-darwin-arm64").mkdir()
    (scope / "claude-code" / "node_modules" / "@anthropic-ai" / "claude-code-darwin-arm64").mkdir(parents=True)
    check(up._stray_platform_packages({}, root=tmp) == ["claude-code-darwin-arm64"],
          "top-level platform copy detected (nested copy inside the main package is not stray)")
    (scope / "claude-code-linux-x64").mkdir()
    check(up._stray_platform_packages({}, root=tmp) == ["claude-code-darwin-arm64", "claude-code-linux-x64"],
          "every top-level claude-code-* sibling listed")
    check(up._stray_platform_packages({}, root=os.path.join(tmp, "nope")) == [], "missing root → none, no exception")
check(up._stray_platform_packages({}, root="") == [], "empty root → none")

# ── 2b. 실행 파일 없음 판별 · 죽은 npm 찌꺼기 정리 · 설치 타임아웃 처리 (2026-11-17) ──
print("\n[missing binary / leftovers / install timeout]")
with tempfile.TemporaryDirectory() as td:
    empty_env = {"PATH": td}
    check(up._claude_version(empty_env) == "", "claude missing → _claude_version returns '' (no exception)")
    check(up._claude_present(empty_env) is None, "claude missing → _claude_present None")
    fake = Path(td) / "claude"
    fake.write_text("#!/bin/sh\necho \"9.9.9 (Claude Code)\"\n"); fake.chmod(0o755)
    check(up._claude_present(empty_env) == str(fake), "executable on PATH → _claude_present finds it")
    check(up._claude_version(empty_env) == "9.9.9", "fake claude --version parsed")
    root = Path(td) / "node_modules"
    scope = root / "@anthropic-ai"
    for n in (".claude-code-2DTsDk1V", ".claude-code-abc", "claude-code", "claude-code-darwin-arm64"):
        (scope / n).mkdir(parents=True)
    gone = up._remove_retired_leftovers(str(root))
    check(gone == [".claude-code-2DTsDk1V", ".claude-code-abc"], f"retired dot-dirs removed: {gone}")
    check(sorted(os.listdir(scope)) == ["claude-code", "claude-code-darwin-arm64"], "live package dirs untouched")
    check(up._remove_retired_leftovers("") == [] and up._remove_retired_leftovers(str(Path(td) / "nope")) == [], "no root / no scope → nothing")

import subprocess as _sp
import daemon.globals as _g
_real_run = _sp.run
def _timeout_run(cmd, **kw):
    raise _sp.TimeoutExpired(cmd, kw.get("timeout"))
_sp.run = _timeout_run
try:
    ok, why = up._npm_install_claude({"PATH": "/nonexistent"}, "2.1.326")
finally:
    _sp.run = _real_run
check(not ok and "timed out" in why, f"npm install timeout → (False, …timed out…): {why[:60]}")
check(_g.cli_install_inflight is False, "cli_install_inflight reset after timeout")
check(up._install_timeout() >= 300, f"install timeout floor 300s (got {up._install_timeout()})")

# ── 3. 실행 파일 공백 재시도 ──
print("\n[popen_retry_missing_binary]")
clk = Clock(0.0)
calls = []
resolved = iter(["", "/opt/homebrew/bin/claude"])


def popen(cmd):
    calls.append(list(cmd))
    if len(calls) < 3:
        raise FileNotFoundError(2, "No such file or directory", cmd[0])
    return {"proc": cmd[0]}


def sleep(sec):
    clk.t += sec


class Log:
    def __init__(self):
        self.warned = []

    def warning(self, m):
        self.warned.append(m)


log = Log()
proc = popen_retry_missing_binary(popen, ["claude", "-p"], resolve=lambda: next(resolved, "/opt/homebrew/bin/claude"),
                                  retry_sec=120, interval=3, log=log, sleep=sleep, clock=clk)
check(proc == {"proc": "/opt/homebrew/bin/claude"}, f"third attempt succeeded with re-resolved path ({proc})")
check(len(calls) == 3 and calls[0][0] == "claude" and calls[2][0] == "/opt/homebrew/bin/claude", f"cmd[0] re-resolved: {[c[0] for c in calls]}")
check(len(log.warned) == 1 and "missing" in log.warned[0], f"warned exactly once ({len(log.warned)})")
check(clk.t == 6, f"slept 2×3s = {clk.t}s")

clk = Clock(0.0)
n = [0]


def always_missing(cmd):
    n[0] += 1
    raise FileNotFoundError(2, "No such file", cmd[0])


try:
    popen_retry_missing_binary(always_missing, ["claude"], retry_sec=10, interval=3, sleep=sleep, clock=clk)
    check(False, "should have raised after the deadline")
except FileNotFoundError:
    check(True, f"raised after deadline ({n[0]} attempts over {clk.t}s)")
check(n[0] == 5, f"attempts bounded by retry_sec/interval (+1): {n[0]}")

# 설치 중(extend_while True)에는 기한을 max_sec 까지 늘린다 (2026-11-17)
clk_e = Clock(0.0)
busy_until = [20.0]
calls_e = [0]
def popen_until_20(c):
    calls_e[0] += 1
    if clk_e.t < 20.0:
        raise FileNotFoundError(2, "No such file or directory", c[0])
    return {"ok": clk_e.t}
def sleep_e(sec):
    clk_e.t += sec
res = popen_retry_missing_binary(popen_until_20, ["claude"], retry_sec=6, interval=3, max_sec=60,
                                 extend_while=lambda: clk_e.t < busy_until[0], sleep=sleep_e, clock=clk_e, log=log)
check(res == {"ok": 21.0}, f"kept waiting past 6s while the install ran, succeeded at t={res}")
check(any("install in progress" in w for w in log.warned), "logged the extension once")
clk_e.t = 0.0
try:
    popen_retry_missing_binary(lambda c: (_ for _ in ()).throw(FileNotFoundError(2, "x", c[0])), ["claude"], retry_sec=6, interval=3,
                               max_sec=30, extend_while=lambda: True, sleep=sleep_e, clock=clk_e)
    check(False, "should have raised at max_sec")
except FileNotFoundError:
    check(30.0 <= clk_e.t <= 33.0, f"install never finished → raised at max_sec ({clk_e.t}s)")
clk_e.t = 0.0
try:
    popen_retry_missing_binary(lambda c: (_ for _ in ()).throw(FileNotFoundError(2, "x", c[0])), ["claude"], retry_sec=6, interval=3,
                               max_sec=60, extend_while=lambda: False, sleep=sleep_e, clock=clk_e)
    check(False, "should have raised at retry_sec")
except FileNotFoundError:
    check(6.0 <= clk_e.t <= 9.0, f"no install running → raised at retry_sec ({clk_e.t}s)")

n[0] = 0
try:
    popen_retry_missing_binary(always_missing, ["claude"], retry_sec=600, interval=3, sleep=sleep, clock=Clock(0.0),
                               should_stop=lambda: True)
    check(False, "should have raised when shutdown requested")
except FileNotFoundError:
    check(n[0] == 1, f"stops retrying on shutdown ({n[0]} attempt)")

try:
    popen_retry_missing_binary(lambda c: (_ for _ in ()).throw(PermissionError("x")), ["claude"], retry_sec=10)
    check(False, "other errors must propagate")
except PermissionError:
    check(True, "other OSError subclasses propagate immediately")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("all passed")
