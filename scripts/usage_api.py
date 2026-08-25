"""Claude 구독 사용량(OAuth usage API) 공용 모듈 — 클라우드 데몬·맥 데몬이 함께 쓴다.

배경(2026-08-25): 두 데몬 모두 15분마다 `claude -p /usage` 를 띄워 배너 값을 얻었다.
그 프로브는 클로드 세션을 하나 소비하고(고객 맥 1대당 하루 96세션), 느린 호스트에선
60~80초씩 걸려 타임아웃에 잘렸다(뉴넥스). OAuth usage API 는 같은 값을 1초 미만에 준다.

실측 응답 구조 (api.anthropic.com/api/oauth/usage):
  five_hour / seven_day: {"utilization": 7.0 (= 7%, 이미 0~100 스케일), "resets_at": ISO}
  limits: [{"kind": "session" | "weekly_all" | "weekly_scoped", "percent": int, "resets_at": ISO}]
  weekly_scoped 가 CLI 의 "Current week (Fable)" 에 해당한다.
토큰이 만료·폐기됐거나 응답을 못 읽으면 None 을 돌려주고, 호출부는 CLI 프로브로 폴백한다
(CLI 실행이 토큰을 갱신하므로 다음 주기부터 API 가 다시 산다).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"  # macOS: 클로드 CLI 가 토큰을 키체인에 둔다


def fmt_reset(iso: str | None) -> str:
    """resets_at ISO → CLI 표기와 같은 'Aug 25, 11:40pm' 꼴 (Asia/Seoul)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        dt = dt.astimezone(timezone(timedelta(hours=9)))
        hour = dt.strftime("%I").lstrip("0") or "12"
        mins = f":{dt.minute:02d}" if dt.minute else ""
        return f"{dt.strftime('%b')} {dt.day}, {hour}{mins}{dt.strftime('%p').lower()}"
    except Exception:
        return str(iso)


def parse_oauth_usage(data: dict) -> dict | None:
    """API 응답 → 배너 dict (session_pct/week_pct/week_fable_pct/session_reset/week_reset).
    못 읽으면 None."""
    def _pct(v):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None

    fh = (data or {}).get("five_hour") or {}
    sd = (data or {}).get("seven_day") or {}
    session_pct, session_reset = _pct(fh.get("utilization")), fh.get("resets_at")
    week_pct, week_reset = _pct(sd.get("utilization")), sd.get("resets_at")
    fable_pct = None
    for lim in (data or {}).get("limits") or []:
        kind = (lim or {}).get("kind")
        if kind == "weekly_scoped" and fable_pct is None:
            fable_pct = _pct(lim.get("percent"))
        elif kind == "session" and session_pct is None:
            session_pct, session_reset = _pct(lim.get("percent")), lim.get("resets_at")
        elif kind == "weekly_all" and week_pct is None:
            week_pct, week_reset = _pct(lim.get("percent")), lim.get("resets_at")
    if session_pct is None and week_pct is None:
        logger.warning(f"[usage] oauth usage: 응답 형태를 못 읽음 keys={sorted(data or {})[:8]}")
        return None
    return {
        "session_pct": session_pct,
        "week_pct": week_pct,
        "week_fable_pct": fable_pct,
        "session_reset": fmt_reset(session_reset),
        "week_reset": fmt_reset(week_reset),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "probe_src": "api",
    }


def token_from_credentials_json(text: str | None) -> str | None:
    try:
        return ((json.loads(text or "") or {}).get("claudeAiOauth") or {}).get("accessToken") or None
    except Exception:
        return None


def local_access_token(config_dir: str | None = None) -> str | None:
    """이 머신의 기본(또는 config_dir) 계정 액세스 토큰.
    macOS 는 키체인이 정본(파일은 stale 로 남는다), 리눅스는 CLAUDE_CONFIG_DIR/.credentials.json."""
    if sys.platform == "darwin" and not config_dir:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                               capture_output=True, text=True, timeout=10)
            tok = token_from_credentials_json(r.stdout.strip()) if r.returncode == 0 else None
            if tok:
                return tok
        except Exception:
            pass
    base = Path(os.path.expanduser(config_dir)) if config_dir else Path(os.path.expanduser("~/.claude"))
    try:
        return token_from_credentials_json((base / ".credentials.json").read_text())
    except Exception:
        return None


def fetch_oauth_usage(token: str, timeout: int = 15) -> dict | None:
    """토큰으로 usage API 호출 → 배너 dict. 실패는 None (토큰 값은 로그에 남기지 않는다)."""
    if not token:
        return None
    try:
        req = urllib.request.Request(OAUTH_USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "pv-daemon",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        logger.info(f"[usage] oauth usage miss: {type(e).__name__} → CLI fallback")
        return None
    return parse_oauth_usage(data)
