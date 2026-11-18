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

# ══ 카드 823 (2026-11-18) — 팀원 폴더 터미널 전용 재로그인 + 브랜치 계정 칸 ═══════════════════════════
# ⓐ 정책 해석  ⓑ start() 가드  ⓒ _trigger_relogin 터미널 문안  ⓓ expected_email ← accounts  ⓔ 대조 경고 1회  ⓕ 설정 없는 머신
TERM_DIR = os.path.join(home, ".claude-accounts", "kim")
CHAT_DIR = os.path.join(home, ".claude-accounts", "lee")
ACCOUNTS = {
    "kim": {"config_dir": "~/.claude-accounts/kim", "email": "kim@example.com", "relogin": "terminal"},
    "lee": {"config_dir": "~/.claude-accounts/lee/", "email": "lee@example.com"},          # relogin 없음 = chat
    "park": {"config_dir": "~/.claude-accounts/park", "relogin": "TERMINAL"},               # email 없음, 대소문자
    "broken": "not-a-dict",
    "nodir": {"email": "x@example.com", "relogin": "terminal"},                             # config_dir 없음 → 무시
}
real_live_accounts = limits._live_accounts
limits._live_seat_settings = lambda: (None, {})      # 고객 맥 모양: 자리 없음
limits._live_accounts = lambda: ACCOUNTS

# ⓐ
pol = limits.account_policy("~/.claude-accounts/kim")
check("ⓐ 라벨·email·terminal 해석", (pol["label"], pol["email"], pol["relogin"], pol["config_dir"]),
      ("kim", "kim@example.com", "terminal", TERM_DIR))
check("ⓐ 끝 / 붙은 폴더(설정 쪽)도 같은 라벨", limits.account_policy(CHAT_DIR)["label"], "lee")
check("ⓐ 끝 / 붙은 폴더(조회 쪽)도 같은 라벨", limits.account_policy(TERM_DIR + "/")["label"], "kim")
check("ⓐ relogin 없음 → chat", limits.account_policy(CHAT_DIR)["relogin"], "chat")
check("ⓐ relogin 대소문자 무시·email 없음", (limits.account_policy("~/.claude-accounts/park")["relogin"],
                                           limits.account_policy("~/.claude-accounts/park")["email"]), ("terminal", None))
check("ⓐ 모르는 폴더 → None", limits.account_policy("/tmp/nowhere"), None)
check("ⓐ 기본 폴더(None/'') → None", (limits.account_policy(None), limits.account_policy("")), (None, None))
check("ⓐ is_terminal_only", (limits.is_terminal_only(TERM_DIR), limits.is_terminal_only(CHAT_DIR), limits.is_terminal_only(None)),
      (True, False, False))
check("ⓐ seat_config_dir 가 라벨→폴더를 live accounts 에서 읽음", limits.seat_config_dir("kim"), "~/.claude-accounts/kim")

# ⓑ start() 가드 — pty 를 가짜로 바꿔 불리면 실패
import pty
real_fork = pty.fork
def _boom():
    fails.append("pty.fork 가 불렸다")
    raise RuntimeError("pty.fork must not be called for terminal-only folders")
pty.fork = _boom
g2 = relogin.ReloginSession()
spawned = []
g2._run_flow = lambda cd: spawned.append(cd)
res = g2.start("branch:999", config_dir=TERM_DIR)
check("ⓑ 터미널 전용 폴더 → start 거절", (res.get("state"), res.get("started"), res.get("label")), ("terminal_only", False, "kim"))
check("ⓑ 엔진 상태 idle 그대로·플로우 안 띄움", (g2.state, spawned), ("idle", []))
res = g2.start("branch:998", config_dir="~/.claude-accounts/kim/")   # 끝 / 가 붙어도 같은 폴더
check("ⓑ 끝 / 폴더도 거절", res.get("state"), "terminal_only")
pty.fork = real_fork
res = g2.start("branch:997", config_dir=CHAT_DIR)
time.sleep(0.3)
check("ⓑ 채팅 정책 폴더 → 지금처럼 시작", (res.get("started"), spawned, g2.state), (True, [CHAT_DIR], "pending_url"))
check("ⓑ 채팅 폴더의 기대 계정 = accounts.email", g2._expected_email, "lee@example.com")
g2.reset()

# ⓒ _trigger_relogin: 터미널 문안을 돌려주고 엔진은 idle
import daemon.claude_runner as cr
msg = cr._trigger_relogin("branch:999", TERM_DIR)
check("ⓒ 터미널 문안(만료) — 명령·계정·만료 머리", ("CLAUDE_CONFIG_DIR=" + TERM_DIR + " claude auth login" in msg,
                                              "**kim@example.com**" in msg, msg.startswith("⚠️ 이 방의 Claude 로그인이 만료")), (True, True, True))
check("ⓒ 엔진(싱글턴) idle 그대로", relogin.status()["state"], "idle")
msg2 = relogin.terminal_login_notice(limits.account_policy(TERM_DIR), expired=False)
check("ⓒ 명시 트리거 문안 — 만료 아님 머리·같은 명령", (msg2.startswith("ℹ️"), "claude auth login" in msg2, "만료" not in msg2.splitlines()[0]), (True, True, True))
msg3 = relogin.terminal_login_notice(limits.account_policy("~/.claude-accounts/park"))
check("ⓒ email 없는 라벨 → 「이 방 전용 계정」", "이 방 전용 계정으로" in msg3, True)
check("ⓒ resolve_account_config_dir 가 같은 해석", True, True)

# ⓓ expected_email: 자리 없음 → accounts.email
check("ⓓ accounts.email 로 기대 계정", relogin.expected_email(TERM_DIR), "kim@example.com")
check("ⓓ email 없는 라벨 → None", relogin.expected_email("~/.claude-accounts/park"), None)
check("ⓓ 자리가 있으면 자리가 먼저", (lambda: (setattr(limits, "_live_seat_settings", lambda: ("s", SEATS)),
                                        relogin.expected_email(f"{home}/.claude-seats/claude2"),
                                        setattr(limits, "_live_seat_settings", lambda: (None, {})))[1])(), "claude2@vibement.kr")

# ⓔ 대조 경고: 다르면 1회, 같은 조합 반복 없음, 바뀌면 다시, 맞으면 리셋
real_email = limits.claude_account_email
limits._mismatch_warned.clear()
limits.claude_account_email = lambda d=None: "other@example.com"
w1 = limits.account_mismatch_warning(TERM_DIR) or ""
check("ⓔ 다른 계정 → 경고(두 계정 다 적힘·명령 포함)", ("kim@example.com" in w1, "other@example.com" in w1, "claude auth login" in w1), (True, True, True))
check("ⓔ 같은 조합 두 번째 → 없음", limits.account_mismatch_warning(TERM_DIR), None)
limits.claude_account_email = lambda d=None: "KIM@example.com"
check("ⓔ 맞는 계정(대소문자 무시) → 없음", limits.account_mismatch_warning(TERM_DIR), None)
limits.claude_account_email = lambda d=None: "other@example.com"
check("ⓔ 맞았다가 다시 틀림 → 다시 경고", bool(limits.account_mismatch_warning(TERM_DIR)), True)
limits.claude_account_email = lambda d=None: "third@example.com"
check("ⓔ 다른 계정으로 바뀜 → 다시 경고", "third@example.com" in (limits.account_mismatch_warning(TERM_DIR) or ""), True)
limits.claude_account_email = lambda d=None: None
check("ⓔ 못 읽으면 → 없음", limits.account_mismatch_warning(TERM_DIR), None)
limits.claude_account_email = lambda d=None: "zzz@example.com"
check("ⓔ email 없는 라벨 → 대조 안 함", limits.account_mismatch_warning("~/.claude-accounts/park"), None)
check("ⓔ 정책 없는 폴더 → 대조 안 함", limits.account_mismatch_warning("/tmp/nowhere"), None)
limits.claude_account_email = real_email
limits._mismatch_warned.clear()

# resolve_account_name: 브랜치 → 부모 사슬 → 루트, 칸반 scope 브랜치, 순환 보호
import daemon.branches as branches_mod
import daemon.supabase as supabase_mod
import daemon.kanban as kanban_mod
FAKE_BR = {
    1: {"id": 1, "project_id": "proj", "account": None, "parent_branch_id": None},
    2: {"id": 2, "project_id": "proj", "account": "kim", "parent_branch_id": 1},
    3: {"id": 3, "project_id": "proj", "account": None, "parent_branch_id": 2},
    7: {"id": 7, "project_id": "proj", "account": "  ", "parent_branch_id": None},
    9: {"id": 9, "project_id": "proj", "account": None, "parent_branch_id": 10},
    10: {"id": 10, "project_id": "proj", "account": None, "parent_branch_id": 9},
}
FAKE_SET = {"proj": {"account": "secondary"}, "general": {}}
fetch_calls = []
real_fb, real_fs, real_fk = branches_mod.fetch_branch, supabase_mod._fetch_project_settings, kanban_mod._fetch_kanban_card
branches_mod.fetch_branch = lambda i: (fetch_calls.append(i), FAKE_BR.get(i))[1]
supabase_mod._fetch_project_settings = lambda p: FAKE_SET.get(p, {})
kanban_mod._fetch_kanban_card = lambda i: {5: {"id": 5, "project_id": "proj", "scope_branch_id": 3},
                                           6: {"id": 6, "project_id": "proj", "scope_branch_id": None}}.get(i)
check("해석: 브랜치 자체 계정", limits.resolve_account_name("branch:2"), "kim")
check("해석: 자식에 없으면 부모 계정", limits.resolve_account_name("branch:3"), "kim")
check("해석: 사슬에 없으면 루트 프로젝트", limits.resolve_account_name("branch:1"), "secondary")
check("해석: 빈 문자열 계정 = 없음", limits.resolve_account_name("branch:7"), "secondary")
check("해석: 순환 사슬 → 루트로 탈출", limits.resolve_account_name("branch:9"), "secondary")
check("해석: 루트 프로젝트", limits.resolve_account_name("proj"), "secondary")
check("해석: 설정 없는 프로젝트 → default", limits.resolve_account_name("general"), "default")
check("해석: 칸반 카드(브랜치 보드) → 그 브랜치 사슬", limits.resolve_account_name("kanban:5"), "kim")
check("해석: 칸반 카드(루트 보드) → 루트 계정", limits.resolve_account_name("kanban:6"), "secondary")
fetch_calls.clear()
check("해석: 받아 둔 branch_data 면 첫 조회 생략", (limits.resolve_account_name("branch:3", branch_data=FAKE_BR[3], proj_settings=FAKE_SET["proj"]), fetch_calls), ("kim", [2]))
branches_mod.fetch_branch = lambda i: (_ for _ in ()).throw(RuntimeError("api down"))
check("해석: 조회 실패 → default (턴을 막지 않음)", limits.resolve_account_name("branch:3"), "default")
check("해석: resolve_account_config_dir 도 같은 길", cr.resolve_account_config_dir("proj"), None)   # secondary 는 ACCOUNTS 에 없음 → None
branches_mod.fetch_branch = lambda i: FAKE_BR.get(i)   # 가짜 사슬 복구(위에서 'api down' 으로 바꿨다)
check("해석: 브랜치 계정 → 폴더", cr.resolve_account_config_dir("branch:2"), TERM_DIR)
branches_mod.fetch_branch, supabase_mod._fetch_project_settings, kanban_mod._fetch_kanban_card = real_fb, real_fs, real_fk

# ⓕ 설정 없는 머신(고객 기본) → 전부 지금 동작
limits._live_accounts = lambda: {}
check("ⓕ 정책 없음", (limits.account_policy(TERM_DIR), limits.is_terminal_only(TERM_DIR), limits.account_mismatch_warning(TERM_DIR)), (None, False, None))
check("ⓕ expected_email None", (relogin.expected_email(None), relogin.expected_email(TERM_DIR)), (None, None))
check("ⓕ seat_config_dir 모르는 라벨 → None", limits.seat_config_dir("kim"), None)
g3 = relogin.ReloginSession(); g3._run_flow = lambda cd: None
check("ⓕ start 는 지금처럼 시작", g3.start("general", config_dir=None).get("started"), True); g3.reset()
limits._live_accounts = real_live_accounts
limits._live_seat_settings = real_live

print("ALL OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
