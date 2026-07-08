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

    # npm 이 배포마다 수정하는 tracked 파일 — pull 전에 로컬 변경을 폐기한다
    _NPM_CHURN = ("scripts/package.json", "scripts/package-lock.json")

    def _discard_npm_churn(self):
        """npm install 이 더럽힌 scripts/package*.json 로컬 변경을 폐기 (있을 때만)."""
        dirty = self._dirty_tracked_files()
        churn = [f for f in self._NPM_CHURN if f in dirty]
        if churn:
            logger.info(f"[updater] discarding npm-churned files before pull: {churn}")
            self._git("checkout", "--", *churn)

    def _dirty_tracked_files(self) -> list[str]:
        """워킹트리에서 변경된 tracked 파일 목록 (untracked 제외)."""
        r = self._git("diff", "--name-only")
        if r.returncode != 0:
            return []
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]

    def _restart_home_portal(self):
        """Restart the Home Portal launchd service (so it reloads native deps/code)."""
        try:
            import os
            uid = os.getuid()
            plist = os.path.expanduser("~/Library/LaunchAgents/com.petervoice.home-portal.plist")
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", plist], capture_output=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist], capture_output=True)
            logger.info("[updater] Home Portal restarted")
        except Exception as e:
            logger.warning(f"[updater] Home Portal restart failed: {e}")

    def _restart_home_portal_if_changed(self, old_head: str, new_head: str):
        """Restart Home Portal launchd service if home-portal.js changed."""
        r = self._git("diff", "--name-only", old_head, new_head)
        # home-portal.js require()s scripts/meeting/* at startup, so changes there
        # also only take effect after a Home Portal restart.
        if r.returncode == 0 and ("home-portal.js" in r.stdout or "scripts/meeting/" in r.stdout):
            logger.info("[updater] home-portal code changed — restarting Home Portal")
            self._restart_home_portal()

    def _clear_pycache(self):
        """Remove __pycache__ dirs under scripts/ to prevent stale .pyc issues."""
        import shutil
        scripts_dir = self._repo_dir / "scripts"
        cleared = 0
        for cache_dir in scripts_dir.rglob("__pycache__"):
            try:
                shutil.rmtree(cache_dir)
                cleared += 1
            except Exception:
                pass
        if cleared:
            logger.info(f"[updater] Cleared {cleared} __pycache__ dirs")

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

    def _npm_install_if_needed(self, old_head: str, new_head: str):
        """Ensure scripts/ npm deps (terminal node-pty/ws) are installed.
        Runs when package.json changed or when the native deps are missing.
        Handles npm's stale hidden lockfile (node_modules/.package-lock.json),
        which can make a plain `npm install` skip a dir that is actually absent."""
        import os
        scripts_dir = self._repo_dir / "scripts"
        pkg = scripts_dir / "package.json"
        if not pkg.exists():
            return
        node_modules = scripts_dir / "node_modules"
        pty_mod = node_modules / "@homebridge" / "node-pty-prebuilt-multiarch"
        ws_mod = node_modules / "ws"

        def deps_ok():
            return pty_mod.exists() and ws_mod.exists()

        changed = False
        r = self._git("diff", "--name-only", old_head, new_head)
        if r.returncode == 0 and ("scripts/package.json" in r.stdout or "scripts/package-lock.json" in r.stdout):
            changed = True
        missing = not deps_ok()

        if not (changed or missing):
            return

        # npm/node may live in homebrew or /usr/local which launchd PATH often omits
        from daemon.globals import _extended_path
        _env = {**os.environ,
                "PATH": "/opt/homebrew/bin:/usr/local/bin:" + _extended_path()}

        def run_npm(args, timeout=600):
            try:
                return subprocess.run(["npm", *args, "--no-audit", "--no-fund"],
                                      cwd=str(scripts_dir), capture_output=True,
                                      text=True, timeout=timeout, env=_env)
            except Exception as e:
                logger.warning(f"[updater] npm {' '.join(args)} failed: {e}")
                return None

        reason = "package.json changed" if changed else "terminal deps missing"
        logger.info(f"[updater] {reason} — installing scripts/ npm deps")

        # If deps are physically missing, drop npm's hidden lockfile so it can't
        # falsely believe they're already installed and skip them.
        if missing:
            try:
                (node_modules / ".package-lock.json").unlink(missing_ok=True)
            except Exception:
                pass

        run_npm(["install"])

        # Verify; if a native dep is still absent, force an explicit reinstall
        # (covers stale-lockfile and partial-install states).
        if not deps_ok():
            logger.warning("[updater] deps still missing after npm install — forcing explicit reinstall")
            run_npm(["install", "@homebridge/node-pty-prebuilt-multiarch", "ws", "--force"])

        if deps_ok():
            logger.info("[updater] terminal deps ready — restarting Home Portal")
            self._restart_home_portal()
        else:
            logger.warning("[updater] terminal deps STILL missing — manual npm install may be needed")

    def _ensure_tmux(self):
        """Terminal mode needs tmux. Install it (brew) if missing so the
        terminal WebSocket can create sessions. macOS only."""
        import os, shutil, platform
        if platform.system() != "Darwin":
            return
        # tmux already present in a standard location?
        for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
            if os.path.exists(p):
                return
        if shutil.which("tmux"):
            return

        brew = shutil.which("brew") or next(
            (b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.exists(b)), None)
        if not brew:
            logger.warning("[updater] tmux missing and brew not found — terminal mode unavailable")
            return
        logger.info("[updater] tmux missing — installing via brew (terminal mode)")
        try:
            res = subprocess.run([brew, "install", "tmux"],
                                 capture_output=True, text=True, timeout=600)
            if res.returncode == 0 and (os.path.exists("/opt/homebrew/bin/tmux") or os.path.exists("/usr/local/bin/tmux")):
                logger.info("[updater] tmux installed — restarting Home Portal")
                self._restart_home_portal()
            else:
                logger.warning(f"[updater] tmux install failed: {res.stderr[:400]}")
        except Exception as e:
            logger.warning(f"[updater] tmux install failed: {e}")

    def _ensure_claude_credentials_file(self):
        """Terminal mode runs claude inside a detached tmux server, which can't
        reach the macOS login Keychain (no GUI to approve the ACL prompt), so
        claude there shows a login screen even though chat works. Mirror the
        Keychain OAuth token into ~/.claude/.credentials.json (file-based auth,
        which works in any context). macOS only; the daemon itself runs in the
        GUI session so it CAN read the Keychain to seed the file."""
        import os, json, platform
        if platform.system() != "Darwin":
            return
        creds_path = os.path.expanduser("~/.claude/.credentials.json")
        if os.path.exists(creds_path):
            return  # already file-based; claude keeps it refreshed
        try:
            res = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=15,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return  # no keychain creds (not logged in yet) — nothing to mirror
            blob = res.stdout.strip()
            json.loads(blob)  # validate
            os.makedirs(os.path.dirname(creds_path), exist_ok=True)
            with open(creds_path, "w") as f:
                f.write(blob)
            os.chmod(creds_path, 0o600)
            logger.info("[updater] seeded ~/.claude/.credentials.json from Keychain (terminal auth)")
        except Exception as e:
            logger.warning(f"[updater] credentials file seed failed: {e}")

    def _update_cli_if_needed(self):
        """Update Claude CLI and Codex CLI to latest if newer versions are available on npm."""
        try:
            from daemon.globals import _extended_path
            _env = {**__import__("os").environ, "PATH": _extended_path()}
            self._update_claude_cli(_env)
            self._update_codex_cli(_env)
        except Exception as e:
            logger.warning(f"[updater] CLI update check failed (non-blocking): {e}")

    def _update_claude_cli(self, _env: dict):
        """Update @anthropic-ai/claude-code to latest."""
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10, env=_env,
        )
        current = r.stdout.strip().split()[0] if r.returncode == 0 else ""
        if not current:
            return

        r = subprocess.run(
            ["npm", "view", "@anthropic-ai/claude-code", "version"],
            capture_output=True, text=True, timeout=15, env=_env,
        )
        latest = r.stdout.strip() if r.returncode == 0 else ""
        if not latest or current == latest:
            return

        logger.info(f"[updater] Updating Claude CLI: {current} → {latest}")
        import platform
        if platform.machine() == "arm64" and __import__("sys").platform == "darwin":
            subprocess.run(
                ["npm", "install", "-g", "@anthropic-ai/claude-code-darwin-arm64"],
                capture_output=True, text=True, timeout=120, env=_env,
            )
        r = subprocess.run(
            ["npm", "install", "-g", f"@anthropic-ai/claude-code@{latest}"],
            capture_output=True, text=True, timeout=180, env=_env,
        )
        if r.returncode == 0:
            import shutil
            new_path = shutil.which("claude", path=_env["PATH"])
            v = subprocess.run(
                [new_path or "claude", "--version"],
                capture_output=True, text=True, timeout=10, env=_env,
            )
            if v.returncode == 0:
                logger.info(f"[updater] Claude CLI updated to {v.stdout.strip()}")
                if new_path:
                    import daemon.globals as g
                    g.CLAUDE_CMD = new_path
            else:
                logger.warning("[updater] Claude CLI binary broken after update — may need manual fix")
        else:
            logger.warning(f"[updater] Claude CLI update failed: {r.stderr.strip()[:200]}")

    def _update_codex_cli(self, _env: dict):
        """Update @openai/codex to latest."""
        r = subprocess.run(
            ["codex", "--version"],
            capture_output=True, text=True, timeout=10, env=_env,
        )
        current = r.stdout.strip().split()[0] if r.returncode == 0 else ""
        if not current:
            return

        r = subprocess.run(
            ["npm", "view", "@openai/codex", "version"],
            capture_output=True, text=True, timeout=15, env=_env,
        )
        latest = r.stdout.strip() if r.returncode == 0 else ""
        if not latest or current == latest:
            return

        logger.info(f"[updater] Updating Codex CLI: {current} → {latest}")
        r = subprocess.run(
            ["npm", "install", "-g", f"@openai/codex@{latest}"],
            capture_output=True, text=True, timeout=180, env=_env,
        )
        if r.returncode == 0:
            v = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10, env=_env)
            if v.returncode == 0:
                logger.info(f"[updater] Codex CLI updated to {v.stdout.strip()}")
            else:
                logger.warning("[updater] Codex CLI binary broken after update — may need manual fix")
        else:
            logger.warning(f"[updater] Codex CLI update failed: {r.stderr.strip()[:200]}")

    def check_once(self):
        if not config.get("auto_update_enabled", True):
            return

        if not self._repo_dir:
            logger.warning("[updater] Could not find git repository root")
            return

        # CLI update — independent of git pull, runs every cycle
        self._update_cli_if_needed()

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

        # 2.5. Discard npm-churned tracked files before pull.
        # `npm install` (node-pty 등, _npm_install_if_needed) modifies the tracked
        # scripts/package.json + package-lock.json, leaving the working tree dirty.
        # `git pull --ff-only` then refuses, so the updater silently sticks at the
        # current commit forever (no rollback, no restart). Discard those local
        # edits before pulling; they are re-generated by npm install after the pull.
        self._discard_npm_churn()

        # 3. Fast-forward pull
        r = self._git("pull", "--ff-only", "origin", branch, timeout=60)
        if r.returncode != 0:
            self._consecutive_failures += 1
            logger.error(f"[updater] git pull --ff-only failed (local changes?): {r.stderr.strip()}")
            # 진단만: npm churn 외 다른 tracked 변경이 막고 있으면 로그로 남긴다.
            # 절대 폐기하지 않는다 — 고객의 다른 로컬 변경을 보존해야 하므로.
            other = [f for f in self._dirty_tracked_files() if f not in self._NPM_CHURN]
            if other:
                logger.error(f"[updater] pull blocked by non-npm dirty files (preserved, not discarded): {other}")
            return

        new_head = self._local_head()
        if new_head == old_head:
            self._consecutive_failures = 0
            return

        logger.info(f"[updater] Updated to {new_head[:8]}")

        # 4. Clear __pycache__ to prevent stale bytecode
        self._clear_pycache()

        # 5. Install dependencies if changed
        self._pip_install_if_changed(old_head, new_head)
        self._npm_install_if_needed(old_head, new_head)

        # 6. Restart Home Portal if home-portal.js changed
        self._restart_home_portal_if_changed(old_head, new_head)

        # 7. Restart daemon
        self._consecutive_failures = 0
        logger.info("[updater] Restarting daemon to apply updates...")
        import daemon.globals as g
        g.restart_requested = True
        shutdown_event.set()

    def run(self):
        if self._repo_dir:
            logger.info(f"[updater] Git auto-updater started (repo={self._repo_dir})")
        else:
            logger.warning("[updater] Git repo not found — auto-updater disabled")
            return

        # Self-heal on startup: ensure terminal deps (tmux + node-pty/ws) exist
        # on this machine even if they were never installed.
        try:
            self._ensure_tmux()
            self._ensure_claude_credentials_file()
            head = self._local_head()
            if head:
                self._npm_install_if_needed(head, head)
        except Exception as e:
            logger.warning(f"[updater] startup self-heal failed: {e}")

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
