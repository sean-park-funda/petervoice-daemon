"""Expert Team v2: team mode within a single project.

Architecture (D6): One user message triggers multiple claude -p subprocess
calls chained internally by process_team_message(). No Supabase roundtrip
between steps — the daemon orchestrates directly.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from daemon.globals import config, logger
from daemon.api import api_request
from daemon.sessions import team_session_key
from daemon.supabase import resolve_user_id, check_stop_requested, fetch_prompt_from_supabase

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


def build_member_prompt(member: dict) -> str:
    system_prompt_pv = fetch_prompt_from_supabase("_petervoice_system", user_id_override=0) or ""
    persona = member.get("persona_prompt", "")
    parts = [p for p in [system_prompt_pv, persona] if p]
    return "\n\n".join(parts)


# ─── Delegate parsing ────────────────────────────────────

DELEGATE_PATTERN = re.compile(
    r'\[delegate:([a-z0-9_-]+)\](.*?)\[/delegate\]',
    re.DOTALL,
)


def parse_delegates(text: str) -> list[dict]:
    return [
        {"member_key": m.group(1), "task": m.group(2).strip()}
        for m in DELEGATE_PATTERN.finditer(text)
    ]


def strip_delegates(text: str) -> str:
    return DELEGATE_PATTERN.sub("", text).strip()


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
) -> tuple[str, str | None, list]:
    from daemon.claude_runner import run_claude

    member_prompt = build_member_prompt(member)
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
) -> list[dict]:
    member_map = {m["member_key"]: m for m in team["members"]}
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
                run_team_member, member, d["task"], original_message, project, branch_id
            )
            futures[future] = member

        for future in as_completed(futures):
            member = futures[future]
            try:
                response, sid, tool_lines = future.result()
            except Exception as e:
                logger.error(f"[team] Member {member['member_key']} failed: {e}")
                response = f"(응답 실패: {e})"

            worker.reply(
                response,
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
                "response": response,
            })

    return results


# ─── Main orchestration ──────────────────────────────────

def process_team_message(
    prompt: str,
    project: str,
    worker,
    msg_id: int,
    branch_id: int | None = None,
) -> tuple[str, list]:
    from daemon.claude_runner import run_claude

    team = load_team(project)
    if not team:
        resp, _, tools = run_claude(prompt, project)
        return (resp, tools)

    all_tool_lines: list[str] = []

    # Step 1: team lead — stream_to_chat=False to hide delegate blocks
    lead_prompt = build_lead_prompt(project, team)
    lead_response, lead_sid, lead_tools = run_claude(
        prompt,
        project,
        prompt_override=lead_prompt,
        stream_to_chat=False,
    )
    all_tool_lines.extend(lead_tools)

    delegates = parse_delegates(lead_response)
    clean_response = strip_delegates(lead_response)

    worker.reply(clean_response, reply_to=[msg_id], project=project, is_final=True)

    if not delegates:
        return (clean_response, all_tool_lines)

    # Step 2: check stop
    uid = resolve_user_id()
    if uid and check_stop_requested(uid):
        return (clean_response + "\n\n(작업이 중단되었습니다)", all_tool_lines)

    # Step 3: parallel member execution
    member_results = run_members_parallel(
        team, delegates, prompt, project, branch_id, worker, msg_id
    )

    if not member_results:
        return (clean_response, all_tool_lines)

    # Step 4: check stop
    if uid and check_stop_requested(uid):
        return (clean_response + "\n\n(팀원 결과 취합 전 중단됨)", all_tool_lines)

    # Step 5: team lead consolidation — stream_to_chat=True (no delegate blocks)
    summary_prompt = format_member_results(member_results)
    summary_response, _, summary_tools = run_claude(
        summary_prompt,
        project,
        prompt_override=lead_prompt,
        stream_to_chat=True,
    )
    all_tool_lines.extend(summary_tools)

    worker.reply(summary_response, reply_to=[msg_id], project=project, is_final=True)

    return (summary_response, all_tool_lines)
