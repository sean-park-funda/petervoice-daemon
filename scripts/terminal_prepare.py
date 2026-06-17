#!/usr/bin/env python3
"""Terminal-mode session preparer.

home-portal.js calls this right before creating a tmux `claude` session so the
terminal claude starts with the SAME project context the chat mode injects:

  - resolves the project working directory the same way the daemon does
    (authoritative source = web API), so chat & terminal share one folder (C-1)
  - composes the project/branch system prompt (common + system + project +
    branch/kanban rules + wiki index + prior-session summary) exactly like chat,
    and writes it to a prompt file for `claude --append-system-prompt-file` (C-2)
  - appends a "how to recall past chat" block pointing at the conversation API,
    since terminal claude isn't wired to the DB the way the daemon is (C-3)

Usage:
    python3 terminal_prepare.py --project <id> [--branch <branch_id>]

Prints a single JSON line to stdout:
    {"dir": "<projectDir>", "prompt_file": "<path>"}

On any failure it still prints valid JSON with whatever it could resolve
(at minimum an empty prompt_file and a best-effort dir) so the terminal can
still open — degraded, not broken.
"""

import argparse
import json
import os
import sys
import uuid
import glob
from pathlib import Path

# Make `daemon` importable regardless of cwd
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

PROMPTS_DIR = Path.home() / ".claude-daemon" / "prompts"
# Per-terminal claude session registry: sessionKey -> {session_id, dir}
TERMINAL_SESSIONS_PATH = Path.home() / ".claude-daemon" / "terminal-sessions.json"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _session_key(project, branch):
    """Mirror home-portal getSessionKey() so the registry key matches the tmux key."""
    def sanitize(s):
        return s.replace(":", "_").replace(".", "_")
    return f"{sanitize(project)}__br{branch}" if branch else sanitize(project)


def _load_registry():
    try:
        return json.loads(TERMINAL_SESSIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_registry(reg):
    try:
        TERMINAL_SESSIONS_PATH.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    except Exception:
        pass


def _transcript_exists(session_id):
    """claude session ids are globally unique, so a match anywhere under
    ~/.claude/projects/*/ confirms the session transcript is still on disk."""
    if not session_id:
        return False
    return bool(glob.glob(str(CLAUDE_PROJECTS_DIR / "*" / f"{session_id}.jsonl")))


def _resolve_session_id(session_key, project_dir):
    """Return (session_id, resume_bool). Reuse the stored id if its transcript
    still exists AND the dir matches; otherwise mint a fresh one and store it."""
    reg = _load_registry()
    entry = reg.get(session_key)
    if entry and entry.get("dir") == project_dir and _transcript_exists(entry.get("session_id")):
        return entry["session_id"], True
    new_id = str(uuid.uuid4())
    reg[session_key] = {"session_id": new_id, "dir": project_dir}
    _save_registry(reg)
    return new_id, False


def _recall_block(project_arg: str) -> str:
    """Instructions so terminal claude can pull past chat history on demand."""
    return f"""# 터미널 모드 — 과거 대화 불러오기

당신은 지금 피터보이스 **터미널 모드**로 실행 중입니다. 채팅 모드와 달리 과거 대화가
자동으로 주입되지 않습니다. 유저가 "이전 대화 확인해", "아까 뭐라 했지?" 등 과거 맥락을
요청하거나 맥락이 필요하면, 아래 API로 최근 대화를 직접 조회하세요.

```bash
API_URL=$(python3 -c "import json,os; c=json.load(open(os.path.expanduser('~/.claude-daemon/config.json'))); print(c.get('api_url','https://peter-voice.vercel.app'))")
API_KEY=$(python3 -c "import json,os; print(json.load(open(os.path.expanduser('~/.claude-daemon/config.json')))['api_key'])")
curl -s "$API_URL/api/bot/conversation?project={project_arg}&limit=20" -H "Authorization: Bearer $API_KEY"
```

- 응답: `{{"messages": [{{"type": "user"|"bot", "text": "..."}}]}}`
- 이 프로젝트의 채팅 모드 대화가 그대로 보입니다. 맥락을 놓쳤으면 먼저 조회 후 답하세요."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--branch", default=None)
    args = ap.parse_args()

    # Reconstruct the daemon's project identifier form
    if args.branch:
        project_arg = f"branch:{args.branch}"
    else:
        project_arg = args.project

    result = {"dir": os.path.expanduser("~"), "prompt_file": "", "session_id": "", "resume": False}
    session_key = _session_key(args.project, args.branch)

    try:
        # Standalone process: load config (api_url/api_key) the daemon way,
        # otherwise every API-backed resolver silently falls back to defaults.
        from daemon.config import load_config
        load_config()

        from daemon.supabase import get_project_dir

        combined = ""
        if project_arg.startswith("branch:"):
            from daemon.branches import fetch_branch, build_branch_prompt, build_branch_context
            branch_id = project_arg.split(":", 1)[1]
            branch_data = fetch_branch(branch_id)
            real_project = (branch_data or {}).get("project_id", "general")
            result["dir"] = get_project_dir(real_project)
            if branch_data:
                combined = build_branch_prompt(branch_data)
                ctx = build_branch_context(branch_data)
                if ctx:
                    combined = (combined + "\n\n---\n\n" + ctx) if combined else ctx
        else:
            from daemon.claude_runner import build_project_prompt
            result["dir"] = get_project_dir(project_arg)
            combined = build_project_prompt(project_arg)

        # C-3: append the recall instructions
        recall = _recall_block(project_arg)
        combined = (combined + "\n\n---\n\n" + recall) if combined else recall

        # Write prompt file (named by project arg, sanitized)
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        safe = project_arg.replace(":", "_").replace("/", "_")
        prompt_file = PROMPTS_DIR / f"_terminal_{safe}.md"
        prompt_file.write_text(combined, encoding="utf-8")
        result["prompt_file"] = str(prompt_file)

        # 이전 터미널 대화 이어가기: 트랜스크립트가 남아있으면 --resume, 아니면 새 --session-id
        sid, resume = _resolve_session_id(session_key, result["dir"])
        result["session_id"] = sid
        result["resume"] = resume
    except Exception as e:
        # Degrade gracefully — terminal still opens, just without injected context
        sys.stderr.write(f"[terminal_prepare] {e}\n")

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
