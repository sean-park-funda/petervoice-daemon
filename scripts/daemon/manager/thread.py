"""Autonomous manager thread: scout, suggest, execute cycles."""

import time
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import daemon.globals as g
from daemon.globals import (
    WORKFLOWS_DIR, MANAGER_STATE_PATH,
    config, sessions_lock, active_projects, active_projects_lock,
    shutdown_event, manager_wake_event, logger,
)
from daemon.utils import _read_json, _write_json
from daemon.api import api_request, inject_system_message
from daemon.supabase import _fetch_recent_conversation
from daemon.claude_runner import run_claude


class ManagerThread(threading.Thread):
    MANAGER_PREFIX = "[매니저]"

    def __init__(self):
        super().__init__(daemon=True, name="manager")
        self.api_key = config["api_key"]
        self.mgr_config = config.get("manager", {})
        self.project_id = self.mgr_config.get("project_id", "manager")
        self.max_wait_sec = self.mgr_config.get("max_wait_sec", 600)
        self.poll_interval_sec = self.mgr_config.get("poll_interval_sec", 5)
        self.suggestion_wait_min = self.mgr_config.get("suggestion_wait_min", 60)
        self.workflows = self._load_workflows()
        self.state = self._load_state()

    # ── Workflow loader ──

    @staticmethod
    def _parse_workflow(path: Path) -> dict | None:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"[manager] Failed to read workflow {path}: {e}")
            return None

        config_data = {}
        prompt_body = text

        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    config_data = yaml.safe_load(parts[1]) or {}
                    if not isinstance(config_data, dict):
                        logger.error(f"[manager] Workflow front matter is not a map: {path}")
                        return None
                    prompt_body = parts[2].strip()
                except Exception as e:
                    logger.error(f"[manager] YAML parse error in {path}: {e}")
                    return None

        project = config_data.get("project", path.stem)
        return {
            "project": project,
            "description": config_data.get("description", project),
            "schedule": config_data.get("schedule", {}),
            "scout": config_data.get("scout", {}),
            "agent": config_data.get("agent", {}),
            "retry": config_data.get("retry", {}),
            "prompt_template": prompt_body,
            "path": str(path),
        }

    def _load_workflows(self) -> dict:
        workflows = {}
        if not WORKFLOWS_DIR.exists():
            logger.info("[manager] No workflows/ directory, falling back to config.projects")
            return workflows

        for wf_path in sorted(WORKFLOWS_DIR.glob("*.md")):
            wf = self._parse_workflow(wf_path)
            if wf:
                workflows[wf["project"]] = wf
                logger.info(f"[manager] Loaded workflow: {wf['project']} ({wf['description']})")

        if workflows:
            logger.info(f"[manager] {len(workflows)} workflows loaded")
        return workflows

    def _reload_workflows(self):
        new_wf = self._load_workflows()
        if new_wf:
            self.workflows = new_wf

    def _get_workflow(self, project: str) -> dict:
        if project in self.workflows:
            return self.workflows[project]
        return {
            "project": project, "description": project,
            "schedule": {}, "scout": {}, "agent": {}, "retry": {},
            "prompt_template": "", "path": None,
        }

    # ── State persistence ──

    def _load_state(self) -> dict:
        defaults = {
            "last_run": None, "run_count": 0, "current_phase": "idle",
            "next_project_idx": 0, "hints": [],
            "retry_queue": {}, "task_queue": [],
        }
        state = _read_json(MANAGER_STATE_PATH, defaults)
        for k, v in defaults.items():
            state.setdefault(k, v)
        return state

    def _save_state(self):
        _write_json(MANAGER_STATE_PATH, self.state)

    # ── Helpers ──

    def _is_quiet_hours(self) -> bool:
        quiet = self.mgr_config.get("quiet_hours", [0, 7])
        if not quiet or len(quiet) != 2:
            return False
        start, end = quiet
        hour = datetime.now().hour
        if start < end:
            return start <= hour < end
        else:
            return hour >= start or hour < end

    def _get_target_projects(self) -> list[str]:
        # config.projects가 명시적으로 빈 배열이면 자율 순회 비활성화 (workflows도 무시)
        explicit = self.mgr_config.get("projects")
        if isinstance(explicit, list) and len(explicit) == 0:
            return []
        if self.workflows:
            return [p for p in self.workflows if p != self.project_id]
        if explicit:
            return [p for p in explicit if p != self.project_id]
        with sessions_lock:
            project_names = set()
            for key in g.sessions:
                proj = key.split(":")[0]
                if proj != self.project_id:
                    project_names.add(proj)
        return sorted(project_names)

    def _is_project_busy(self, project: str) -> bool:
        with active_projects_lock:
            return project in active_projects

    def _has_active_session(self, project: str) -> bool:
        with sessions_lock:
            has_session = any(key.startswith(f"{project}:") for key in g.sessions)
        return has_session

    # ── Message injection via API ──

    def _inject_message(self, project: str, text: str) -> tuple[int | None, str]:
        msg_id, ts = inject_system_message(project, text, prefix=self.MANAGER_PREFIX)
        if msg_id:
            logger.info(f"[manager] Injected msg #{msg_id} → {project}: {text[:60]}")
        else:
            logger.error(f"[manager] Failed to inject message for {project}")
        return msg_id, ts

    def _wait_for_response(self, project: str, injected_msg_id: int, inject_ts: str) -> str | None:
        deadline = time.time() + self.max_wait_sec
        found_texts = None

        while time.time() < deadline and not shutdown_event.is_set():
            result = api_request(self.api_key, "GET",
                f"/api/bot/messages/poll"
                f"?project={urllib.parse.quote(project)}"
                f"&type=bot&after_ts={urllib.parse.quote(inject_ts)}"
                f"&reply_to={injected_msg_id}&limit=10&order=asc",
                timeout=10)

            if result and result.get("messages"):
                texts = []
                for r in result["messages"]:
                    t = (r.get("text") or "").strip()
                    if t and not t.startswith("\U0001f527"):
                        texts.append(t)
                if texts:
                    if not self._is_project_busy(project):
                        return "\n\n".join(texts)
                    found_texts = texts

            shutdown_event.wait(self.poll_interval_sec)

        if found_texts:
            logger.info(f"[manager] Returning partial response from {project} ({len(found_texts)} chunks)")
            return "\n\n".join(found_texts)

        logger.warning(f"[manager] Timeout waiting for response from {project}")
        return None

    def _inject_and_wait(self, project: str, text: str) -> str | None:
        msg_id, ts = self._inject_message(project, text)
        if msg_id is None:
            return None
        wf = self._get_workflow(project)
        orig_wait = self.max_wait_sec
        self.max_wait_sec = wf.get("scout", {}).get("max_wait_sec", orig_wait)
        result = self._wait_for_response(project, msg_id, ts)
        self.max_wait_sec = orig_wait
        return result

    # ── Manager's own Claude session ──

    INTERNAL_PROJECT_ID = "_manager_internal"

    def _ask_manager(self, prompt: str) -> str:
        response, sid, tool_lines = run_claude(prompt, self.INTERNAL_PROJECT_ID)
        return response

    # ── Scout ──

    def _scout_project(self, project: str) -> str | None:
        logger.info(f"[manager] Scouting {project}")
        wf = self._get_workflow(project)

        hints = [h for h in self.state.get("hints", []) if h.get("project") == project]
        hint_ctx = ""
        if hints:
            hint_texts = "; ".join(h["text"] for h in hints)
            hint_ctx = f"\n유저 아이디어: {hint_texts}"

        focus = wf.get("scout", {}).get("focus", [])
        focus_ctx = ", ".join(focus) if focus else "최근 변경사항, 미완성 작업, 개선 가능한 점"

        prompt_extra = ""
        if wf.get("prompt_template"):
            prompt_extra = f"\n프로젝트 점검 가이드:\n{wf['prompt_template']}\n"

        question = self._ask_manager(
            f"프로젝트 '{project}' ({wf['description']})를 점검한다.{hint_ctx}{prompt_extra}\n"
            f"너는 이 프로젝트의 매니저다. 단순 상태 확인이 아니라, "
            f"이 서비스를 더 성장시키고 발전시킬 방법을 찾아야 한다.\n"
            f"다음 중 하나를 질문해라:\n"
            f"- 버그나 에러가 있는지\n"
            f"- 미완성 기능의 진행 상태\n"
            f"- 유저 경험/전환율을 개선할 포인트\n"
            f"- 경쟁사 대비 부족한 기능\n"
            f"- SEO, 마케팅, 성장을 위해 추가할 것\n"
            f"집중 영역: {focus_ctx}.\n"
            f"이전 사이클에서 같은 질문을 반복하지 마라. 매번 다른 관점에서 질문해라.\n"
            f"질문만 출력해라."
        )

        logger.info(f"[manager] [{project}] scout: {question[:80]}")
        response = self._inject_and_wait(project, question)
        if response is None:
            logger.warning(f"[manager] [{project}] no response")
        return response

    # ── Suggestion ──

    def _generate_suggestion(self, project: str, scout_response: str) -> str:
        hints = [h for h in self.state.get("hints", []) if h.get("project") == project]
        hint_ctx = ""
        if hints:
            hint_texts = "; ".join(h["text"] for h in hints)
            hint_ctx = f"\n유저가 준 아이디어: {hint_texts}"

        suggestion = self._ask_manager(
            f"[{project}] 프로젝트 응답:\n{scout_response}\n"
            f"{hint_ctx}\n---\n"
            f"유저에게 보낼 제안 하나를 만들어라.\n"
            f"형식: '션, {{프로젝트명}}에 {{제안 내용}}. 해볼까요?'\n"
            f"2-3문장으로 짧게. 제안만 출력해라."
        )
        return suggestion

    # ── User interaction ──

    def _post_to_user(self, text: str):
        api_request(self.api_key, "POST", "/api/bot/reply", {
            "text": text, "project": self.project_id, "is_final": True,
        })

    def _poll_user_response(self, after_ts: str) -> str | None:
        result = api_request(self.api_key, "GET",
            f"/api/bot/messages/poll"
            f"?project={urllib.parse.quote(self.project_id)}"
            f"&type=user&after_ts={urllib.parse.quote(after_ts)}"
            f"&limit=1&order=desc",
            timeout=10)

        if result and result.get("messages"):
            text = (result["messages"][0].get("text") or "").strip()
            if text and not text.startswith(self.MANAGER_PREFIX):
                return text
        return None

    def _send_suggestion_and_wait(self, suggestion: str) -> str | None:
        self._post_to_user(suggestion)

        sent_ts = datetime.now(timezone.utc).isoformat()
        self.state["current_phase"] = "awaiting_response"
        self._save_state()

        deadline = time.time() + self.suggestion_wait_min * 60
        while time.time() < deadline and not shutdown_event.is_set():
            resp = self._poll_user_response(sent_ts)
            if resp:
                logger.info(f"[manager] User response: {resp[:80]}")
                return resp
            shutdown_event.wait(30)

        logger.info("[manager] Suggestion response timeout")
        return None

    @staticmethod
    def _is_approval(text: str) -> bool:
        t = text.strip().lower()
        approvals = {"응", "어", "해", "좋아", "ㅇ", "ㅇㅇ", "승인", "go", "해줘",
                      "ㄱ", "ㄱㄱ", "해봐", "진행", "ok", "yes", "네", "그래"}
        return t in approvals or t.startswith("응 ") or t.startswith("해 ")

    # ── Execution ──

    def _execute_directive(self, project: str, directive: str) -> str | None:
        logger.info(f"[manager] Executing on {project}: {directive[:80]}")
        result = self._inject_and_wait(project, directive)
        if result:
            summary = self._ask_manager(
                f"[{project}]에 지시를 보냈고 결과가 왔다:\n{result[:2000]}\n\n"
                f"유저에게 한 줄로 결과를 알려줘. '완료: ...' 형식으로."
            )
            result_preview = result[:500] + ('...' if len(result) > 500 else '')
            self._post_to_user(
                f"**→ {project}**\n\n"
                f"**지시:**\n{directive}\n\n"
                f"**결과 요약:** {summary}\n\n"
                f"**응답 앞부분:**\n{result_preview}"
            )
        else:
            self._post_to_user(f"{project} 작업 응답이 없었어요.")
        return result

    def _needs_continuation(self, project: str, result: str | None) -> bool:
        if not result:
            return False
        judgment = self._ask_manager(
            f"[{project}] 작업 결과:\n{result[:2000]}\n\n"
            f"이 작업이 완료됐는지 판단해라. "
            f"완료됐으면 'DONE', 아직 할 일이 남았으면 'CONTINUE'만 출력해라."
        )
        return "CONTINUE" in judgment.upper()

    def _execute_with_continuation(self, project: str, directive: str,
                                     suggestion: str, scout_response: str):
        wf = self._get_workflow(project)
        max_turns = wf.get("agent", {}).get("max_turns", 1)

        result = self._execute_directive(project, directive)
        turn = 1

        while turn < max_turns and self._needs_continuation(project, result):
            turn += 1
            logger.info(f"[manager] Continuation turn {turn}/{max_turns} for {project}")

            if self._is_project_busy(project):
                logger.info(f"[manager] {project} busy during continuation, stopping")
                break

            continuation = self._ask_manager(
                f"[{project}] 작업 {turn-1}턴 결과:\n{result[:2000]}\n\n"
                f"원래 제안: {suggestion}\n\n"
                f"아직 완료되지 않았다. 다음에 할 후속 지시를 만들어라. "
                f"지시만 출력해라."
            )
            result = self._execute_directive(project, continuation)

        if turn >= max_turns:
            logger.info(f"[manager] Max turns ({max_turns}) reached for {project}")

    # ── Hint management ──

    def _remove_hints_for(self, project: str):
        self.state["hints"] = [
            h for h in self.state.get("hints", []) if h.get("project") != project
        ]
        self._save_state()

    # ── Retry queue ──

    def _schedule_retry(self, project: str, error: str, is_continuation: bool = False):
        retry_queue = self.state.get("retry_queue", {})
        existing = retry_queue.get(project, {})
        attempt = existing.get("attempt", 0) + 1

        wf = self._get_workflow(project)
        max_backoff = wf.get("retry", {}).get("max_backoff_sec", 300)

        if is_continuation:
            delay = 1
        else:
            delay = min(10 * (2 ** (attempt - 1)), max_backoff)

        due_at = time.time() + delay
        retry_queue[project] = {
            "attempt": attempt, "due_at": due_at,
            "error": error, "scheduled_at": datetime.now().isoformat(),
        }
        self.state["retry_queue"] = retry_queue
        self._save_state()
        logger.info(f"[manager] Retry scheduled: {project} attempt={attempt} delay={delay}s error={error}")

    def _pop_due_retries(self) -> list[str]:
        retry_queue = self.state.get("retry_queue", {})
        now = time.time()
        return [p for p, r in retry_queue.items() if r.get("due_at", 0) <= now]

    def _clear_retry(self, project: str):
        retry_queue = self.state.get("retry_queue", {})
        retry_queue.pop(project, None)
        self.state["retry_queue"] = retry_queue
        self._save_state()

    # ── Stall detection ──

    def _check_stall(self, project: str, start_time: float) -> bool:
        wf = self._get_workflow(project)
        stall_timeout = wf.get("agent", {}).get("stall_timeout_sec", 300)
        if stall_timeout <= 0:
            return False
        elapsed = time.time() - start_time
        if elapsed > stall_timeout:
            logger.warning(f"[manager] Stall detected: {project} ({elapsed:.0f}s > {stall_timeout}s)")
            return True
        return False

    # ── Deep task queue ──

    def enqueue_deep_task(self, project: str, task: str, context: str = ""):
        task_entry = {
            "project": project, "task": task,
            "context": context, "queued_at": datetime.now().isoformat(),
        }
        self.state.setdefault("task_queue", []).append(task_entry)
        self._save_state()
        logger.info(f"[manager] Deep task queued: {project} → {task[:80]}")
        manager_wake_event.set()

    def _pop_deep_task(self) -> dict | None:
        queue = self.state.get("task_queue", [])
        if not queue:
            return None
        task = queue.pop(0)
        self.state["task_queue"] = queue
        self._save_state()
        return task

    def _run_deep_task(self, task_entry: dict):
        project = task_entry["project"]
        task_text = task_entry["task"]
        context = task_entry.get("context", "")
        wf = self._get_workflow(project)
        max_turns = wf.get("agent", {}).get("max_turns", 10)

        logger.info(f"[manager] ═══ Deep task: {project} ═══")
        logger.info(f"[manager] Task: {task_text}")
        if context:
            logger.info(f"[manager] Context: {len(context)} chars from recent conversation")

        self.state["current_phase"] = f"deep_task:{project}"
        self._save_state()

        context_block = ""
        if context:
            context_block = (
                f"\n\n## 프로젝트 최근 대화 (맥락)\n"
                f"{context}\n"
                f"---\n"
            )

        step = self._ask_manager(
            f"유저가 '{project}' 프로젝트에 다음 작업을 요청했다:\n\n"
            f"{task_text}"
            f"{context_block}\n\n"
            f"위 대화 맥락을 참고하여, 프로젝트 Claude가 1턴(약 2분)에 완료할 수 있는 첫 번째 단계를 구체적 지시로 만들어라.\n"
            f"지시만 출력해라."
        )

        turn = 0
        while turn < max_turns:
            turn += 1
            logger.info(f"[manager] Deep task turn {turn}/{max_turns}: {project}")

            if self._is_project_busy(project):
                logger.info(f"[manager] {project} busy, waiting 30s...")
                shutdown_event.wait(30)
                if shutdown_event.is_set():
                    break
                continue

            turn_start = time.time()
            result = self._inject_and_wait(project, step)

            if not result:
                if self._check_stall(project, turn_start):
                    self._post_to_user(f"[{project}] 작업 중 응답 없음 (turn {turn}). 나중에 재시도합니다.")
                    self._schedule_retry(project, "deep task stalled")
                else:
                    self._post_to_user(f"[{project}] 작업 중 응답 없음 (turn {turn}).")
                break

            progress = self._ask_manager(
                f"[{project}] 작업 진행 보고 (turn {turn}/{max_turns}).\n\n"
                f"지시: {step[:500]}\n\n"
                f"결과: {result[:2000]}\n\n"
                f"유저에게 한 줄로 진행상황을 알려줘. 형식: '[{turn}/{max_turns}] ...'")
            result_preview = result[:500] + ('...' if len(result) > 500 else '')
            self._post_to_user(
                f"**[Turn {turn}/{max_turns}] → {project}**\n\n"
                f"**지시:**\n{step}\n\n"
                f"**결과 요약:** {progress}\n\n"
                f"**응답 앞부분:**\n{result_preview}"
            )

            judgment = self._ask_manager(
                f"[{project}] 원래 작업: {task_text}\n\n"
                f"지금까지 {turn}턴 실행했고, 마지막 결과:\n{result[:2000]}\n\n"
                f"이 작업이 완료됐으면 'DONE'만 출력해라.\n"
                f"아직 할 일이 남았으면 다음 단계의 구체적 지시를 출력해라 (DONE이라는 단어 없이)."
            )

            if "DONE" in judgment.upper().split():
                logger.info(f"[manager] Deep task completed in {turn} turns: {project}")
                summary = self._ask_manager(
                    f"[{project}] 작업 완료 보고.\n"
                    f"원래 작업: {task_text}\n"
                    f"총 {turn}턴 실행.\n"
                    f"마지막 결과: {result[:2000]}\n\n"
                    f"유저에게 완료 보고를 해라. 형식: '완료: ...'"
                )
                self._post_to_user(summary)
                break

            step = judgment

        else:
            logger.info(f"[manager] Deep task max turns ({max_turns}) reached: {project}")
            self._post_to_user(
                f"[{project}] 작업이 {max_turns}턴 내에 완료되지 않았습니다.\n"
                f"→ {task_text[:100]}\n"
                f"필요하면 /do 로 다시 시작해주세요."
            )

        self.state["current_phase"] = "idle"
        self._save_state()

    # ── Main cycle ──

    def _run_cycle(self):
        cycle_start = time.time()
        cycle_num = self.state.get("run_count", 0) + 1

        deep_task = self._pop_deep_task()
        if deep_task:
            self._run_deep_task(deep_task)
            self.state["last_run"] = datetime.now().isoformat()
            self.state["run_count"] = cycle_num
            self._save_state()
            return

        due_retries = self._pop_due_retries()
        for retry_project in due_retries:
            retry_entry = self.state.get("retry_queue", {}).get(retry_project, {})
            logger.info(f"[manager] Processing retry: {retry_project} attempt={retry_entry.get('attempt', '?')}")
            if not self._is_project_busy(retry_project):
                self._clear_retry(retry_project)
                self._run_project_cycle(retry_project, cycle_num, cycle_start,
                                        attempt=retry_entry.get("attempt", 1))
                return

        projects = self._get_target_projects()
        if not projects:
            logger.info("[manager] No projects to check")
            return

        idx = self.state.get("next_project_idx", 0) % len(projects)
        project = projects[idx]
        self.state["next_project_idx"] = (idx + 1) % len(projects)
        self._save_state()

        self._run_project_cycle(project, cycle_num, cycle_start)

    def _run_project_cycle(self, project: str, cycle_num: int, cycle_start: float,
                            attempt: int | None = None):
        logger.info(f"[manager] ═══ Cycle #{cycle_num}: {project}"
                     f"{f' (retry #{attempt})' if attempt else ''} ═══")

        if self._is_project_busy(project):
            logger.info(f"[manager] {project} is busy, skipping")
            if attempt:
                self._schedule_retry(project, "project busy", is_continuation=True)
            return

        # Phase 1: Scout
        self.state["current_phase"] = "scouting"
        self._save_state()
        scout_start = time.time()
        response = self._scout_project(project)
        if not response:
            if self._check_stall(project, scout_start):
                self._schedule_retry(project, "scout stalled")
            self.state["current_phase"] = "idle"
            self.state["last_run"] = datetime.now().isoformat()
            self.state["run_count"] = cycle_num
            self._save_state()
            return

        # Phase 2: Triage
        self.state["current_phase"] = "triaging"
        self._save_state()

        wf = self._get_workflow(project)
        auto_fix = wf.get("scout", {}).get("auto_fix", False)
        autonomous = wf.get("agent", {}).get("autonomous", False)

        triage = self._ask_manager(
            f"[{project}] 프로젝트 점검 결과:\n{response[:2000]}\n\n"
            f"너는 이 프로젝트의 매니저다. 점검 결과를 보고 다음 행동을 결정해라:\n"
            f"1. 긴급 버그/에러 수정 → 'AUTO: {{구체적 지시}}'\n"
            f"2. 성장/개선/새 기능 작업 → 'ASK: {{구체적 제안}}'\n"
            f"3. 정말로 아무것도 할 일이 없을 때만 → 'SKIP'\n\n"
            f"중요: '변동 없음'이나 '커밋 없음'은 SKIP 사유가 아니다. "
            f"커밋이 없으면 오히려 새 작업을 시작할 기회다. "
            f"이 서비스가 더 성장하려면 무엇이 필요한지 생각하고, "
            f"웹 조사나 경쟁사 분석 등을 통해 발전적 제안을 해라.\n"
            f"반드시 AUTO:, ASK:, SKIP 중 하나로 시작해라."
        )
        logger.info(f"[manager] Triage: {triage[:100]}")

        triage_upper = triage.strip().upper()

        if triage_upper.startswith("SKIP"):
            logger.info(f"[manager] {project}: nothing to do")
            self._clear_retry(project)

        elif triage_upper.startswith("AUTO:") and (auto_fix or autonomous):
            directive = triage.split(":", 1)[1].strip() if ":" in triage else triage[5:].strip()
            logger.info(f"[manager] Auto-fix: {project}: {directive[:80]}")
            self.state["current_phase"] = "auto_fixing"
            self._save_state()
            self._execute_with_continuation(project, directive, directive, response)
            self._remove_hints_for(project)
            self._clear_retry(project)

        elif autonomous and triage_upper.startswith("ASK:"):
            suggestion_hint = triage.split(":", 1)[1].strip() if ":" in triage else triage[4:].strip()
            self.state["current_phase"] = "autonomous_executing"
            self._save_state()
            suggestion = self._generate_suggestion(project, response)
            logger.info(f"[manager] Autonomous auto-approved: {suggestion[:100]}")
            self._post_to_user(f"[자율실행] {suggestion}")
            directive = self._ask_manager(
                f"자율 모드로 실행한다. 제안:\n{suggestion}\n\n"
                f"프로젝트 응답:\n{response[:2000]}\n\n"
                f"'{project}' 프로젝트에게 보낼 구체적 작업 지시를 만들어라. "
                f"지시만 출력해라."
            )
            self._execute_with_continuation(project, directive, suggestion, response)
            self._remove_hints_for(project)
            self._clear_retry(project)

        else:
            if triage_upper.startswith("AUTO:"):
                suggestion_hint = triage.split(":", 1)[1].strip() if ":" in triage else triage[5:].strip()
            elif triage_upper.startswith("ASK:"):
                suggestion_hint = triage.split(":", 1)[1].strip() if ":" in triage else triage[4:].strip()
            else:
                suggestion_hint = triage

            self.state["current_phase"] = "suggesting"
            self._save_state()
            suggestion = self._generate_suggestion(project, response)
            logger.info(f"[manager] Suggestion: {suggestion[:100]}")

            if autonomous:
                self.state["current_phase"] = "autonomous_executing"
                self._save_state()
                logger.info(f"[manager] Autonomous (fallback) auto-approved: {suggestion[:100]}")
                self._post_to_user(f"[자율실행] {suggestion}")
                directive = self._ask_manager(
                    f"자율 모드로 실행한다. 제안:\n{suggestion}\n\n"
                    f"프로젝트 응답:\n{response[:2000]}\n\n"
                    f"'{project}' 프로젝트에게 보낼 구체적 작업 지시를 만들어라. "
                    f"도구를 사용하지 말고 지시 텍스트만 출력해라."
                )
                self._execute_with_continuation(project, directive, suggestion, response)
                self._remove_hints_for(project)
                self._clear_retry(project)
            else:
                user_resp = self._send_suggestion_and_wait(suggestion)

                if user_resp and self._is_approval(user_resp):
                    self.state["current_phase"] = "executing"
                    self._save_state()
                    logger.info(f"[manager] Approved! Creating directive for {project}")
                    directive = self._ask_manager(
                        f"유저가 승인했다. 아까 제안:\n{suggestion}\n\n"
                        f"프로젝트 응답:\n{response[:2000]}\n\n"
                        f"'{project}' 프로젝트에게 보낼 구체적 작업 지시를 만들어라. "
                        f"도구를 사용하지 말고 지시 텍스트만 출력해라."
                    )
                    self._execute_with_continuation(project, directive, suggestion, response)
                    self._remove_hints_for(project)
                    self._clear_retry(project)
                elif user_resp:
                    logger.info(f"[manager] Rejected or other response: {user_resp[:80]}")
                    self._clear_retry(project)
                else:
                    logger.info(f"[manager] No response, skipping")
                    self._clear_retry(project)

        self.state["last_run"] = datetime.now().isoformat()
        self.state["run_count"] = cycle_num
        self.state["current_phase"] = "idle"
        self._save_state()

        duration = time.time() - cycle_start
        logger.info(f"[manager] ═══ Cycle #{cycle_num} complete ({duration:.0f}s) ═══")

    def get_snapshot(self) -> dict:
        retry_queue = self.state.get("retry_queue", {})
        retrying = []
        for proj, entry in retry_queue.items():
            retrying.append({
                "project": proj,
                "attempt": entry.get("attempt", 0),
                "due_at": datetime.fromtimestamp(
                    entry.get("due_at", 0), tz=timezone.utc
                ).isoformat() if entry.get("due_at") else None,
                "error": entry.get("error"),
            })
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "current_phase": self.state.get("current_phase", "idle"),
            "last_run": self.state.get("last_run"),
            "run_count": self.state.get("run_count", 0),
            "workflows": list(self.workflows.keys()),
            "retrying": retrying,
            "task_queue": self.state.get("task_queue", []),
            "hints": self.state.get("hints", []),
        }

    def _wait_or_wake(self, seconds: float) -> bool:
        manager_wake_event.wait(timeout=seconds)
        if manager_wake_event.is_set():
            manager_wake_event.clear()
            self.state = self._load_state()
            return True
        return False

    def run(self):
        logger.info("[manager] Manager thread started")
        interval_min = self.mgr_config.get("interval_minutes", 60)
        interval_sec = interval_min * 60
        forced = False

        forced = self._wait_or_wake(60)

        while not shutdown_event.is_set():
            try:
                if not forced and self._is_quiet_hours():
                    logger.debug("[manager] Quiet hours, skipping")
                    forced = self._wait_or_wake(300)
                    continue

                last_run = self.state.get("last_run")
                if not forced and last_run:
                    elapsed = (datetime.now() - datetime.fromisoformat(last_run)).total_seconds()
                    if elapsed < interval_sec:
                        remaining = interval_sec - elapsed
                        forced = self._wait_or_wake(min(remaining, 300))
                        continue

                forced = False
                self._reload_workflows()
                self._run_cycle()
                manager_wake_event.clear()

            except Exception as e:
                logger.error(f"[manager] Cycle error: {e}", exc_info=True)

            forced = self._wait_or_wake(min(interval_sec, 300))

        logger.info("[manager] Manager thread stopped")
