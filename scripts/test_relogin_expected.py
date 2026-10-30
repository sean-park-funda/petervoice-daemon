#!/usr/bin/env python3
"""재로그인 「맞는 계정」 시험 — 2026-10-30 claude2 자리에 sean@vibement.kr 이 들어간 사고 대응.

실행: python3 scripts/test_relogin_expected.py [--live]
- 기본: expected_email(자리 → 기대 계정), 링크 안내문, 로그인 뒤 계정 대조 경고를 가짜 자리 설정으로 확인한다.
- --live: 버리는 폴더(/tmp)로 실제 `claude auth login --email` 을 띄워 링크에 계정이 미리 채워지는지 보고 바로 끈다
  (코드를 넣지 않으므로 어떤 자리의 로그인도 바뀌지 않는다).
"""
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import daemon.limits as limits  # noqa: E402
import daemon.relogin as relogin  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(("OK  " if ok else "FAIL") + f" {name}: {got!r}" + ("" if ok else f" (기대 {want!r})"))
    if not ok:
        fails.append(name)


# ── 가짜 자리 설정 (Sean 맥미니 모양) ─────────────────────────────
SEATS = {
    "ptjcomics": {"config_dir": "", "email": "sungjunpark@ptjcomics.com"},
    "claude2": {"config_dir": "~/.claude-seats/claude2", "email": "claude2@vibement.kr"},
    "old": {"config_dir": "~/.claude-seats/old"},   # 이메일 없는 자리
}
real_live = limits._live_seat_settings
limits._live_seat_settings = lambda: ("claude2", SEATS)
home = os.path.expanduser("~")
check("자리 폴더 → 기대 계정", relogin.expected_email(f"{home}/.claude-seats/claude2"), "claude2@vibement.kr")
check("끝 / 붙은 폴더도 같은 자리", relogin.expected_email(f"{home}/.claude-seats/claude2/"), "claude2@vibement.kr")
check("~ 경로도 같은 자리", relogin.expected_email("~/.claude-seats/claude2"), "claude2@vibement.kr")
check("기본 폴더(None) → 기본 자리 계정", relogin.expected_email(None), "sungjunpark@ptjcomics.com")
check("이메일 없는 자리 → None", relogin.expected_email(f"{home}/.claude-seats/old"), None)
check("모르는 폴더 → None", relogin.expected_email("/tmp/nowhere"), None)
limits._live_seat_settings = lambda: (None, {})
check("자리 설정 없는 머신(고객) → None", relogin.expected_email(None), None)
limits._live_seat_settings = real_live

# ── 안내문 · 로그인 뒤 대조 ──────────────────────────────────────
s = relogin.ReloginSession()
s.url = "https://claude.com/cai/oauth/authorize?x=1"
s._expected_email = "claude2@vibement.kr"
check("안내문에 기대 계정", "**claude2@vibement.kr** 계정으로 로그인" in s._url_message(), True)
s._expected_email = None
check("기대 계정 없으면 예전 안내문", "본인 Claude 계정으로 로그인해 주세요" in s._url_message(), True)

real_who = relogin._logged_in_email
s._expected_email = "claude2@vibement.kr"
relogin._logged_in_email = lambda d=None: "sean@vibement.kr"
w = s._account_warning() or ""
check("다른 계정 → 경고", "sean@vibement.kr" in w and "claude2@vibement.kr" in w, True)
relogin._logged_in_email = lambda d=None: "Claude2@vibement.kr"
check("같은 계정(대소문자 무시) → 경고 없음", s._account_warning(), None)
relogin._logged_in_email = lambda d=None: None
check("계정을 못 읽으면 → 경고 없음(성공 그대로)", s._account_warning(), None)
s._expected_email = None
relogin._logged_in_email = lambda d=None: "sean@vibement.kr"
check("기대 계정 없는 머신 → 대조 안 함", s._account_warning(), None)
relogin._logged_in_email = real_who

# 결과 발송: 채팅 모드는 경고문이 성공 문구를 대신한다
sent = []
s._on_event = None
s._reply = lambda text, project=None: sent.append(text)
s._publish_result(True, "⚠️ 경고")
check("채팅 모드 결과 = 경고문", sent[-1:], ["⚠️ 경고"])
events = []
s._on_event = lambda ev: events.append(ev)
s._publish_result(True, "⚠️ 경고")
check("설정 화면 모드 → done + warning, 채팅에도 경고", (events[-1], sent[-1]), ({"type": "done", "warning": "⚠️ 경고"}, "⚠️ 경고"))

# ── 조각난 출력: 링크가 1024바이트 경계에서 끊겨도 온전히 잡는가 (가짜 CLI) ──────
# 실제 CLI 2.1.312 는 OSC 하이퍼링크(ESC]8;;URL BEL) + 보이는 URL 을 쓰고, 맥 pty 는 한 번에 1024바이트까지만 읽힌다
# → 그 경계가 보이는 URL 한가운데(login_hint=claude2%)에 떨어졌다. 가짜 CLI 는 첫 조각을 1024바이트 안에서 그 자리에 끊는다.
FAKE_URL = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code"
            "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=" + "x" * 150
            + "&state=" + "s" * 43 + "&login_hint=claude2%40vibement.kr")
fake_dir = tempfile.mkdtemp(prefix="pv-fake-cli-")
fake = os.path.join(fake_dir, "claude")
out = ("Opening browser...\r\n\x1b]8;;" + FAKE_URL + "\x07" + FAKE_URL + "\x1b]8;;\x07\r\nPaste code here if prompted > ")
cut = out.rindex("login_hint=claude2%") + len("login_hint=claude2%")   # 두 번째(보이는) URL 한가운데
assert cut < 1024, cut   # 첫 조각이 한 번에 읽혀야 「끊긴 URL」 상황이 재현된다
with open(fake, "w") as f:
    f.write("#!/usr/bin/env python3\nimport sys, time\n"
            f"out = {out!r}\ncut = {cut}\n"
            "sys.stdout.write(out[:cut]); sys.stdout.flush(); time.sleep(0.4)\n"
            "sys.stdout.write(out[cut:]); sys.stdout.flush(); time.sleep(30)\n")
os.chmod(fake, 0o755)
real_cmd = relogin.CLAUDE_CMD
relogin.CLAUDE_CMD = fake
fk = relogin.ReloginSession()
fk._publish_url = lambda: None
fk._wait_and_verify = lambda fd: None
t = threading.Thread(target=fk._spawn_and_capture_url, args=(None,), daemon=True)
t.start()
t.join(timeout=20)
fk._kill_proc()
relogin.CLAUDE_CMD = real_cmd
shutil.rmtree(fake_dir, ignore_errors=True)
check("조각난 출력에서 링크 전체를 잡음", fk.url, FAKE_URL)
check("state 도 온전히", fk.oauth_state, "s" * 43)

# ── 실제 CLI: 링크에 계정이 미리 채워지는가 ──────────────────────
if "--live" in sys.argv:
    tmp = tempfile.mkdtemp(prefix="pv-relogin-test-")
    live = relogin.ReloginSession()
    live._config_dir = tmp
    live._expected_email = "claude2@vibement.kr"
    msgs = []
    live._publish_url = lambda: msgs.append(live._url_message())
    live._wait_and_verify = lambda fd: None   # 코드는 넣지 않는다
    t = threading.Thread(target=live._spawn_and_capture_url, args=(tmp,), daemon=True)
    t.start()
    t.join(timeout=60)
    live._kill_proc()
    shutil.rmtree(tmp, ignore_errors=True)
    url = live.url or ""
    check("실제 링크 캡처", bool(url), True)
    check("링크에 기대 계정이 온전히 실림", url.endswith("login_hint=claude2%40vibement.kr") or "login_hint=claude2%40vibement.kr&" in url, True)

print("ALL OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
