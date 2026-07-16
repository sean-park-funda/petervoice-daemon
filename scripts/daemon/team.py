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

# Last team results per project — for direct member context
_last_team_results: dict = {}  # project -> {results, team, branch_id, team_project, ts}


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
    parts = [base]
    if patch:
        parts.append(patch)
    return "\n\n".join(parts)


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

_RESPONSE_RULES = """## ⚠️ 응답 규칙 (반드시 준수)
- 전체 응답 800자 이내. 이것은 절대 규칙입니다.
- 형식: **결론 한 줄** → 근거 2-3개 (각 1-2문장) → 끝.
- 서론, 배경 설명, 요약 반복 금지. 바로 핵심부터.
- 확신 없으면 "판단 유보" + 이유 한 줄.
- 위에 "CEO 확인 학습"이 있으면 반드시 활용하세요. CEO가 검증한 지식입니다.
"""

_LEAD_FOLLOWUP_PROMPT = """## 비판적 리뷰 & 보강 조사 지시

원래 CEO 요청:
---
{original_question}
---

## 팀원 1차 분석 결과:

{member_results}

---

팀장으로서 각 분석을 비판적으로 검토하세요:
1. 근거가 약하거나 주관적 추측에 의존하는 부분은?
2. 빠진 관점, 누락된 데이터, 검증 안 된 가정은?
3. 팀원 간 모순되는 주장이 있는가?

보강 조사가 필요한 팀원에게만 **구체적인 추가 조사**를 지시하세요.
[delegate:member-key]
구체적인 보강 조사 지시 (무엇을, 왜 더 조사해야 하는지)
[/delegate]

⚠️ 규칙:
- 모든 분석이 충분하면 "보강 불필요"라고만 답하세요.
- 불필요한 보강은 비용 낭비. 진짜 약한 부분만.
- "비판을 위한 비판" 금지. 구체적 조사 방향을 제시하세요.
"""

# ─── Lead Learning: after discussion, lead decides what the team should learn

_LEAD_LEARNING_PROMPT = """당신은 팀장입니다. 방금 팀 토론이 끝났습니다.

## 이번 토론 결과
{consolidation_result}

## 각 멤버의 원래 분석
{member_results_summary}

---

이번 토론에서 **팀원들이 다음에 기억해야 할 교훈**이 있는지 판단하세요.

판단 기준:
- 이번 분석에서만 해당되는 사실(작품 고유 정보)은 제외
- 다른 시나리오 분석에도 적용 가능한 패턴, 방법론, 판단 기준만 선별
- 특정 멤버가 틀렸거나 시각이 부족했던 부분 → 해당 멤버에게 교훈

교훈이 있으면:
[learn:member-key]
교훈 제목: ...
내용: ...
[/learn]

여러 멤버에게 각각 교훈이 있으면 [learn:] 블록을 여러 개 작성하세요.
교훈이 없으면 "학습 사항 없음"이라고만 답하세요."""

LEARN_PATTERN = re.compile(
    r'\[learn:([a-z0-9_-]+)\](.*?)\[/learn\]',
    re.DOTALL,
)


def build_member_prompt(
    member: dict, project: str,
    team_members: list[dict] | None = None,
    include_response_rules: bool = True,
) -> str:
    from daemon.claude_runner import _load_wiki_index
    system_prompt_pv = fetch_prompt_from_supabase("_petervoice_system", user_id_override=0) or ""
    persona = member.get("persona_prompt", "")
    wiki_index = _load_wiki_index(project)
    knowledge = _load_knowledge_docs(member, project)
    member_key = member.get("member_key", "unknown")
    expertise = _load_member_expertise(member_key, project)
    memory = _load_member_memory(member_key, project)
    growth = _RESPONSE_RULES if include_response_rules else ""
    crosstalk = ""
    if team_members:
        others = [f"  - `{m['member_key']}` — {m.get('icon', '')} {m.get('name', m['member_key'])}"
                  for m in team_members if m["member_key"] != member_key]
        if others:
            member_list = "\n".join(others)
            crosstalk = _CROSSTALK_INSTRUCTIONS.replace("{member_list}", member_list)
    parts = [p for p in [system_prompt_pv, persona, wiki_index, knowledge, expertise, memory, growth, crosstalk] if p]
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
    return "## 공유 참조 문서 (읽기 전용)\n\n" + "\n\n".join(sections)


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
    return f"## 팀장 확인 학습\n\n{combined}"


def _load_member_expertise(member_key: str, project: str) -> str:
    if not member_key:
        return ""
    project_dir = get_project_dir(project)
    if not project_dir:
        return ""
    expertise_dir = Path(project_dir) / "docs" / "team" / "members" / member_key / "expertise"
    if not expertise_dir.exists():
        return ""
    sections = []
    for md in sorted(expertise_dir.glob("*.md")):
        try:
            content = md.read_text(encoding="utf-8").strip()
            if content and len(content) < 3000:
                sections.append(content)
        except Exception:
            continue
    if not sections:
        return ""
    combined = "\n\n---\n\n".join(sections)
    if len(combined) > 4000:
        combined = combined[:4000] + "\n\n(... 이하 생략)"
    return f"## 내 전문 지식 (개인 연구 축적)\n\n{combined}"


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


def format_followup_results(results: list[dict]) -> str:
    lines = ["[보강 조사 결과]", ""]
    for r in results:
        lines.append(f"{r['icon']} {r['name']} (보강):")
        lines.append(r["response"])
        lines.append("")
    lines.append("위 보강 조사 결과를 1차 분석과 함께 종합하세요.")
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
    full_prompt = f"[⚠️ 800자 이내로 답하세요. 결론→근거 순서. 서론/요약 금지.]\n\n{task}\n\n[맥락]\nCEO 요청: {original_message}"

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
                response, sid, tool_lines, _ = future.result()
            except Exception as e:
                logger.error(f"[team] Member {member['member_key']} failed: {e}")
                response = f"(응답 실패: {e})"

            from daemon.claude_runner import SHUTDOWN_INTERRUPTED
            if response == SHUTDOWN_INTERRUPTED:
                logger.info(f"[team] Member {member['member_key']} interrupted by shutdown — skipping reply")
                continue

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


# ─── Lead follow-up round (critical review → targeted investigation) ───

def run_lead_followup_round(
    member_results: list[dict],
    team: dict,
    original_message: str,
    project: str,
    branch_id: int | None,
    worker,
    msg_id: int,
    lead_prompt: str,
    lead_sk: str,
    team_project: str | None = None,
) -> list[dict]:
    """Lead critically reviews R1 results and issues targeted follow-up
    investigations to members with weak/incomplete analyses."""
    from daemon.claude_runner import run_claude

    member_map = {m["member_key"]: m for m in team["members"]}
    _team_proj = team_project or project

    results_text = "\n\n".join(
        f"{r['icon']} **{r['name']}** ({r['member_key']}):\n{r['response']}"
        for r in member_results
    )
    followup_prompt = _LEAD_FOLLOWUP_PROMPT.format(
        original_question=original_message,
        member_results=results_text,
    )

    worker.reply(
        "🔍 팀장이 분석을 검토 중...",
        reply_to=[msg_id], project=project, is_final=False,
    )

    lead_response, _, _, _ = run_claude(
        followup_prompt,
        project,
        session_key_override=lead_sk,
        prompt_override=lead_prompt,
        stream_to_chat=False,
    )

    from daemon.claude_runner import SHUTDOWN_INTERRUPTED
    if lead_response == SHUTDOWN_INTERRUPTED:
        logger.info("[team] Lead followup interrupted by shutdown — skipping")
        return []

    followup_delegates = parse_delegates(lead_response)

    if not followup_delegates:
        logger.info("[team] Lead followup: no additional investigation needed")
        clean = strip_delegates(lead_response)
        if clean and "보강 불필요" not in clean:
            worker.reply(clean, reply_to=[msg_id], project=project, is_final=True)
        return []

    logger.info(f"[team] Lead followup: {len(followup_delegates)} members need investigation")
    display = format_delegates_for_display(lead_response, member_map)
    worker.reply(display, reply_to=[msg_id], project=project, is_final=True)

    followup_results = run_members_parallel(
        team, followup_delegates, original_message, project,
        branch_id, worker, msg_id, _team_proj,
    )

    logger.info(f"[team] Lead followup: {len(followup_results)} follow-up results")
    return followup_results


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
                response, _, _, _ = future.result()
            except Exception as e:
                logger.error(f"[team] Crosstalk member {recipient_key} failed: {e}")
                response = f"(응답 실패: {e})"

            from daemon.claude_runner import SHUTDOWN_INTERRUPTED
            if response == SHUTDOWN_INTERRUPTED:
                logger.info(f"[team] Crosstalk member {recipient_key} interrupted by shutdown — skipping reply")
                continue

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


# ─── Lead Learning (post-discussion, lead-driven) ────────

def run_lead_learning_round(
    member_results: list[dict],
    consolidation_result: str,
    team: dict,
    project: str,
    branch_id: int | None,
    worker,
    msg_id: int,
    lead_prompt: str,
    lead_sk: str,
    team_project: str | None = None,
):
    """Lead judges what the team should learn after discussion, saves to team-memory."""
    from daemon.claude_runner import run_claude
    from datetime import date

    _team_proj = team_project or project
    today = date.today().isoformat()

    member_results_summary = "\n\n".join(
        f"{r['icon']} **{r['name']}** ({r['member_key']}):\n{r['response'][:500]}"
        for r in member_results
    )
    learning_prompt = _LEAD_LEARNING_PROMPT.format(
        consolidation_result=consolidation_result[:2000],
        member_results_summary=member_results_summary,
    )

    lead_response, _, _, _ = run_claude(
        learning_prompt,
        project,
        session_key_override=lead_sk,
        prompt_override=lead_prompt,
        stream_to_chat=False,
    )

    learn_blocks = LEARN_PATTERN.findall(lead_response)
    if not learn_blocks:
        logger.info("[team] Lead learning: no learnings to save")
        return

    project_dir = get_project_dir(_team_proj)
    if not project_dir:
        return

    saved = []
    member_map = {m["member_key"]: m for m in team["members"]}
    for member_key, content in learn_blocks:
        content = content.strip()
        if not content:
            continue
        mem_dir = Path(project_dir) / "docs" / "team-memory" / member_key
        mem_dir.mkdir(parents=True, exist_ok=True)
        first_line = content.split("\n")[0].strip()
        topic = re.sub(r"[^\w가-힣-]", "", first_line[:30].replace(" ", "-")) or "learning"
        filename = f"{today}-{topic}.md"
        filepath = mem_dir / filename
        filepath.write_text(f"# 팀장 확인 학습\n\n{content}\n", encoding="utf-8")
        saved.append((member_key, first_line[:60]))
        logger.info(f"[team] Learning saved: {member_key}/{filename}")

    if saved:
        lines = []
        for key, summary in saved:
            m = member_map.get(key, {})
            lines.append(f"{m.get('icon', '👤')} {m.get('name', key)}: {summary}")
        worker.reply(
            "📚 팀장 학습 판단:\n" + "\n".join(lines),
            reply_to=[msg_id], project=project, is_final=True,
        )
        logger.info(f"[team] Lead learning: {len(saved)} learnings saved")


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


# ─── Direct member communication (@mention) ─────────────

def parse_direct_mention(prompt: str, team: dict) -> tuple[dict | None, str]:
    """Detect @member-key or @member-name at start of message.
    Handles names with spaces (e.g. '스토리 분석가') and optional colon."""
    text = prompt.strip()
    if not text.startswith("@"):
        return None, prompt

    after_at = text[1:]

    candidates = []
    for m in team["members"]:
        candidates.append((m["member_key"], m))
        name = m.get("name", "")
        if name:
            candidates.append((name, m))

    candidates.sort(key=lambda x: len(x[0]), reverse=True)

    for key, member in candidates:
        if after_at.startswith(key):
            rest = after_at[len(key):].lstrip(": ").strip()
            if rest:
                return member, rest
    return None, prompt


def handle_direct_member_message(
    member: dict,
    prompt: str,
    team: dict,
    project: str,
    branch_id: int | None,
    worker,
    msg_id: int,
    team_project: str | None = None,
) -> tuple[str, list]:
    """Handle direct CEO-to-member communication, bypassing the lead.
    No 800-char limit — research mode with personal expertise saving."""
    from daemon.claude_runner import run_claude
    from datetime import date

    _team_proj = team_project or project
    member_key = member.get("member_key", "unknown")

    cached = _last_team_results.get(project)
    context = ""
    if cached and (time.time() - cached["ts"] < 1800):
        prev = next(
            (r for r in cached["results"] if r["member_key"] == member_key),
            None,
        )
        if prev:
            context = f"\n\n[이전 분석]\n{prev['response']}"

    today = date.today().isoformat()
    research_guide = (
        f"\n\n[연구 저장 규칙]\n"
        f"조사/연구 결과는 반드시 docs/team/members/{member_key}/expertise/ 에 저장하세요.\n"
        f"파일명: {today}-{{topic}}.md\n"
        f"docs/knowledge/ 는 공유 참조 문서입니다. 수정하지 마세요."
    )
    full_task = f"[CEO 직접 지시]{context}\n\n{prompt}{research_guide}"

    member_prompt = build_member_prompt(member, _team_proj, team["members"], include_response_rules=False)
    sk = team_session_key(project, member_key, branch_id)

    worker.reply(
        f"{member['icon']} {member['name']}에게 직접 전달 중...",
        reply_to=[msg_id], project=project, is_final=False,
    )

    response, sid, tools, _ = run_claude(
        full_task,
        project,
        session_key_override=sk,
        prompt_override=member_prompt,
        stream_to_chat=False,
    )

    from daemon.claude_runner import SHUTDOWN_INTERRUPTED
    if response == SHUTDOWN_INTERRUPTED:
        # 종료 드레인 초과 — 응답 없이 센티널 전파 (worker가 dequeue를 건너뜀)
        return (SHUTDOWN_INTERRUPTED, tools)

    worker.reply(
        response,
        reply_to=[msg_id], project=project, is_final=True,
        member_id=member_key,
        member_name=member["name"],
        member_icon=member["icon"],
    )

    _update_graphify(_team_proj)

    logger.info(f"[team] Direct message to {member_key}: {len(response)} chars")
    return (response, tools)


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
        resp, _, tools, _ = run_claude(prompt, project)
        return (resp, tools)

    all_tool_lines: list[str] = []
    _team_proj = team_project or project

    # Step 0: direct member communication (@mention bypass)
    direct_member, clean_prompt = parse_direct_mention(prompt, team)
    if direct_member:
        return handle_direct_member_message(
            direct_member, clean_prompt, team, project,
            branch_id, worker, msg_id, _team_proj,
        )

    # Step 1: team lead — stream_to_chat=False to hide delegate blocks
    worker.reply(
        "📋 팀장이 분석 중입니다...",
        reply_to=[msg_id], project=project, is_final=False,
    )
    lead_prompt = build_lead_prompt(_team_proj, team)
    lead_sk = team_session_key(project, "_lead", branch_id)
    lead_response, lead_sid, lead_tools, _ = run_claude(
        prompt,
        project,
        session_key_override=lead_sk,
        prompt_override=lead_prompt,
        stream_to_chat=False,
    )
    all_tool_lines.extend(lead_tools)

    from daemon.claude_runner import SHUTDOWN_INTERRUPTED
    if lead_response == SHUTDOWN_INTERRUPTED:
        # 종료 드레인 초과 — 센티널 전파 (worker가 응답/dequeue를 건너뛰어 자동재개)
        return (SHUTDOWN_INTERRUPTED, all_tool_lines)

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

    # Step 4: lead critical review & follow-up investigation (Round 2)
    followup_results = []
    if len(member_results) >= 2:
        if uid and check_stop_requested(uid):
            return (display_response + "\n\n(보강 조사 전 중단됨)", all_tool_lines)
        followup_results = run_lead_followup_round(
            member_results, team, prompt, project,
            branch_id, worker, msg_id, lead_prompt, lead_sk, _team_proj,
        )

    # Step 4.5: check stop
    if uid and check_stop_requested(uid):
        return (display_response + "\n\n(팀원 결과 취합 전 중단됨)", all_tool_lines)

    # Step 5: team lead consolidation — stream_to_chat=True (no delegate blocks)
    summary_prompt = format_member_results(member_results)
    if followup_results:
        summary_prompt += "\n\n" + format_followup_results(followup_results)
    if conversation_log:
        summary_prompt += "\n\n" + format_conversation_log(conversation_log, member_map)
    summary_response, _, summary_tools, _ = run_claude(
        summary_prompt,
        project,
        session_key_override=lead_sk,
        prompt_override=lead_prompt,
        stream_to_chat=True,
    )
    all_tool_lines.extend(summary_tools)

    if summary_response == SHUTDOWN_INTERRUPTED:
        return (SHUTDOWN_INTERRUPTED, all_tool_lines)

    worker.reply(summary_response, reply_to=[msg_id], project=project, is_final=True)

    # Step 5.5: hint about direct member communication
    member_names = [f"@{m.get('name', m['member_key'])}" for m in team["members"]
                    if m["member_key"] in {r["member_key"] for r in member_results}]
    if member_names:
        hint = "💡 특정 팀원에게 직접 지시: " + " / ".join(member_names) + " + 내용"
        worker.reply(hint, reply_to=[msg_id], project=project, is_final=True)

    # Step 6: lead learning — lead judges what the team should learn
    try:
        run_lead_learning_round(
            member_results, summary_response, team, project,
            branch_id, worker, msg_id, lead_prompt, lead_sk, _team_proj,
        )
    except Exception as e:
        logger.warning(f"[team] Lead learning round error: {e}")

    # Step 6.5: cache results for direct member context
    _last_team_results[project] = {
        "results": member_results,
        "team": team,
        "branch_id": branch_id,
        "team_project": _team_proj,
        "ts": time.time(),
    }

    # Step 7: update graphify index so new docs are available next session
    _update_graphify(_team_proj)

    return (summary_response, all_tool_lines)
