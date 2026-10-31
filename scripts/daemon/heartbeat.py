"""Heartbeat thread: periodically inject due task messages."""

import os
import re
import subprocess
import time
import threading
from datetime import datetime, timedelta, timezone

import daemon.globals as g
from daemon.globals import config, active_projects, active_projects_lock, processed_ids, shutdown_event, logger, CLAUDE_CMD
from daemon.api import api_request, inject_system_message
from daemon.supabase import get_project_dir
from daemon.limits import clear_account_cooldown_if_stale, claude_account_email, seat_config_dir
from daemon import scout
import usage_api


# ─── Claude 사용 한도(/usage) 수집 — 주기(heartbeat) + 온디맨드(main loop) 공용 ───
def fetch_usage_limits() -> dict | None:
    """세션/주간 리밋 % + 리셋 시각.

    1순위: OAuth usage API (usage_api 모듈, 1초 미만, 클로드 세션 미소비).
    폴백: `claude -p /usage` — API 가 실패(토큰 만료·폐기 등)했을 때만. CLI 실행이 토큰을
    갱신하므로 다음 주기부터 API 가 다시 산다. (2026-08-25, 기존엔 15분마다 CLI 만 돌아
    고객 맥 1대당 하루 96세션을 배너 하나에 썼다)"""
    # 계정 자리(claude_seat)가 걸려 있으면 그 자리를 잰다 — 보드가 "Sean 이 지금 어느 계정인지"를 이 값으로 안다
    cdir = seat_config_dir("default")
    limits = usage_api.fetch_oauth_usage(usage_api.local_access_token(cdir))
    if limits:
        limits["account"] = claude_account_email(cdir)
        return limits
    env = dict(os.environ)
    if cdir:
        env["CLAUDE_CONFIG_DIR"] = cdir
    try:
        r = subprocess.run(
            [CLAUDE_CMD, "-p", "/usage"],
            capture_output=True, text=True, timeout=90, env=env,
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
        "account": claude_account_email(cdir),
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
    # 배너만 갱신하고 끝내면 안 된다 — 여기서 얻은 계정/사용률은 채팅을 막고 있는 쿨다운이
    # 아직 유효한지 판정할 수 있는 유일한 신호다(재로그인·오파싱 자가복구).
    clear_account_cooldown_if_stale(limits.get("session_pct"))
    return True


class HeartbeatThread(threading.Thread):
    POLL_INTERVAL = 60
    USAGE_INTERVAL = 15 * 60  # Claude 사용 한도 수집 주기(초)
    HEARTBEAT_MSG = "HEARTBEAT.md를 확인하고 할 일이 있으면 처리해줘. 없으면 '할 일 없음'이라고 답해."
    # 간격 하한(분). 2026-10-12 Sean 지시로 30분 하한 폐지 — 1분은 폴링 주기(60초) 때문이지 정책이 아니다.
    MIN_INTERVAL_MIN = 1
    # 프로젝트가 작업 중일 때 미루는 상한(분). 예전엔 고정 5분이라 1분 간격 태스크도 5분을 기다렸다.
    BUSY_POSTPONE_MAX_MIN = 5
    # 주입했는데 워커가 아직 집지 않은 메시지를 기억하는 시간(초). 넘으면 잊고 다시 주입한다.
    PENDING_TTL_SEC = 10 * 60

    def __init__(self):
        super().__init__(daemon=True, name="heartbeat")
        self._last_usage_fetch = 0.0
        # project → (직전 주입 msg_id, 주입 시각). 짧은 간격에서 큐에 메시지가 쌓이는 것을 막는다.
        self._pending: dict[str, tuple[int, float]] = {}
        # 정찰 턴(scout) — 동시에 도는 정찰 수 상한과, 정찰 중인 프로젝트(같은 태스크를 다시 집지 않게)
        self._scout_sem = threading.BoundedSemaphore(max(1, int(scout.settings().get("max_concurrent") or 2)))
        self._scouting: set[str] = set()
        self._scouting_lock = threading.Lock()

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
        try:
            scout.maybe_cleanup_transcripts()
        except Exception as e:
            logger.warning(f"[scout] cleanup error: {e}")

    def _fetch_due_tasks(self) -> list[dict]:
        api_key = config.get("api_key", "")
        if not api_key:
            return []
        result = api_request(api_key, "GET", "/api/bot/tasks", timeout=10)
        if result and "tasks" in result:
            return result["tasks"]
        return []

    @staticmethod
    def _heartbeat_filename(project: str) -> str:
        """브랜치 세션은 부모와 작업 디렉토리를 공유하므로 파일을 분리한다.
        branch:656 → docs/HEARTBEAT-branch-656.md, 그 외 → docs/HEARTBEAT.md"""
        if project.startswith("branch:"):
            return f"HEARTBEAT-branch-{project.split(':')[1]}.md"
        return "HEARTBEAT.md"

    def _check_heartbeat_md(self, project: str) -> str | None:
        """Check if the task's HEARTBEAT file exists. Returns warning message or None."""
        try:
            project_dir = get_project_dir(project)
            if not project_dir:
                return "프로젝트 디렉토리를 찾을 수 없음"
            filename = self._heartbeat_filename(project)
            heartbeat_path = os.path.join(project_dir, "docs", filename)
            if not os.path.exists(heartbeat_path):
                return f"{filename} 파일 없음 — 태스크가 실행되어도 할 일이 정의되지 않음"
            content = open(heartbeat_path).read().strip()
            if not content:
                return f"{filename}가 비어 있음"
            if "[ ]" not in content:
                return f"{filename}에 미완료 항목 없음"
            return None
        except Exception:
            return None

    @classmethod
    def _interval_min(cls, task: dict) -> int:
        """태스크 간격(분). 비정상 값은 30, 하한은 MIN_INTERVAL_MIN."""
        try:
            iv = int(task.get("interval_min") or 30)
        except (TypeError, ValueError):
            iv = 30
        return max(cls.MIN_INTERVAL_MIN, iv)

    @classmethod
    def _busy_postpone_min(cls, interval: int) -> int:
        """작업 중일 때 미루는 분. 간격보다 길게 미루지 않는다(상한 5분) —
        1분 간격 태스크는 1분 뒤 다시 보므로 긴 단계가 끝난 직후 다음 단계가 뜬다."""
        return max(cls.MIN_INTERVAL_MIN, min(interval, cls.BUSY_POSTPONE_MAX_MIN))

    def _previous_wakeup_queued(self, project: str) -> bool:
        """직전에 주입한 하트비트 메시지를 워커가 아직 집지 않았으면 True.
        (워커 동시 처리 슬롯이 꽉 찼을 때 1분 간격 태스크가 메시지를 쌓아 연속 실행되는 것을 막는다.
        워커는 메시지를 집는 순간 processed_ids 에 넣는다 — 그 뒤는 active_projects 가 맡는다.)"""
        ent = self._pending.get(project)
        if not ent:
            return False
        msg_id, injected_at = ent
        if msg_id in processed_ids or time.time() - injected_at > self.PENDING_TTL_SEC:
            self._pending.pop(project, None)
            return False
        return True

    def _process_task(self, task: dict):
        project = task["project"]
        interval = self._interval_min(task)

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
            # 창 밖 — 다음 창이 열리는 시각으로 미루고 한 줄 남긴다. 전에는 로그 없이 돌아가(매 폴링마다
            # due 로 다시 떴다) 하루를 건너뛴 날이 어디에도 남지 않았다 (2026-10-21 evolution 10-20 결번 진단).
            nxt = self._next_window_start(task["active_hours"])
            if nxt:
                self._update_task(task["id"], {"next_run_at": nxt.isoformat()})
                logger.info(f"[heartbeat] {project} outside active hours {task['active_hours']} → skipped, "
                            f"next window {nxt.astimezone().strftime('%m-%d %H:%M')}")
            return

        if task.get("max_runs") and task["run_count"] >= task["max_runs"]:
            self._update_task(task["id"], {"status": "done"})
            logger.info(f"[heartbeat] {project} max_runs reached → done")
            return

        with active_projects_lock:
            busy = project in active_projects
        if busy:
            delay = self._busy_postpone_min(interval)
            next_run = datetime.now(timezone.utc) + timedelta(minutes=delay)
            self._update_task(task["id"], {"next_run_at": next_run.isoformat()})
            logger.info(f"[heartbeat] {project} busy → postponed {delay}m")
            return

        if self._previous_wakeup_queued(project):
            next_run = datetime.now(timezone.utc) + timedelta(minutes=self.MIN_INTERVAL_MIN)
            self._update_task(task["id"], {"next_run_at": next_run.isoformat()})
            logger.info(f"[heartbeat] {project} previous wake-up still queued → postponed {self.MIN_INTERVAL_MIN}m")
            return

        # Skip inject if no valid HEARTBEAT.md
        if warning and "파일 없음" in warning:
            next_run = self._next_run_from_schedule(task, interval)
            self._update_task(task["id"], {
                "next_run_at": next_run.isoformat(),
            })
            logger.info(f"[heartbeat] {project} skipped (no HEARTBEAT.md)")
            return

        hb_name = self._heartbeat_filename(project)
        msg = self.HEARTBEAT_MSG.replace("HEARTBEAT.md", hb_name)

        # ── 정찰 턴 (2026-10-31, Sean 승인) — 본 세션을 깨우기 전에 가벼운 새 세션이 「지금 할 일이 있나」를 판정 ──
        # 빈 회차가 턴의 44%였고 문맥 45만 세션은 「할 일 없음」 한 줄에 $5 였다. 본 세션은 손대지 않는다(지식 보존).
        # force_every 회차마다 한 번은 정찰 없이 본 세션을 깨워, 상태를 세션 안에만 둔 담당의 일이 영영 미뤄지지 않게 한다.
        if scout.enabled() and not (warning and "비어 있음" in warning):
            with self._scouting_lock:
                already = project in self._scouting
            if already:
                logger.info(f"[scout] {project} still scouting → skip this tick")
                return
            forced = scout.should_force_main(task.get("run_count") or 0, scout.settings().get("force_every"))
            if not forced and scout.account_in_cooldown(project):
                # 계정 한도 쿨다운 중 — 정찰도 본 세션도 못 돈다. busy 처럼 조금 미룬다(간격만큼 넘기면 일일 태스크가 하루를 건너뛴다)
                delay = self._busy_postpone_min(interval)
                self._update_task(task["id"], {"next_run_at": (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat()})
                logger.info(f"[scout] {project} account cooldown → postponed {delay}m")
                return
            if not forced:
                hb_text = self._read_heartbeat_text(project, hb_name)
                if hb_text is not None:
                    # 스케줄을 먼저 넘긴다 — 정찰이 도는 동안 다음 폴링이 같은 태스크를 또 집지 않게
                    next_run = self._next_run_from_schedule(task, interval)
                    self._update_task(task["id"], {
                        "next_run_at": next_run.isoformat(),
                        "run_count": task["run_count"] + 1,
                    })
                    with self._scouting_lock:
                        self._scouting.add(project)
                    threading.Thread(target=self._scout_then_inject, args=(task, msg, hb_name, hb_text, interval),
                                     daemon=True, name=f"scout-{project}").start()
                    return
            else:
                logger.info(f"[scout] {project} run#{task['run_count'] + 1} forced main session (every {scout.settings().get('force_every')})")

        msg_id, ts = inject_system_message(project, msg, prefix="[heartbeat]")
        if not msg_id:
            logger.warning(f"[heartbeat] {project} inject failed")
            return

        self._pending[project] = (msg_id, time.time())
        next_run = self._next_run_from_schedule(task, interval)
        self._update_task(task["id"], {
            "next_run_at": next_run.isoformat(),
            "run_count": task["run_count"] + 1,
        })
        logger.info(f"[heartbeat] {project} woke up (run #{task['run_count'] + 1}, next in {interval}m)")

    def _read_heartbeat_text(self, project: str, hb_name: str) -> str | None:
        """HEARTBEAT 파일 본문(정찰 입력). 없거나 비면 None → 정찰하지 않고 기존 경로로."""
        try:
            path = os.path.join(get_project_dir(project), "docs", hb_name)
            text = open(path, encoding="utf-8", errors="replace").read()
            return text if text.strip() else None
        except Exception:
            return None

    def _scout_then_inject(self, task: dict, msg: str, hb_name: str, hb_text: str, interval: int):
        """정찰 스레드: 판정이 「있음」이면 본 세션에 하트비트를 주입한다(정찰 결과를 덧붙여 본 세션이 점검을 되풀이하지 않게)."""
        project = task["project"]
        run_no = int(task.get("run_count") or 0) + 1
        try:
            with self._scout_sem:
                if shutdown_event.is_set():
                    return
                verdict = scout.run_scout(project, task, hb_name, hb_text)
            if not verdict.get("work"):
                return  # 조용히 — 채팅에 아무것도 남기지 않는다 (기록은 ledger·로그)
            text = msg
            if verdict.get("reason"):
                text = f"{msg}\n\n(정찰 결과: {verdict['reason']})"
            msg_id, _ts = inject_system_message(project, text, prefix="[heartbeat]")
            if not msg_id:
                logger.warning(f"[heartbeat] {project} inject failed (after scout)")
                return
            self._pending[project] = (msg_id, time.time())
            logger.info(f"[heartbeat] {project} woke up after scout (run #{run_no}, next in {interval}m)")
        except Exception as e:
            logger.error(f"[scout] {project} thread error: {e}")
        finally:
            with self._scouting_lock:
                self._scouting.discard(project)

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

    @staticmethod
    def _parse_ts(value) -> datetime | None:
        """API 의 ISO 시각 문자열 → aware UTC datetime. 못 읽으면 None."""
        if not value or not isinstance(value, str):
            return None
        try:
            s = value.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    @classmethod
    def _next_run_from_schedule(cls, task: dict, interval: int, now: datetime | None = None) -> datetime:
        """다음 실행 시각 = **예정 시각(next_run_at) + interval**. 이미 지난 칸은 건너뛰고 미래의 첫 칸을
        잡는다(밀린 회차를 연달아 돌리지 않는다). 예정 시각이 없으면 지금 기준.

        2026-10-21 수정 — 전에는 '깬 시각 + interval' 이라 폴링 지연(수 초~1분)이 매일 쌓여 일일 태스크가
        하루 약 30초씩 밀렸고, 좁은 active_hours 창을 쓰는 태스크는 창을 넘긴 날 조용히 하루를 건너뛰었다
        (evolution 05:00–06:00: 10-19 05:59:26 → 10-20 결번 → 10-21 05:00:55)."""
        now = now or datetime.now(timezone.utc)
        step = timedelta(minutes=interval)
        base = cls._parse_ts(task.get("next_run_at"))
        if base is None or base > now:
            base = now
        nxt = base + step
        if nxt <= now:
            nxt += step * (int((now - nxt) // step) + 1)
            # 한 칸 넘게 밀려 따라잡는 경우 코앞의 칸은 건너뛴다 — 방금 돌았는데 곧바로 또 도는 것을 막는다
            if nxt - now < step / 2:
                nxt += step
        return nxt

    @staticmethod
    def _next_window_start(hours_str: str) -> datetime | None:
        """active_hours 창 밖일 때 다음으로 창이 열리는 시각(UTC). _in_active_hours 와 같은 로컬 시각 기준.
        파싱 실패면 None (호출부는 예전처럼 그냥 돌아간다)."""
        try:
            start_str, _ = hours_str.split("-")
            now = datetime.now()
            start = now.replace(hour=int(start_str[:2]), minute=int(start_str[3:]), second=0, microsecond=0)
            if start <= now:
                start += timedelta(days=1)
            return start.astimezone(timezone.utc)
        except Exception:
            return None
