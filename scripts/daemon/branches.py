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
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://www.peter-voice.site'))")
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

### 이 브랜치 아래 서브 브랜치 만들기 (깊이 제한 없음)
```bash
curl -X POST "$API_URL/api/branches" -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{{"project_id": "{project_id}", "parent_branch_id": {branch_id}, "title": "하위 작업 제목", "auto_context": true}}'
```
- `auto_context: true` 면 이 브랜치의 최근 대화가 요약 첨부된다. 응답 `branch.id` 로 릴레이·안내 링크 사용.

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


def fetch_branch_ancestors(branch: dict, max_depth: int = 50) -> list:
    """루트 브랜치 → 부모 순서의 조상 목록 (자기 자신 제외). parent_branch_id 체인을 API 로 따라간다."""
    chain = []
    seen = {branch.get("id")}
    parent_id = branch.get("parent_branch_id")
    while parent_id and parent_id not in seen and len(chain) < max_depth:
        parent = fetch_branch(int(parent_id))
        if not parent:
            break
        seen.add(parent.get("id"))
        chain.insert(0, parent)
        parent_id = parent.get("parent_branch_id")
    return chain


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
    from daemon.prompts import get_prompt_file, build_connected_services_note, apply_roster_placeholder

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

    # 이 브랜치가 자기 반복 작업(하트비트)을 직접 등록하는 방법
    heartbeat_hint = (
        f"\n## 내 반복 작업(하트비트) 등록\n"
        f"이 브랜치가 주기적으로 스스로 일하게 하려면 **프로젝트 메인이 아니라 이 브랜치로** 등록하세요.\n"
        f"1. 부모 프로젝트 작업 디렉토리에 `docs/HEARTBEAT-branch-{branch_id}.md` 작성 (체크리스트 형식, `- [ ]` 항목 필수)\n"
        f"   — 메인의 `docs/HEARTBEAT.md`와 **다른 파일**입니다. 메인 파일을 건드리지 마세요.\n"
        f"2. 등록: `POST $API_URL/api/tasks` body `{{\"project\": \"branch:{branch_id}\", \"interval_min\": 30, \"max_runs\": 20}}`\n"
        f"   (interval_min 최소 30, max_runs 필수. 메인과 별개로 등록되며 서로 간섭하지 않습니다)\n"
        f"3. 하트비트 수신 시 그 파일을 읽고 다음 미완료 항목을 처리 → 완료 시 `[x]` 마킹\n"
    )

    # 릴레이 가이드 (모든 브랜치 공통)
    relay_guide = _build_branch_relay_guide(project_id, branch_id)

    # Team project: load lead_prompt_patch for both kanban and pure branches
    lead_patch = ""
    try:
        from daemon.team import load_team
        team = load_team(project_id)
        if team and team.get("lead_prompt_patch"):
            lead_patch = team["lead_prompt_patch"]
    except ImportError:
        pass

    kanban_card_full = branch.get("kanban_card_full")
    if kanban_card_full:
        # 칸반 카드가 연결된 브랜치 → 기존 카드 규칙 사용
        from daemon.kanban import build_kanban_prompt
        kanban_combined = build_kanban_prompt(kanban_card_full)
        combined = "\n\n".join(p for p in [system_prompt_pv, kanban_combined, lead_patch, relay_guide, conversation_hint, heartbeat_hint] if p)
        return apply_roster_placeholder(combined)
    else:
        # 순수 브랜치 → 간결한 브랜치 규칙
        branch_num = branch.get("branch_number", branch.get("id"))
        branch_id = branch.get("id")
        # 브랜치 추가 프롬프트 (유저가 웹 UI에서 설정) + 조상 브랜치 프롬프트 상속(루트→부모 순, 합산 8000자 상한)
        ancestors = fetch_branch_ancestors(branch) if branch.get("parent_branch_id") else []
        inherited_parts = []
        budget = 8000
        for anc in reversed(ancestors):  # 가까운 부모부터 예산 배정, 넘치면 먼 조상부터 탈락
            ap = (anc.get("prompt") or "").strip()
            if not ap:
                continue
            if len(ap) > budget:
                inherited_parts.insert(0, f"## (상속) 브랜치 #{anc.get('branch_number')} {anc.get('title','')} 프롬프트\n[…생략 — 조상 프롬프트 합산 상한(8000자) 초과]")
                break
            inherited_parts.insert(0, f"## (상속) 브랜치 #{anc.get('branch_number')} {anc.get('title','')} 프롬프트\n{ap}")
            budget -= len(ap)
        branch_prompt = "\n\n".join(inherited_parts + ([branch.get("prompt")] if branch.get("prompt") else []))
        path_parts = [project_id] + [f"#{a.get('branch_number')} {a.get('title','')}" for a in ancestors] + [f"#{branch_num} {branch.get('title','')}"]
        parent_line = (
            f"부모 브랜치: #{ancestors[-1].get('branch_number')} {ancestors[-1].get('title','')} (내부ID: {ancestors[-1].get('id')}, 릴레이 to_branch_id)\n"
            if ancestors else ""
        )
        branch_rules = f"""# 브랜치 #{branch_num}: {branch.get('title', '')} (내부ID: {branch_id})
소속 프로젝트: {project_id}
경로: {' › '.join(path_parts)}
{parent_line}
## 규칙
- 이 브랜치의 작업에 집중하세요.
- 커밋 메시지 앞에 [branch-{branch_id}]를 붙이세요.
- 작업 범위를 임의로 넓히지 마세요.
- **대화 상대는 비개발자일 수 있습니다.** 기술 용어를 최소화하고 쉽게 설명하세요.

## 작업 종료 (보관 개념 없음 — 삭제만, 반드시 확인)
유저가 "다 됐어", "끝" 등을 말하면:
1. 변경 요약을 유저에게 보고
2. **"이 브랜치를 삭제할까요? (하위 브랜치·대화 기록도 함께 지워지고 되돌릴 수 없습니다)"** 라고 명시적으로 묻는다
3. 유저가 분명히 동의했을 때만 삭제:
```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://www.peter-voice.site'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")
curl -X DELETE "$API_URL/api/branches/{branch_id}" -H "X-Api-Key: $API_KEY"
```
동의가 없으면 그대로 둔다. 삭제 후에는 이 세션에 더 응답할 수 없으므로 삭제 직전 메시지가 마지막 보고여야 한다.
"""
        connected_services = build_connected_services_note()
        combined = "\n\n".join(p for p in [system_prompt_pv, common_prompt, connected_services, project_prompt, branch_prompt, branch_rules, lead_patch, relay_guide, conversation_hint, heartbeat_hint] if p)
        return apply_roster_placeholder(combined)


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
        parts.append(f"\n## 이 브랜치의 배경 (부모 프로젝트/브랜치 대화에서 캡처)\n{parent_context}")

    parts.append("\n위 맥락을 바탕으로 작업을 시작하세요.\n맥락이 부족하면 먼저 질문하세요.")

    return "\n".join(parts)
