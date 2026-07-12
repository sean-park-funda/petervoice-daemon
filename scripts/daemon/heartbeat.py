"""Heartbeat thread: periodically inject due task messages."""

import json
import os
import re
import subprocess
import time
import threading
from datetime import datetime, timedelta, timezone

import daemon.globals as g
from daemon.globals import config, active_projects, active_projects_lock, shutdown_event, logger, CLAUDE_CMD
from daemon.api import api_request, inject_system_message
from daemon.supabase import get_project_dir


# ─── Claude 사용 한도(/usage) 수집 — 주기(heartbeat) + 온디맨드(main loop) 공용 ───
def fetch_usage_limits() -> dict | None:
    """claude -p '/usage' 실행 후 세션/주간 리밋 % + 리셋 시각 파싱."""
    try:
        r = subprocess.run(
            [CLAUDE_CMD, "-p", "/usage"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        logger.warning(f"[usage] claude /usage failed: {e}")
        return None
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if not out.strip():
        return None

    def _pct(pattern: str):
        m = re.search(pattern, out)
        if not m:
            return None, None
        pct = int(m.group(1))
        reset = (m.group(2).strip() if m.lastindex and m.lastindex >= 2 else "")
        reset = re.sub(r"\s*\(.*\)$", "", reset).strip()  # "(Asia/Seoul)" 제거
        return pct, reset

    session_pct, session_reset = _pct(r"Current session:\s*(\d+)% used(?:\s*·\s*resets\s*(.+))?")
    week_pct, week_reset = _pct(r"Current week \(all models\):\s*(\d+)% used(?:\s*·\s*resets\s*(.+))?")
    fable_pct, _ = _pct(r"Current week \(Fable\):\s*(\d+)% used")
    if session_pct is None and week_pct is None:
        return None
    return {
        "session_pct": session_pct,
        "week_pct": week_pct,
        "week_fable_pct": fable_pct,
        "session_reset": session_reset,
        "week_reset": week_reset,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def collect_and_store_usage() -> bool:
    """즉시 /usage 수집 후 user_status.usage_limits 갱신. 성공 여부 반환."""
    limits = fetch_usage_limits()
    if not limits:
        return False
    api_key = config.get("api_key", "")
    if not api_key:
        return False
    api_request(api_key, "PATCH", "/api/bot/status", body={"usage_limits": limits}, timeout=10)
    logger.info(f"[usage] session={limits.get('session_pct')}% week={limits.get('week_pct')}%")
    return True


class HeartbeatThread(threading.Thread):
    POLL_INTERVAL = 60
    USAGE_INTERVAL = 15 * 60  # Claude 사용 한도 수집 주기(초)
    HEARTBEAT_MSG = "HEARTBEAT.md를 확인하고 할 일이 있으면 처리해줘. 없으면 '할 일 없음'이라고 답해."

    def __init__(self):
        super().__init__(daemon=True, name="heartbeat")
        self._last_usage_fetch = 0.0

    def run(self):
        logger.info("[heartbeat] Thread started")
        time.sleep(60)
        while not shutdown_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"[heartbeat] tick error: {e}")
            try:
                self._maybe_update_usage()
            except Exception as e:
                logger.warning(f"[usage] update error: {e}")
            shutdown_event.wait(self.POLL_INTERVAL)
        logger.info("[heartbeat] Thread stopped")

    # ─── Claude 사용 한도(/usage) 주기 수집 ────────────────────────
    def _maybe_update_usage(self):
        now = time.time()
        if now - self._last_usage_fetch < self.USAGE_INTERVAL:
            return
        self._last_usage_fetch = now
        collect_and_store_usage()

    def _tick(self):
        tasks = self._fetch_due_tasks()
        for task in tasks:
            if shutdown_event.is_set():
                break
            try:
                self._process_task(task)
            except Exception as e:
                logger.error(f"[heartbeat] {task.get('project', '?')} error: {e}")

    def _fetch_due_tasks(self) -> list[dict]:
        api_key = config.get("api_key", "")
        if not api_key:
            return []
        result = api_request(api_key, "GET", "/api/bot/tasks", timeout=10)
        if result and "tasks" in result:
            return result["tasks"]
        return []

    def _check_heartbeat_md(self, project: str) -> str | None:
        """Check if docs/HEARTBEAT.md exists. Returns warning message or None."""
        try:
            project_dir = get_project_dir(project)
            if not project_dir:
                return "프로젝트 디렉토리를 찾을 수 없음"
            heartbeat_path = os.path.join(project_dir, "docs", "HEARTBEAT.md")
            if not os.path.exists(heartbeat_path):
                return "HEARTBEAT.md 파일 없음 — 태스크가 실행되어도 할 일이 정의되지 않음"
            content = open(heartbeat_path).read().strip()
            if not content:
                return "HEARTBEAT.md가 비어 있음"
            if "[ ]" not in content:
                return "HEARTBEAT.md에 미완료 항목 없음"
            return None
        except Exception:
            return None

    def _process_task(self, task: dict):
        project = task["project"]

        # Check HEARTBEAT.md existence and update warning
        warning = self._check_heartbeat_md(project)
        prev_warning = task.get("warning")
        if warning != prev_warning:
            self._update_task(task["id"], {"warning": warning})
            if warning:
                logger.info(f"[heartbeat] {project} warning: {warning}")
            else:
                logger.info(f"[heartbeat] {project} warning cleared")

        if task.get("active_hours") and not self._in_active_hours(task["active_hours"]):
            return

        if task.get("max_runs") and task["run_count"] >= task["max_runs"]:
            self._update_task(task["id"], {"status": "done"})
            logger.info(f"[heartbeat] {project} max_runs reached → done")
            return

        with active_projects_lock:
            busy = project in active_projects
        if busy:
            next_run = datetime.now(timezone.utc) + timedelta(minutes=5)
            self._update_task(task["id"], {"next_run_at": next_run.isoformat()})
            logger.info(f"[heartbeat] {project} busy → postponed 5m")
            return

        # Skip inject if no valid HEARTBEAT.md
        if warning and "파일 없음" in warning:
            interval = task.get("interval_min", 30)
            next_run = datetime.now(timezone.utc) + timedelta(minutes=interval)
            self._update_task(task["id"], {
                "next_run_at": next_run.isoformat(),
            })
            logger.info(f"[heartbeat] {project} skipped (no HEARTBEAT.md)")
            return

        msg_id, ts = inject_system_message(project, self.HEARTBEAT_MSG, prefix="[heartbeat]")
        if not msg_id:
            logger.warning(f"[heartbeat] {project} inject failed")
            return

        interval = task.get("interval_min", 30)
        next_run = datetime.now(timezone.utc) + timedelta(minutes=interval)
        self._update_task(task["id"], {
            "next_run_at": next_run.isoformat(),
            "run_count": task["run_count"] + 1,
        })
        logger.info(f"[heartbeat] {project} woke up (run #{task['run_count'] + 1}, next in {interval}m)")

    def _update_task(self, task_id: str, updates: dict):
        api_key = config.get("api_key", "")
        if not api_key:
            return
        api_request(api_key, "PATCH", "/api/bot/tasks", body={"id": task_id, "updates": updates}, timeout=10)

    @staticmethod
    def _in_active_hours(hours_str: str) -> bool:
        try:
            start_str, end_str = hours_str.split("-")
            now = datetime.now()
            start = now.replace(hour=int(start_str[:2]), minute=int(start_str[3:]), second=0, microsecond=0)
            end = now.replace(hour=int(end_str[:2]), minute=int(end_str[3:]), second=0, microsecond=0)
            if start <= end:
                return start <= now <= end
            else:
                return now >= start or now <= end
        except Exception:
            return True
