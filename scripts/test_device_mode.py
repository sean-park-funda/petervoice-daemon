#!/usr/bin/env python3
"""기기 모드(한 계정 · 여러 기기) 시험 (2026-11-26).

실행: python3 scripts/test_device_mode.py   (네트워크 없음, 1초 안팎)
기기 열쇠로 붙은 데몬은 ① /api/bot/me 의 device 칸으로 기기임을 알고 ② 계정의 프로젝트 목록을 건드리지 않고
③ 허용된 비밀값이 0개가 되면 받아 둔 것을 지운다. 본기기(유저 열쇠)는 셋 다 지금까지와 같아야 한다.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="pv-device-test-")
sys.argv = [sys.argv[0], "--config-dir", _TMP]   # 실제 ~/.claude-daemon 을 건드리지 않는다

import daemon.globals as g  # noqa: E402
import daemon.supabase as sb  # noqa: E402
import daemon.prompts as prompts  # noqa: E402
import daemon.syncers.secrets as sec  # noqa: E402

assert str(g.DAEMON_DIR) == _TMP, g.DAEMON_DIR
SECRETS = Path(_TMP) / "secrets.env"
sec.SECRETS_ENV_PATH = SECRETS            # 시험용 경로로 고정 (실제 비밀값 파일 보호)
g.config.clear()
g.config.update({"api_key": "test-key", "api_url": "http://fixture.invalid", "username": "fixture"})

calls: list[tuple[str, str]] = []
responses: dict[tuple[str, str], object] = {}


def fake_api(api_key, method, path, body=None, timeout=None, **kw):
    calls.append((method, path))
    return responses.get((method, path.split("?")[0]))


sb.api_request = fake_api
sec.api_request = fake_api
checks = 0


def check(cond, label):
    global checks
    checks += 1
    if not cond:
        raise SystemExit(f"FAIL: {label}")


def reset(device=None):
    calls.clear()
    responses.clear()
    g.DEVICE_NAME = device
    g._cached_user_id = None
    g.config.pop("device_name", None)


# ── 1. 기기 판별 ────────────────────────────────────────────────
reset()
responses[("GET", "/api/bot/me")] = {"userId": 7, "username": "fixture"}
check(sb.resolve_user_id() == 7 and g.DEVICE_NAME is None, "user key → primary (no device)")

reset()
responses[("GET", "/api/bot/me")] = {"userId": 7, "username": "fixture", "device": "gamemac"}
check(sb.resolve_user_id() == 7 and g.DEVICE_NAME == "gamemac", "device key → device name from server")

reset()
g.config["device_name"] = "gamemac"      # 서버에 못 닿은 채 시작
check(sb.resolve_user_id() is None and g.DEVICE_NAME == "gamemac", "server unreachable → device name from config")

# ── 2. 기기는 계정의 프로젝트 목록을 건드리지 않는다 ─────────────
reset(device="gamemac")
sb.sync_null_directories()
check(calls == [], "device: sync_null_directories makes no API call")
prompts._roster_cache.clear()
check(prompts.build_agent_roster() == "", "device: roster is empty without API calls")

reset()
responses[("GET", "/api/projects")] = {"projects": []}
sb.sync_null_directories()
check(calls == [("GET", "/api/projects")], "primary: sync_null_directories unchanged")

# ── 3. 비밀값: 기기는 허용이 0개가 되면 지운다 ──────────────────
def sync(secrets):
    responses[("GET", "/api/secrets")] = {"secrets": secrets} if secrets is not None else None
    s.sync_once()


reset(device="gamemac")
s = sec.SecretsSyncer()
s._sync_google_keyring = lambda *a, **k: None
sync([{"key": "DEVTEST_A", "value": "v1"}])
check(os.environ.get("DEVTEST_A") == "v1" and "DEVTEST_A" in SECRETS.read_text(), "device: allowed secret synced")
sync([])
check("DEVTEST_A" not in os.environ and SECRETS.read_text() == "", "device: empty allow-list clears env + file")
check(oct(SECRETS.stat().st_mode & 0o777) == "0o600", "device: cleared file stays 0600")
sync([])   # 두 번째 빈 응답 — 할 일 없음
sync(None)  # 조회 실패 — 아무것도 지우지 않는다
sync([{"key": "DEVTEST_A", "value": "v2"}])
sync(None)
check(os.environ.get("DEVTEST_A") == "v2", "device: fetch failure keeps what was synced")
os.environ.pop("DEVTEST_A", None)

# 재시작 직후(_synced_keys 비어 있음)에도 남아 있던 파일은 지운다
SECRETS.write_text('OLD="x"\n')
s2 = sec.SecretsSyncer()
s2._sync_google_keyring = lambda *a, **k: None
s = s2
sync([])
check(SECRETS.read_text() == "", "device: stale file from before restart is cleared")

# 본기기: 빈 응답은 지금처럼 아무것도 하지 않는다
reset()
s = sec.SecretsSyncer()
s._sync_google_keyring = lambda *a, **k: None
sync([{"key": "DEVTEST_B", "value": "v1"}])
sync([])
check(os.environ.get("DEVTEST_B") == "v1" and "DEVTEST_B" in SECRETS.read_text(), "primary: empty response keeps secrets (unchanged)")
os.environ.pop("DEVTEST_B", None)

print(f"OK — {checks} checks")
