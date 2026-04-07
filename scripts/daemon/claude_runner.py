"""Claude CLI execution: run_claude() and rewrite_for_voice()."""

import os
import json
import time
import select
import subprocess
from pathlib import Path

from daemon.globals import (
    IS_WINDOWS, CLAUDE_CMD, PROMPTS_DIR, SECRETS_ENV_PATH,
    MAX_CONTEXT_OVERFLOW_RETRIES,
    config, sessions_lock, shutdown_event, logger,
)
import daemon.globals as g
from daemon.utils import _strip_ansi
from daemon.api import api_request
from daemon.supabase import (
    resolve_user_id, _fetch_project_settings, _fetch_recent_conversation,
    get_project_dir, fetch_prompt_from_supabase, check_stop_requested, clear_stop_requested,
)
from daemon.sessions import (
    get_session_id, update_session, reset_session, session_key,
    save_session_context, _save_session_summary, _build_session_context_prompt,
)
from daemon.tasks import get_current_task, get_task_description
from daemon.prompts import get_prompt_file, build_system_prompt


def run_claude(prompt: str, project: str, _retry_count: int = 0, _overload_retry: int = 0) -> tuple[str, str | None, list[str]]:
    api_key = config["api_key"]

    # branch:{branch_id} → 부모 프로젝트 디렉토리 사용
    is_branch = project.startswith("branch:")
    is_kanban = project.startswith("kanban:")
    if is_branch:
        from daemon.branches import fetch_branch, build_branch_prompt, build_branch_context
        branch_id = int(project.split(":")[1])
        branch_data = fetch_branch(branch_id)
        real_project = branch_data.get("project_id", "general") if branch_data else "general"
        project_dir = get_project_dir(real_project)
    elif is_kanban:
        from daemon.kanban import build_kanban_prompt, _fetch_kanban_card
        kanban_card_id = int(project.split(":")[1])
        kanban_card = _fetch_kanban_card(kanban_card_id)
        real_project = kanban_card.get("project_id", "general") if kanban_card else "general"
        project_dir = get_project_dir(real_project)
    else:
        project_dir = get_project_dir(project)

    sid = get_session_id(project)

    is_demo = project.startswith("demo_")
    cmd = [
        CLAUDE_CMD, "-p",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if not is_demo:
        cmd.append("--dangerously-skip-permissions")

    settings_project = real_project if (is_branch or is_kanban) else project
    proj_settings = _fetch_project_settings(settings_project)
    if proj_settings.get("chrome"):
        cmd.append("--chrome")

    model = proj_settings.get("model") or config.get("claude_model")
    if model:
        cmd.extend(["--model", model])
    effort = config.get("claude_effort")
    if effort:
        cmd.extend(["--effort", effort])

    if is_branch and branch_data:
        # 브랜치: 시스템 프롬프트 + 새 세션이면 부모 맥락/브랜치 정보를 첫 메시지에 prepend
        combined = build_branch_prompt(branch_data)
        if not sid:
            context_block = build_branch_context(branch_data)
            prompt = f"{context_block}\n\n---\n\n{prompt}"
    elif is_kanban and kanban_card:
        # 칸반 카드: 시스템 프롬프트(규칙) + 새 세션이면 카드 정보를 첫 메시지에 prepend
        from daemon.kanban import build_kanban_card_context
        combined = build_kanban_prompt(kanban_card)
        if not sid:
            # 새 세션: 카드 배경 정보를 유저 메시지 앞에 붙임
            card_context = build_kanban_card_context(kanban_card)
            prompt = f"{card_context}\n\n---\n\n{prompt}"
    else:
        current_task = get_current_task(project)
        task_desc = get_task_description(project, current_task)
        sys_prompt = build_system_prompt(project, current_task, task_desc)

        # System-wide prompt shared across ALL users (user_id=0)
        system_prompt_pv = fetch_prompt_from_supabase("_petervoice_system", user_id_override=0) or ""

        if is_demo:
            common_prompt = ""
        else:
            common_prompt = fetch_prompt_from_supabase("_common") or ""
            if common_prompt and "{동적으로 키 목록 삽입}" in common_prompt:
                secret_keys = []
                if SECRETS_ENV_PATH.exists():
                    for line in SECRETS_ENV_PATH.read_text(encoding="utf-8").splitlines():
                        if "=" in line:
                            secret_keys.append(line.split("=", 1)[0])
                key_list = "\n".join(f"- {k}" for k in secret_keys) if secret_keys else "(없음)"
                common_prompt = common_prompt.replace("{동적으로 키 목록 삽입}", key_list)

        if is_demo:
            prompt_file = get_prompt_file("_demo")
        else:
            prompt_file = get_prompt_file(project)
        prompt_content = prompt_file.read_text(encoding="utf-8") if prompt_file and prompt_file.exists() else ""

        session_context = ""
        if not sid and not is_demo:
            session_context = _build_session_context_prompt(project)
            if session_context:
                logger.info(f"Injecting session context for {project} ({len(session_context)} chars)")

        combined = "\n\n".join(p for p in [sys_prompt, system_prompt_pv, common_prompt, prompt_content, session_context] if p)

    if combined:
        combined_file = PROMPTS_DIR / f"_combined_{project}.md"
        combined_file.write_text(combined, encoding="utf-8")
        cmd.extend(["--append-system-prompt-file", str(combined_file)])

    if sid:
        cmd.extend(["--resume", sid])

    cmd.extend(["--", prompt])

    bot_name = config.get("bot_name", "bot")

    # Resolve account — auto-reset session if account changed
    account_name = proj_settings.get("account") or "default"
    if sid:
        key = session_key(project)
        with sessions_lock:
            prev_account = g.sessions.get(key, {}).get("account") or "default"
        if prev_account != account_name:
            logger.info(f"Account changed for {project}: {prev_account} → {account_name}, auto-resetting session")
            save_session_context(project)
            reset_session(project)
            sid = None
    accounts = config.get("accounts", {})
    account_config_dir = accounts.get(account_name, {}).get("config_dir") if account_name != "default" else None
    if account_config_dir:
        logger.info(f"[{bot_name}] Claude: project={project}, dir={project_dir}, session={sid or 'new'}, account={account_name}")
    else:
        logger.info(f"[{bot_name}] Claude: project={project}, dir={project_dir}, session={sid or 'new'}")

    try:
        g.claude_semaphore.acquire()
        claude_env = {
            **{k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
            "LANG": "en_US.UTF-8",
        }
        if account_config_dir:
            claude_env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(account_config_dir)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_dir,
            env=claude_env,
            shell=IS_WINDOWS,
        )

        response_text = ""
        new_session_id = sid
        last_stream_time = time.time()
        last_activity_time = time.time()
        process_start_time = time.time()
        last_tool_time = 0.0
        stdout_timeout = config.get("claude_stdout_timeout_sec", 600)
        hard_timeout = config.get("claude_hard_timeout_sec", 900)
        hard_timeout_with_tools = config.get("claude_hard_timeout_with_tools_sec", 1800)
        stream_interval = config.get("stream_interval_sec", 2.0)
        tool_lines = []

        while True:
            if shutdown_event.is_set():
                proc.terminate()
                return ("(데몬 종료 중)", new_session_id, tool_lines)

            elapsed = time.time() - process_start_time
            effective_hard_timeout = hard_timeout_with_tools if last_tool_time > 0 else hard_timeout
            if elapsed > effective_hard_timeout:
                label = "with-tools" if last_tool_time > 0 else "no-tools"
                logger.error(f"[{bot_name}] Claude hard timeout ({label}, {effective_hard_timeout}s, elapsed {elapsed:.0f}s) for {project}, killing")
                proc.kill()
                return (f"(Claude 실행 시간 초과 - {elapsed:.0f}초 경과)", sid, tool_lines)

            if os.name == "nt":
                import threading as _thr
                _line_ready = _thr.Event()
                def _check_readable():
                    try:
                        if proc.stdout.readable():
                            _line_ready.set()
                    except Exception:
                        _line_ready.set()
                _t = _thr.Thread(target=_check_readable, daemon=True)
                _t.start()
                _t.join(timeout=10)
                ready = _line_ready.is_set()
            else:
                ready_list, _, _ = select.select([proc.stdout], [], [], 10)
                ready = bool(ready_list)
            if not ready:
                if proc.poll() is not None:
                    break
                if time.time() - last_activity_time > stdout_timeout:
                    logger.error(f"[{bot_name}] Claude stdout timeout ({stdout_timeout}s) for {project}, killing")
                    proc.kill()
                    return (f"(Claude 응답 시간 초과 - {stdout_timeout}초 동안 출력 없음)", sid, tool_lines)
                uid = resolve_user_id()
                if uid and check_stop_requested(uid):
                    logger.info(f"[{bot_name}] Stop requested for {project}, terminating claude process")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    clear_stop_requested(uid)
                    partial = _strip_ansi(response_text).strip()
                    result = partial + "\n\n(작업이 중단되었습니다)" if partial else "(작업이 중단되었습니다)"
                    return (result, new_session_id, tool_lines)
                continue

            raw = proc.stdout.readline()
            if not raw:
                break

            last_activity_time = time.time()
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "system" and "session_id" in event:
                new_session_id = event["session_id"]

            if etype == "assistant":
                msg_content = event.get("message", {}).get("content", [])
                for block in msg_content:
                    if block.get("type") == "tool_use":
                        last_tool_time = time.time()
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        tool_detail = ""
                        if tool_name == "Bash":
                            c = tool_input.get("command", "")
                            tool_detail = f": {c[:80]}" if c else ""
                        elif tool_name in ("Read", "Write", "Edit"):
                            fp = tool_input.get("file_path", "")
                            tool_detail = f": {fp}" if fp else ""
                        elif tool_name in ("Glob", "Grep"):
                            pat = tool_input.get("pattern", "")
                            tool_detail = f": {pat}" if pat else ""
                        elif tool_name == "WebSearch":
                            q = tool_input.get("query", "")
                            tool_detail = f": {q[:60]}" if q else ""
                        elif tool_name == "WebFetch":
                            u = tool_input.get("url", "")
                            tool_detail = f": {u[:60]}" if u else ""
                        if tool_name:
                            tool_lines.append(f"🔧 {tool_name}{tool_detail}")
                            api_request(api_key, "POST", "/api/bot/reply", {
                                "text": "\n".join(tool_lines), "project": project, "is_final": False,
                            })
                if response_text and not response_text.endswith("\n\n"):
                    response_text += "\n\n"

            if etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    response_text += delta.get("text", "")

            now = time.time()
            if response_text and (now - last_stream_time) >= stream_interval:
                streaming = "\n".join(tool_lines) + "\n\n" + response_text if tool_lines else response_text
                api_request(api_key, "POST", "/api/bot/reply", {
                    "text": streaming, "project": project, "is_final": False,
                })
                last_stream_time = now

            if etype == "result":
                if "session_id" in event:
                    new_session_id = event["session_id"]
                if event.get("result") and not response_text.strip():
                    response_text = event["result"]
                if event.get("is_error"):
                    error_text = str(event.get("error", "")) + str(event.get("result", ""))
                    if "overloaded" in error_text.lower() or '"529"' in error_text or "529" in error_text:
                        MAX_OVERLOAD_RETRIES = 3
                        wait_times = [10, 30, 60]
                        if _overload_retry < MAX_OVERLOAD_RETRIES:
                            wait = wait_times[_overload_retry]
                            logger.warning(f"[{bot_name}] Overloaded (529) for {project}, retry {_overload_retry + 1}/{MAX_OVERLOAD_RETRIES} in {wait}s")
                            api_request(api_key, "POST", "/api/bot/reply", {
                                "text": f"(Anthropic 서버 과부하, {wait}초 후 재시도 {_overload_retry + 1}/{MAX_OVERLOAD_RETRIES}...)", "project": project, "is_final": False,
                            })
                            proc.wait(timeout=5)
                            g.claude_semaphore.release()
                            time.sleep(wait)
                            return run_claude(prompt, project, _retry_count, _overload_retry + 1)
                        return ("(Anthropic 서버 과부하 - 잠시 후 다시 시도해주세요)", new_session_id, tool_lines)
                    if "context" in error_text.lower():
                        logger.warning(f"[{bot_name}] Context overflow for {project}, resetting (retry {_retry_count + 1})")
                        conv = _fetch_recent_conversation(project, limit=10)
                        if conv:
                            _save_session_summary(project, f"[컨텍스트 오버플로우로 자동 리셋 — 최근 대화 원본]\n\n{conv}")
                        reset_session(project)
                        proc.wait(timeout=5)
                        g.claude_semaphore.release()
                        if _retry_count >= MAX_CONTEXT_OVERFLOW_RETRIES:
                            return ("(컨텍스트 초과 - 최대 재시도 횟수 초과)", None, tool_lines)
                        return run_claude(prompt, project, _retry_count + 1)

        proc.wait(timeout=300)

        stderr_output = proc.stderr.read().decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not response_text:
            logger.error(f"[{bot_name}] Claude exited {proc.returncode}: {stderr_output[:500]}")
            if "context" in stderr_output.lower():
                logger.warning(f"[{bot_name}] Context overflow (stderr) for {project}, resetting (retry {_retry_count + 1})")
                conv = _fetch_recent_conversation(project, limit=10)
                if conv:
                    _save_session_summary(project, f"[컨텍스트 오버플로우(stderr)로 자동 리셋 — 최근 대화 원본]\n\n{conv}")
                reset_session(project)
                claude_semaphore.release()
                if _retry_count >= MAX_CONTEXT_OVERFLOW_RETRIES:
                    return ("(컨텍스트 초과 - 최대 재시도 횟수 초과)", None, tool_lines)
                return run_claude(prompt, project, _retry_count + 1)
            return (f"(Claude 오류: exit {proc.returncode})", new_session_id, tool_lines)

        if not response_text:
            response_text = "(작업 완료)" if tool_lines else "(응답 없음)"

        response_text = _strip_ansi(response_text)

        if new_session_id:
            update_session(project, new_session_id, account=account_name)
            # 브랜치 세션 ID를 DB에도 동기화
            if is_branch and new_session_id != sid:
                from daemon.branches import update_branch_session
                update_branch_session(int(project.split(":")[1]), new_session_id)

        return (response_text, new_session_id, tool_lines)

    except subprocess.TimeoutExpired:
        proc.kill()
        logger.error(f"[{bot_name}] Claude timed out for {project}")
        return ("(Claude 응답 시간 초과)", sid, [])
    except Exception as e:
        logger.error(f"[{bot_name}] Claude error: {e}")
        return (f"(Claude 실행 오류: {e})", sid, [])
    finally:
        try:
            g.claude_semaphore.release()
        except ValueError:
            pass


def rewrite_for_voice(text: str) -> str:
    """Rewrite Claude response via Haiku for voice-friendly output."""
    if not config.get("rewriter_enabled", False):
        return text
    if len(text) < 20:
        return text
    try:
        model = config.get("rewriter_model", "haiku")
        effort = config.get("rewriter_effort", "low")
        timeout = config.get("rewriter_timeout_sec", 25)

        cmd = [
            CLAUDE_CMD, "-p",
            "--model", model,
            "--effort", effort,
            "--dangerously-skip-permissions",
        ]

        prompt_file = Path(config.get(
            "rewriter_prompt_file",
            "~/.claude-daemon/rewriter_prompt.txt"
        )).expanduser()
        if prompt_file.exists():
            cmd.extend(["--append-system-prompt-file", str(prompt_file)])

        cmd.append(text)

        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=os.path.expanduser("~"),
            env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
            shell=IS_WINDOWS,
        )

        rewritten = _strip_ansi(result.stdout.strip())
        if rewritten:
            logger.info(f"Rewriter: {len(text)} → {len(rewritten)} chars")
            return rewritten
        return text
    except subprocess.TimeoutExpired:
        logger.warning("Rewriter timed out, using original text")
        return text
    except Exception as e:
        logger.warning(f"Rewriter error: {e}, using original text")
        return text
