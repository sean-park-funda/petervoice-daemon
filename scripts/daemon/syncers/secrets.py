"""Secrets syncer: periodically sync secrets from Supabase to local .env and os.environ."""

import os
import json
import threading

from daemon.globals import config, SECRETS_ENV_PATH, shutdown_event, logger
from daemon.api import api_request

_KEYRING_SERVICE = "agent-skills"
_GOOGLE_SKILLS = ["google-docs", "google-calendar", "google-sheets", "google-drive", "gmail"]
_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


class SecretsSyncer(threading.Thread):
    SYNC_INTERVAL = 60

    def __init__(self):
        super().__init__(daemon=True, name="secrets-syncer")
        self.api_key = config["api_key"]
        self._last_google_refresh_token: str | None = None

    def _sync_google_keyring(self):
        """Sync Google OAuth token from env vars to keyring for all Google skills."""
        refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")

        if not (refresh_token and client_id and client_secret):
            return

        # Skip if token hasn't changed since last sync
        if refresh_token == self._last_google_refresh_token:
            return

        try:
            import keyring
        except ImportError:
            logger.debug("[secrets] keyring not installed, skipping Google keyring sync")
            return

        token_json = json.dumps({
            "token": None,
            "refresh_token": refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": _GOOGLE_SCOPES,
        })

        updated = []
        for skill in _GOOGLE_SKILLS:
            key = f"{skill}-token-json"
            try:
                existing = keyring.get_password(_KEYRING_SERVICE, key)
                if existing:
                    existing_data = json.loads(existing)
                    if existing_data.get("refresh_token") == refresh_token:
                        continue
                keyring.set_password(_KEYRING_SERVICE, key, token_json)
                updated.append(skill)
            except Exception:
                pass

        self._last_google_refresh_token = refresh_token
        if updated:
            logger.info(f"[secrets] Google keyring synced for: {', '.join(updated)}")

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

        # Sync Google tokens to keyring for skill compatibility
        self._sync_google_keyring()

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
