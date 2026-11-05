#!/usr/bin/env python3
"""로그인 만료일 보고 시험 (2026-11-05 카드 743) — 데몬이 usage_limits.login_expires_at 를 함께 올리고,
날짜를 못 읽어도 사용량 보고는 그대로 나가는지 본다. 토큰·모델을 쓰지 않는다.
실행: python3 scripts/test_login_expires.py
"""
import json
import stat
import sys
import tempfile
from pathlib import Path

import queue  # noqa: F401,E402  (scripts/daemon/queue.py 가림 방지)
import logging.handlers  # noqa: F401,E402
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

import usage_api  # noqa: E402
import daemon.heartbeat as hb  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS" if ok else "FAIL"), name, detail if not ok else "")
    fails += 0 if ok else 1


def fake_cli(tmp, name, body):
    p = Path(tmp) / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


WANT = "2026-11-12T05:55:47+00:00"
check("문자열 Z", usage_api.login_expiry_iso("2026-11-12T05:55:47Z") == WANT)
check("문자열 밀리초", usage_api.login_expiry_iso("2026-11-12T05:55:47.000Z") == WANT)
check("저장 파일 밀리초", usage_api.login_expiry_iso(1794462947000) == WANT)
check("빈 값·이상한 값은 None", all(usage_api.login_expiry_iso(v) is None for v in (None, "", True, "soon", {})))

with tempfile.TemporaryDirectory() as tmp:
    status = json.dumps({"loggedIn": True, "loginExpiresAt": "2026-11-12T05:55:47.000Z"})
    good = fake_cli(tmp, "good", f"[ \"$1 $2\" = \"auth status\" ] && echo '{status}'")
    check("auth status 날짜", usage_api.cli_login_expires_at(good) == WANT)
    none = fake_cli(tmp, "none", "echo '{\"loggedIn\": true, \"loginExpiresAt\": null}'")
    check("날짜 칸 null → None", usage_api.cli_login_expires_at(none) is None)
    old = fake_cli(tmp, "old", "echo '{\"loggedIn\": true}'")
    check("옛 CLI(칸 없음) → None", usage_api.cli_login_expires_at(old) is None)
    text = fake_cli(tmp, "text", "echo 'Login method: Claude Max'")
    check("JSON 아님 → None", usage_api.cli_login_expires_at(text) is None)
    rc1 = fake_cli(tmp, "rc1", f"echo '{status}'; exit 1")
    check("종료 코드 1이어도 값은 읽음", usage_api.cli_login_expires_at(rc1) == WANT)
    check("명령 없음 → None", usage_api.cli_login_expires_at(str(Path(tmp) / "missing")) is None)
    seat = fake_cli(tmp, "seat", f"if [ \"$CLAUDE_CONFIG_DIR\" = /seat/x ]; then echo '{status}'; else echo '{{}}'; fi")
    check("자리 폴더를 env 로 넘김", usage_api.cli_login_expires_at(seat, {"CLAUDE_CONFIG_DIR": "/seat/x"}) == WANT)

    # fetch_usage_limits: API 경로·CLI 폴백 경로 모두 칸이 붙고, 날짜 실패가 보고를 막지 않는다
    saved = (usage_api.fetch_oauth_usage, usage_api.local_access_token, hb.CLAUDE_CMD,
             hb.seat_config_dir, hb.claude_account_email)
    usage_api.local_access_token = lambda d=None: "tok"
    hb.seat_config_dir = lambda *_a, **_k: None
    hb.claude_account_email = lambda *_a, **_k: "a@example.com"
    try:
        usage_api.fetch_oauth_usage = lambda t, timeout=15: {"session_pct": 1, "week_pct": 2}
        hb.CLAUDE_CMD = good
        got = hb.fetch_usage_limits()
        check("API 경로에 날짜", got.get("login_expires_at") == WANT and got.get("session_pct") == 1, got)

        hb.CLAUDE_CMD = text
        got = hb.fetch_usage_limits()
        check("API 경로, 날짜 실패 → 보고 유지(None)",
              "login_expires_at" in got and got["login_expires_at"] is None and got.get("week_pct") == 2, got)

        usage_api.fetch_oauth_usage = lambda t, timeout=15: None
        both = fake_cli(tmp, "both", f"if [ \"$1\" = auth ]; then echo '{status}'; "
                                     "else echo 'Current session: 7% used'; echo 'Current week (all models): 9% used'; fi")
        hb.CLAUDE_CMD = both
        got = hb.fetch_usage_limits()
        check("CLI 폴백 경로에 날짜", got.get("login_expires_at") == WANT and got.get("session_pct") == 7, got)

        usage_api.fetch_oauth_usage = lambda t, timeout=15: {"session_pct": 1, "week_pct": 2}
        hb.seat_config_dir = lambda *_a, **_k: "/seat/x"
        hb.CLAUDE_CMD = seat
        got = hb.fetch_usage_limits()
        check("지금 자리의 날짜를 읽음", got.get("login_expires_at") == WANT, got)
    finally:
        (usage_api.fetch_oauth_usage, usage_api.local_access_token, hb.CLAUDE_CMD,
         hb.seat_config_dir, hb.claude_account_email) = saved

print("실패", fails)
sys.exit(1 if fails else 0)
