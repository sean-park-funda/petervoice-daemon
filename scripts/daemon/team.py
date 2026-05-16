"""Expert Team v2: team mode within a single project.

Architecture (D6): One user message triggers multiple claude -p subprocess
calls chained internally by process_team_message(). No Supabase roundtrip
between steps — the daemon orchestrates directly.
"""

import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from daemon.globals import config, logger
from daemon.api import api_request
from daemon.sessions import team_session_key
from daemon.supabase import resolve_user_id, check_stop_requested, fetch_prompt_from_supabase, get_project_dir

# ─── Team cache ──────────────────────────────────────────

_team_cache: dict = {}
TEAM_CACHE_TTL = 300


def load_team(project_id: str) -> dict | None:
    now = time.time()
    cached = _team_cache.get(project_id)
    if cached and (now - cached[1]) < TEAM_CACHE_TTL:
        return cached[0]

    api_key = config.get("api_key", "")
    if not api_key:
        return None

    result = api_request(api_key, "GET", f"/api/bot/team?project={project_id}", timeout=10)
    if not result or not result.get("team"):
        _team_cache[project_id] = (None, now)
        return None

    team_data = {
        "team_id": result["team"]["id"],
        "name": result["team"].get("name", ""),
        "lead_prompt_patch": result["team"].get("lead_prompt_patch", ""),
        "workflow": result["team"].get("workflow"),
        "config": result["team"].get("config", {}),
        "members": result.get("members", []),
    }
    _team_cache[project_id] = (team_data, now)
    return team_data


def is_team_project(project_id: str) -> bool:
    return load_team(project_id) is not None


def invalidate_team_cache(project_id: str):
    _team_cache.pop(project_id, None)


# ─── Prompt builders ─────────────────────────────────────

def build_lead_prompt(project: str, team: dict) -> str:
    from daemon.claude_runner import build_project_prompt
    base = build_project_prompt(project)
    patch = team.get("lead_prompt_patch", "")
    if patch:
        return base + "\n\n" + patch
    return base


_CROSSTALK_INSTRUCTIONS = """## 팀원 간 소통 규칙
- 다른 팀원의 전문성이 필요하면 [msg:member-key]로 직접 메시지를 보낼 수 있습니다.
- 사용 가능한 팀원: {member_list}
- 메시지 형식:
  [msg:genre-expert]
  질문이나 데이터 공유 내용
  [/msg]
- 꼭 필요할 때만 사용하세요. 불필요한 대화는 비용과 시간을 낭비합니다.
- 메시지를 받으면 자신의 전문성 관점에서 답변하고, [msg:상대key]로 회신하세요.
"""

_GROWTH_INSTRUCTIONS = """## 지식 축적 규칙
- 분석 중 새로운 패턴, 방법론, 인사이트를 발견하면 docs/team-memory/{member_key}/ 에 마크다운으로 기록하세요.
- 파일명 형식: YYYY-MM-DD-{주제}.md (예: 2026-05-16-character-arc-patterns.md)
- 기록 대상: 반복 사용할 분석 프레임워크, 발견한 장르 패턴, 피드백에서 배운 교훈
- 기록하지 않는 것: 개별 작품의 분석 결과 (그건 보고서에 포함), 일회성 정보
- CEO(유저)가 피드백을 주면, 그 피드백의 핵심을 learnings.md에 추가하세요.
- 이전 학습 기록이 위에 포함되어 있으면, 그 맥락을 활용해 더 나은 분석을 하세요.
"""


def build_member_prompt(member: dict, project: str, team_members: list[dict] | None = None) -> str:
    from daemon.claude_runner import _load_wiki_index
    system_prompt_pv = fetch_prompt_from_supabase("_petervoice_system", user_id_override=0) or ""
    persona = member.get("persona_prompt", "")
    wiki_index = _load_wiki_index(project)
    knowledge = _load_knowledge_docs(member, project)
    member_key = member.get("member_key", "unknown")
    memory = _load_member_memory(member_key, project)
    growth = _GROWTH_INSTRUCTIONS.replace("{member_key}", member_key)
    crosstalk = ""
    if team_members:
        others = [f"  - `{m['member_key']}` — {m.get('icon', '')} {m.get('name', m['member_key'])}"
                  for m in team_members if m["member_key"] != member_key]
        if others:
            member_list = "\n".join(others)
            crosstalk = _CROSSTALK_INSTRUCTIONS.replace("{member_list}", member_list)
    parts = [p for p in [system_prompt_pv, persona, wiki_index, knowledge, memory, growth, crosstalk] if p]
    return "\n\n".join(parts)


def _load_knowledge_docs(member: dict, project: str) -> str:
    docs = member.get("knowledge_docs")
    if not docs:
        return ""
    sections = []
    if isinstance(docs, dict):
        for name, content in list(docs.items())[:5]:
            content = (content or "").strip()
            if content and len(content) < 3000:
                sections.append(f"### {name}\n{content}")
    elif isinstance(docs, list):
        project_dir = get_project_dir(project)
        if not project_dir:
            return ""
        for doc_path in docs[:5]:
            full_path = Path(project_dir) / doc_path
            if full_path.exists():
                try:
                    content = full_path.read_text(encoding="utf-8").strip()
                    if content and len(content) < 3000:
                        sections.append(f"### {doc_path}\n{content}")
                except Exception:
                    continue
    if not sections:
        return ""
    return "## 참조 문서\n\n" + "\n\n".join(sections)


def _load_member_memory(member_key: str, project: str) -> str:
    if not member_key:
        return ""
    project_dir = get_project_dir(project)
    if not project_dir:
        return ""
    memory_dir = Path(project_dir) / "docs" / "team-memory" / member_key
    if not memory_dir.exists():
        return ""
    sections = []
    for md_file in sorted(memory_dir.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8").strip()
            if content:
                sections.append(content)
        except Exception:
            continue
    if not sections:
        return ""
    combined = "\n\n---\n\n".join(sections)
    if len(combined) > 2000:
        combined = combined[:2000] + "\n\n(... 이하 생략)"
    return f"## 나의 학습 기록\n\n{combined}"


# ─── Delegate parsing ────────────────────────────────────

DELEGATE_PATTERN = re.compile(
    r'\[delegate:([a-z0-9_-]+)\](.*?)\[/delegate\]',
    re.DOTALL,
)

# ─── Cross-talk parsing ─────────────────────────────────

MSG_PATTERN = re.compile(
    r'\[msg:([a-z0-9_-]+)\](.*?)\[/msg\]',
    re.DOTALL,
)


def parse_crosstalk(text: str) -> list[dict]:
    return [
        {"to": m.group(1), "content": m.group(2).strip()}
        for m in MSG_PATTERN.finditer(text)
    ]


def strip_crosstalk(text: str) -> str:
    return MSG_PATTERN.sub("", text).strip()


def format_crosstalk_for_display(text: str, from_member: dict, member_map: dict) -> str:
    def _replace(m):
        to_key = m.group(1)
        content = m.group(2).strip()
        to_m = member_map.get(to_key, {})
        to_icon = to_m.get("icon", "👤")
        to_name = to_m.get("name", to_key)
        quoted = "\n".join(f"> {line}" for line in content.splitlines())
        return f"\n\n💬 → {to_icon} **{to_name}:**\n{quoted}\n"
    return MSG_PATTERN.sub(_replace, text).strip()


def format_incoming_messages(messages: list[dict], member_map: dict, round_num: int) -> str:
    lines = [f"## 팀 대화 (라운드 {round_num})", ""]
    for msg in messages:
        from_m = member_map.get(msg["from_key"], {})
        icon = from_m.get("icon", "👤")
        name = from_m.get("name", msg["from_key"])
        quoted = "\n".join(f"> {line}" for line in msg["content"].splitlines())
        lines.append(f"{icon} **{name}** → 나:")
        lines.append(quoted)
        lines.append("")
    lines.append("위 메시지에 답변하세요. 필요하면 다른 팀원에게도 [msg:member-key]로 메시지를 보낼 수 있습니다.")
    return "\n".join(lines)


def format_conversation_log(log: list[dict], member_map: dict) -> str:
    lines = ["[팀원 간 대화 기록]", ""]
    for msg in log:
        from_m = member_map.get(msg["from_key"], {})
        to_m = member_map.get(msg["to_key"], {})
        lines.append(
            f"{from_m.get('icon', '👤')} {from_m.get('name', msg['from_key'])} → "
            f"{to_m.get('icon', '👤')} {to_m.get('name', msg['to_key'])}:"
        )
        lines.append(msg["content"])
        lines.append("")
    return "\n".join(lines)


def parse_delegates(text: str) -> list[dict]:
    return [
        {"member_key": m.group(1), "task": m.group(2).strip()}
        for m in DELEGATE_PATTERN.finditer(text)
    ]


def strip_delegates(text: str) -> str:
    return DELEGATE_PATTERN.sub("", text).strip()


def format_delegates_for_display(text: str, member_map: dict) -> str:
    """Replace [delegate:key]task[/delegate] with readable delegation text."""
    def _replace(m):
        key = m.group(1)
        task = m.group(2).strip()
        member = member_map.get(key, {})
        icon = member.get("icon", "👤")
        name = member.get("name", key)
        quoted = "\n".join(f"> {line}" for line in task.splitlines())
        return f"\n\n{icon} **{name}에게 위임:**\n{quoted}\n"
    return DELEGATE_PATTERN.sub(_replace, text).strip()


def format_member_results(results: list[dict]) -> str:
    lines = ["[팀원 결과 취합]", ""]
    for r in results:
        lines.append(f"{r['icon']} {r['name']}:")
        lines.append(r["response"])
        lines.append("")
    lines.append("위 팀원들의 결과를 종합하여 CEO에게 보고하세요.")
    return "\n".join(lines)


# ─── Team member execution ───────────────────────────────

def run_team_member(
    member: dict,
    task: str,
    original_message: str,
    project: str,
    branch_id: int | None,
    team_project: str | None = None,
    team_members: list[dict] | None = None,
) -> tuple[str, str | None, list]:
    from daemon.claude_runner import run_claude

    member_prompt = build_member_prompt(member, team_project or project, team_members)
    sk = team_session_key(project, member["member_key"], branch_id)
    full_prompt = f"{task}\n\n[맥락]\nCEO 요청: {original_message}"

    return run_claude(
        full_prompt,
        project,
        session_key_override=sk,
        prompt_override=member_prompt,
        stream_to_chat=False,
    )


def run_members_parallel(
    team: dict,
    delegates: list[dict],
    original_message: str,
    project: str,
    branch_id: int | None,
    worker,
    msg_id: int,
    team_project: str | None = None,
) -> list[dict]:
    member_map = {m["member_key"]: m for m in team["members"]}
    team_members = team["members"]
    results = []

    icons = " ".join(
        member_map[d["member_key"]]["icon"]
        for d in delegates
        if d["member_key"] in member_map
    )
    worker.reply(
        f"{icons} 팀원들이 작업 중입니다...",
        reply_to=[msg_id],
        project=project,
        is_final=False,
    )

    with ThreadPoolExecutor(max_workers=len(delegates)) as pool:
        futures = {}
        for d in delegates:
            member = member_map.get(d["member_key"])
            if not member:
                logger.warning(f"[team] Unknown member_key: {d['member_key']}")
                continue
            future = pool.submit(
                run_team_member, member, d["task"], original_message,
                project, branch_id, team_project, team_members,
            )
            futures[future] = member

        for future in as_completed(futures):
            member = futures[future]
            try:
                response, sid, tool_lines = future.result()
            except Exception as e:
                logger.error(f"[team] Member {member['member_key']} failed: {e}")
                response = f"(응답 실패: {e})"

            display = format_crosstalk_for_display(
                strip_crosstalk(response) if not parse_crosstalk(response) else response,
                member, member_map,
            )
            clean = strip_crosstalk(display)

            worker.reply(
                clean,
                reply_to=[msg_id],
                project=project,
                is_final=True,
                member_id=member["member_key"],
                member_name=member["name"],
                member_icon=member["icon"],
            )
            results.append({
                "member_key": member["member_key"],
                "name": member["name"],
                "icon": member["icon"],
                "response": strip_crosstalk(response),
                "raw_response": response,
            })

    return results


# ─── Cross-talk round execution ─────────────────────────

def run_crosstalk_round(
    round_num: int,
    pending: list[dict],
    team: dict,
    original_message: str,
    project: str,
    branch_id: int | None,
    worker,
    msg_id: int,
    team_project: str | None = None,
) -> tuple[list[dict], list[dict]]:
    member_map = {m["member_key"]: m for m in team["members"]}
    team_members = team["members"]

    by_recipient: dict[str, list[dict]] = {}
    for msg in pending:
        by_recipient.setdefault(msg["to_key"], []).append(msg)

    icons = " ".join(
        member_map[k]["icon"] for k in by_recipient if k in member_map
    )
    worker.reply(
        f"💬 라운드 {round_num}: {icons} 팀원 간 대화 중...",
        reply_to=[msg_id], project=project, is_final=False,
    )

    results = []
    new_pending = []

    with ThreadPoolExecutor(max_workers=max(len(by_recipient), 1)) as pool:
        futures = {}
        for recipient_key, messages in by_recipient.items():
            member = member_map.get(recipient_key)
            if not member:
                continue
            context = format_incoming_messages(messages, member_map, round_num)
            future = pool.submit(
                run_team_member, member, context, original_message,
                project, branch_id, team_project, team_members,
            )
            futures[future] = (member, recipient_key)

        for future in as_completed(futures):
            member, recipient_key = futures[future]
            try:
                response, _, _ = future.result()
            except Exception as e:
                logger.error(f"[team] Crosstalk member {recipient_key} failed: {e}")
                response = f"(응답 실패: {e})"

            outgoing = parse_crosstalk(response)
            for out in outgoing:
                if out["to"] != recipient_key:
                    new_pending.append({
                        "round": round_num,
                        "from_key": recipient_key,
                        "to_key": out["to"],
                        "content": out["content"],
                    })

            clean = strip_crosstalk(response)
            from_m = member_map.get(recipient_key, {})
            to_keys = [out["to"] for out in outgoing]
            to_names = []
            for tk in to_keys:
                tm = member_map.get(tk, {})
                to_names.append(f"{tm.get('icon', '👤')} {tm.get('name', tk)}")

            worker.reply(
                clean,
                reply_to=[msg_id],
                project=project,
                is_final=True,
                member_id=recipient_key,
                member_name=member["name"],
                member_icon=member["icon"],
                to_member_name=", ".join(to_names) if to_names else None,
            )
            results.append({
                "member_key": recipient_key,
                "name": member["name"],
                "icon": member["icon"],
                "response": clean,
                "raw_response": response,
            })

    logger.info(f"[team] Crosstalk round {round_num}: {len(results)} responses, {len(new_pending)} new messages")
    return results, new_pending


# ─── Graphify integration ────────────────────────────────

def _update_graphify(project: str):
    project_dir = get_project_dir(project)
    if not project_dir:
        return
    try:
        env = {**os.environ, "PATH": f"/Users/sean/.local/bin:{os.environ.get('PATH', '')}"}
        subprocess.run(
            ["graphify", "update", "."],
            cwd=project_dir, timeout=30, capture_output=True, env=env,
        )
        logger.info(f"[team] graphify updated for {project}")
    except Exception as e:
        logger.warning(f"[team] graphify update failed: {e}")


# ─── Main orchestration ──────────────────────────────────

def process_team_message(
    prompt: str,
    project: str,
    worker,
    msg_id: int,
    branch_id: int | None = None,
    team_project: str | None = None,
) -> tuple[str, list]:
    from daemon.claude_runner import run_claude

    team = load_team(team_project or project)
    if not team:
        resp, _, tools = run_claude(prompt, project)
        return (resp, tools)

    all_tool_lines: list[str] = []
    _team_proj = team_project or project

    # Step 1: team lead — stream_to_chat=False to hide delegate blocks
    worker.reply(
        "📋 팀장이 분석 중입니다...",
        reply_to=[msg_id], project=project, is_final=False,
    )
    lead_prompt = build_lead_prompt(_team_proj, team)
    lead_sk = team_session_key(project, "_lead", branch_id)
    lead_response, lead_sid, lead_tools = run_claude(
        prompt,
        project,
        session_key_override=lead_sk,
        prompt_override=lead_prompt,
        stream_to_chat=False,
    )
    all_tool_lines.extend(lead_tools)

    delegates = parse_delegates(lead_response)
    member_map = {m["member_key"]: m for m in team["members"]}

    logger.info(f"[team] Lead response: {len(lead_response)} chars, delegates found: {len(delegates)}")
    if delegates:
        logger.info(f"[team] Delegate keys: {[d['member_key'] for d in delegates]}")
        display_response = format_delegates_for_display(lead_response, member_map)
    else:
        logger.warning(f"[team] No delegates found. Lead response preview: {lead_response[:500]}")
        display_response = strip_delegates(lead_response)

    worker.reply(display_response, reply_to=[msg_id], project=project, is_final=True)

    if not delegates:
        return (display_response, all_tool_lines)

    # Step 2: check stop
    uid = resolve_user_id()
    if uid and check_stop_requested(uid):
        return (display_response + "\n\n(작업이 중단되었습니다)", all_tool_lines)

    # Step 3: parallel member execution
    member_results = run_members_parallel(
        team, delegates, prompt, project, branch_id, worker, msg_id, _team_proj
    )

    if not member_results:
        return (display_response, all_tool_lines)

    # Step 3.5: cross-talk loop
    max_ct_rounds = team.get("config", {}).get("max_crosstalk_rounds", 3)
    conversation_log: list[dict] = []

    pending_msgs: list[dict] = []
    for r in member_results:
        for ct in parse_crosstalk(r.get("raw_response", "")):
            if ct["to"] != r["member_key"]:
                pending_msgs.append({
                    "round": 1, "from_key": r["member_key"],
                    "to_key": ct["to"], "content": ct["content"],
                })
    conversation_log.extend(pending_msgs)

    ct_round = 2
    while pending_msgs and ct_round <= max_ct_rounds + 1:
        if uid and check_stop_requested(uid):
            break
        ct_results, new_pending = run_crosstalk_round(
            ct_round, pending_msgs, team, prompt, project,
            branch_id, worker, msg_id, _team_proj,
        )
        conversation_log.extend(new_pending)
        pending_msgs = new_pending
        ct_round += 1

    # Step 4: check stop
    if uid and check_stop_requested(uid):
        return (display_response + "\n\n(팀원 결과 취합 전 중단됨)", all_tool_lines)

    # Step 5: team lead consolidation — stream_to_chat=True (no delegate blocks)
    summary_prompt = format_member_results(member_results)
    if conversation_log:
        summary_prompt += "\n\n" + format_conversation_log(conversation_log, member_map)
    summary_response, _, summary_tools = run_claude(
        summary_prompt,
        project,
        session_key_override=lead_sk,
        prompt_override=lead_prompt,
        stream_to_chat=True,
    )
    all_tool_lines.extend(summary_tools)

    worker.reply(summary_response, reply_to=[msg_id], project=project, is_final=True)

    # Step 6: update graphify index so new docs are available next session
    _update_graphify(_team_proj)

    return (summary_response, all_tool_lines)
