#!/usr/bin/env python3
"""
Gateway Log Pusher — tails OpenClaw logs and pushes to Peter Voice API.
Merges gateway.log (human-readable) + /tmp/openclaw/ detail log (JSON) by timestamp.

Usage:
  BOT_API_KEY=pv_xxx python3 log_pusher.py

Environment:
  BOT_API_KEY       — Required. Peter Voice bot API key.
  PETER_VOICE_URL   — API base URL (default: https://peter-voice.vercel.app)
  PUSH_INTERVAL     — Seconds between pushes (default: 5)
  MAX_LINES         — Max lines to keep (default: 200)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

API_KEY = os.environ.get("BOT_API_KEY", "")
API_URL = os.environ.get("PETER_VOICE_URL", "https://peter-voice.vercel.app")
GATEWAY_LOG = Path(os.path.expanduser("~/.openclaw/logs/gateway.log"))
DETAIL_LOG_DIR = Path("/tmp/openclaw")
PUSH_INTERVAL = int(os.environ.get("PUSH_INTERVAL", "5"))
MAX_LINES = int(os.environ.get("MAX_LINES", "200"))

# gateway.log pattern: 2026-02-21T12:13:00.636Z [source] message...
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+"
    r"\[(?P<source>[^\]]+)\]\s+"
    r"(?P<message>.*)$"
)


def parse_gateway_line(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {}
    m = LINE_RE.match(raw)
    if m:
        msg = m.group("message")
        level = "info"
        lower = msg.lower()
        if "error" in lower or "fail" in lower or "crash" in lower:
            level = "error"
        elif "warn" in lower:
            level = "warn"
        return {
            "ts": m.group("ts"),
            "level": level,
            "source": m.group("source"),
            "message": msg,
        }
    # Skip box-drawing / decorative lines from doctor output
    if any(c in raw for c in "─│├╮╯◇╰┤"):
        return {}
    return {"ts": "", "level": "info", "source": "raw", "message": raw}


def parse_detail_line(raw: str) -> dict:
    """Parse a JSON line from /tmp/openclaw/openclaw-YYYY-MM-DD.log"""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    meta = d.get("_meta", {})
    level = meta.get("logLevelName", "INFO").lower()
    ts = d.get("time", "")

    # "1" is subsystem message, "0" is fallback
    msg = d.get("1") or d.get("0", "")
    if not msg or not isinstance(msg, str):
        return {}

    # Extract subsystem name from "0" field like '{"subsystem":"diagnostic"}'
    sub_raw = d.get("0", "")
    source = "detail"
    if isinstance(sub_raw, str) and "subsystem" in sub_raw:
        try:
            source = json.loads(sub_raw).get("subsystem", "detail")
        except (json.JSONDecodeError, AttributeError):
            pass

    # Skip noise: empty messages, redundant startup lines
    if len(msg) < 3:
        return {}

    return {"ts": ts, "level": level, "source": source, "message": msg}


def get_detail_log_path() -> Path:
    """Get today's detail log path: /tmp/openclaw/openclaw-YYYY-MM-DD.log"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return DETAIL_LOG_DIR / f"openclaw-{today}.log"


def read_tail(path: Path, max_lines: int) -> list[str]:
    """Read last max_lines from file."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk_size = min(size, 512 * 1024)
            f.seek(size - chunk_size)
            data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            return lines[-max_lines:]
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[log_pusher] read error ({path.name}): {e}", file=sys.stderr)
        return []


def merge_and_sort(gw_lines: list[dict], detail_lines: list[dict], max_lines: int) -> list[dict]:
    """Merge two log sources, deduplicate similar entries, sort by timestamp, trim to max_lines."""
    # Use a set to deduplicate: gateway.log and detail log often log the same event
    seen = set()
    merged = []

    for line in gw_lines + detail_lines:
        if not line:
            continue
        # Dedup key: timestamp + first 60 chars of message
        key = (line.get("ts", ""), line.get("message", "")[:60])
        if key in seen:
            continue
        seen.add(key)
        merged.append(line)

    # Sort by timestamp
    merged.sort(key=lambda x: x.get("ts", ""))
    return merged[-max_lines:]


def push_logs(parsed_lines: list[dict]) -> bool:
    """Push parsed log lines to Peter Voice API."""
    url = f"{API_URL}/api/bot/push-logs"
    payload = json.dumps({
        "lines": parsed_lines,
        "log_file": "gateway+detail",
    }).encode("utf-8")

    req = Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except URLError as e:
        print(f"[log_pusher] push failed: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[log_pusher] push error: {e}", file=sys.stderr)
        return False


def main():
    if not API_KEY:
        print("ERROR: BOT_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)

    print(f"[log_pusher] watching {GATEWAY_LOG} + {DETAIL_LOG_DIR}/openclaw-*.log")
    print(f"[log_pusher] pushing to {API_URL}/api/bot/push-logs every {PUSH_INTERVAL}s")

    last_hash = ""
    consecutive_errors = 0

    while True:
        try:
            # Read both log files
            gw_raw = read_tail(GATEWAY_LOG, MAX_LINES)
            detail_path = get_detail_log_path()
            detail_raw = read_tail(detail_path, MAX_LINES)

            current_hash = (
                str(len(gw_raw)) + (gw_raw[-1] if gw_raw else "") +
                str(len(detail_raw)) + (detail_raw[-1] if detail_raw else "")
            )

            # Only push if content changed
            if current_hash != last_hash and (gw_raw or detail_raw):
                gw_parsed = [p for line in gw_raw if (p := parse_gateway_line(line))]
                detail_parsed = [p for line in detail_raw if (p := parse_detail_line(line))]
                merged = merge_and_sort(gw_parsed, detail_parsed, MAX_LINES)

                if push_logs(merged):
                    last_hash = current_hash
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
            else:
                consecutive_errors = 0

            sleep_time = min(PUSH_INTERVAL * (2 ** consecutive_errors), 60)
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n[log_pusher] stopped")
            break
        except Exception as e:
            print(f"[log_pusher] unexpected error: {e}", file=sys.stderr)
            time.sleep(PUSH_INTERVAL)


if __name__ == "__main__":
    main()
