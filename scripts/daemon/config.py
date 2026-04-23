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
from daemon.api import api_request

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


# ── Auto-create default projects ──

_SYSADMIN_PROMPT = """\
# 시스템 관리자

## 역할
{customer_name}님의 Mac을 관리하고, 피터보이스 플랫폼 문제를 진단하는 시스템 관리자입니다.

## 핵심 업무
- macOS 설정, 앱 설치/삭제, 파일 관리
- 네트워크, 프린터, 주변기기 설정
- 문제 해결, 에러 진단
- 보안 설정, 백업 관리
- 피터보이스 데몬 트러블슈팅

## 피터보이스 플랫폼 이해

### 동작 구조
{customer_name}님이 웹 채팅(peter-voice.vercel.app)에서 메시지를 보내면:
1. 웹 UI → Supabase DB에 메시지 저장
2. Mac에서 돌아가는 **데몬**(백그라운드 프로세스)이 메시지를 감지
3. 데몬이 **Claude Code CLI**를 실행하여 응답 생성
4. 응답이 DB → 웹 UI로 전달

즉, Mac이 꺼지거나 데몬이 멈추면 피터가 응답하지 않습니다.

### 핵심 개념
- **프로젝트**: 작업 영역 단위. 각 프로젝트는 별도 디렉토리와 프롬프트를 가짐
- **브랜치**: 프로젝트 하위의 독립 대화 세션. 특정 주제를 따로 다룰 때 사용
- **데몬**: launchd로 관리되는 Python 프로세스. Mac 부팅 시 자동 시작
- **프롬프트**: 에이전트의 역할/규칙을 정의. 웹 UI 프로젝트 설정에서 편집 가능

### 트러블슈팅

**"피터가 응답 안 해요"**
```bash
# 1. 데몬이 살아있는지 확인
launchctl list | grep petervoice

# 2. 최근 로그 확인 (에러 메시지 찾기)
tail -30 ~/.claude-daemon/daemon.log

# 3. 재시작 (반드시 이 방법으로)
(sleep 5 && launchctl stop com.petervoice.claude-daemon) &
# → launchd가 10초 내 자동 재시작함
# ⚠️ 절대 pkill, kill, killall 사용 금지
```

**"특정 프로젝트만 안 돼요"**
- 해당 프로젝트의 작업 디렉토리가 존재하는지 확인
- `~/.claude-daemon/config.json`에서 프로젝트 경로 확인

**"응답이 너무 느려요"**
- `top` 또는 `ps aux | grep claude`로 Claude 프로세스 확인
- 다른 프로젝트에서 이미 응답 중이면 큐에 쌓임 (동시 처리 제한)

### 주의
- DB 직접 접근, 데몬 코드 수정은 하지 말 것
- 설정 파일(`~/.claude-daemon/config.json`) 수정은 구조를 이해한 후에만

## 규칙
- 위험한 작업(포맷, 대량 삭제 등)은 반드시 확인 후 실행
- 설정 변경 전 현재 값을 기록하고, 롤백 방법을 안내
- 문제 진단은 단계별로 — 한 번에 여러 가지를 변경하지 말 것
- 기술 용어를 쉽게 설명
- 작업 전후 상태를 알려줌
- 한국어로 답변
"""

_DEFAULT_PROJECTS = [
    {
        "id": "sysadmin",
        "name": "🖥️ 시스템 관리자",
        "prompt": _SYSADMIN_PROMPT,
    },
    {
        "id": "manager",
        "name": "📋 매니저",
        "prompt": None,  # manager prompt is set by ManagerThread
    },
]


def ensure_default_projects():
    """Ensure sysadmin and manager projects exist. Called once at daemon startup."""
    api_key = config.get("api_key")
    if not api_key:
        return

    result = api_request(api_key, "GET", "/api/projects", timeout=10)
    if not result or "projects" not in result:
        logger.warning("[default-projects] Failed to fetch projects, skipping auto-create")
        return

    existing_ids = {p["id"] for p in result["projects"]}

    # Get customer name for prompt
    customer_name = config.get("bot_name", "고객")

    for proj in _DEFAULT_PROJECTS:
        if proj["id"] in existing_ids:
            continue

        logger.info(f"[default-projects] Creating '{proj['id']}' project...")

        create_result = api_request(api_key, "POST", "/api/projects", body={
            "id": proj["id"],
            "name": proj["name"],
        }, timeout=10)

        if not create_result or "error" in str(create_result).lower():
            logger.error(f"[default-projects] Failed to create {proj['id']}: {create_result}")
            continue

        if proj["prompt"]:
            prompt_content = proj["prompt"].replace("{customer_name}", customer_name)
            api_request(api_key, "PUT", "/api/prompts", body={
                "project": proj["id"],
                "content": prompt_content,
            }, timeout=10)

        logger.info(f"[default-projects] '{proj['id']}' created")
