"""Auto-updater: periodically git pull and restart if new commits found."""

import os
import re
import subprocess
import threading
import time

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


NPM_INSTALL_TIMEOUT_DEFAULT = 900   # npm install -g 상한(초) — config cli_install_timeout_sec


class CliUpdatePolicy:
    """CLI(claude·codex) npm 교체를 '언제' 할지 — 순수 판단만 (시험: scripts/test_auto_updater_policy.py).

    배경(2026-11-16, 11/15 23:53 맥미니 커널 패닉 → 자동로그인 2분 뒤 WindowServer 재크래시):
    - 부팅 32초 뒤 업데이터가 Claude CLI npm 설치(실패)·Codex 재설치를 돌려 이미 넘친 기계를 더 눌렀다.
    - `npm install -g @anthropic-ai/claude-code@…` 는 옛 패키지를 치운 뒤 새것을 깔므로 수십 초 동안 실행 파일이 없다.
      그 순간 시작한 턴은 `[Errno 2] … /opt/homebrew/bin/claude` 로 죽었다(11/10~15 Sean 맥 16건).
    - 같은 버전 설치가 5분마다 실패·롤백을 반복했다(하루 200~283회) — 공백도 그만큼 반복됐다.

    규칙:
    - 데몬 시작 뒤 boot_grace_sec(기본 900초) 안에는 시도하지 않는다.
    - 턴이 도는 중(inflight > 0)에는 미룬다. 새 버전을 처음 본 뒤 max_defer_sec(기본 6시간)가 지나면 그래도 한다
      (바쁜 기계에서 영영 안 올라가는 일은 없게).
    - 설치가 실패한 버전은 retry_sec(기본 6시간) 동안 다시 시도하지 않는다. 다른 버전이 나오면 바로 다시 본다.
    - **복구(repair)는 다른 규칙이다(2026-11-17).** 실행 파일이 아예 없거나 돌지 않는 것은 '업데이트'가 아니라 장애다 —
      턴 유무·6시간 백오프를 기다리지 않고, 부팅 완충과 repair_retry_sec(기본 30분)만 지킨다.
      배경: 11/15~16 고객 맥 3곳에서 `npm install -g` 가 우리 180초 타임아웃에 죽어(138MB 플랫폼 바이너리 내려받기가
      3~5분) 실행 파일이 사라졌는데, 업데이터는 `claude --version` 실패를 '건너뜀'으로 처리해 30시간 넘게 아무도 다시 깔지 않았다.
    """

    def __init__(self, boot_grace_sec: float = 900, max_defer_sec: float = 6 * 3600,
                 retry_sec: float = 6 * 3600, clock=time.monotonic, repair_retry_sec: float = 1800):
        self._clock = clock
        self.boot_grace_sec = max(0.0, float(boot_grace_sec or 0))
        self.max_defer_sec = max(0.0, float(max_defer_sec or 0))
        self.retry_sec = max(0.0, float(retry_sec or 0))
        self.repair_retry_sec = max(0.0, float(repair_retry_sec or 0))
        self._started_at = clock()
        self._pending_version: str | None = None
        self._pending_since: float = 0.0
        self._fail: dict | None = None
        self._logged: tuple | None = None   # (version, reason) — 같은 미룸 사유는 한 번만 INFO 로 남긴다
        self._repair_at: float | None = None

    def in_boot_grace(self) -> bool:
        return self._clock() - self._started_at < self.boot_grace_sec

    def decide_repair(self) -> tuple[bool, str]:
        """실행 파일이 없거나 돌지 않을 때 지금 복구 설치를 할지 — 장애이므로 턴 유무·실패 백오프는 보지 않는다."""
        now = self._clock()
        if self.in_boot_grace():
            return False, "boot grace"
        if self._repair_at is not None and now - self._repair_at < self.repair_retry_sec:
            return False, f"repair tried {int((now - self._repair_at) // 60)}m ago"
        return True, ""

    def note_repair_attempt(self):
        self._repair_at = self._clock()

    def decide(self, latest: str, inflight: int) -> tuple[bool, str]:
        """(지금 설치해도 되나, 미루는 이유). 이유는 로그용."""
        now = self._clock()
        if latest != self._pending_version:
            self._pending_version, self._pending_since = latest, now
        if self.in_boot_grace():
            return False, "boot grace"
        if self._fail and self._fail["version"] == latest and now - self._fail["at"] < self.retry_sec:
            return False, "failed earlier"
        if inflight > 0 and now - self._pending_since < self.max_defer_sec:
            return False, f"{inflight} turn(s) in flight"
        return True, ""

    def note_failure(self, version: str):
        self._fail = {"version": version, "at": self._clock()}

    def note_success(self):
        self._fail = None
        self._pending_version, self._pending_since = None, 0.0
        self._repair_at = None

    def should_log_defer(self, version: str, reason: str) -> bool:
        key = (version, reason.split(" ")[-1] if "in flight" in reason else reason)
        if key == self._logged:
            return False
        self._logged = key
        return True


class AutoUpdater(threading.Thread):
    CHECK_INTERVAL = 300  # 5 minutes
    MAX_FAILURES = 3

    def __init__(self):
        super().__init__(daemon=True, name="auto-updater")
        self._repo_dir = _find_repo_dir()
        self._consecutive_failures = 0
        self._codex_repair_tried = False
        self._codex_broken_seen = 0
        _policy_kw = dict(
            boot_grace_sec=config.get("cli_update_boot_grace_sec", 900),
            max_defer_sec=config.get("cli_update_max_defer_sec", 6 * 3600),
            retry_sec=config.get("cli_update_retry_sec", 6 * 3600),
            repair_retry_sec=config.get("cli_repair_retry_sec", 1800),
        )
        self._claude_broken_seen = 0   # `claude --version` 연속 실패 수 (복구 판단)
        self._claude_policy = CliUpdatePolicy(**_policy_kw)
        self._codex_policy = CliUpdatePolicy(**_policy_kw)

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
            out = subprocess.run(["launchctl", "bootout", f"gui/{uid}", plist], capture_output=True, text=True)
            boot = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist], capture_output=True, text=True)
            # 결과 코드를 안 보고 "restarted" 를 찍던 자리 — 실패해도 성공으로 기록돼, 포털이 옛 코드로
            # 계속 도는 것을 아무도 몰랐다 (2026-09-21: 데몬은 최신인데 포털만 구코드인 기기 실측).
            if out.returncode == 0 and boot.returncode == 0:
                logger.info("[updater] Home Portal restarted")
            else:
                logger.warning(
                    f"[updater] Home Portal restart NOT confirmed — portal may still run old code "
                    f"(bootout rc={out.returncode} {(out.stderr or '').strip()[:120]!r}, "
                    f"bootstrap rc={boot.returncode} {(boot.stderr or '').strip()[:120]!r})"
                )
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

    @staticmethod
    def _turns_inflight() -> int:
        try:
            import daemon.globals as g
            return int(getattr(g, "turns_inflight", 0) or 0)
        except Exception:
            return 0

    def _log_defer(self, what: str, policy: CliUpdatePolicy, latest: str, reason: str):
        if policy.should_log_defer(latest, reason):
            logger.info(f"[updater] {what} {latest} available — deferred ({reason})")

    def _stray_platform_packages(self, _env: dict, root: str | None = None) -> list[str]:
        """전역 node_modules/@anthropic-ai/ 에 따로 깔린 플랫폼 패키지(claude-code-darwin-arm64 등) 이름 목록.

        install.cjs 는 플랫폼 패키지가 '본체 안(node_modules)' 과 '전역 최상위' 두 곳에 있으면 어느 쪽도 쓰지 않고
        설치를 실패시킨다("Two copies of the native package"). 예전 업데이터가 플랫폼 패키지를 먼저 전역에 깔던
        단계가 그 두 번째 사본을 만들었다(2026-11-10~15 매 5분 실패). 설치 전에 치운다.
        """
        try:
            if root is None:
                r = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20, env=_env)
                root = r.stdout.strip() if r.returncode == 0 else ""
            if not root:
                return []
            scope = os.path.join(root, "@anthropic-ai")
            if not os.path.isdir(scope):
                return []
            return sorted(n for n in os.listdir(scope)
                          if n.startswith("claude-code-") and os.path.isdir(os.path.join(scope, n)))
        except Exception as e:
            logger.warning(f"[updater] stray platform package check failed: {e}")
            return []

    def _npm_root(self, _env: dict) -> str:
        try:
            r = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20, env=_env)
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    def _remove_stray_platform_packages(self, _env: dict) -> list[str]:
        stray = self._stray_platform_packages(_env)
        root = self._npm_root(_env) if stray else ""
        for name in stray:
            logger.info(f"[updater] removing stray top-level @anthropic-ai/{name} (install.cjs rejects two copies)")
            subprocess.run(["npm", "rm", "-g", f"@anthropic-ai/{name}"],
                           capture_output=True, text=True, timeout=120, env=_env)
            # package.json 이 없는 깨진 사본(죽은 npm 이 남긴 것)은 npm rm 이 못 지운다 — 직접 지운다
            left = os.path.join(root, "@anthropic-ai", name) if root else ""
            if left and os.path.isdir(left):
                import shutil
                shutil.rmtree(left, ignore_errors=True)
        return stray

    def _remove_retired_leftovers(self, root: str) -> list[str]:
        """npm 이 교체 중 죽으면 옛 패키지를 `.claude-code-XXXX` 로 돌려놓은 채 남긴다(각 300MB+). 복구 설치 전에 치운다."""
        scope = os.path.join(root, "@anthropic-ai") if root else ""
        if not scope or not os.path.isdir(scope):
            return []
        import shutil
        gone = []
        for n in sorted(os.listdir(scope)):
            path = os.path.join(scope, n)
            if n.startswith(".claude-code-") and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                gone.append(n)
        if gone:
            logger.info(f"[updater] removed retired npm leftovers: {gone}")
        return gone

    def _claude_version(self, _env: dict, path: str = "claude") -> str:
        try:
            v = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, env=_env)
        except FileNotFoundError:
            return ""
        return v.stdout.strip().split()[0] if v.returncode == 0 and v.stdout.strip() else ""

    @staticmethod
    def _claude_present(_env: dict) -> str | None:
        import shutil
        return shutil.which("claude", path=_env.get("PATH"))

    def _install_timeout(self) -> int:
        # 11/17 실측: 고객 맥에서 `npm install -g @anthropic-ai/claude-code` 가 3~5분(플랫폼 바이너리 138MB). 180초는 npm 을 죽였다.
        return max(300, int(config.get("cli_install_timeout_sec", NPM_INSTALL_TIMEOUT_DEFAULT) or NPM_INSTALL_TIMEOUT_DEFAULT))

    def _npm_install_claude(self, _env: dict, latest: str) -> tuple[bool, str]:
        """`npm install -g @anthropic-ai/claude-code@<latest>` 를 끝까지 기다린다. (성공 여부, 오류 요약).
        도중에 죽이면(타임아웃·데몬 종료) 실행 파일이 없는 채로 남으므로 상한은 넉넉히, 종료 드레인은 g.cli_install_inflight 를 본다."""
        import daemon.globals as g
        timeout = self._install_timeout()
        g.cli_install_inflight = True
        try:
            r = subprocess.run(["npm", "install", "-g", f"@anthropic-ai/claude-code@{latest}", "--no-audit", "--no-fund"],
                               capture_output=True, text=True, timeout=timeout, env=_env)
        except subprocess.TimeoutExpired:
            return False, f"npm install timed out after {timeout}s (killed — the claude binary may be missing until the repair runs)"
        finally:
            g.cli_install_inflight = False
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            hint = " (two copies of the platform package — official fix: npm rm -g both, then reinstall)" if "Two copies" in err else ""
            return False, f"npm error code {r.returncode}: {err[:200]}{hint}"
        return True, ""

    def _clean_reinstall_claude(self, _env: dict, latest: str) -> str:
        """공식 처방: 본체·플랫폼 패키지·죽은 npm 의 찌꺼기를 전부 지우고 다시 깐다. 설치된 버전(실행 확인) 또는 ""."""
        import shutil
        root = self._npm_root(_env)
        self._remove_stray_platform_packages(_env)
        self._remove_retired_leftovers(root)
        subprocess.run(["npm", "rm", "-g", "@anthropic-ai/claude-code"],
                       capture_output=True, text=True, timeout=120, env=_env)
        main_dir = os.path.join(root, "@anthropic-ai", "claude-code") if root else ""
        if main_dir and os.path.isdir(main_dir):
            shutil.rmtree(main_dir, ignore_errors=True)
        ok, why = self._npm_install_claude(_env, latest)
        if not ok:
            logger.warning(f"[updater] clean reinstall failed: {why}")
        new_path = self._claude_present(_env)
        return self._claude_version(_env, new_path or "claude")

    def _latest_claude_version(self, _env: dict) -> str:
        r = subprocess.run(["npm", "view", "@anthropic-ai/claude-code", "version"],
                           capture_output=True, text=True, timeout=15, env=_env)
        return r.stdout.strip() if r.returncode == 0 else ""

    def _repair_claude_cli(self, _env: dict, reason: str):
        """실행 파일이 없거나 돌지 않을 때 — 장애 복구. 턴 유무·6h 백오프를 기다리지 않는다 (2026-11-17)."""
        ok, why = self._claude_policy.decide_repair()
        if not ok:
            if self._claude_policy.should_log_defer("repair", why):
                logger.error(f"[updater] Claude CLI {reason} — repair deferred ({why})")
            return
        self._claude_policy.note_repair_attempt()
        latest = self._latest_claude_version(_env) or "latest"
        logger.error(f"[updater] Claude CLI {reason} — repairing now with a clean reinstall of @anthropic-ai/claude-code@{latest} "
                     f"(up to {self._install_timeout()}s; turns fail with Errno 2 until this finishes)")
        v = self._clean_reinstall_claude(_env, latest)
        if v:
            logger.info(f"[updater] Claude CLI repaired: {v}")
            self._claude_policy.note_success()
            self._claude_broken_seen = 0
            new_path = self._claude_present(_env)
            if new_path:
                import daemon.globals as g
                g.CLAUDE_CMD = new_path
        else:
            logger.error(f"[updater] Claude CLI still broken after the clean reinstall — next try in "
                         f"{self._claude_policy.repair_retry_sec / 60:.0f}m; manual fix: "
                         "npm rm -g @anthropic-ai/claude-code @anthropic-ai/claude-code-darwin-arm64 && "
                         "npm i -g @anthropic-ai/claude-code")

    def _update_claude_cli(self, _env: dict):
        """Update @anthropic-ai/claude-code to latest — 시점은 CliUpdatePolicy 가 정한다.
        실행 파일이 없거나(Errno 2) 돌지 않으면 업데이트가 아니라 복구다 — _repair_claude_cli (2026-11-17)."""
        current = self._claude_version(_env)
        if not current:
            path = self._claude_present(_env)
            if path is None:
                self._claude_broken_seen = 2          # 파일 자체가 없다 — 바로 복구
                reason = "binary missing (Errno 2)"
            else:
                self._claude_broken_seen += 1         # 파일은 있는데 --version 이 실패 — 두 번 연속이면 복구(일시 오류 제외)
                reason = f"binary at {path} not runnable"
            if self._claude_broken_seen >= 2:
                self._repair_claude_cli(_env, reason)
            return
        self._claude_broken_seen = 0

        latest = self._latest_claude_version(_env)
        if not latest or current == latest:
            self._claude_policy.note_success()
            return

        ok, why = self._claude_policy.decide(latest, self._turns_inflight())
        if not ok:
            self._log_defer("Claude CLI", self._claude_policy, latest, why)
            return

        logger.info(f"[updater] Updating Claude CLI: {current} → {latest} (up to {self._install_timeout()}s)")
        # 플랫폼 패키지는 본체의 optionalDependencies 로 따라온다. 전역에 따로 깔지 않는다(두 벌이면 install.cjs 가 거절).
        self._remove_stray_platform_packages(_env)
        ok, why = self._npm_install_claude(_env, latest)
        if not ok:
            self._claude_policy.note_failure(latest)
            logger.warning(f"[updater] Claude CLI update failed: {why} — next try for {latest} in "
                           f"{self._claude_policy.retry_sec / 3600:.0f}h")
            if self._claude_present(_env) is None:
                # 죽은 npm 이 실행 파일을 없앴다 — 다음 점검(5분)에서 복구 경로가 바로 다시 깐다
                logger.error("[updater] claude binary is missing after the failed update — repair will run on the next check")
            return

        new_path = self._claude_present(_env)
        v = self._claude_version(_env, new_path or "claude")
        if not v:
            # 설치는 됐다는데 실행이 안 됨 — 공식 처방(본체·플랫폼 패키지 둘 다 지우고 재설치)을 한 번만 해 본다
            logger.warning("[updater] Claude CLI binary broken after update — trying the clean reinstall once")
            v = self._clean_reinstall_claude(_env, latest)
            new_path = self._claude_present(_env)
        if v:
            logger.info(f"[updater] Claude CLI updated to {v}")
            self._claude_policy.note_success()
            if new_path:
                import daemon.globals as g
                g.CLAUDE_CMD = new_path
        else:
            self._claude_policy.note_failure(latest)
            logger.error("[updater] Claude CLI still broken after clean reinstall — manual fix needed: "
                         "npm rm -g @anthropic-ai/claude-code @anthropic-ai/claude-code-darwin-arm64 && "
                         "npm i -g @anthropic-ai/claude-code")

    def _update_codex_cli(self, _env: dict):
        """Update @openai/codex to latest, repairing the install if npm left it broken."""
        import shutil
        if not config.get("codex_cli_update_enabled", True):
            return   # 이 기계의 codex 버전은 다른 담당이 고정해 관리한다(좌석 설정과 맞물림) — 올리지도 고치지도 않는다
        if shutil.which("codex", path=_env.get("PATH")) is None and not self._codex_installed_per_npm(_env):
            return   # 이 기계엔 codex 가 없다(설치한 적 없음) — 고칠 것도 올릴 것도 없다. 매 5분 Errno 2 경고를 남기지 않는다 (2026-11-17)
        current = self._codex_version(_env)
        if not current:
            # Installed but not runnable: repair instead of giving up here, otherwise
            # every later cycle bails out at this line and codex stays dead until a
            # human notices. 단, 부팅 직후(boot grace)와 첫 관측에서는 고치지 않는다 — 켜진 직후의
            # 부하로 한 번 삐끗한 것을 '깨짐' 으로 보고 재설치해 기계를 더 누른 일이 있다(2026-11-15 23:56).
            self._codex_broken_seen += 1
            if self._codex_policy.in_boot_grace() or self._codex_broken_seen < 2:
                return
            current = self._repair_codex_cli(_env)
            if not current:
                return
        self._codex_broken_seen = 0

        r = subprocess.run(
            ["npm", "view", "@openai/codex", "version"],
            capture_output=True, text=True, timeout=15, env=_env,
        )
        latest = r.stdout.strip() if r.returncode == 0 else ""
        if not latest or current == latest:
            self._codex_policy.note_success()
            return

        ok, why = self._codex_policy.decide(latest, self._turns_inflight())
        if not ok:
            self._log_defer("Codex CLI", self._codex_policy, latest, why)
            return

        logger.info(f"[updater] Updating Codex CLI: {current} → {latest} (up to {self._install_timeout()}s)")
        import daemon.globals as g
        g.cli_install_inflight = True
        try:
            r = subprocess.run(
                ["npm", "install", "-g", f"@openai/codex@{latest}", "--no-audit", "--no-fund"],
                capture_output=True, text=True, timeout=self._install_timeout(), env=_env,
            )
        except subprocess.TimeoutExpired:
            self._codex_policy.note_failure(latest)
            logger.warning(f"[updater] Codex CLI update timed out after {self._install_timeout()}s (killed) — "
                           f"next try for {latest} in {self._codex_policy.retry_sec / 3600:.0f}h")
            return
        finally:
            g.cli_install_inflight = False
        if r.returncode != 0:
            self._codex_policy.note_failure(latest)
            logger.warning(f"[updater] Codex CLI update failed: {r.stderr.strip()[:200]} — "
                           f"next try for {latest} in {self._codex_policy.retry_sec / 3600:.0f}h")
            return

        updated = self._codex_version(_env) or self._repair_codex_cli(_env)
        if updated:
            logger.info(f"[updater] Codex CLI updated to {updated}")
            self._codex_policy.note_success()
        else:
            self._codex_policy.note_failure(latest)

    def _codex_installed_per_npm(self, _env: dict) -> bool:
        """npm 전역 목록에 @openai/codex 가 있나(실행 파일이 없어도 — 죽은 npm 이 남긴 상태)."""
        try:
            r = subprocess.run(["npm", "ls", "-g", "--depth=0", "@openai/codex"],
                               capture_output=True, text=True, timeout=30, env=_env)
            return "@openai/codex@" in (r.stdout or "")
        except Exception:
            return False

    def _codex_version(self, _env: dict) -> str:
        """Installed codex version, or "" when the binary refuses to run (or is missing)."""
        try:
            r = subprocess.run(
                ["codex", "--version"],
                capture_output=True, text=True, timeout=10, env=_env,
            )
        except FileNotFoundError:
            return ""
        if r.returncode != 0:
            return ""
        # `codex --version` prints e.g. "codex-cli 0.144.3" \u2014 extract the semver token,
        # not the leading "codex-cli" name (that caused every cycle to reinstall).
        m = re.search(r"\d+\.\d+\.\d+", r.stdout)
        return m.group(0) if m else ""

    def _repair_codex_cli(self, _env: dict) -> str:
        """Reinstall a codex whose binary won't run; returns the working version or "".

        npm can drop the platform package (@openai/codex-darwin-arm64) during an upgrade,
        after which every codex turn dies with exit 1 and only a reinstall brings it back
        (2026-09-23, 0.155.1 -> 0.156.0). A failed repair is latched so a machine that
        cannot be fixed does not reinstall on every 5-minute cycle.
        """
        if self._codex_repair_tried:
            return ""
        r = subprocess.run(
            ["npm", "ls", "-g", "--depth=0", "@openai/codex"],
            capture_output=True, text=True, timeout=30, env=_env,
        )
        if "@openai/codex@" not in r.stdout:
            return ""  # codex is not installed here \u2014 nothing to repair
        logger.warning("[updater] Codex CLI installed but not runnable \u2014 reinstalling")
        subprocess.run(
            ["npm", "install", "-g", "@openai/codex@latest"],
            capture_output=True, text=True, timeout=300, env=_env,
        )
        version = self._codex_version(_env)
        if version:
            logger.info(f"[updater] Codex CLI repaired: {version}")
        else:
            self._codex_repair_tried = True
            logger.error("[updater] Codex CLI still broken after reinstall \u2014 "
                         "needs a manual: npm install -g @openai/codex@latest")
        return version

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
