#!/usr/bin/env python3
"""계정 한도 문구 감지 시험 — CLI 2.1.312 문구 변경(session limit → 5-hour limit) 대응 (2026-10-29).

실행: python3 scripts/test_limits_detect.py
limits.py 의 detect_usage_limit 을 직접 돌리고, cloud_daemon.py 는 같은 정규식 문자열을 쓰는지 본문으로 대조한다
(cloud_daemon 은 import 에 환경이 필요해 텍스트 대조만).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from daemon.limits import detect_usage_limit, _SESSION_LIMIT_RE, _MODEL_LABEL_RE  # noqa: E402

CASES = [
    # (입력, 기대 kind, 기대 label, 기대 resets)
    ("You've hit your session limit · resets 4:10am (Asia/Seoul)", "session", "session", "4:10am (Asia/Seoul)"),
    ("You've hit your 5-hour limit · resets 4:10am (Asia/Seoul)", "session", "5-hour", "4:10am (Asia/Seoul)"),
    ("You've hit your weekly limit · resets Oct 31, 3am (UTC)", "session", "weekly", "Oct 31, 3am (UTC)"),
    ("You've hit your monthly limit — raise it below", "session", "monthly", ""),
    ("You've hit your monthly spend limit · your limit is $50", "session", "monthly spend", ""),
    ("You've hit your usage credit limit · resets 3am", "session", "usage credit", "3am"),
    ("You've hit your limit (daily; resets 2026-08-08 00:00 UTC)", "session", "", "2026-08-08 00:00 UTC)"),
    ("You've hit your Opus limit · resets Oct 31, 3am (UTC)", "model", "Opus", "Oct 31, 3am (UTC)"),
    ("You've hit your Sonnet limit · resets 3am", "model", "Sonnet", "3am"),
    ("You've hit your Fable limit · resets 3am", "model", "Fable", "3am"),
    ("You've reached your Fable 5 limit. Switch to another model", "model", "Fable 5", ""),
]
NEGATIVE = [
    "You've hit your channel's monthly spend limit.",
    "You've hit your team's shared budget",
    "The rate limit will reset shortly",
    "I hit my limit of patience with this bug",
    "",
]

fails = 0
for text, kind, label, resets in CASES:
    got = detect_usage_limit(text)
    ok = got is not None and got["kind"] == kind and got["label"] == label and got["resets"] == resets
    print(("PASS" if ok else "FAIL"), repr(text[:48]), "→", got)
    fails += 0 if ok else 1
for text in NEGATIVE:
    got = detect_usage_limit(text)
    ok = got is None
    print(("PASS" if ok else "FAIL"), "negative", repr(text[:48]), "→", got)
    fails += 0 if ok else 1

# cloud_daemon.py 가 같은 규칙을 쓰는지 — 두 파일이 따로 놀면 클라우드만 옛 문구에 머문다
cloud_src = (ROOT / "cloud_daemon.py").read_text(encoding="utf-8")
def src_pattern(name: str) -> str | None:
    m = re.search(name + r' = re\.compile\(r"((?:[^"\\]|\\.)*)", re\.I\)', cloud_src)
    return m.group(1) if m else None
for name, compiled in [("_SESSION_LIMIT_RE", _SESSION_LIMIT_RE), ("_MODEL_LABEL_RE", _MODEL_LABEL_RE)]:
    ok = src_pattern(name) == compiled.pattern
    print(("PASS" if ok else "FAIL"), "cloud_daemon 동일 규칙:", name, "" if ok else f"(cloud={src_pattern(name)!r} vs limits={compiled.pattern!r})")
    fails += 0 if ok else 1
ok = 'kind = "model" if _MODEL_LABEL_RE.match(label) else "session"' in cloud_src
print(("PASS" if ok else "FAIL"), "cloud_daemon 동일 규칙: kind 분류")
fails += 0 if ok else 1

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
