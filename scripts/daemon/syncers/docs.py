"""Docs syncer: periodically sync local docs/ folders via API."""

import json
import hashlib
import threading
import urllib.parse
from pathlib import Path

import daemon.globals as g
from daemon.globals import DAEMON_DIR, DOCS_STATE_PATH, config, shutdown_event, logger
from daemon.api import api_request


class DocsSyncer(threading.Thread):
    SYNC_INTERVAL = 30
    MAX_FILE_SIZE = 512 * 1024

    def __init__(self):
        super().__init__(daemon=True, name="docs-syncer")
        self.state: dict[str, dict[str, str]] = {}
        self._load_state()

    def _load_state(self):
        try:
            if DOCS_STATE_PATH.exists():
                self.state = json.loads(DOCS_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            self.state = {}

    def _save_state(self):
        try:
            DOCS_STATE_PATH.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[docs] Failed to save state: {e}")

    def _get_all_projects(self) -> list[dict]:
        api_key = config.get("api_key", "")
        if not api_key:
            return []
        result = api_request(api_key, "GET", "/api/bot/projects", timeout=10)
        if not result or "projects" not in result:
            return []
        projects = []
        for p in result["projects"]:
            directory = p.get("directory") or str(DAEMON_DIR / "projects" / p["id"])
            projects.append({"id": p["id"], "directory": directory})
        return projects

    def _scan_docs_dir(self, docs_dir: Path) -> dict[str, str]:
        files = {}
        try:
            for f in docs_dir.rglob("*.md"):
                if not f.is_file():
                    continue
                if f.stat().st_size > self.MAX_FILE_SIZE:
                    continue
                rel = str(f.relative_to(docs_dir))
                try:
                    content = f.read_text(encoding="utf-8")
                    files[rel] = hashlib.md5(content.encode("utf-8")).hexdigest()
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"[docs] Scan error {docs_dir}: {e}")
        return files

    def _sync_project(self, project_id: str, project_dir: str):
        docs_dir = Path(project_dir) / "docs"
        current_files = self._scan_docs_dir(docs_dir) if docs_dir.is_dir() else {}
        prev_files = self.state.get(project_id, {})

        files_to_sync = []
        files_to_delete = []

        for rel_path, content_hash in current_files.items():
            if prev_files.get(rel_path) == content_hash:
                continue
            file_full = docs_dir / rel_path
            try:
                content = file_full.read_text(encoding="utf-8")
            except Exception:
                continue
            title = Path(rel_path).stem
            parent_path = str(Path(rel_path).parent)
            if parent_path == ".":
                parent_path = None
            files_to_sync.append({
                "path": rel_path,
                "title": title,
                "content": content,
                "parent_path": parent_path,
            })

        for rel_path in prev_files:
            if rel_path not in current_files:
                files_to_delete.append(rel_path)

        if not files_to_sync and not files_to_delete:
            self.state[project_id] = current_files
            return

        api_key = config.get("api_key", "")
        if not api_key:
            return

        result = api_request(api_key, "POST", "/api/bot/docs/sync", body={
            "projects": [{
                "id": project_id,
                "files": files_to_sync,
                "deleted": files_to_delete,
            }]
        }, timeout=30)

        if result:
            synced = result.get("synced", 0)
            deleted = result.get("deleted", 0)
            if synced:
                logger.info(f"[docs] {project_id}: synced {synced} files")
            if deleted:
                logger.info(f"[docs] {project_id}: deleted {deleted} files")

        self.state[project_id] = current_files

    def sync_once(self):
        projects = self._get_all_projects()
        for proj in projects:
            try:
                # 프로젝트 디렉토리 + docs 폴더 미리 생성 (웹에서 신규 프로젝트 생성 시 즉시 문서탭 사용 가능)
                docs_dir = Path(proj["directory"]) / "docs"
                docs_dir.mkdir(parents=True, exist_ok=True)
                self._sync_project(proj["id"], proj["directory"])
            except Exception as e:
                logger.error(f"[docs] Error syncing {proj['id']}: {e}")
        self._save_state()

    def run(self):
        logger.info("[docs] Syncer started")
        shutdown_event.wait(5)
        if shutdown_event.is_set():
            return
        try:
            self.sync_once()
        except Exception as e:
            logger.error(f"[docs] Initial sync error: {e}")

        while not shutdown_event.is_set():
            shutdown_event.wait(self.SYNC_INTERVAL)
            if shutdown_event.is_set():
                break
            try:
                self.sync_once()
            except Exception as e:
                logger.error(f"[docs] Sync error: {e}")

        logger.info("[docs] Syncer stopped")
