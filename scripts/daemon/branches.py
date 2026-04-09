"""Branch session support for the daemon.

Fetches branch data via API and builds prompts for branch sessions.
"""

from daemon.globals import config, SECRETS_ENV_PATH, logger
from daemon.api import api_request
from daemon.supabase import fetch_prompt_from_supabase


def _build_branch_relay_guide(project_id: str, branch_id: int) -> str:
    """Build relay (agent-to-agent communication) guide for a branch."""
    return f"""## 릴레이 (에이전트 간 통신)
다른 프로젝트/브랜치에 메시지를 보내거나, 다른 에이전트로부터 메시지를 받을 수 있습니다.

### 메시지 보내기
```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://peter-voice.vercel.app'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")

curl -X POST "$API_URL/api/relay/message" \\
  -H "X-Api-Key: $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "from_project": "{project_id}",
    "from_branch_id": {branch_id},
    "to_project": "대상_프로젝트_ID",
    "to_branch_id": null,
    "text": "메시지 내용",
    "attachments": []
  }}'
```
- `to_project`: **필수** — 대상 프로젝트 ID. 보내기 전 프로젝트 목록 조회 필수.
- `to_branch_id`: 특정 브랜치에 보내려면 브랜치 ID(숫자). 프로젝트 본체에 보내려면 생략 또는 null.
- `from_project`, `from_branch_id`: 자기 정보 — 수신 에이전트가 응답을 보낼 수 있도록 반드시 포함.
- `attachments`: 파일 절대 경로 배열 (선택). 상대가 Read로 읽음.

### 프로젝트 목록 조회 (릴레이 전 필수)
```bash
curl -s "$API_URL/api/projects" -H "X-Api-Key: $API_KEY" | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('projects', []):
    print(f'  {{p[\"id\"]}} — {{p[\"name\"]}}')
"
```

### 대상 프로젝트의 브랜치 목록 조회
```bash
curl -s "$API_URL/api/branches?project_id=대상_프로젝트_ID" -H "X-Api-Key: $API_KEY"
```

### [relay] 메시지 수신 시
- `[relay from:프로젝트명 branch:N]` 형식으로 수신됨
- 📎 첨부 문서가 있으면 **반드시 Read로 읽고** 응답
- 응답이 필요하면 `from_project`와 `from_branch_id`(있으면)로 릴레이 회신
"""


def fetch_branch(branch_id: int) -> dict | None:
    """Fetch branch data via GET /api/bot/branch?id=N."""
    api_key = config.get("api_key", "")
    if not api_key:
        return None
    result = api_request(api_key, "GET", f"/api/bot/branch?id={branch_id}", timeout=5)
    return result if result and "error" not in result else None


def update_branch_session(branch_id: int, session_id: str):
    """Update branch session_id via PATCH /api/bot/branch."""
    api_key = config.get("api_key", "")
    if not api_key:
        return
    api_request(api_key, "PATCH", "/api/bot/branch", body={
        "id": branch_id,
        "session_id": session_id,
    }, timeout=5)


def build_branch_prompt(branch: dict) -> str:
    """Build the combined system prompt for a branch session.

    Layers:
    1. _petervoice_system (모든 유저 공유)
    2. _common (유저별 공통)
    3. 프로젝트 프롬프트
    4. 브랜치/칸반 규칙
    """
    from daemon.prompts import get_prompt_file

    project_id = branch.get("project_id", "")

    # Layer 1: PeterVoice system prompt
    system_prompt_pv = fetch_prompt_from_supabase("_petervoice_system", user_id_override=0) or ""

    # Layer 2: Common prompt
    common_prompt = fetch_prompt_from_supabase("_common") or ""
    if common_prompt and "{동적으로 키 목록 삽입}" in common_prompt:
        secret_keys = []
        if SECRETS_ENV_PATH.exists():
            for line in SECRETS_ENV_PATH.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    secret_keys.append(line.split("=", 1)[0])
        key_list = "\n".join(f"- {k}" for k in secret_keys) if secret_keys else "(없음)"
        common_prompt = common_prompt.replace("{동적으로 키 목록 삽입}", key_list)

    # Layer 3: Project prompt
    from daemon.globals import PROMPTS_DIR
    project_prompt_file = PROMPTS_DIR / f"{project_id}.md"
    project_prompt = project_prompt_file.read_text(encoding="utf-8") if project_prompt_file.exists() else ""

    # Layer 4: Branch/kanban rules
    branch_num = branch.get("branch_number", branch.get("id"))
    branch_id = branch.get("id")
    # 이전 대화 조회 시 사용할 project ID (kanban 카드가 아닌 branch ID)
    conversation_hint = f"\n## 이전 대화 조회\n이 세션의 대화 기록은 `project=branch:{branch_id}`로 조회하세요. 칸반 카드 ID가 아닌 **branch:{branch_id}**를 사용할 것.\n"

    # 릴레이 가이드 (모든 브랜치 공통)
    relay_guide = _build_branch_relay_guide(project_id, branch_id)

    kanban_card_full = branch.get("kanban_card_full")
    if kanban_card_full:
        # 칸반 카드가 연결된 브랜치 → 기존 카드 규칙 사용
        from daemon.kanban import build_kanban_prompt
        kanban_combined = build_kanban_prompt(kanban_card_full)
        combined = "\n\n".join(p for p in [system_prompt_pv, kanban_combined, relay_guide, conversation_hint] if p)
        return combined
    else:
        # 순수 브랜치 → 간결한 브랜치 규칙
        branch_num = branch.get("branch_number", branch.get("id"))
        branch_id = branch.get("id")
        branch_rules = f"""# 브랜치 #{branch_num}: {branch.get('title', '')} (내부ID: {branch_id})
소속 프로젝트: {project_id}

## 규칙
- 이 브랜치의 작업에 집중하세요.
- 커밋 메시지 앞에 [branch-{branch_id}]를 붙이세요.
- 작업 범위를 임의로 넓히지 마세요.
- **대화 상대는 비개발자일 수 있습니다.** 기술 용어를 최소화하고 쉽게 설명하세요.

## 작업 종료
유저가 "다 됐어", "끝" 등을 말하면:
1. 변경 요약을 유저에게 보고
2. 브랜치 상태를 archived로 변경:
```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://peter-voice.vercel.app'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")
curl -X PATCH "$API_URL/api/branches/{branch_id}" \\
  -H "X-Api-Key: $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"status": "archived"}}'
```
"""
        combined = "\n\n".join(p for p in [system_prompt_pv, common_prompt, project_prompt, branch_rules, relay_guide, conversation_hint] if p)
        return combined


def build_branch_context(branch: dict) -> str:
    """Build the initial context block for a new branch session (prepended to first message)."""
    branch_num = branch.get("branch_number", branch.get("id"))
    title = branch.get("title", "")
    description = branch.get("description", "")
    parent_context = branch.get("parent_context", "")

    parts = [f"# 브랜치 #{branch_num}: {title}"]

    if description:
        parts.append(f"\n{description}")

    if parent_context:
        parts.append(f"\n## 이 브랜치의 배경 (부모 프로젝트 대화에서 캡처)\n{parent_context}")

    parts.append("\n위 맥락을 바탕으로 작업을 시작하세요.\n맥락이 부족하면 먼저 질문하세요.")

    return "\n".join(parts)
