"""Heartbeat thread: periodically inject due task messages."""

import json
import time
import threading
from datetime import datetime, timedelta, timezone

import daemon.globals as g
from daemon.globals import config, active_projects, active_projects_lock, shutdown_event, logger
from daemon.api import api_request, inject_system_message


class HeartbeatThread(threading.Thread):
    POLL_INTERVAL = 60
    HEARTBEAT_MSG = "HEARTBEAT.md를 확인하고 할 일이 있으면 처리해줘. 없으면 '할 일 없음'이라고 답해."

    def __init__(self):
        super().__init__(daemon=True, name="heartbeat")

    def run(self):
        logger.info("[heartbeat] Thread started")
        time.sleep(60)
        while not shutdown_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"[heartbeat] tick error: {e}")
            shutdown_event.wait(self.POLL_INTERVAL)
        logger.info("[heartbeat] Thread stopped")

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

    def _process_task(self, task: dict):
        project = task["project"]

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
