"""Task management: multi-session per project."""

from datetime import datetime

import daemon.globals as g
from daemon.globals import TASKS_PATH, tasks_lock, logger
from daemon.utils import _read_json, _write_json


def load_tasks():
    g.tasks = _read_json(TASKS_PATH, {})
    # Migrate 2-part keys ("bot_id:project") to 1-part ("project")
    migrated = {}
    need_save = False
    for key, val in g.tasks.items():
        parts = key.split(":")
        if len(parts) == 2:
            new_key = parts[1]
            if new_key not in migrated:
                migrated[new_key] = val
            need_save = True
        else:
            migrated[key] = val
    g.tasks = migrated
    if need_save:
        logger.info(f"Migrated task keys to 1-part format ({len(g.tasks)} projects)")
        save_tasks()
    logger.info(f"Tasks loaded: {len(g.tasks)} project(s)")


def save_tasks():
    with tasks_lock:
        try:
            _write_json(TASKS_PATH, g.tasks)
        except Exception as e:
            logger.error(f"Failed to save tasks: {e}")


def get_current_task(project: str) -> str:
    with tasks_lock:
        entry = g.tasks.get(project)
        if not entry:
            return "default"
        return entry.get("current_task", "default")


def set_current_task(project: str, task_name: str, description: str = ""):
    with tasks_lock:
        if project not in g.tasks:
            g.tasks[project] = {"current_task": "default", "tasks": {"default": {"created_at": datetime.now().isoformat(), "description": ""}}}
        g.tasks[project]["current_task"] = task_name
        if task_name not in g.tasks[project]["tasks"]:
            g.tasks[project]["tasks"][task_name] = {"created_at": datetime.now().isoformat(), "description": description}
        elif description:
            g.tasks[project]["tasks"][task_name]["description"] = description
    save_tasks()


def list_tasks(project: str) -> dict:
    with tasks_lock:
        entry = g.tasks.get(project)
        if not entry:
            return {"default": {"created_at": "", "description": ""}}
        return entry.get("tasks", {})


def get_task_description(project: str, task_name: str) -> str:
    with tasks_lock:
        entry = g.tasks.get(project)
        if not entry:
            return ""
        return entry.get("tasks", {}).get(task_name, {}).get("description", "")
