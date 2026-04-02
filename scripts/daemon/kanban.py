"""Kanban card agent session management.

Polls kanban_messages for unprocessed user messages, runs Claude with
card-specific context and isolated config dir, and saves responses back.
All DB access goes through PeterVoice web API.
"""

import os
import json
import subprocess
from pathlib import Path

import daemon.globals as g
from daemon.globals import (
    config, shutdown_event, logger, PROMPTS_DIR, SECRETS_ENV_PATH,
    DAEMON_DIR, CLAUDE_CMD, IS_WINDOWS,
)
from daemon.api import api_request
from daemon.supabase import (
    resolve_user_id, get_project_dir, fetch_prompt_from_supabase,
    _fetch_project_settings,
)
from daemon.prompts import build_system_prompt
from daemon.sessions import session_key


# ─── Constants ──────────────────────────────────────────────

KANBAN_DIR = DAEMON_DIR / "kanban"


# ─── API helpers ───────────────────────────────────────────

def _fetch_kanban_card(card_id: int) -> dict | None:
    """Fetch a single kanban card by ID."""
    api_key = config.get("api_key", "")
    if not api_key:
        return None
    result = api_request(api_key, "GET", f"/api/bot/kanban?card_id={card_id}", timeout=5)
    return result if result and "error" not in result else None


def _get_kanban_enabled_projects() -> set[str]:
    """Fetch project IDs where kanban_enabled=true for the current user."""
    api_key = config.get("api_key", "")
    if not api_key:
        return set()
    result = api_request(api_key, "GET", "/api/bot/kanban?projects=enabled", timeout=5)
    if result and "project_ids" in result:
        return set(result["project_ids"])
    return set()


def fetch_pending_kanban_messages() -> list[dict]:
    """Fetch unprocessed kanban_messages for the current user's cards."""
    api_key = config.get("api_key", "")
    if not api_key:
        return []
    result = api_request(api_key, "GET", "/api/bot/kanban/messages", timeout=10)
    if result and "messages" in result:
        return result["messages"]
    return []


def mark_kanban_message_processed(msg_id: int):
    """Mark a kanban_message as processed."""
    api_key = config.get("api_key", "")
    if not api_key:
        return
    api_request(api_key, "PATCH", "/api/bot/kanban/messages", body={"id": msg_id}, timeout=5)


def save_kanban_reply(card_id: int, text: str, msg_type: str = "bot"):
    """Save a message for kanban card."""
    api_key = config.get("api_key", "")
    if not api_key:
        return
    api_request(api_key, "POST", "/api/bot/kanban/reply", body={
        "card_id": card_id,
        "text": text,
        "type": msg_type,
    }, timeout=10)


def update_card_session(card_id: int, session_id: str):
    """Update kanban_cards.session_id."""
    api_key = config.get("api_key", "")
    if not api_key:
        return
    api_request(api_key, "PATCH", "/api/bot/kanban/session", body={
        "card_id": card_id,
        "session_id": session_id,
    }, timeout=5)


# ─── Context building ───────────────────────────────────────

def build_kanban_prompt(card: dict) -> str:
    """Build the combined system prompt for a kanban card session."""
    project_id = card.get("project_id", "")

    # Layer 1: Common prompt
    common_prompt = fetch_prompt_from_supabase("_common") or ""
    if common_prompt and "{동적으로 키 목록 삽입}" in common_prompt:
        secret_keys = []
        if SECRETS_ENV_PATH.exists():
            for line in SECRETS_ENV_PATH.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    secret_keys.append(line.split("=", 1)[0])
        key_list = "\n".join(f"- {k}" for k in secret_keys) if secret_keys else "(없음)"
        common_prompt = common_prompt.replace("{동적으로 키 목록 삽입}", key_list)

    # Layer 2: Project prompt
    project_prompt_file = PROMPTS_DIR / f"{project_id}.md"
    project_prompt = project_prompt_file.read_text(encoding="utf-8") if project_prompt_file.exists() else ""

    # Layer 3: Card rules (시스템 프롬프트 — 매번 주입)
    card_num = card.get('card_number') or card.get('id')
    card_rules = f"""# 칸반 카드 #{card_num} (내부ID: {card.get('id')}) — 규칙

## 규칙 (매우 중요)
- 이 카드의 작업만 수행하세요
- 커밋 메시지 앞에 [card-{card.get('id')}] 를 붙이세요
- **대화 상대는 비개발자일 수 있습니다.** 기술 용어를 최소화하고 쉽게 설명하세요.

## 과거 대화 기록 조회
이 카드의 대화 기록을 확인해야 할 때 (이전 대화 내용, 맥락 파악 등):
```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://peter-voice.vercel.app'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")
curl -s "$API_URL/api/kanban/{card.get('id')}/messages" \\
  -H "Authorization: Bearer $API_KEY"
```
**주의**: 부모 프로젝트의 대화가 아닌, 반드시 **이 카드(`kanban:{card.get('id')}`)의 대화 기록**을 조회하세요.

## 작업 프로세스 (필수)
코드를 수정하기 **전에** 반드시 아래 프로세스를 따르세요:

1. **분석**: 요청을 분석하고, 현재 코드를 읽어 영향 범위를 파악
2. **계획 제시**: 무엇을 어떻게 바꿀지 계획을 **비개발자가 이해할 수 있는 언어로** 설명
   - 변경할 파일 목록과 각 변경의 목적
   - 예상되는 결과 (사용자가 보게 될 변화)
   - 위험 요소가 있다면 알림
3. **컨펌 대기**: "이대로 진행할까요?" 라고 물어보고 **상대방의 확인을 받은 후에만** 코드 수정
4. **구현**: 승인받은 계획대로만 구현. 범위를 임의로 넓히지 말 것
5. **결과 보고**: 완료 후 무엇이 바뀌었는지 쉬운 말로 요약

**예외**: "바로 해줘", "빨리 수정해" 등 명시적으로 즉시 실행을 요청하면 계획 단계를 생략할 수 있음.

## 상태 변경 API
```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://peter-voice.vercel.app'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")
curl -s -X PATCH "$API_URL/api/kanban/{card.get('id')}/status" \\
  -H "Authorization: Bearer $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"status": "STATUS_HERE"}}'
```

## 작업 종료 및 상태 전환
사용자가 **종료 의도**를 표현하면 ("다 됐어", "끝났어", "종료", "리뷰로 넘겨" 등):

### Step 1: 개발 완료 보고 작성
`git log --oneline --all --grep="[card-{card.get('id')}]"`로 이 카드의 커밋을 수집하고, 아래 형식으로 정리:
```
## 개발 완료 보고
### 커밋 목록
- [card-{card.get('id')}] abc1234 커밋 메시지
- [card-{card.get('id')}] def5678 커밋 메시지
### 변경 파일
- path/to/file.tsx (신규/수정 - 간단 설명)
### 변경 요약
무엇을 왜 바꿨는지 1-3줄 요약
### 수락 기준 충족 여부
- [x] 충족된 기준
- [ ] 미충족 기준 (사유)
```
이 보고를 **사용자에게 보여주세요** (응답 텍스트에 포함).

### Step 1.5: 개발 결과를 카드에 저장
커밋 목록과 변경 요약을 카드 DB에 저장합니다 (프로젝트 요약에 사용됨):
```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://peter-voice.vercel.app'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")
curl -s -X PATCH "$API_URL/api/kanban/{card.get('id')}" \\
  -H "Authorization: Bearer $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d "$(python3 -c "
import json, subprocess
commits = subprocess.run(['git', 'log', '--oneline', '--all', '--grep=[card-{card.get('id')}]'], capture_output=True, text=True).stdout.strip().split('\\n')
commits = [c for c in commits if c]
summary = '위에서 작성한 변경 요약을 여기에'
print(json.dumps({{'result_commits': commits, 'result_notes': summary}}))
")"
```
**중요**: `result_notes`에는 위 개발 완료 보고의 "변경 요약" 내용을 넣으세요.

### Step 2: 상태 변경
상태 변경 API로 카드를 **review**로 변경.
- 명시적으로 "완료(done)"를 요청하면 review 대신 done으로 변경

### Step 3: 코드리뷰 의뢰 (review 전환 시에만)
review로 전환한 경우, 아래 curl로 code-reviewer 에이전트에게 리뷰를 의뢰:
```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://peter-voice.vercel.app'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")
curl -s -X POST "$API_URL/api/relay/message" \\
  -H "X-Api-Key: $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d "$(python3 -c "
import json
msg = {{
    'to_project': 'code-reviewer',
    'from_project': '{card.get('project_id', '')}',
    'text': '''카드 #{card.get('id')} 코드리뷰 요청

프로젝트: {card.get('project_id', '')}
카드 제목: {card.get('title', '')}
수락 기준: {card.get('acceptance_criteria', '없음')}

[여기에 위에서 작성한 개발 완료 보고 전문을 붙여넣으세요]'''
}}
print(json.dumps(msg))
")"
```
**중요**: `text` 필드에 개발 완료 보고 전문을 포함해야 합니다. `[여기에...]` 부분을 실제 보고 내용으로 교체하세요.

완료 후 "카드 #{card.get('id')}을 리뷰 상태로 변경하고, 코드리뷰를 의뢰했습니다" 라고 알림.

## 리뷰/완료 카드에서 수정 요청 받았을 때
이 카드가 **review** 또는 **done** 상태인데 수정 요청이 오면:
1. 상태 변경 API로 **dev**로 되돌림
2. "카드를 다시 개발 상태로 되돌렸습니다" 라고 알림
3. 일반 작업 프로세스대로 수정 진행
- 단순 질문은 상태 변경 없이 답변만
"""

    combined = "\n\n".join(p for p in [common_prompt, project_prompt, card_rules] if p)
    return combined


def build_kanban_card_context(card: dict) -> str:
    """Build the initial card context message (injected only at session start)."""
    card_num = card.get('card_number') or card.get('id')
    return f"""아래는 이 칸반 카드의 정보입니다. 이 내용을 바탕으로 작업을 시작하세요.

## 카드 #{card_num}: {card.get('title', '')}

### 설명
{card.get('description', '') or '(없음)'}

### 수락 기준
{card.get('acceptance_criteria', '') or '(없음)'}

### 우선순위
{card.get('priority', 'normal')}

이 카드에서 무엇을 해야 하는지 분석한 후, 계획을 제시해 주세요."""


# ─── Session management ─────────────────────────────────────

def get_kanban_config_dir(card_id: int) -> Path:
    """Get the isolated CLAUDE_CONFIG_DIR for a kanban card."""
    card_dir = KANBAN_DIR / f"card-{card_id}"
    card_dir.mkdir(parents=True, exist_ok=True)
    return card_dir


def run_kanban_claude(
    prompt: str,
    card: dict,
) -> tuple[str, str | None]:
    """Run Claude CLI for a kanban card with isolated session.

    Returns (response_text, session_id).
    """
    card_id = card.get("id")
    project_id = card.get("project_id", "")
    project_dir = get_project_dir(project_id)
    session_id = card.get("session_id")

    # Build command
    cmd = [
        CLAUDE_CMD, "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]

    # Model from project settings
    proj_settings = _fetch_project_settings(project_id)
    model = proj_settings.get("model") or config.get("claude_model")
    if model:
        cmd.extend(["--model", model])

    # Build prompt file
    combined = build_kanban_prompt(card)
    prompt_file = PROMPTS_DIR / f"_kanban_{card_id}.md"
    prompt_file.write_text(combined, encoding="utf-8")
    cmd.extend(["--append-system-prompt-file", str(prompt_file)])

    # Resume session if exists
    if session_id:
        cmd.extend(["--resume", session_id])

    cmd.extend(["--", prompt])

    # Environment — use account config dir if specified, otherwise default ~/.claude
    account_name = proj_settings.get("account") or "default"
    accounts = config.get("accounts", {})
    account_config_dir = accounts.get(account_name, {}).get("config_dir") if account_name != "default" else None

    claude_env = {
        **{k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        "LANG": "en_US.UTF-8",
    }
    if account_config_dir:
        claude_env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(account_config_dir)

    bot_name = config.get("bot_name", "bot")
    logger.info(f"[{bot_name}] Kanban Claude: card=#{card_id}, project={project_id}, session={session_id or 'new'}")

    try:
        g.claude_semaphore.acquire()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_dir,
            env=claude_env,
            shell=IS_WINDOWS,
        )

        import time
        import select as sel
        from daemon.utils import _strip_ansi

        response_text = ""
        new_session_id = session_id
        stdout_timeout = config.get("claude_stdout_timeout_sec", 600)
        last_stream_time = time.time()

        while True:
            if shutdown_event.is_set():
                proc.terminate()
                return ("(데몬 종료 중)", new_session_id)

            if time.time() - last_stream_time > stdout_timeout:
                logger.warning(f"[kanban] Card #{card_id}: stdout timeout ({stdout_timeout}s)")
                proc.terminate()
                break

            if IS_WINDOWS:
                raw = proc.stdout.readline()
            else:
                rlist, _, _ = sel.select([proc.stdout], [], [], 2.0)
                if not rlist:
                    if proc.poll() is not None:
                        break
                    continue
                raw = proc.stdout.readline()

            if not raw:
                if proc.poll() is not None:
                    break
                continue

            last_stream_time = time.time()
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")
            if etype == "result":
                response_text = _strip_ansi(event.get("result", ""))
                new_session_id = event.get("session_id", new_session_id)
            elif etype == "system" and event.get("subtype") == "init":
                new_session_id = event.get("session_id", new_session_id)

        proc.wait(timeout=10)

        # Log stderr for debugging
        try:
            stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
            if stderr_out:
                logger.warning(f"[kanban] Card #{card_id} stderr: {stderr_out[:500]}")
        except Exception:
            pass

        # Update card session_id if new
        if new_session_id and new_session_id != session_id:
            update_card_session(card_id, new_session_id)

        # If empty response and we resumed a session, retry without resume
        if not response_text and session_id:
            logger.warning(f"[kanban] Card #{card_id}: empty response with resumed session, retrying fresh")
            update_card_session(card_id, "")
            card_copy = dict(card)
            card_copy["session_id"] = None
            return run_kanban_claude(prompt, card_copy)

        return (response_text or "(응답 없음)", new_session_id)

    except Exception as e:
        logger.error(f"[kanban] Card #{card_id} error: {e}", exc_info=True)
        return (f"(에러: {e})", session_id)
    finally:
        g.claude_semaphore.release()


# ─── Process a single kanban message ────────────────────────

def process_kanban_message(msg: dict):
    """Process a single pending kanban message."""
    msg_id = msg.get("id")
    card = msg.get("kanban_cards", {})
    card_id = card.get("id")
    sender_name = msg.get("sender_name", "user")
    text = msg.get("text", "").strip()
    project_id = card.get("project_id", "")

    if not text or not card_id:
        mark_kanban_message_processed(msg_id)
        return

    bot_name = config.get("bot_name", "bot")
    logger.info(f"[{bot_name}] Kanban msg #{msg_id}: card=#{card_id}, project={project_id}, sender={sender_name}")

    # Prefix with sender name
    prompt = f"[{sender_name}] {text}" if sender_name else text

    mark_kanban_message_processed(msg_id)

    # Run Claude
    response, sid = run_kanban_claude(prompt, card)

    # Save reply
    if response:
        from daemon.claude_runner import rewrite_for_voice
        response = rewrite_for_voice(response)
        save_kanban_reply(card_id, response, "bot")

    logger.info(f"[{bot_name}] Kanban replied card=#{card_id}: {len(response)} chars")
