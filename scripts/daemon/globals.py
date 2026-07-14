"""Shared state for all daemon modules.

All global variables, locks, events, and paths are declared here.
Every other daemon module imports from this file — this module must NOT
import from any other daemon.* module (it is the lowest-level dependency).
"""

import os
import sys
import logging
import threading
import argparse
import shutil
import subprocess
import time
from pathlib import Path

# ─── Platform ────────────────────────────────────────────────────
IS_WINDOWS = os.name == "nt"

# ─── Build extended PATH including nvm/local bin ────────────────
def _extended_path() -> str:
    """Return PATH augmented with common non-standard CLI locations."""
    extra = []
    home = Path.home()
    # nvm: pick the highest installed node version
    nvm_dir = home / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        versions = sorted(nvm_dir.iterdir(), reverse=True)
        if versions:
            extra.append(str(versions[0] / "bin"))
    # local bin
    local_bin = home / ".local" / "bin"
    if local_bin.is_dir():
        extra.append(str(local_bin))
    current = os.environ.get("PATH", "")
    return ":".join(extra + [current]) if extra else current


# ─── Resolve claude CLI path ────────────────────────────────────
def _find_claude_cmd() -> str:
    # 1. config.json claude_path (set during onboarding or manual fix)
    try:
        import json
        _cfg_path = Path.home() / ".claude-daemon" / "config.json"
        _cfg = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}
        _cp = _cfg.get("claude_path", "")
        if _cp and Path(_cp).exists():
            return _cp
    except Exception:
        pass
    # 2. which() with extended PATH
    found = shutil.which("claude", path=_extended_path())
    if found:
        return found
    # 3. Windows fallback
    if IS_WINDOWS:
        npm_claude = Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd"
        if npm_claude.exists():
            return str(npm_claude)
    return "claude"

CLAUDE_CMD = _find_claude_cmd()

# ─── Resolve codex CLI path ────────────────────────────────────
def _find_codex_cmd() -> str:
    found = shutil.which("codex", path=_extended_path())
    if found:
        return found
    if IS_WINDOWS:
        npm_codex = Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"
        if npm_codex.exists():
            return str(npm_codex)
    return "codex"

CODEX_CMD = _find_codex_cmd()

# ─── Codex 가용성(설치+인증) 감지 — 5분 캐시 ────────────────────
# 웹 헤더가 이 값으로 Codex 모델 선택지를 활성/비활성("코덱스 미설치") 표시한다.
_codex_avail_cache = {"ts": 0.0, "val": False}
def detect_codex_available() -> bool:
    """codex CLI 설치+인증 여부. 설치=`codex --version` 성공, 인증=~/.codex/auth.json 또는 config.openai_api_key."""
    now = time.time()
    if now - _codex_avail_cache["ts"] < 300 and _codex_avail_cache["ts"] > 0:
        return _codex_avail_cache["val"]
    val = False
    try:
        r = subprocess.run([CODEX_CMD, "--version"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            auth_json = Path.home() / ".codex" / "auth.json"
            val = auth_json.exists() or bool(config.get("openai_api_key"))
    except Exception:
        val = False
    _codex_avail_cache.update(ts=now, val=val)
    return val

# ─── Daemon version (git HEAD short) — heartbeat 로 리포트해 stale 고객 식별 ──
def _daemon_version() -> str:
    """데몬 레포의 현재 git HEAD short. 시작 시 1회 계산(재시작하면 갱신). 실패 시 'unknown'."""
    try:
        import subprocess
        d = Path(__file__).resolve().parent  # scripts/daemon/
        for _ in range(5):
            if (d / ".git").exists():
                r = subprocess.run(
                    ["git", "-C", str(d), "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.stdout.strip() if r.returncode == 0 else "unknown"
            d = d.parent
    except Exception:
        pass
    return "unknown"

DAEMON_VERSION = _daemon_version()

# ─── Parse --config-dir before path setup ────────────────────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--config-dir", default=None)
_args, _ = _parser.parse_known_args()

# ─── Paths ──────────────────────────────────────────────────────
DAEMON_DIR = Path(_args.config_dir) if _args.config_dir else Path.home() / ".claude-daemon"
CONFIG_PATH = DAEMON_DIR / "config.json"
SESSIONS_PATH = DAEMON_DIR / "sessions.json"
TASKS_PATH = DAEMON_DIR / "tasks.json"
LOG_PATH = DAEMON_DIR / "daemon.log"
PID_PATH = DAEMON_DIR / "daemon.pid"
PROMPTS_DIR = DAEMON_DIR / "prompts"
QUEUE_PATH = DAEMON_DIR / "queue.json"
PENDING_RESETS_PATH = DAEMON_DIR / "pending_resets.json"
DOWNLOADS_DIR = DAEMON_DIR / "downloads"
SECRETS_ENV_PATH = DAEMON_DIR / ".env.secrets"
MANAGER_STATE_PATH = DAEMON_DIR / "manager_state.json"
WORKFLOWS_DIR = DAEMON_DIR / "workflows"
DOCS_STATE_PATH = DAEMON_DIR / "docs_state.json"  # legacy, kept for cleanup
RESTART_TRIGGER_PATH = DAEMON_DIR / "restart_trigger.json"
CODEX_SESSIONS_PATH = DAEMON_DIR / "codex_sessions.json"

# ─── Constants ──────────────────────────────────────────────────
MAX_CONTEXT_OVERFLOW_RETRIES = 2
ACK_MESSAGE = "접수했습니다. 처리 중..."
SESSION_MANAGER_PROJECT = "session-manager"
SKILLS_DIR = Path.home() / ".claude" / "skills"


# ─── Runtime state ──────────────────────────────────────────────
config: dict = {}
_cached_user_id: int | None = None
sessions: dict = {}       # "project:task" -> { session_id, created_at, message_count }
tasks: dict = {}          # "project" -> { current_task, tasks: { name: { created_at, description } } }
processed_ids: set = set()
restart_requested: bool = False

# ─── Thread synchronization ────────────────────────────────────
sessions_lock = threading.Lock()
tasks_lock = threading.Lock()
queue_lock = threading.Lock()
shutdown_event = threading.Event()
claude_semaphore: threading.Semaphore | None = None
project_locks: dict = {}
project_locks_lock = threading.Lock()
active_projects: set = set()
active_projects_lock = threading.Lock()
manager_wake_event = threading.Event()

# ─── Cached / singleton ────────────────────────────────────────
_project_settings_cache: dict[str, tuple[float, dict]] = {}
_manager_instance = None

# ─── Logger ─────────────────────────────────────────────────────
logger = logging.getLogger("claude-daemon")
