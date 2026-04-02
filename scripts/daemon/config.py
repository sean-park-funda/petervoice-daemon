"""Configuration, logging setup, PID lock management."""

import os
import sys
import json
import time
import logging
import threading
from logging.handlers import TimedRotatingFileHandler

from daemon.globals import (
    IS_WINDOWS, DAEMON_DIR, CONFIG_PATH, LOG_PATH, PID_PATH,
    SESSIONS_PATH, config, logger,
)
from daemon.utils import _read_json, _write_json

if os.name != "nt":
    import fcntl
else:
    import msvcrt


def setup_logging():
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        str(LOG_PATH), when="midnight", backupCount=7, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(console)
    logger.setLevel(logging.INFO)


def acquire_pid_lock():
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = open(str(PID_PATH), "w")
    try:
        if os.name == "nt":
            msvcrt.locking(pid_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(pid_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"ERROR: Another daemon is already running (PID file: {PID_PATH})")
        sys.exit(1)
    pid_file.write(str(os.getpid()))
    pid_file.flush()
    return pid_file


def release_pid_lock(pid_file):
    try:
        if os.name == "nt":
            msvcrt.locking(pid_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(pid_file, fcntl.LOCK_UN)
        pid_file.close()
        PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def load_config():
    import daemon.globals as g
    if not CONFIG_PATH.exists():
        logger.error(f"Config not found: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        new_config = json.load(f)
    # Update in-place so all modules holding a reference to config see the new values
    g.config.clear()
    g.config.update(new_config)
    g.claude_semaphore = threading.Semaphore(g.config.get("max_concurrent", 3))
    logger.info(f"Config loaded: bot={g.config.get('bot_name', '?')}, {len(g.config.get('project_dirs', {}))} project dirs")


def cleanup_stale_state():
    """Check for dead daemon PID files and recover stale state on startup."""
    if PID_PATH.exists():
        try:
            old_pid = int(PID_PATH.read_text().strip())
            os.kill(old_pid, 0)
            logger.warning(f"Previous daemon still running (PID {old_pid}), waiting up to 30s...")
            for i in range(30):
                time.sleep(1)
                try:
                    os.kill(old_pid, 0)
                except (ProcessLookupError, OSError):
                    logger.info(f"Previous daemon exited after {i+1}s")
                    PID_PATH.unlink(missing_ok=True)
                    break
            else:
                logger.error(f"Another daemon is still running (PID {old_pid})")
                sys.exit(1)
        except (ValueError, ProcessLookupError, OSError):
            logger.warning(f"Cleaning up stale PID file (dead process)")
            PID_PATH.unlink(missing_ok=True)
        except PermissionError:
            logger.error(f"Another daemon may be running (PID file exists, permission denied)")
            sys.exit(1)

    if SESSIONS_PATH.exists():
        data = _read_json(SESSIONS_PATH, None)
        if data is None:
            logger.warning("Corrupt sessions.json detected, resetting to empty")
            _write_json(SESSIONS_PATH, {})
        else:
            logger.info(f"Sessions file OK: {len(data)} entries")
