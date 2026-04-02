"""Secrets syncer: periodically sync secrets from Supabase to local .env and os.environ."""

import os
import threading

from daemon.globals import config, SECRETS_ENV_PATH, shutdown_event, logger
from daemon.api import api_request


class SecretsSyncer(threading.Thread):
    SYNC_INTERVAL = 60

    def __init__(self):
        super().__init__(daemon=True, name="secrets-syncer")
        self.api_key = config["api_key"]

    def sync_once(self):
        result = api_request(self.api_key, "GET", "/api/secrets?raw=true", timeout=10)
        if result is None:
            logger.warning("[secrets] Failed to fetch secrets from API")
            return

        secrets = result.get("secrets", [])
        if not secrets:
            return

        lines = []
        keys = []
        for s in secrets:
            key = s.get("key", "").strip()
            value = s.get("value", "").strip()
            if key and value:
                safe_value = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                lines.append(f'{key}="{safe_value}"')
                keys.append(key)
                os.environ[key] = value

        SECRETS_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        SECRETS_ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(SECRETS_ENV_PATH, 0o600)

        logger.info(f"[secrets] Synced {len(keys)} secrets: {', '.join(keys)}")

    def run(self):
        logger.info("[secrets] Syncer started")
        try:
            self.sync_once()
        except Exception as e:
            logger.error(f"[secrets] Initial sync error: {e}")

        while not shutdown_event.is_set():
            shutdown_event.wait(self.SYNC_INTERVAL)
            if shutdown_event.is_set():
                break
            try:
                self.sync_once()
            except Exception as e:
                logger.error(f"[secrets] Sync error: {e}")

        logger.info("[secrets] Syncer stopped")
