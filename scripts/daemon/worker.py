"""Worker thread: message polling and processing."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import daemon.globals as g
from daemon.globals import (
    config, processed_ids, shutdown_event,
    active_projects, active_projects_lock,
    project_locks, project_locks_lock,
    SECRETS_ENV_PATH, MANAGER_STATE_PATH,
    logger,
)
from daemon.api import api_request, mark_message_processed
from daemon.supabase import (
    resolve_user_id, get_project_dir, fetch_prompt_from_supabase,
    _fetch_recent_conversation, clear_stop_requested,
)
from daemon.sessions import (
    get_session_id, session_key, reset_session, save_session_context,
)
from daemon.tasks import (
    get_current_task, get_task_description, set_current_task, list_tasks,
)
from daemon.prompts import get_prompt_file, build_system_prompt
from daemon.claude_runner import run_claude, rewrite_for_voice
from daemon.queue import enqueue_message, dequeue_message
from daemon.utils import download_files, cleanup_downloads, _split_text_chunks, _read_json, _write_json
# kanban messages now flow through messages table — no separate kanban import needed


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="worker")
        self.api_key = config["api_key"]
        self.bot_name = config.get("bot_name", "bot")
        max_workers = config.get("max_concurrent", 3)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="msg")
        self._spawned_ids: set[int] = set()
        self._spawned_lock = threading.Lock()

    def poll(self) -> list | None:
        result = api_request(self.api_key, "GET", "/api/bot/poll", timeout=30)
        if result is None:
            return None
        return result.get("pending", [])

    def reply(self, text: str, reply_to=None, project="general", is_final=True, subtype=None):
        payload = {"text": text, "reply_to": reply_to, "project": project, "is_final": is_final}
        if subtype:
            payload["subtype"] = subtype
        api_request(self.api_key, "POST", "/api/bot/reply", payload)

    def heartbeat(self, is_working=False, current_task=None, project=None):
        with active_projects_lock:
            projects_list = sorted(active_projects)
        payload = {
            "is_working": is_working or len(projects_list) > 0,
            "current_task": current_task,
            "timestamp": datetime.now().isoformat(),
            "active_projects": projects_list,
        }
        if project:
            payload["project"] = project
        api_request(self.api_key, "POST", "/api/bot/heartbeat", payload)

    def process_message(self, msg: dict):
        msg_id = msg.get("id")
        text = msg.get("text", "").strip()
        project = msg.get("project", "general") or "general"

        if msg_id in processed_ids:
            return
        processed_ids.add(msg_id)
        if len(processed_ids) > 1000:
            processed_ids.clear()
            processed_ids.add(msg_id)

        enqueue_message(msg)
        mark_message_processed(msg_id)

        files = msg.get("files", [])
        downloaded_paths = download_files(files) if files else []

        if not text and not downloaded_paths:
            return

        logger.info(f"[{self.bot_name}] msg #{msg_id}: project={project}, text={text[:80]}, files={len(downloaded_paths)}")

        with active_projects_lock:
            active_projects.add(project)
        self.heartbeat(is_working=True, current_task=f"{text[:50]}", project=project)

        # Special commands
        if text.startswith("/restart"):
            g.restart_requested = True
            self.reply("데몬을 재시작합니다... (약 10초 소요)", reply_to=[msg_id], project=project)
            dequeue_message(msg_id)
            from daemon.utils import _write_json
            _write_json(g.RESTART_TRIGGER_PATH, {"project": project, "timestamp": datetime.now().isoformat()})
            logger.info(f"[{self.bot_name}] Restart requested via /restart command")
            shutdown_event.set()
            return

        if text.startswith("/reset") or text.startswith("/새세션"):
            current_task_name = get_current_task(project)
            self.reply("세션 맥락을 저장 중...", reply_to=[msg_id], project=project, is_final=False)
            save_session_context(project)
            reset_session(project)
            self.reply(f"세션을 초기화했습니다. 이전 맥락이 저장되었습니다. (작업: {current_task_name})", reply_to=[msg_id], project=project)
            dequeue_message(msg_id)
            return

        if text.startswith("/rewriter"):
            current = config.get("rewriter_enabled", False)
            config["rewriter_enabled"] = not current
            state = "ON" if config["rewriter_enabled"] else "OFF"
            self.reply(f"리라이터 {state}", reply_to=[msg_id], project=project)
            dequeue_message(msg_id)
            return

        if text.startswith("/remember"):
            self.reply("⚠️ /remember는 더 이상 사용되지 않습니다. 기억이 필요하면 대화에서 직접 '기억해줘'라고 말하세요 (Claude 자동 메모리에 저장됩니다).", reply_to=[msg_id], project=project)
            dequeue_message(msg_id)
            return

        if text.startswith("/status"):
            current_task_name = get_current_task(project)
            key = session_key(project)
            sess = g.sessions.get(key, {})
            self.reply(
                f"Bot: {self.bot_name}\nProject: {project}\n"
                f"Task: {current_task_name}\n"
                f"Dir: {get_project_dir(project)}\n"
                f"Session: {sess.get('session_id', 'none')}\n"
                f"Messages: {sess.get('message_count', 0)}\n"
                f"Rewriter: {'ON' if config.get('rewriter_enabled') else 'OFF'}\n"
                f"Model: {config.get('claude_model', 'default')}\n"
                f"Effort: {config.get('claude_effort', 'default')}",
                reply_to=[msg_id], project=project,
            )
            dequeue_message(msg_id)
            return

        if text.startswith("/prompt"):
            current_task_name = get_current_task(project)
            task_desc = get_task_description(project, current_task_name)
            sys_prompt = build_system_prompt(project, current_task_name, task_desc)
            common_prompt = fetch_prompt_from_supabase("_common") or ""
            if common_prompt and "{동적으로 키 목록 삽입}" in common_prompt:
                secret_keys = []
                if SECRETS_ENV_PATH.exists():
                    for line in SECRETS_ENV_PATH.read_text(encoding="utf-8").splitlines():
                        if "=" in line:
                            secret_keys.append(line.split("=", 1)[0])
                key_list = "\n".join(f"- {k}" for k in secret_keys) if secret_keys else "(없음)"
                common_prompt = common_prompt.replace("{동적으로 키 목록 삽입}", key_list)
            prompt_file = get_prompt_file(project)
            prompt_content = prompt_file.read_text(encoding="utf-8") if prompt_file and prompt_file.exists() else ""
            content = "\n\n".join(p for p in [sys_prompt, common_prompt, prompt_content] if p)
            self.reply(
                f"프롬프트 (combined):\n---\n{content[:3000]}",
                reply_to=[msg_id], project=project,
            )
            dequeue_message(msg_id)
            return

        if text.startswith("/manager"):
            from daemon.globals import manager_wake_event
            parts = text.split(None, 1)
            sub = parts[1] if len(parts) > 1 else ""
            state = _read_json(MANAGER_STATE_PATH, {})
            if sub == "run":
                state["last_run"] = None
                _write_json(MANAGER_STATE_PATH, state)
                manager_wake_event.set()
                self.reply("매니저 사이클을 즉시 시작합니다.", reply_to=[msg_id], project=project)
            else:
                phase = state.get("current_phase", "idle")
                last_run = state.get("last_run", "없음")
                run_count = state.get("run_count", 0)
                awaiting = state.get("awaiting_feedback", False)
                convs = state.get("project_conversations", {})
                conv_info = ", ".join(f"{k}({v.get('turns', 0)}턴)" for k, v in convs.items()) or "없음"
                self.reply(
                    f"매니저 상태:\n"
                    f"Phase: {phase}\n"
                    f"Last run: {last_run}\n"
                    f"Cycles: {run_count}\n"
                    f"Awaiting feedback: {awaiting}\n"
                    f"Recent conversations: {conv_info}",
                    reply_to=[msg_id], project=project,
                )
            dequeue_message(msg_id)
            return

        if text.startswith("/do "):
            task_text = text[4:].strip()
            if not task_text:
                self.reply("사용법: /do <작업 지시> 또는 /do <턴수> <작업 지시>", reply_to=[msg_id], project=project)
                dequeue_message(msg_id)
                return
            if g._manager_instance is None:
                self.reply("매니저가 비활성화 상태입니다.", reply_to=[msg_id], project=project)
                dequeue_message(msg_id)
                return
            max_turns = 50
            parts = task_text.split(None, 1)
            if parts and parts[0].isdigit():
                max_turns = int(parts[0])
                task_text = parts[1] if len(parts) > 1 else ""
                if not task_text:
                    self.reply("사용법: /do <턴수> <작업 지시>", reply_to=[msg_id], project=project)
                    dequeue_message(msg_id)
                    return
            context = _fetch_recent_conversation(project, limit=10)
            g._manager_instance.enqueue_deep_task(project, task_text, context=context, max_turns=max_turns)
            self.reply(f"매니저에 작업을 등록했습니다 (최대 {max_turns}턴). 완료되면 알려드릴게요.\n→ {task_text[:100]}", reply_to=[msg_id], project=project)
            dequeue_message(msg_id)
            return

        if text.startswith("/task"):
            parts = text.split(None, 2)
            if len(parts) == 1:
                current_task_name = get_current_task(project)
                desc = get_task_description(project, current_task_name)
                reply_text = f"현재 작업: {current_task_name}"
                if desc:
                    reply_text += f" — {desc}"
                self.reply(reply_text, reply_to=[msg_id], project=project)
            elif parts[1] == "list":
                all_tasks = list_tasks(project)
                current_task_name = get_current_task(project)
                lines = []
                for name, info in all_tasks.items():
                    marker = "→ " if name == current_task_name else "  "
                    desc = info.get("description", "")
                    line = f"{marker}{name}"
                    if desc:
                        line += f" — {desc}"
                    lines.append(line)
                self.reply("작업 목록:\n" + "\n".join(lines), reply_to=[msg_id], project=project)
            else:
                task_name = parts[1]
                description = parts[2] if len(parts) > 2 else ""
                set_current_task(project, task_name, description)
                logger.info(f"[{self.bot_name}] Task switched: {project} → {task_name}")
                self.reply(f"작업 전환: {task_name}", reply_to=[msg_id], project=project)
            dequeue_message(msg_id)
            return

        # Append file paths to prompt
        prompt_text = text
        if downloaded_paths:
            file_lines = "\n".join(f"- {p}" for p in downloaded_paths)
            image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
            has_images = any(p.suffix.lower() in image_exts for p in downloaded_paths)
            has_docs = any(p.suffix.lower() not in image_exts for p in downloaded_paths)
            if has_images and has_docs:
                hint = "이미지는 Read 도구로, 문서(xlsx/docx/pdf 등)는 Bash에서 python으로 읽으세요"
            elif has_docs:
                hint = "Bash에서 python으로 파일 내용을 읽으세요 (예: openpyxl, python-docx, PyPDF2 등)"
            else:
                hint = "Read 도구로 확인하세요"
            prompt_text = f"{text}\n\n[첨부 파일 ({hint})]\n{file_lines}"

        uid = resolve_user_id()
        if uid:
            clear_stop_requested(uid)

        response, sid, tool_lines = run_claude(prompt_text, project)

        if tool_lines:
            self.reply("\n".join(tool_lines), reply_to=[msg_id], project=project, is_final=True, subtype="tool_log")

        # Rewriter — skip for manager-injected messages
        from daemon.manager.thread import ManagerThread
        is_manager_msg = text.startswith(ManagerThread.MANAGER_PREFIX)
        if not text.startswith("/") and not is_manager_msg:
            response = rewrite_for_voice(response)

        chunks = _split_text_chunks(response)
        for chunk in chunks:
            self.reply(chunk, reply_to=[msg_id], project=project, is_final=True)

        self.reply("", reply_to=None, project=project, is_final=False)

        if downloaded_paths:
            cleanup_downloads(downloaded_paths)

        dequeue_message(msg_id)

        logger.info(f"[{self.bot_name}] Replied msg #{msg_id}: {len(response)} chars, {len(chunks)} chunk(s)")

    def _get_project_lock(self, project: str) -> threading.Lock:
        with project_locks_lock:
            if project not in project_locks:
                project_locks[project] = threading.Lock()
            return project_locks[project]

    def _process_message_safe(self, msg: dict):
        msg_id = msg.get("id")
        project = msg.get("project", "general") or "general"
        lock = self._get_project_lock(project)
        with lock:
            try:
                self.process_message(msg)
            except Exception as e:
                logger.error(f"[{self.bot_name}] Error msg {msg_id}: {e}", exc_info=True)
                try:
                    self.reply(f"(처리 오류: {e})", reply_to=[msg_id], project=project)
                except Exception:
                    pass
            finally:
                with active_projects_lock:
                    active_projects.discard(project)
                with self._spawned_lock:
                    self._spawned_ids.discard(msg_id)
                self.heartbeat(is_working=False)

    def run(self):
        import time
        logger.info(f"[{self.bot_name}] Worker started")
        consecutive_errors = 0
        last_heartbeat = 0
        poll_interval = config.get("poll_interval_sec", 3)

        while not shutdown_event.is_set():
            try:
                now = time.time()
                if now - last_heartbeat > 30:
                    self.heartbeat(is_working=False)
                    last_heartbeat = now

                messages = self.poll()

                if messages is None:
                    consecutive_errors += 1
                    wait = min(30, 2 ** consecutive_errors)
                    logger.warning(f"[{self.bot_name}] Poll error #{consecutive_errors}, wait {wait}s")
                    shutdown_event.wait(wait)
                    continue

                consecutive_errors = 0

                for msg in messages:
                    if shutdown_event.is_set():
                        break
                    msg_id = msg.get("id")
                    with self._spawned_lock:
                        if msg_id in self._spawned_ids:
                            continue
                        self._spawned_ids.add(msg_id)
                    self._executor.submit(self._process_message_safe, msg)

                # kanban messages now flow through messages table (project="kanban:{card_id}")
                # No separate kanban_messages polling needed

                if not messages:
                    shutdown_event.wait(poll_interval)

            except BaseException as e:
                if isinstance(e, (SystemExit, KeyboardInterrupt)):
                    logger.info(f"[{self.bot_name}] Worker received {type(e).__name__}, stopping")
                    shutdown_event.set()
                    break
                consecutive_errors += 1
                wait = min(30, 2 ** consecutive_errors)
                logger.error(f"[{self.bot_name}] Loop error: {e}", exc_info=True)
                shutdown_event.wait(wait)

        logger.info(f"[{self.bot_name}] Worker stopped")
