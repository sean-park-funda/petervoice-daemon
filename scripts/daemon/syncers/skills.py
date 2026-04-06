"""Skills syncer: periodically sync skills from API to ~/.claude/skills/."""

import json
import shutil
import threading
from pathlib import Path

from daemon.globals import SKILLS_DIR, config, shutdown_event, logger
from daemon.api import api_request

CLEANUP_FLAG = SKILLS_DIR / ".cleanup-v1-done"
BUNDLE_INSTALLED_FLAG = SKILLS_DIR / ".bundle-v1-done"


def _find_bundle_dir() -> Path | None:
    """레포의 skills/ 폴더를 찾는다."""
    d = Path(__file__).resolve().parent
    for _ in range(5):
        candidate = d / "skills"
        if (d / ".git").exists() and candidate.is_dir():
            return candidate
        d = d.parent
    return None


class SkillsSyncer(threading.Thread):
    SYNC_INTERVAL = 300

    def __init__(self):
        super().__init__(daemon=True, name="skills-syncer")

    def _cleanup_auto_installed(self):
        """일회성: 예전 auto-install로 깔린 마켓 스킬을 전부 제거.
        유저가 마켓에서 직접 다시 설치하도록 함."""
        if CLEANUP_FLAG.exists():
            return

        api_key = config.get("api_key", "")
        if not api_key:
            return

        # DB에서 마켓 스킬 ID 목록 조회
        result = api_request(api_key, "GET", "/api/bot/skills", timeout=10)
        if not result or "skills" not in result:
            return
        market_ids = {s["id"].strip() for s in result["skills"] if s.get("id")}

        if not SKILLS_DIR.exists():
            CLEANUP_FLAG.touch()
            return

        removed = []
        for d in SKILLS_DIR.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            # 마켓에 있는 스킬만 제거 (유저가 직접 만든 로컬 스킬은 보존)
            if d.name in market_ids:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d.name)

        CLEANUP_FLAG.touch()
        if removed:
            logger.info(f"[skills] Cleanup: removed {len(removed)} auto-installed skills")

    def _install_bundled_skills(self):
        """레포의 skills/ 폴더에 있는 번들 스킬을 로컬에 설치 (없는 것만)."""
        bundle_dir = _find_bundle_dir()
        if not bundle_dir:
            return

        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        installed = []

        for skill_dir in bundle_dir.iterdir():
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            local_dir = SKILLS_DIR / skill_dir.name
            # 이미 로컬에 있으면 건드리지 않음 (유저 삭제 존중: flag 파일로 체크)
            if local_dir.exists():
                continue
            # 복사
            shutil.copytree(skill_dir, local_dir)
            installed.append(skill_dir.name)

        if installed:
            logger.info(f"[skills] Bundled skills installed: {', '.join(installed)}")

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
        synced = []

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

    def run(self):
        logger.info("[skills] Syncer started")

        # 일회성 정리: 예전 auto-install된 마켓 스킬 제거
        try:
            self._cleanup_auto_installed()
        except Exception as e:
            logger.error(f"[skills] Cleanup error: {e}")

        # 번들 스킬 설치 (매 시작 시 — 새 번들 추가분만 복사)
        try:
            self._install_bundled_skills()
        except Exception as e:
            logger.error(f"[skills] Bundle install error: {e}")

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
