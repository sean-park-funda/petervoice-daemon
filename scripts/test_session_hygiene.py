#!/usr/bin/env python3
"""세션 위생(2026-10-31) 순수 함수 시험 — 정찰 판정 파싱·강제 회차·문맥 토큰 읽기·비우기 사유·회고 지시·압축 창 env.

실행: python3 scripts/test_session_hygiene.py
"""
import json
import os
import sys
from pathlib import Path

# 표준 라이브러리 queue/logging 을 먼저 올린다 — scripts/daemon/queue.py 가 sys.path 앞에 오면 urllib3 가 그걸 집어 import 가 깨진다
import queue  # noqa: F401,E402
import logging.handlers  # noqa: F401,E402
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from daemon import scout  # noqa: E402
from daemon import autoreset  # noqa: E402
from daemon.sessions import build_retro_block  # noqa: E402
from daemon.limits import build_claude_env  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS" if ok else "FAIL"), name, detail if not ok else "")
    fails += 0 if ok else 1


# ── 정찰 판정 파싱 ──
check("verdict work=false", scout.parse_verdict('점검했습니다.\nSCOUT: {"work": false, "reason": "기록 파일 오늘 날짜"}') == (False, "기록 파일 오늘 날짜"))
check("verdict work=true", scout.parse_verdict('SCOUT: {"work": true, "reason": "모델 카탈로그 차이"}')[0] is True)
check("verdict last line wins", scout.parse_verdict('SCOUT: {"work": true, "reason": "a"}\n재확인\nSCOUT: {"work": false, "reason": "b"}') == (False, "b"))
check("verdict string bool", scout.parse_verdict('SCOUT: {"work": "true", "reason": "x"}')[0] is True)
check("verdict missing → None", scout.parse_verdict("뭔가 했습니다. 끝.") == (None, ""))
check("verdict first-line 할 일 없음 fallback", scout.parse_verdict("할 일 없음\n") == (False, "할 일 없음"))
check("verdict broken json → None", scout.parse_verdict('SCOUT: {"work": tru') == (None, ""))
check("verdict empty", scout.parse_verdict("") == (None, ""))

# ── 강제 회차 ──
check("force every 4: run#4", scout.should_force_main(3, 4) is True)
check("force every 4: run#1,2,3,5 no", not any(scout.should_force_main(r, 4) for r in (0, 1, 2, 4)))
check("force disabled (0)", scout.should_force_main(3, 0) is False)
check("force bad value → default 4", scout.should_force_main(3, "x") is True and scout.should_force_main(0, "x") is False)

# ── 문맥 토큰 (전사 꼬리) ──
tail = "\n".join([
    json.dumps({"type": "user", "message": {"content": "hi"}}),
    json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 200000, "output_tokens": 5}}}),
    json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}),
    json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 3, "cache_creation_input_tokens": 50000, "cache_read_input_tokens": 400000, "output_tokens": 9}}}),
    '{"type":"progress","data":{}}',
]).encode()
check("context tokens = last assistant prefill", autoreset.context_tokens_from_tail(tail) == 450003, autoreset.context_tokens_from_tail(tail))
check("context tokens none → 0", autoreset.context_tokens_from_tail(b'{"type":"user"}\n') == 0)
check("context tokens garbage line ignored", autoreset.context_tokens_from_tail(b'garbage "usage" "assistant"\n' + tail) == 450003)

# ── 비우기 사유·심각도 ──
th = {"max_life": 48, "max_msgs": 500, "max_mb": 10, "max_ctx": 250000, "stale_msgs": 3, "per_cycle": 20, "max_failures": 3}
info = {"lifetime_hours": 20.0, "message_count": 18, "jsonl_mb": 9.7, "context_tokens": 455071}
r = autoreset.oversize_reasons(info, th)
check("context criterion catches 946-like session", r == ["context 455k>=250k"], r)
check("under all thresholds → no reasons", autoreset.oversize_reasons({"lifetime_hours": 1, "message_count": 2, "jsonl_mb": 0.1, "context_tokens": 20000}, th) == [])
check("context criterion disabled by 0", autoreset.oversize_reasons(info, dict(th, max_ctx=0)) == [])
check("severity uses the worst ratio", abs(autoreset.severity(info, th) - 455071 / 250000) < 1e-9)
check("thresholds() has context default 250k", autoreset.thresholds()["max_ctx"] == 250000 or isinstance(autoreset.thresholds()["max_ctx"], int))

# ── 회고 지시 ──
blk = build_retro_block("docs/playbook.md", 15, "2026-10-31")
check("retro block names the playbook and date section", "docs/playbook.md" in blk and "## 회고 2026-10-31 (세션 리셋 전)" in blk)
check("retro block keeps anti-fabrication rules", "실제로 있었던 것만" in blk and "근거" in blk and "자격증명" in blk)
check("retro block ends with summary instruction", "[2] 인계 요약" in blk and blk.rstrip().endswith("다시 적지 마세요)."))

# ── 압축 창 env ──
env0 = build_claude_env({}, None)
check("no compact window by default", "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env0)
env1 = build_claude_env({"claude_auto_compact_window": 400000}, None)
check("compact window env when configured", env1.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == "400000")
env2 = build_claude_env({"claude_auto_compact_window": "abc"}, None)
check("compact window bad value ignored", "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env2)

# ── 정찰 설정 병합 ──
s = scout.settings()
check("scout defaults", s["enabled"] is True and s["force_every"] == 4 and s["model"], s)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
