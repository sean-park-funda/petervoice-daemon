"""Claude/Codex CLI execution: run_claude(), run_codex(), and rewrite_for_voice()."""

import os
import sys
import json
import time
import select
import subprocess
from pathlib import Path

import re

from daemon.globals import (
    IS_WINDOWS, CLAUDE_CMD, CODEX_CMD, PROMPTS_DIR, SECRETS_ENV_PATH,
    MAX_CONTEXT_OVERFLOW_RETRIES,
    config, sessions_lock, shutdown_event, logger,
)
from daemon.limits import (
    build_claude_env, detect_cap_hit, record_cap_hit, cap_notice,
    detect_usage_limit, usage_limit_notice,
    start_account_cooldown, clear_account_cooldown, account_cooldown_notice,
)
import daemon.globals as g
import signal
import threading

from daemon.utils import _strip_ansi, count_dirty_files, mark_interrupted
from daemon.api import api_request
from daemon.supabase import (
    resolve_user_id, _fetch_project_settings, _fetch_recent_conversation,
    get_project_dir, fetch_prompt_from_supabase, check_stop_requested, clear_stop_requested,
    get_stop_request,
)
from daemon.sessions import (
    get_session_id, update_session, update_session_by_key,
    reset_session, session_key,
    save_session_context, _save_session_summary, _build_session_context_prompt,
    get_codex_session_id, update_codex_session, reset_codex_session,
)
from daemon.tasks import get_current_task, get_task_description
from daemon.prompts import get_prompt_file, build_system_prompt, build_connected_services_note


# ── 종료 드레인 센티널 ─────────────────────────────────────────
# 드레인 상한 초과로 턴이 kill됐음을 나타내는 내부 마커.
# worker/team은 이 값을 받으면 응답 전송·dequeue를 모두 건너뛴다
# → 메시지가 큐에 남아 재시작 후 _recover_after_restart가 --resume으로 재처리.
SHUTDOWN_INTERRUPTED = "__PV_SHUTDOWN_INTERRUPTED__"


# ── 인증만료 감지 & 재로그인 트리거 (macOS 셀프서비스) ─────────────
# 완전 로그아웃(hoon 사례)은 401 텍스트 없이 exit 1 + stderr 빈값으로 새어나가고,
# `claude auth status`는 거짓보고(loggedIn:true)하므로 status 로는 판별 불가.
# → plain 프로브(echo | claude -p -- "ok")로 인증만료를 확정한다.
_AUTH_EXPIRED_MARKERS = (
    "not logged in", "please run /login", "please run `/login`",
    "run /login", "invalid authentication", "authentication_error",
    "oauth token has expired",
    # karl(2026-07-09) 실사고: 실제 문구가 위에 없어 자동감지 실패했던 케이스 보완.
    # "Failed to authenticate: OAuth session expired and could not be refreshed"
    "failed to authenticate", "session expired", "could not be refreshed",
)


def _auth_diag(spawn_concurrency: int) -> str:
    """인증만료 시점의 진단 정보. 만료 순간 병렬 스폰이 겹치면 refresh-token 갱신 경합으로
    토큰 패밀리가 폐기된다는 가설(2026-07-10~15 연쇄 로그아웃)을 데이터로 확증하기 위함."""
    idle = (time.time() - g.last_claude_ok) if g.last_claude_ok else -1
    return f"[auth-diag concurrent_spawns={spawn_concurrency} idle_before={idle:.0f}s]"


def _text_signals_auth_expired(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if "401" in text:
        return True
    return any(m in low for m in _AUTH_EXPIRED_MARKERS)


# ── 모델별 한도 폴백 체인 ──────────────────────────────────────
# "Switch to another model" 은 에러 메시지 자체가 말하는 처방이다. 모델별 주간 쿼터는 서로
# 독립이라 한 단계만 내려가도 대개 살아난다. sonnet 은 더 내리지 않는다 — 거기서 막혔다면
# 계정 전체가 소진된 것에 가깝고, haiku 로 내려서 나오는 결과는 자율 루프에 오히려 해롭다.
_MODEL_FALLBACK = (
    ("fable", "claude-opus-5"),
    ("opus", "claude-sonnet-5"),
)


def _fallback_model(current: str) -> str | None:
    explicit = config.get("limit_fallback_model")
    if explicit and explicit != current:
        return explicit
    low = (current or "").lower()
    for needle, target in _MODEL_FALLBACK:
        if needle in low and target != current:
            return target
    return None


def _probe_auth_expired(config_dir: str | None = None) -> bool:
    """원인 불명 오류(exit≠0 & stderr 빈값 & 응답 빈값) 시 plain 프로브로 인증만료 확정.
    Keychain/credentials 는 절대 건드리지 않는다 — 읽기만 하는 무해한 프로브."""
    if IS_WINDOWS:
        return False
    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["LANG"] = "en_US.UTF-8"
        if config_dir:
            env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
        r = subprocess.run(
            [CLAUDE_CMD, "-p", "--", "ok"],
            input="", capture_output=True, text=True, timeout=30, env=env,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return _text_signals_auth_expired(out)
    except Exception as e:
        logger.warning(f"[auth-probe] failed: {e}")
        return False


def resolve_account_config_dir(project: str) -> str | None:
    """프로젝트의 account 설정에 해당하는 CLAUDE_CONFIG_DIR 반환 (default 면 None).
    worker 의 명시적 재로그인 트리거에서 멀티계정을 올바른 config 로 띄우기 위함."""
    try:
        real = project
        if project.startswith("branch:"):
            from daemon.branches import fetch_branch
            bd = fetch_branch(int(project.split(":")[1]))
            real = bd.get("project_id", "general") if bd else "general"
        elif project.startswith("kanban:"):
            from daemon.kanban import _fetch_kanban_card
            kc = _fetch_kanban_card(int(project.split(":")[1]))
            real = kc.get("project_id", "general") if kc else "general"
        settings = _fetch_project_settings(real)
        account_name = settings.get("account") or "default"
        if account_name == "default":
            return None
        accounts = config.get("accounts", {})
        cd = accounts.get(account_name, {}).get("config_dir")
        return os.path.expanduser(cd) if cd else None
    except Exception:
        return None


def _trigger_relogin(project: str, config_dir: str | None = None) -> str:
    """인증만료 확정 시 재로그인 플로우를 시작하고, 유저에게 보낼 즉시 안내문을 반환.
    실제 로그인 URL 은 relogin 엔진이 준비되는 대로 채팅에 별도 안내한다.
    macOS 전용 — Windows 는 기존 수동 안내 유지."""
    if IS_WINDOWS:
        return "(Claude 인증이 만료되었습니다. 관리자에게 재로그인을 요청해주세요.)"
    try:
        from daemon import relogin
        relogin.start(project, config_dir=config_dir)
        return "⚠️ Claude 인증이 만료되어 재로그인을 시작합니다. 잠시 후 안내 링크를 보내드릴게요..."
    except Exception as e:
        logger.error(f"[relogin] trigger failed: {e}", exc_info=True)
        return "(Claude 인증이 만료되었습니다. 관리자에게 재로그인을 요청해주세요.)"


def _load_wiki_index(project: str) -> str:
    """Load wiki/index.md for auto-memory injection. Returns empty string if not found."""
    project_dir = get_project_dir(project)
    if not project_dir:
        return ""
    wiki_index = Path(project_dir) / "wiki" / "index.md"
    if not wiki_index.exists():
        return ""
    try:
        content = wiki_index.read_text(encoding="utf-8").strip()
        if not content:
            return ""
        return f"## 프로젝트 지식 인덱스 (자동 주입)\n\n{content}"
    except Exception:
        return ""


def build_project_prompt(project: str) -> str:
    """Build the full system prompt for a project (extracted for team reuse)."""
    current_task = get_current_task(project)
    task_desc = get_task_description(project, current_task)
    sys_prompt = build_system_prompt(project, current_task, task_desc)

    system_prompt_pv = fetch_prompt_from_supabase("_petervoice_system", user_id_override=0) or ""

    is_demo = project.startswith("demo_")
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

    global_wiki = ""
    if not is_demo:
        global_wiki_path = Path.home() / ".claude-daemon" / "global-graph" / "wiki" / "index.md"
        if global_wiki_path.exists():
            try:
                gw = global_wiki_path.read_text(encoding="utf-8").strip()
                if gw:
                    global_wiki = f"## 글로벌 지식 인덱스 (전 프로젝트, 자동 주입)\n\n{gw}"
            except Exception:
                pass

    if is_demo:
        prompt_file = get_prompt_file("_demo")
    else:
        prompt_file = get_prompt_file(project)
    prompt_content = prompt_file.read_text(encoding="utf-8") if prompt_file and prompt_file.exists() else ""

    session_context = ""
    if not get_session_id(project) and not is_demo:
        session_context = _build_session_context_prompt(project)

    wiki_index = _load_wiki_index(project)

    connected_services = "" if is_demo else build_connected_services_note()

    return "\n\n".join(p for p in [sys_prompt, system_prompt_pv, common_prompt, connected_services, prompt_content, global_wiki, wiki_index, session_context] if p)


def run_claude(
    prompt: str,
    project: str,
    _retry_count: int = 0,
    _overload_retry: int = 0,
    # ── Team support (D7) ──
    session_key_override: str | None = None,
    prompt_override: str | None = None,
    stream_to_chat: bool = True,
    # 한 턴에서 나온 완결 발화들(result 이벤트별)을 받아갈 리스트.
    # 백그라운드 작업이 끝나며 턴이 재개되면 발화가 여러 개 생기고, 서로 시각이 다르다.
    # 리스트를 넘기면 각 발화가 따로 담긴다 → 호출자가 별도 메시지로 보낼 수 있다.
    # 안 넘기면 기존과 동일하게 합쳐진 response_text만 쓰면 된다.
    segments_out: list[str] | None = None,
    # 계정 한도(모델별) 폴백 재시도용 — 원래 모델 대신 이 모델로 1회 다시 띄운다
    model_override: str | None = None,
    _limit_retry: int = 0,
) -> tuple[str, str | None, list[str], bool]:
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

    # D7: session key override for team members
    _sid_key = session_key_override or session_key(project)
    with sessions_lock:
        sid = g.sessions.get(_sid_key, {}).get("session_id")

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

    branch_model = branch_data.get("model") if is_branch and branch_data else None
    # 기본 모델은 Opus 5. 브랜치/프로젝트/config에 지정이 없으면 opus-5로 폴백.
    model = model_override or branch_model or proj_settings.get("model") or config.get("claude_model") or "claude-opus-5"
    # 엔진=claude인데 Codex(gpt-*) 모델 코드가 남아있으면 무시하고 Claude 기본값 사용.
    if model.startswith("gpt-"):
        model = config.get("claude_model") or "claude-sonnet-5"
    if model:
        cmd.extend(["--model", model])
    # effort: 브랜치 > 프로젝트 > 전역 config 순 (모델과 동일 우선순위)
    branch_effort = branch_data.get("effort") if is_branch and branch_data else None
    effort = branch_effort or proj_settings.get("effort") or config.get("claude_effort")
    if effort:
        cmd.extend(["--effort", effort])
    logger.info(f"[claude] project={project} model={model} effort={effort or '(none)'}")

    if prompt_override is not None:
        # D7: externally assembled prompt (team lead / team member)
        combined = prompt_override
    elif is_branch and branch_data:
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
        combined = build_project_prompt(project)
        if not sid and not project.startswith("demo_"):
            sc = _build_session_context_prompt(project)
            if sc:
                logger.info(f"Injecting session context for {project} ({len(sc)} chars)")
                combined = combined + "\n\n" + sc if combined else sc

    # Workspace rule: pin the agent to its working directory (single source of truth).
    # Covers all paths (project / branch / kanban / team) since project_dir is already resolved.
    if project_dir:
        workspace_note = (
            "## Workspace\n"
            f"`{project_dir}` is your working directory: the project root and single source of truth.\n"
            "- Create ALL files (code, docs, configs, build output) inside this directory only.\n"
            "- Never scaffold a project in a hand-picked path (e.g. ~/Projects/...); new apps "
            "(Next.js, etc.) go here, in the root or a subfolder.\n"
            "- Docs live in `docs/` under this directory (the web UI serves docs from there).\n"
            "- Need a different location? Don't move files yourself — ask the user to change the "
            "project's directory setting."
        )
        combined = (combined + "\n\n" + workspace_note) if combined else workspace_note

    if combined:
        # D9-A: unique file per session key to avoid race condition with parallel team members
        _safe_key = _sid_key.replace(":", "_")
        combined_file = PROMPTS_DIR / f"_combined_{_safe_key}.md"
        combined_file.write_text(combined, encoding="utf-8")
        cmd.extend(["--append-system-prompt-file", str(combined_file)])

    bot_name = config.get("bot_name", "bot")

    # Resolve account — auto-reset session if account changed (must happen before --resume)
    account_name = proj_settings.get("account") or "default"
    if sid:
        # D9-C: use _sid_key (not hardcoded session_key) for account lookup
        with sessions_lock:
            prev_account = g.sessions.get(_sid_key, {}).get("account") or "default"
        if prev_account != account_name:
            logger.info(f"Account changed for {project}: {prev_account} → {account_name}, auto-resetting session")
            save_session_context(project)
            reset_session(project, key_override=_sid_key)
            sid = None

    if sid:
        cmd.extend(["--resume", sid])

    cmd.extend(["--", prompt])
    accounts = config.get("accounts", {})
    account_config_dir = accounts.get(account_name, {}).get("config_dir") if account_name != "default" else None
    if account_config_dir:
        logger.info(f"[{bot_name}] Claude: project={project}, dir={project_dir}, session={sid or 'new'}, account={account_name}")
    else:
        logger.info(f"[{bot_name}] Claude: project={project}, dir={project_dir}, session={sid or 'new'}")

    # 계정 세션 한도 쿨다운 중이면 CLI 를 아예 띄우지 않는다. 계정 한도는 프로젝트들이
    # 나눠 쓰는 자원이라, 한 프로젝트가 계속 찔러대면 다른 프로젝트의 자율 루프까지 죽는다.
    # (2026-08-18 02:18~02:36 릴레이 백로그가 18분간 201회 헛스폰한 실사고)
    _cooldown_msg = account_cooldown_notice(account_name)
    if _cooldown_msg:
        logger.warning(f"[limits] {project}: 계정 한도 쿨다운 중 — 스폰 생략 (account={account_name})")
        return (_cooldown_msg, sid, [], False)

    total_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "model": ""}
    _counted = False
    _spawn_concurrency = 0   # 진단용 기본값 (로그 경로에서 NameError 나지 않도록)

    # 세마포어는 이 함수에서 '반드시 한 번만' 반납한다.
    # 에러 분기들이 재귀 전에 release() 하고 finally 가 또 release() 했는데,
    # threading.Semaphore(BoundedSemaphore 아님)라 예외 없이 카운트만 +1 된다
    # → 재시도가 일어날 때마다 max_concurrent 가 영구히 늘어난다(동시 스폰 증가 = 한도 소진 가속).
    _sem_held = False

    def _release_semaphore():
        nonlocal _sem_held
        if _sem_held:
            _sem_held = False
            g.claude_semaphore.release()

    try:
        g.claude_semaphore.acquire()
        _sem_held = True
        # 인증만료 진단: 이 스폰 시점의 동시 실행 수 기록 (RT 갱신 레이스 가설 확증용)
        with g.claude_inflight_lock:
            g.claude_inflight += 1
            _spawn_concurrency = g.claude_inflight
        _counted = True
        # 상한 주입은 반드시 build_claude_env 경유 (limits.py) — 실행 지점이 5곳이라 직접 만들면 샌다
        claude_env = build_claude_env(config, account_config_dir)
        # 이 턴의 대화 라우팅 값 (브랜치면 branch:N, 칸반이면 kanban:N) —
        # browser-handoff 등 스킬이 API 회신을 정확한 대화로 받기 위해 사용.
        # basename $PWD 방식은 브랜치·공유 디렉토리에서 틀린다 (2026-07-30 실측)
        claude_env["PV_PROJECT"] = project
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_dir,
            env=claude_env,
            shell=IS_WINDOWS,
            # 강제 킬 에스컬레이션 시 killpg 로 자식(도구/서브에이전트)까지 정리하기 위해 그룹 분리
            start_new_session=not IS_WINDOWS,
        )

        # ── 멈춤 감지 폴러 (2초 간격, 별도 스레드 — 스트리밍 루프를 API 지연으로 막지 않기 위함) ──
        # 이 턴의 project 와 일치하는(또는 구형 웹의 전역) stop 요청만 잡는다.
        stop_event = threading.Event()
        def _stop_poller():
            while not shutdown_event.is_set():
                if proc.poll() is not None:
                    return
                time.sleep(2.0)
                uid = resolve_user_id()
                if not uid:
                    continue
                try:
                    sr = get_stop_request(uid)
                except Exception:
                    continue
                if sr.get("requested") and (not sr.get("project") or sr.get("project") == project):
                    stop_event.set()
                    return
        threading.Thread(target=_stop_poller, daemon=True).start()
        stop_initiated = False
        stop_forced = False
        stop_deadline = 0.0

        response_text = ""
        _cap_notified = False   # 폭주 상한 알림은 턴당 1회
        result_segments: list[str] = []
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
        total_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "model": ""}
        _drain_deadline = None

        while True:
            if shutdown_event.is_set():
                # 드레인 종료: 진행 중 턴은 상한까지 완주시킨다.
                # 상한 초과 시에만 kill — 이때 응답을 보내지 않고 센티널을 반환해
                # 메시지가 큐에 남게 한다 (재시작 후 --resume 자동재개).
                if _drain_deadline is None:
                    _drain_sec = config.get("shutdown_drain_sec", 90)
                    _drain_deadline = time.time() + _drain_sec
                    logger.info(f"[{bot_name}] Shutdown requested — draining claude turn for {project} (up to {_drain_sec}s)")
                elif time.time() >= _drain_deadline:
                    logger.warning(f"[{bot_name}] Drain limit exceeded for {project} — killing claude; message stays queued for auto-resume")
                    proc.kill()
                    return (SHUTDOWN_INTERRUPTED, new_session_id, tool_lines, False)

            # ── 멈춤 처리: SIGINT(협조적, Esc 상당) → 10초 내 미종료 시 killpg SIGTERM → 5초 후 SIGKILL ──
            # SIGINT 후에는 루프를 계속 돌며 stdout 을 드레인한다 — CLI 가 도구를 취소하고
            # 마지막 result 이벤트를 방출한 뒤 스스로 종료한다 (실측 확인, 계획서 D3).
            if stop_event.is_set() and not stop_initiated:
                stop_initiated = True
                stop_deadline = time.time() + 10
                logger.info(f"[{bot_name}] Stop requested for {project} — sending SIGINT")
                uid = resolve_user_id()
                if uid:
                    clear_stop_requested(uid)
                try:
                    if IS_WINDOWS:
                        proc.terminate()
                    else:
                        proc.send_signal(signal.SIGINT)
                except Exception as e:
                    logger.warning(f"[{bot_name}] SIGINT failed: {e}")
            if stop_initiated and proc.poll() is None and time.time() > stop_deadline:
                stop_forced = True
                logger.warning(f"[{bot_name}] Graceful stop timed out for {project} — killing process group")
                try:
                    if IS_WINDOWS:
                        proc.kill()
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                break

            elapsed = time.time() - process_start_time
            effective_hard_timeout = hard_timeout_with_tools if last_tool_time > 0 else hard_timeout
            if elapsed > effective_hard_timeout:
                label = "with-tools" if last_tool_time > 0 else "no-tools"
                logger.error(f"[{bot_name}] Claude hard timeout ({label}, {effective_hard_timeout}s, elapsed {elapsed:.0f}s) for {project}, killing")
                proc.kill()
                return (f"(Claude 실행 시간 초과 - {elapsed:.0f}초 경과)", sid, tool_lines, True)

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
                _t.join(timeout=2)
                ready = _line_ready.is_set()
            else:
                # 2초 — 조용한 구간(긴 도구 실행 등)에도 멈춤 요청에 빠르게 반응
                ready_list, _, _ = select.select([proc.stdout], [], [], 2)
                ready = bool(ready_list)
            if not ready:
                if proc.poll() is not None:
                    break
                if time.time() - last_activity_time > stdout_timeout:
                    logger.error(f"[{bot_name}] Claude stdout timeout ({stdout_timeout}s) for {project}, killing")
                    proc.kill()
                    return (f"(Claude 응답 시간 초과 - {stdout_timeout}초 동안 출력 없음)", sid, tool_lines, True)
                # (멈춤 처리는 루프 상단의 stop_event 메커니즘이 담당 — 폴러 스레드가 2초 간격 감지)
                continue

            raw = proc.stdout.readline()
            if not raw:
                break

            last_activity_time = time.time()
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # 폭주 상한에 걸렸는지 — CLI 가 사람이 읽을 문구로 알려준다(limits.py CAP_MESSAGES).
            # 턴당 1회만 알린다: 같은 상한 메시지가 반복돼 나오므로 채팅이 도배된다.
            if not _cap_notified:
                _cap_label = detect_cap_hit(line)
                if _cap_label:
                    _cap_notified = True
                    logger.warning(f"[limits] {project}: {_cap_label} 상한 도달 — 작업 일부 중단됨")
                    record_cap_hit(project, _cap_label, config)
                    if stream_to_chat:
                        api_request(api_key, "POST", "/api/bot/reply", {
                            "text": cap_notice(_cap_label), "project": project, "is_final": False,
                        })
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "system" and "session_id" in event:
                new_session_id = event["session_id"]

            if etype == "result":
                _rusage = event.get("usage", {})
                if _rusage:
                    total_usage["input"] = _rusage.get("input_tokens", 0)
                    total_usage["output"] = _rusage.get("output_tokens", 0)
                    total_usage["cache_read"] = _rusage.get("cache_read_input_tokens", 0)
                    total_usage["cache_write"] = _rusage.get("cache_creation_input_tokens", 0)

            if etype == "assistant":
                _amsg = event.get("message", {})
                total_usage["model"] = _amsg.get("model", "") or total_usage["model"]
                msg_content = _amsg.get("content", [])
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
                            if stream_to_chat:
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
            if stream_to_chat and response_text and (now - last_stream_time) >= stream_interval:
                streaming = "\n".join(tool_lines) + "\n\n" + response_text if tool_lines else response_text
                api_request(api_key, "POST", "/api/bot/reply", {
                    "text": streaming, "project": project, "is_final": False,
                })
                last_stream_time = now

            if etype == "result":
                if "session_id" in event:
                    new_session_id = event["session_id"]
                _rtxt = event.get("result")
                if _rtxt and not event.get("is_error"):
                    # 한 claude -p 턴에서 result 이벤트가 여러 번 올 수 있다.
                    # (run_in_background 서브에이전트 완료 시 턴이 재개되며 두 번째 result 발생)
                    # 각 result는 '완결된 메시지'이므로 첫 것만 남기지 말고 모두 이어붙인다.
                    # (예전엔 `not response_text.strip()` 가드가 2번째 이후 result를 버려 백그라운드 결과가 유실됨)
                    # 각 result는 '다른 시각에 끝난' 별개의 발화다.
                    # (예: "분석 중입니다" → 수 분 뒤 "분석 완료했습니다")
                    result_segments.append(_rtxt)
                    if response_text.strip():
                        # 합본(programmatic 호출자용)에는 구분선으로 발화 경계를 표시
                        response_text = response_text.rstrip() + "\n\n---\n\n" + _rtxt
                    else:
                        response_text = _rtxt
                if event.get("is_error"):
                    error_text = str(event.get("error", "")) + str(event.get("result", ""))
                    _err_lc = error_text.lower()
                    # 어떤 분기로 가든 원문을 남긴다. 분기 라벨만 남으면 오분류를 못 잡는다.
                    logger.warning(f"[{bot_name}] CLI error for {project}: {error_text[:300]}")
                    # 529 판정은 '상태코드 모양'일 때만. 예전엔 `"529" in error_text` 라서
                    # 숫자에 529가 우연히 섞이기만 해도 과부하로 오판했다.
                    # (예: "prompt is too long: 195529 tokens" → 컨텍스트 초과인데 과부하로 처리되어
                    #  세션 리셋 대신 같은 요청을 3번 재시도하고, 진짜 원인은 로그에 안 남음)
                    _is_overloaded = (
                        "overloaded" in _err_lc
                        or re.search(r"(?:status|code|error|http)\D{0,10}\b529\b", _err_lc) is not None
                        or "(529)" in error_text
                    )
                    if _is_overloaded:
                        MAX_OVERLOAD_RETRIES = 3
                        wait_times = [10, 30, 60]
                        if _overload_retry < MAX_OVERLOAD_RETRIES:
                            wait = wait_times[_overload_retry]
                            logger.warning(f"[{bot_name}] Overloaded (529) for {project}, retry {_overload_retry + 1}/{MAX_OVERLOAD_RETRIES} in {wait}s: {error_text[:300]}")
                            if stream_to_chat:
                                api_request(api_key, "POST", "/api/bot/reply", {
                                    "text": f"(Anthropic 서버 과부하, {wait}초 후 재시도 {_overload_retry + 1}/{MAX_OVERLOAD_RETRIES}...)", "project": project, "is_final": False,
                                })
                            proc.wait(timeout=5)
                            _release_semaphore()
                            time.sleep(wait)
                            # D9-B: forward team params on retry
                            return run_claude(prompt, project, _retry_count, _overload_retry + 1,
                                session_key_override=session_key_override, prompt_override=prompt_override, stream_to_chat=stream_to_chat,
                                segments_out=segments_out)
                        return ("(Anthropic 서버 과부하 - 잠시 후 다시 시도해주세요)", new_session_id, tool_lines, False)
                    # 계정 사용 한도 — 세션 리셋도 재시도도 답이 아니다. 모델별이면 갈아타고,
                    # 세션창이면 리셋까지 전역으로 멈춘다. (2026-08-16~18 실사고, limits.py 주석 참조)
                    _limit = detect_usage_limit(error_text)
                    if _limit:
                        record_cap_hit(project, f"계정 한도({_limit['kind']}:{_limit['label']})", config)
                        proc.wait(timeout=5)
                        _release_semaphore()
                        if _limit["kind"] == "model":
                            _fb = _fallback_model(model) if _limit_retry == 0 else None
                            if _fb:
                                logger.warning(f"[{bot_name}] {model} limit for {project} → falling back to {_fb}")
                                if stream_to_chat:
                                    api_request(api_key, "POST", "/api/bot/reply", {
                                        "text": f"({_limit['label']} 한도 도달 — {_fb} 로 전환해 다시 시도합니다)",
                                        "project": project, "is_final": False,
                                    })
                                return run_claude(prompt, project, _retry_count, 0,
                                    session_key_override=session_key_override, prompt_override=prompt_override,
                                    stream_to_chat=stream_to_chat, segments_out=segments_out,
                                    model_override=_fb, _limit_retry=_limit_retry + 1)
                            return (usage_limit_notice(_limit), new_session_id, tool_lines, False)
                        start_account_cooldown("세션 한도", _limit.get("resets", ""), account_name)
                        return (usage_limit_notice(_limit), new_session_id, tool_lines, False)
                    if "could not process image" in error_text.lower() or "invalid_request_error" in error_text.lower():
                        logger.warning(f"[{bot_name}] Image/request error for {project}, resetting session (retry {_retry_count + 1})")
                        conv = _fetch_recent_conversation(project, limit=10)
                        if conv:
                            _save_session_summary(project, f"[이미지 처리 오류로 자동 리셋 — 최근 대화 원본]\n\n{conv}")
                        reset_session(project, key_override=_sid_key)
                        proc.wait(timeout=5)
                        _release_semaphore()
                        if _retry_count >= MAX_CONTEXT_OVERFLOW_RETRIES:
                            return ("(이미지 처리 오류 - 세션이 초기화되었습니다. 다시 말씀해주세요.)", None, tool_lines, False)
                        return run_claude(prompt, project, _retry_count + 1, 0,
                            session_key_override=session_key_override, prompt_override=prompt_override, stream_to_chat=stream_to_chat,
                                segments_out=segments_out)
                    if "context" in error_text.lower() or "prompt is too long" in error_text.lower():
                        logger.warning(f"[{bot_name}] Context overflow for {project}, resetting (retry {_retry_count + 1})")
                        conv = _fetch_recent_conversation(project, limit=10)
                        if conv:
                            _save_session_summary(project, f"[컨텍스트 오버플로우로 자동 리셋 — 최근 대화 원본]\n\n{conv}")
                        reset_session(project, key_override=_sid_key)
                        proc.wait(timeout=5)
                        _release_semaphore()
                        if _retry_count >= MAX_CONTEXT_OVERFLOW_RETRIES:
                            return ("(컨텍스트 초과 - 최대 재시도 횟수 초과)", None, tool_lines, False)
                        return run_claude(prompt, project, _retry_count + 1, 0,
                            session_key_override=session_key_override, prompt_override=prompt_override, stream_to_chat=stream_to_chat,
                                segments_out=segments_out)
                    if _text_signals_auth_expired(error_text):
                        # Do NOT touch credentials/Keychain — Claude CLI manages its own token
                        # refresh, and deleting the Keychain entry can destroy the only valid
                        # credential source (file may be expired while Keychain is valid).
                        logger.warning(f"[{bot_name}] Auth expired for {project}: starting self-service re-login {_auth_diag(_spawn_concurrency)}")
                        proc.kill()
                        _release_semaphore()
                        return (_trigger_relogin(project, account_config_dir), new_session_id, tool_lines, False)

        proc.wait(timeout=300)

        stderr_output = proc.stderr.read().decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not response_text:
            logger.error(f"[{bot_name}] Claude exited {proc.returncode}: {stderr_output[:500]}")
            if "context" in stderr_output.lower() or "prompt is too long" in stderr_output.lower():
                logger.warning(f"[{bot_name}] Context overflow (stderr) for {project}, resetting (retry {_retry_count + 1})")
                conv = _fetch_recent_conversation(project, limit=10)
                if conv:
                    _save_session_summary(project, f"[컨텍스트 오버플로우(stderr)로 자동 리셋 — 최근 대화 원본]\n\n{conv}")
                reset_session(project, key_override=_sid_key)
                _release_semaphore()
                if _retry_count >= MAX_CONTEXT_OVERFLOW_RETRIES:
                    return ("(컨텍스트 초과 - 최대 재시도 횟수 초과)", None, tool_lines, False)
                return run_claude(prompt, project, _retry_count + 1, 0,
                    session_key_override=session_key_override, prompt_override=prompt_override, stream_to_chat=stream_to_chat,
                                segments_out=segments_out)
            if "no conversation found" in stderr_output.lower():
                logger.warning(f"[{bot_name}] Invalid session for {project}, clearing and retrying")
                reset_session(project, key_override=_sid_key)
                _release_semaphore()
                if _retry_count >= MAX_CONTEXT_OVERFLOW_RETRIES:
                    return ("(세션 오류 - 최대 재시도 횟수 초과)", None, tool_lines, False)
                return run_claude(prompt, project, _retry_count + 1, 0,
                    session_key_override=session_key_override, prompt_override=prompt_override, stream_to_chat=stream_to_chat,
                                segments_out=segments_out)
            # 한도 문구가 stderr 로만 새는 경우(스트림에 result 이벤트가 안 온 턴)도 같이 잡는다
            _limit = detect_usage_limit(stderr_output)
            if _limit:
                record_cap_hit(project, f"계정 한도(stderr:{_limit['kind']})", config)
                _release_semaphore()
                if _limit["kind"] == "session":
                    start_account_cooldown("세션 한도", _limit.get("resets", ""))
                elif _limit_retry == 0 and _fallback_model(model):
                    _fb = _fallback_model(model)
                    logger.warning(f"[{bot_name}] {model} limit (stderr) for {project} → falling back to {_fb}")
                    return run_claude(prompt, project, _retry_count, 0,
                        session_key_override=session_key_override, prompt_override=prompt_override,
                        stream_to_chat=stream_to_chat, segments_out=segments_out,
                        model_override=_fb, _limit_retry=_limit_retry + 1)
                return (usage_limit_notice(_limit), new_session_id, tool_lines, False)
            # stderr 에 인증만료 텍스트가 직접 보이면 즉시 재로그인
            if _text_signals_auth_expired(stderr_output):
                logger.warning(f"[{bot_name}] Auth expired (stderr) for {project}: starting self-service re-login {_auth_diag(_spawn_concurrency)}")
                _release_semaphore()
                return (_trigger_relogin(project, account_config_dir), new_session_id, tool_lines, False)
            # 원인 불명(exit≠0 & stderr 빈값 & 응답 빈값) → plain 프로브로 인증만료 확정 (hoon 사례)
            if not stderr_output and _probe_auth_expired(account_config_dir):
                logger.warning(f"[{bot_name}] Auth expired (probe) for {project}: starting self-service re-login {_auth_diag(_spawn_concurrency)}")
                _release_semaphore()
                return (_trigger_relogin(project, account_config_dir), new_session_id, tool_lines, False)
            return (f"(Claude 오류: exit {proc.returncode})", new_session_id, tool_lines, False)

        if not response_text:
            response_text = "(작업 완료)" if tool_lines else "(응답 없음)"

        response_text = _strip_ansi(response_text)

        # ── 멈춤으로 끝난 턴: 정직한 중단 보고 (계획서 D5/D6/D7) ──
        if stop_initiated:
            dirty = count_dirty_files(project_dir)
            note = "⏹ 작업이 중단되었습니다"
            if dirty:
                note += f" — 커밋되지 않은 변경 {dirty}개 파일이 남아 있습니다"
            note += ". 이미 실행된 외부 작업(배포·발송 등)은 취소되지 않습니다."
            if stop_forced:
                # 강제 킬 — 세션 transcript 가 온전치 않을 수 있으니 다음 턴에 상태확인 가드 주입
                mark_interrupted(project)
            result_segments.append(note)
            response_text = (response_text.rstrip() + "\n\n" + note) if response_text.strip() else note

        if response_text.strip().lower() == "prompt is too long" and _retry_count < MAX_CONTEXT_OVERFLOW_RETRIES:
            logger.warning(f"[{bot_name}] Prompt too long for {project}, resetting (retry {_retry_count + 1})")
            conv = _fetch_recent_conversation(project, limit=10)
            if conv:
                _save_session_summary(project, f"[프롬프트 초과로 자동 리셋 — 최근 대화 원본]\n\n{conv}")
            reset_session(project, key_override=_sid_key)
            _release_semaphore()
            return run_claude(prompt, project, _retry_count + 1, 0,
                session_key_override=session_key_override, prompt_override=prompt_override, stream_to_chat=stream_to_chat,
                                segments_out=segments_out)

        if new_session_id:
            # D7: save session by overridden key when team member
            _save_key = session_key_override or session_key(project)
            update_session_by_key(_save_key, new_session_id, account=account_name)
            # 브랜치 세션 ID를 DB에도 동기화
            if is_branch and new_session_id != sid and not session_key_override:
                from daemon.branches import update_branch_session
                update_branch_session(int(project.split(":")[1]), new_session_id)

        if segments_out is not None:
            # response_text 와 동일하게 ANSI 이스케이프를 벗긴다.
            # (안 벗기면 발화가 2개 이상일 때만 raw escape 가 채팅에 노출되는 불일치가 생김)
            segments_out[:] = [t for t in (_strip_ansi(s).strip() for s in result_segments) if t]

        # 정상 완료 = 이 시점엔 access token 이 유효했다는 뜻 (토큰 신선도 기준점)
        g.last_claude_ok = time.time()
        # 한도도 풀렸다는 뜻 — 쿨다운이 걸려 있었다면(카나리아 통과) 즉시 해제
        clear_account_cooldown(account_name)
        return (response_text, new_session_id, tool_lines, False)

    except subprocess.TimeoutExpired:
        proc.kill()
        logger.error(f"[{bot_name}] Claude timed out for {project}")
        return ("(Claude 응답 시간 초과)", sid, [], True)
    except Exception as e:
        logger.error(f"[{bot_name}] Claude error: {e}")
        return (f"(Claude 실행 오류: {e})", sid, [], False)
    finally:
        if total_usage.get("model"):
            try:
                api_request(api_key, "POST", "/api/usage", body={
                    "project": project,
                    "model": total_usage["model"],
                    "input_tokens": total_usage["input"],
                    "output_tokens": total_usage["output"],
                    "cache_read_tokens": total_usage["cache_read"],
                    "cache_write_tokens": total_usage["cache_write"],
                })
            except Exception as e:
                logger.warning(f"[{bot_name}] Usage report failed: {e}")
        if _counted:
            with g.claude_inflight_lock:
                g.claude_inflight -= 1
        try:
            _release_semaphore()
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
            env=build_claude_env(config),
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


# ─── Codex CLI execution ───────────────────────────────────────

_AGENTS_MD_MARKER_START = "<!-- PETERVOICE-DAEMON-PROMPT-START -->"
_AGENTS_MD_MARKER_END = "<!-- PETERVOICE-DAEMON-PROMPT-END -->"


def _prepare_agents_md(project_dir: str, prompt_content: str):
    """Write daemon prompt into AGENTS.md in the project directory.

    If AGENTS.md already exists, the daemon section (between markers) is
    replaced while preserving the rest of the file.
    """
    agents_path = os.path.join(project_dir, "AGENTS.md")
    existing = ""
    if os.path.exists(agents_path):
        try:
            with open(agents_path, "r", encoding="utf-8") as f:
                existing = f.read()
        except Exception:
            existing = ""

    daemon_block = f"{_AGENTS_MD_MARKER_START}\n{prompt_content}\n{_AGENTS_MD_MARKER_END}"

    if _AGENTS_MD_MARKER_START in existing:
        pattern = re.escape(_AGENTS_MD_MARKER_START) + r".*?" + re.escape(_AGENTS_MD_MARKER_END)
        merged = re.sub(pattern, daemon_block, existing, flags=re.DOTALL)
    elif existing.strip():
        merged = f"{existing}\n\n{daemon_block}"
    else:
        merged = daemon_block

    with open(agents_path, "w", encoding="utf-8") as f:
        f.write(merged)


def run_codex(prompt: str, project: str, _retry_count: int = 0) -> tuple[str, str | None, list[str], bool]:
    """Run Codex CLI and return (response_text, session_id, tool_lines)."""
    api_key = config["api_key"]

    # Resolve project directory (same logic as run_claude)
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

    sid = get_codex_session_id(project)

    settings_project = real_project if (is_branch or is_kanban) else project
    proj_settings = _fetch_project_settings(settings_project)

    # Build system prompt (reuse existing logic)
    if is_branch and branch_data:
        combined = build_branch_prompt(branch_data)
        if not sid:
            context_block = build_branch_context(branch_data)
            prompt = f"{context_block}\n\n---\n\n{prompt}"
    elif is_kanban and kanban_card:
        from daemon.kanban import build_kanban_card_context
        combined = build_kanban_prompt(kanban_card)
        if not sid:
            card_context = build_kanban_card_context(kanban_card)
            prompt = f"{card_context}\n\n---\n\n{prompt}"
    else:
        current_task = get_current_task(project)
        task_desc = get_task_description(project, current_task)
        sys_prompt = build_system_prompt(project, current_task, task_desc)
        system_prompt_pv = fetch_prompt_from_supabase("_petervoice_system", user_id_override=0) or ""
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
        session_context = ""
        if not sid:
            session_context = _build_session_context_prompt(project)
        combined = "\n\n".join(p for p in [sys_prompt, system_prompt_pv, common_prompt, prompt_content, session_context] if p)

    # Write AGENTS.md with daemon prompt
    if combined:
        _prepare_agents_md(project_dir, combined)

    # Build command
    # Codex 모델 해석: model 컬럼 우선(통합 UI가 여기에 gpt-* 저장), codex_model은 하위호환 fallback.
    branch_model = branch_data.get("model") if is_branch and branch_data else None
    model = (
        branch_model
        or proj_settings.get("model")
        or proj_settings.get("codex_model")
        or config.get("codex_default_model")
    )
    # 엔진=codex인데 Claude 모델 코드가 남아있으면(과거 설정 잔재) 무시하고 Codex 기본값 사용.
    if model and (model.startswith("claude") or model in ("opus", "sonnet", "haiku")):
        model = config.get("codex_default_model")
    use_model_flag = bool(model)
    bot_name = config.get("bot_name", "bot")

    if sid:
        cmd = [
            CODEX_CMD, "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        if use_model_flag:
            cmd.extend(["-m", model])
        cmd.extend(["resume", sid, prompt])
    else:
        cmd = [
            CODEX_CMD, "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", project_dir,
        ]
        if use_model_flag:
            cmd.extend(["-m", model])
        cmd.append(prompt)

    logger.info(f"[{bot_name}] Codex: project={project}, dir={project_dir}, session={sid or 'new'}, model={model or 'default'}")

    # run_claude 와 동일한 이유로 한 번만 반납한다 (세션 오류 재시도 시 이중 release → 동시성 누수)
    _sem_held = False

    def _release_semaphore():
        nonlocal _sem_held
        if _sem_held:
            _sem_held = False
            g.claude_semaphore.release()

    try:
        g.claude_semaphore.acquire()
        _sem_held = True
        codex_env = {
            **os.environ,
            "LANG": "en_US.UTF-8",
        }
        # Inject OpenAI API key from daemon config if available
        openai_key = config.get("openai_api_key")
        if openai_key:
            codex_env["OPENAI_API_KEY"] = openai_key

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_dir,
            env=codex_env,
            shell=IS_WINDOWS,
        )

        response_text = ""
        new_session_id = sid
        tool_lines = []
        last_stream_time = time.time()
        last_activity_time = time.time()
        process_start_time = time.time()
        stdout_timeout = config.get("claude_stdout_timeout_sec", 600)
        hard_timeout = config.get("claude_hard_timeout_sec", 900)
        stream_interval = config.get("stream_interval_sec", 2.0)

        _drain_deadline = None

        while True:
            if shutdown_event.is_set():
                # 드레인 종료 (run_claude와 동일): 상한까지 완주, 초과 시에만 kill+센티널
                if _drain_deadline is None:
                    _drain_sec = config.get("shutdown_drain_sec", 90)
                    _drain_deadline = time.time() + _drain_sec
                    logger.info(f"[{bot_name}] Shutdown requested — draining codex turn for {project} (up to {_drain_sec}s)")
                elif time.time() >= _drain_deadline:
                    logger.warning(f"[{bot_name}] Drain limit exceeded for {project} — killing codex; message stays queued for auto-resume")
                    proc.kill()
                    return (SHUTDOWN_INTERRUPTED, new_session_id, tool_lines, False)

            elapsed = time.time() - process_start_time
            if elapsed > hard_timeout:
                logger.error(f"[{bot_name}] Codex hard timeout ({hard_timeout}s, elapsed {elapsed:.0f}s) for {project}, killing")
                proc.kill()
                return (f"(Codex 실행 시간 초과 - {elapsed:.0f}초 경과)", sid, tool_lines, True)

            if IS_WINDOWS:
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
                _t.join(timeout=2)
                ready = _line_ready.is_set()
            else:
                # 2초 — 조용한 구간(긴 도구 실행 등)에도 멈춤 요청에 빠르게 반응
                ready_list, _, _ = select.select([proc.stdout], [], [], 2)
                ready = bool(ready_list)

            if not ready:
                if proc.poll() is not None:
                    break
                if time.time() - last_activity_time > stdout_timeout:
                    logger.error(f"[{bot_name}] Codex stdout timeout ({stdout_timeout}s) for {project}, killing")
                    proc.kill()
                    return (f"(Codex 응답 시간 초과 - {stdout_timeout}초 동안 출력 없음)", sid, tool_lines, True)
                # Check stop request (project 매칭 — 구형 웹의 전역 stop 도 허용)
                uid = resolve_user_id()
                _sr = get_stop_request(uid) if uid else {"requested": False}
                if _sr.get("requested") and (not _sr.get("project") or _sr.get("project") == project):
                    logger.info(f"[{bot_name}] Stop requested for {project}, terminating codex process")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    clear_stop_requested(uid)
                    partial = response_text.strip()
                    result = partial + "\n\n(작업이 중단되었습니다)" if partial else "(작업이 중단되었습니다)"
                    return (result, new_session_id, tool_lines, False)
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

            if etype == "thread.started":
                new_session_id = event.get("thread_id")

            elif etype == "item.completed":
                item = event.get("item", {})
                item_type = item.get("type", "")

                if item_type == "agent_message":
                    msg = item.get("text", "")
                    response_text = response_text + "\n\n" + msg if response_text else msg
                    streaming = "\n".join(tool_lines) + "\n\n" + response_text if tool_lines else response_text
                    api_request(api_key, "POST", "/api/bot/reply", {
                        "text": streaming, "project": project, "is_final": False,
                    })
                    last_stream_time = time.time()

                elif item_type == "command":
                    cmd_str = item.get("exec_command", "")
                    tool_lines.append(f"🔧 Shell: {cmd_str[:80]}")
                    api_request(api_key, "POST", "/api/bot/reply", {
                        "text": "\n".join(tool_lines), "project": project, "is_final": False,
                    })

                elif item_type == "file_change":
                    fp = item.get("file_path", "")
                    tool_lines.append(f"🔧 FileChange: {fp}")

            elif etype == "turn.completed":
                usage = event.get("usage", {})
                logger.info(f"[{bot_name}] Codex turn done for {project}: input={usage.get('input_tokens', '?')}, output={usage.get('output_tokens', '?')}")

            elif etype == "error":
                error_msg = event.get("message", str(event))
                logger.error(f"[{bot_name}] Codex error for {project}: {error_msg}")
                if not response_text:
                    response_text = f"(Codex 오류: {error_msg[:200]})"

        proc.wait(timeout=300)

        stderr_output = proc.stderr.read().decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not response_text:
            logger.error(f"[{bot_name}] Codex exited {proc.returncode}: {stderr_output[:500]}")
            # On session error, reset and retry
            if "session" in stderr_output.lower() or "not found" in stderr_output.lower():
                logger.warning(f"[{bot_name}] Invalid codex session for {project}, resetting (retry {_retry_count + 1})")
                reset_codex_session(project)
                _release_semaphore()
                if _retry_count >= MAX_CONTEXT_OVERFLOW_RETRIES:
                    return ("(Codex 세션 오류 - 최대 재시도 횟수 초과)", None, tool_lines, False)
                return run_codex(prompt, project, _retry_count + 1)
            return (f"(Codex 오류: exit {proc.returncode})", new_session_id, tool_lines, False)

        if not response_text:
            response_text = "(작업 완료)" if tool_lines else "(응답 없음)"

        if new_session_id:
            update_codex_session(project, new_session_id)

        return (response_text, new_session_id, tool_lines, False)

    except subprocess.TimeoutExpired:
        proc.kill()
        logger.error(f"[{bot_name}] Codex timed out for {project}")
        return ("(Codex 응답 시간 초과)", sid, [], True)
    except FileNotFoundError:
        logger.error(f"[{bot_name}] Codex not installed on this machine ({CODEX_CMD})")
        return (
            "이 기기에는 Codex(OpenAI CLI)가 설치되어 있지 않습니다. "
            "Codex를 설치하고 로그인하면 이 모델을 사용할 수 있어요. "
            "그 전까지는 헤더에서 Claude 모델을 선택해 주세요.",
            sid, [], False,
        )
    except Exception as e:
        logger.error(f"[{bot_name}] Codex error: {e}")
        return (f"(Codex 실행 오류: {e})", sid, [], False)
    finally:
        try:
            _release_semaphore()
        except ValueError:
            pass
