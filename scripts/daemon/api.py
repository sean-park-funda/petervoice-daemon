"""PeterVoice API helpers and message utilities.

All DB access goes through PeterVoice web API — no direct Supabase.
"""

import json
import urllib.request
import urllib.error

from daemon.globals import config, logger


def api_request(api_key: str, method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict | None:
    url = f"{config['api_url']}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("X-Api-Key", api_key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        logger.error(f"HTTP {e.code} {method} {path}: {body_text}")
        return None
    except Exception as e:
        logger.error(f"Request failed {method} {path}: {e}")
        return None


def mark_message_processed(msg_id: int):
    api_key = config.get("api_key", "")
    if not api_key:
        return
    api_request(api_key, "PATCH", "/api/bot/message", body={"id": msg_id, "updates": {"processed": True}}, timeout=5)


def inject_system_message(project: str, text: str, prefix: str = "[heartbeat]") -> tuple[int | None, str]:
    """Insert a synthetic user message for the Worker to process.
    Returns (message_id, timestamp) or (None, "")."""
    api_key = config.get("api_key", "")
    if not api_key:
        logger.error("[inject] Cannot inject message: no api_key")
        return None, ""

    result = api_request(api_key, "POST", "/api/bot/message", body={
        "project": project,
        "text": f"{prefix} {text}",
        "type": "user",
        "processed": False,
    }, timeout=10)

    if result and result.get("id"):
        msg_id = result["id"]
        ts = result.get("created_at", "")
        logger.info(f"[inject] {prefix} msg #{msg_id} → {project}: {text[:60]}")
        return msg_id, ts
    logger.error(f"[inject] Failed for {project}")
    return None, ""
