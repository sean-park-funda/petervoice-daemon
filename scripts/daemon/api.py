"""PeterVoice API helpers and message utilities.

All DB access goes through PeterVoice web API — no direct Supabase.
"""

import json
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from daemon.globals import config, logger

# Module-level session with connection pooling and keep-alive.
# Reusing TCP connections prevents TIME_WAIT port exhaustion on long-running daemons.
_session_lock = threading.Lock()
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=4,
                    pool_maxsize=10,
                    max_retries=Retry(total=0),
                )
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _session = s
    return _session


def api_request(api_key: str, method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict | None:
    url = f"{config['api_url']}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    }
    try:
        resp = _get_session().request(
            method,
            url,
            json=body,
            headers=headers,
            timeout=timeout,
        )
        if not resp.ok:
            body_text = resp.text[:200] if resp.text else ""
            logger.error(f"HTTP {resp.status_code} {method} {path}: {body_text}")
            return None
        return resp.json()
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
