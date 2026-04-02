#!/usr/bin/env python3
"""
Claude Daemon — Peter Voice ↔ Claude Code CLI bridge
Single worker polling thread. Sessions are keyed by project:task.

This is a thin entry point. All logic lives in the daemon/ package.
"""

import sys
import signal

import daemon.globals as g
from daemon.globals import config, shutdown_event, logger
from daemon.config import setup_logging, load_config, acquire_pid_lock, release_pid_lock, cleanup_stale_state
from daemon.sessions import load_sessions, _process_pending_resets
from daemon.tasks import load_tasks
from daemon.prompts import ensure_template
from daemon.queue import load_queue
from daemon.supabase import resolve_user_id, check_force_restart, clear_force_restart, check_bot_response_exists
from daemon.worker import Worker
from daemon.health import SessionHealthChecker
from daemon.syncers.secrets import SecretsSyncer
from daemon.syncers.skills import SkillsSyncer
from daemon.syncers.docs import DocsSyncer
from daemon.syncers.auto_updater import AutoUpdater
from daemon.heartbeat import HeartbeatThread
from daemon.manager.thread import ManagerThread
from daemon.manager.http_server import start_manager_http_server
from daemon.api import api_request


def handle_signal(signum, frame):
    sig_name = signal.Signals(signum).name
    logger.info(f"Received {sig_name}, shutting down...")
    shutdown_event.set()


def _recover_after_restart(worker):
    """재시작 후 복구: 트리거 프로젝트에 완료 알림, 중단된 작업 재처리."""
    from daemon.utils import _read_json
    from daemon.api import api_request

    api_key = config.get("api_key", "")
    notified_projects = set()

    # 1. 재시작 트리거 확인 (어떤 프로젝트가 재시작을 지시했는지)
    trigger = _read_json(g.RESTART_TRIGGER_PATH, {})
    if trigger:
        try:
            g.RESTART_TRIGGER_PATH.unlink()
        except OSError:
            pass
        trigger_project = trigger.get("project")
        if trigger_project:
            api_request(api_key, "POST", "/api/bot/reply", {
                "text": "데몬 재시작 완료.",
                "project": trigger_project,
                "is_final": True,
            })
            notified_projects.add(trigger_project)
            logger.info(f"[recovery] Notified restart trigger project: {trigger_project}")

    # 2. 중단된 메시지 확인 및 재처리
    pending_queue = load_queue()
    if pending_queue:
        logger.info(f"Recovering {len(pending_queue)} queued message(s) from previous run")
        for msg in pending_queue:
            msg_id = msg.get("id")
            project = msg.get("project", "unknown")

            # 이미 응답이 전달됐는지 확인
            has_response = check_bot_response_exists(msg_id)

            if has_response:
                # 응답이 이미 있으면 알림만
                if project not in notified_projects:
                    api_request(api_key, "POST", "/api/bot/reply", {
                        "text": "데몬이 재시작됐습니다. 이전 응답은 정상 전달됐습니다.",
                        "project": project,
                        "is_final": True,
                    })
                    notified_projects.add(project)
                    logger.info(f"[recovery] {project}: response already delivered, notified user")
                from daemon.queue import dequeue_message
                dequeue_message(msg_id)
            else:
                # 응답이 없으면 재처리
                if project not in notified_projects:
                    api_request(api_key, "POST", "/api/bot/reply", {
                        "text": "데몬이 재시작됐습니다. 이전 작업을 다시 처리합니다.",
                        "project": project,
                        "is_final": True,
                    })
                    notified_projects.add(project)
                    logger.info(f"[recovery] {project}: response missing, reprocessing")
                with worker._spawned_lock:
                    worker._spawned_ids.add(msg_id)
                worker._executor.submit(worker._process_message_safe, msg)


def _ensure_cloudflared():
    """cloudflared가 없으면 자동 설치. 성공 시 True, 실패 시 False."""
    import shutil, subprocess, platform
    if shutil.which("cloudflared"):
        return True

    logger.info("[home-portal] cloudflared not found, installing...")
    try:
        # macOS: brew 시도
        if platform.system() == "Darwin":
            brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
            result = subprocess.run(
                [brew, "install", "cloudflared"],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                logger.info("[home-portal] cloudflared installed via brew")
                return True
            # brew 실패 시 직접 다운로드
            logger.warning(f"[home-portal] brew install failed, trying direct download")
            arch = "arm64" if platform.machine() == "arm64" else "amd64"
            url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-{arch}.tgz"
            subprocess.run(
                ["curl", "-sL", url, "-o", "/tmp/cloudflared.tgz"],
                timeout=120, check=True
            )
            subprocess.run(
                ["tar", "-xzf", "/tmp/cloudflared.tgz", "-C", "/usr/local/bin/"],
                timeout=30, check=True
            )
            logger.info("[home-portal] cloudflared installed via direct download")
            return True
        else:
            logger.error("[home-portal] Auto-install only supported on macOS")
            return False
    except Exception as e:
        logger.error(f"[home-portal] Failed to install cloudflared: {e}")
        return False


def _ensure_home_portal():
    """Home Portal launchd 확인/시작 + tunnel_url 서버 등록."""
    if not config.get("home_portal_enabled", True):
        logger.info("[home-portal] Disabled via config")
        return

    api_key = config.get("api_key", "")
    if not api_key:
        return

    try:
        # 0. cloudflared 설치 확인
        if not _ensure_cloudflared():
            logger.error("[home-portal] cloudflared required but installation failed")
            return

        # 1. username 조회
        me = api_request(api_key, "GET", "/api/bot/me")
        if not me or not me.get("username"):
            logger.warning("[home-portal] Could not resolve username from /api/bot/me")
            return
        username = me["username"]

        # 2. Home Portal launchd 시작 (이미 실행 중이면 스킵)
        import subprocess
        from pathlib import Path
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.petervoice.home-portal.plist"
        portal_running = False
        if plist_path.exists():
            try:
                result = subprocess.run(
                    ["launchctl", "list", "com.petervoice.home-portal"],
                    capture_output=True, text=True
                )
                portal_running = result.returncode == 0
            except Exception:
                pass

        if not portal_running:
            from daemon.site_manager import start_home_portal
            result = start_home_portal(username=username)
            if result.get("error"):
                logger.error(f"[home-portal] Failed to start: {result['error']}")
                return
            logger.info(f"[home-portal] Started: {result.get('url')}")
        else:
            logger.info("[home-portal] Already running")

        # 3. tunnel_url 서버에 등록
        username_slug = username.lower().replace("_", "-")
        username_slug = "".join(c for c in username_slug if c.isalnum() or c == "-")
        tunnel_url = f"https://{username_slug}.peter-voice.site"
        api_request(api_key, "PATCH", "/api/bot/status", body={"tunnel_url": tunnel_url})
        logger.info(f"[home-portal] Registered tunnel_url: {tunnel_url}")

    except Exception as e:
        logger.error(f"[home-portal] Error: {e}")


def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("Claude Daemon starting...")

    cleanup_stale_state()

    pid_file = acquire_pid_lock()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        load_config()
        load_sessions()
        load_tasks()
        ensure_template()

        if not config.get("api_key"):
            logger.error("No api_key configured in config.json")
            sys.exit(1)

        logger.info(f"API URL: {config['api_url']}")
        logger.info(f"Bot: {config.get('bot_name', '?')}")
        from daemon.globals import CLAUDE_CMD
        logger.info(f"Claude CLI: {CLAUDE_CMD}")

        uid = resolve_user_id()
        if uid:
            logger.info(f"User ID: {uid}")
        else:
            logger.warning("Could not resolve user_id from api_key — force_restart/project queries may fail")

        # Start syncer threads
        SecretsSyncer().start()
        SkillsSyncer().start()
        DocsSyncer().start()
        AutoUpdater().start()

        worker = Worker()
        worker.start()

        # Session health checker
        session_health_config = config.get("session_health", {})
        if session_health_config.get("enabled", True):
            health_checker = SessionHealthChecker()
            if session_health_config.get("interval_hours"):
                health_checker.CHECK_INTERVAL = session_health_config["interval_hours"] * 3600
            health_checker.start()
            logger.info(f"Session health checker started (interval={health_checker.CHECK_INTERVAL // 3600}h)")

        # Heartbeat thread
        HeartbeatThread().start()
        logger.info("HeartbeatThread started")

        # Manager thread
        mgr_config = config.get("manager", {})
        if mgr_config.get("enabled", False):
            manager_thread = ManagerThread()
            g._manager_instance = manager_thread
            manager_thread.start()
            logger.info(f"Manager thread started (interval={mgr_config.get('interval_minutes', 60)}m)")
            try:
                start_manager_http_server(port=mgr_config.get("status_port", 7777))
            except Exception as e:
                logger.warning(f"Manager status API failed to start: {e}")

        # Recover after restart: notify users and reprocess pending messages
        _recover_after_restart(worker)

        # Ensure Home Portal is running + register tunnel URL
        _ensure_home_portal()

        # Main loop: watchdog + force_restart polling
        user_id = resolve_user_id()
        watchdog_tick = 0
        while not shutdown_event.is_set():
            shutdown_event.wait(1)
            if shutdown_event.is_set():
                break
            watchdog_tick += 1

            if watchdog_tick % 10 == 0:
                if not worker.is_alive():
                    logger.error("Worker thread died! Restarting daemon...")
                    g.restart_requested = True
                    shutdown_event.set()
                    break

            if watchdog_tick % 10 == 0:
                try:
                    _process_pending_resets()
                except Exception as e:
                    logger.warning(f"pending_resets check error: {e}")

            if watchdog_tick % 30 == 0 and user_id is not None:
                try:
                    if check_force_restart(user_id):
                        logger.info("Force restart requested via web UI")
                        clear_force_restart(user_id)
                        g.restart_requested = True
                        shutdown_event.set()
                        break
                except Exception as e:
                    logger.warning(f"force_restart check error: {e}")

        worker._executor.shutdown(wait=False, cancel_futures=True)
        worker.join(timeout=5)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
        shutdown_event.set()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        if g.restart_requested:
            logger.info("Claude Daemon restarting (exit 1 for launchd)...")
        else:
            logger.info("Claude Daemon stopped")
        release_pid_lock(pid_file)
        if g.restart_requested:
            sys.exit(1)


if __name__ == "__main__":
    main()
