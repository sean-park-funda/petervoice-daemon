"""Auto-updater: periodically git pull and restart if new commits found."""

import subprocess
import threading

from daemon.globals import config, shutdown_event, logger


# Repo root: two levels up from this file (syncers/ -> daemon/ -> scripts/ -> repo root)
def _find_repo_dir():
    """Walk up from this file to find .git directory."""
    from pathlib import Path
    d = Path(__file__).resolve().parent
    for _ in range(5):
        if (d / ".git").exists():
            return d
        d = d.parent
    return None


class AutoUpdater(threading.Thread):
    CHECK_INTERVAL = 300  # 5 minutes
    MAX_FAILURES = 3

    def __init__(self):
        super().__init__(daemon=True, name="auto-updater")
        self._repo_dir = _find_repo_dir()
        self._consecutive_failures = 0

    def _git(self, *args, timeout=30) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self._repo_dir)] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )

    def _local_head(self) -> str:
        r = self._git("rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else ""

    def _restart_home_portal_if_changed(self, old_head: str, new_head: str):
        """Restart Home Portal launchd service if home-portal.js changed."""
        r = self._git("diff", "--name-only", old_head, new_head)
        if r.returncode == 0 and "home-portal.js" in r.stdout:
            logger.info("[updater] home-portal.js changed — restarting Home Portal")
            try:
                import os
                uid = os.getuid()
                plist = os.path.expanduser("~/Library/LaunchAgents/com.petervoice.home-portal.plist")
                subprocess.run(["launchctl", "bootout", f"gui/{uid}", plist], capture_output=True)
                subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist], capture_output=True)
                logger.info("[updater] Home Portal restarted")
            except Exception as e:
                logger.warning(f"[updater] Home Portal restart failed: {e}")

    def _pip_install_if_changed(self, old_head: str, new_head: str):
        """Run pip install if requirements.txt changed between commits."""
        r = self._git("diff", "--name-only", old_head, new_head)
        if r.returncode == 0 and "requirements.txt" in r.stdout:
            logger.info("[updater] requirements.txt changed — running pip install")
            try:
                import sys
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r",
                     str(self._repo_dir / "requirements.txt"), "-q"],
                    capture_output=True, timeout=120,
                )
            except Exception as e:
                logger.warning(f"[updater] pip install failed: {e}")

    def check_once(self):
        if not config.get("auto_update_enabled", False):
            return

        if not self._repo_dir:
            logger.warning("[updater] Could not find git repository root")
            return

        if self._consecutive_failures >= self.MAX_FAILURES:
            logger.warning("[updater] Paused after %d consecutive failures", self.MAX_FAILURES)
            return

        branch = config.get("update_branch", "main")

        # 1. Fetch remote
        r = self._git("fetch", "origin", branch, timeout=30)
        if r.returncode != 0:
            self._consecutive_failures += 1
            logger.warning(f"[updater] git fetch failed: {r.stderr.strip()}")
            return

        # 2. Compare local vs remote
        old_head = self._local_head()
        r = self._git("rev-parse", f"origin/{branch}")
        remote_head = r.stdout.strip() if r.returncode == 0 else ""

        if not old_head or not remote_head:
            self._consecutive_failures += 1
            return

        if old_head == remote_head:
            self._consecutive_failures = 0
            return

        logger.info(f"[updater] New commits: {old_head[:8]} → {remote_head[:8]}")

        # 3. Fast-forward pull
        r = self._git("pull", "--ff-only", "origin", branch, timeout=60)
        if r.returncode != 0:
            self._consecutive_failures += 1
            logger.error(f"[updater] git pull --ff-only failed (local changes?): {r.stderr.strip()}")
            return

        new_head = self._local_head()
        logger.info(f"[updater] Updated to {new_head[:8]}")

        # 4. Install dependencies if changed
        self._pip_install_if_changed(old_head, new_head)

        # 5. Restart Home Portal if home-portal.js changed
        self._restart_home_portal_if_changed(old_head, new_head)

        # 6. Restart daemon
        self._consecutive_failures = 0
        logger.info("[updater] Restarting daemon to apply updates...")
        shutdown_event.set()

    def run(self):
        if self._repo_dir:
            logger.info(f"[updater] Git auto-updater started (repo={self._repo_dir})")
        else:
            logger.warning("[updater] Git repo not found — auto-updater disabled")
            return

        # Wait 30s before first check
        shutdown_event.wait(30)
        if shutdown_event.is_set():
            return

        while not shutdown_event.is_set():
            try:
                self.check_once()
            except Exception as e:
                self._consecutive_failures += 1
                logger.error(f"[updater] Check error: {e}")
            shutdown_event.wait(self.CHECK_INTERVAL)

        logger.info("[updater] Auto-updater stopped")
