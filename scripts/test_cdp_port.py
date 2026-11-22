#!/usr/bin/env python3
"""CDP 포트 배정 시험 (2026-11-22, KI-122) — 공유 맥 테넌트는 19000+uid, 그 외는 9222 인지,
build_claude_env 가 PV_CDP_PORT 를 넣는지, start-browser.sh 의 소유권 판정 함수가 내 리스너/남의 리스너를 가르는지.

실행: python3 scripts/test_cdp_port.py   (네트워크 없음, 5초 안팎)
배경: 11/22 공유 맥(777inked777)에서 테넌트 pv-a21802 의 start-browser.sh 가 소유자 크롬(9222)을 "already running" 으로
      보고 그 뒤 --cdp 9222 가 전부 소유자 브라우저에 들어갔다(finding #162). 포트 응답이 아니라 리스너 uid 로 판정한다.
"""
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import daemon.limits as limits  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ── 1. 포트 배정 ──
check("owner → 9222", limits.cdp_port_for_daemon({}, uid=501) == 9222)
check("portal_shared tenant uid 502 → 19502", limits.cdp_port_for_daemon({"portal_shared": True}, uid=502) == 19502)
check("tenant, uid None → 19000+os.getuid()",
      limits.cdp_port_for_daemon({"portal_shared": True}) == 19000 + os.getuid())
check("tenant, bad uid → 9222 (fallback)", limits.cdp_port_for_daemon({"portal_shared": True}, uid=0) == 9222)
_orig_getuser = limits.getpass.getuser
try:
    limits.getpass.getuser = lambda: "pv-a21802"
    check("pv-* account without flag → tenant", limits.is_shared_mac_tenant({}) is True)
    check("pv-* account → 19000+uid", limits.cdp_port_for_daemon({}, uid=502) == 19502)
    limits.getpass.getuser = lambda: "777inked777"
    check("owner account name → not tenant", limits.is_shared_mac_tenant({}) is False)
finally:
    limits.getpass.getuser = _orig_getuser

# ── 2. build_claude_env 주입 (파일 부작용 없는 경로로) ──
_orig_seat, _orig_ensure = limits.seat_config_dir, limits.ensure_agents_md_ignored
limits.seat_config_dir = lambda *_a, **_k: None
limits.ensure_agents_md_ignored = lambda *_a, **_k: False
try:
    os.environ.pop("PV_CDP_PORT", None)
    env = limits.build_claude_env({})
    check("owner env PV_CDP_PORT=9222", env.get("PV_CDP_PORT") == "9222", env.get("PV_CDP_PORT"))
    os.environ["PV_CDP_PORT"] = "9333"
    env = limits.build_claude_env({})
    check("owner env keeps inherited PV_CDP_PORT", env.get("PV_CDP_PORT") == "9333", env.get("PV_CDP_PORT"))
    env = limits.build_claude_env({"portal_shared": True})
    check("tenant env forces 19000+uid even if inherited", env.get("PV_CDP_PORT") == str(19000 + os.getuid()),
          env.get("PV_CDP_PORT"))
finally:
    os.environ.pop("PV_CDP_PORT", None)
    limits.seat_config_dir, limits.ensure_agents_md_ignored = _orig_seat, _orig_ensure

# ── 3. start-browser.sh 의 cdp_port_is_mine (함수만 뽑아 실행) ──
script = ROOT.parent / "skills" / "browser-handoff" / "scripts" / "start-browser.sh"
text = script.read_text()
start = text.index("cdp_port_is_mine() {")
end = text.index("\n}\n", start) + 3
func = text[start:end]


def is_mine(port: int) -> int:
    r = subprocess.run(["bash", "-c", func + f'\ncdp_port_is_mine {port}'], capture_output=True, text=True, timeout=15)
    return r.returncode


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

    def log_message(self, *_):
        pass


srv = HTTPServer(("127.0.0.1", 0), _H)
my_port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.2)
check(f"my listener on {my_port} → mine (rc 0)", is_mine(my_port) == 0)
srv.shutdown()

# 남의 리스너: root 소유 sshd(22) 또는 launchd 소켓이 열려 있을 때만 판정(없으면 건너뜀)
other = None
for p in (22, 5900, 631):
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=0.5):
            other = p
            break
    except OSError:
        continue
if other and os.geteuid() != 0:
    check(f"root-owned listener on {other} → not mine (rc 1)", is_mine(other) == 1)
else:
    print("skip  no root-owned listener to test against (or running as root)")
# 아무도 안 듣는 포트 → not mine
with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); free_port = s.getsockname()[1]
check(f"closed port {free_port} → not mine (rc 1)", is_mine(free_port) == 1)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL PASSED")
