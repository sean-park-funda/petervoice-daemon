"""Skills syncer: periodically sync skills from API to ~/.claude/skills/."""

import json
import threading

from daemon.globals import SKILLS_DIR, config, shutdown_event, logger
from daemon.api import api_request


class SkillsSyncer(threading.Thread):
    SYNC_INTERVAL = 300

    def __init__(self):
        super().__init__(daemon=True, name="skills-syncer")

    def sync_once(self):
        api_key = config.get("api_key", "")
        if not api_key:
            return

        result = api_request(api_key, "GET", "/api/bot/skills", timeout=10)
        if not result or "skills" not in result:
            logger.warning("[skills] Failed to fetch skills")
            return
        skills = result["skills"]

        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        synced, removed = [], []

        for skill in skills:
            skill_id = skill.get("id", "").strip()
            content = skill.get("content", "")
            if not skill_id:
                continue

            skill_dir = SKILLS_DIR / skill_id
            skill_file = skill_dir / "SKILL.md"

            # 업데이트 전용: 로컬에 이미 설치된 스킬만 DB 내용으로 갱신
            # 로컬에 없는 스킬은 건드리지 않음 (마켓 UI에서 설치)
            if not skill_file.exists():
                continue

            if skill_file.read_text(encoding="utf-8") != content:
                skill_file.write_text(content, encoding="utf-8")
                synced.append(skill_id)

        if synced:
            logger.info(f"[skills] Synced: {', '.join(synced)}")
        if removed:
            logger.info(f"[skills] Removed: {', '.join(removed)}")

    def run(self):
        logger.info("[skills] Syncer started")
        try:
            self.sync_once()
        except Exception as e:
            logger.error(f"[skills] Initial sync error: {e}")

        while not shutdown_event.is_set():
            shutdown_event.wait(self.SYNC_INTERVAL)
            if shutdown_event.is_set():
                break
            try:
                self.sync_once()
            except Exception as e:
                logger.error(f"[skills] Sync error: {e}")

        logger.info("[skills] Syncer stopped")
