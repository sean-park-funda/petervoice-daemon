"""Prompt file management: get, ensure template, build system prompt."""

from daemon.globals import PROMPTS_DIR, logger
from daemon.supabase import fetch_prompt_from_supabase, get_project_dir


def get_prompt_file(project: str):
    """프로젝트별 CLAUDE.md 파일 경로 반환. Supabase에서 동기화 후 로컬 파일 반환."""
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    prompt_file = PROMPTS_DIR / f"{project}.md"
    content = fetch_prompt_from_supabase(project)
    if content is not None:
        prompt_file.write_text(content, encoding="utf-8")
        return prompt_file
    if not prompt_file.exists():
        template = PROMPTS_DIR / "_template.md"
        if template.exists():
            content = template.read_text(encoding="utf-8")
            content = content.replace("{{project}}", project)
            content = content.replace("{{project_dir}}", get_project_dir(project))
        else:
            content = f"# {project}\n\n프로젝트 컨텍스트를 여기에 작성하세요.\n"
        prompt_file.write_text(content, encoding="utf-8")
        logger.info(f"Created prompt file: {prompt_file}")
    return prompt_file


def ensure_template():
    """_template.md 자동 생성."""
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    template = PROMPTS_DIR / "_template.md"
    if not template.exists():
        template.write_text(
            "# {{project}}\n\n"
            "## 프로젝트 경로\n{{project_dir}}\n\n"
            "## 컨텍스트\n프로젝트 설명을 여기에 작성하세요.\n",
            encoding="utf-8"
        )
        logger.info(f"Created template: {template}")


def build_system_prompt(project: str, task_name: str | None = None, task_desc: str | None = None) -> str:
    """task 컨텍스트만 생성. 공통 지시사항은 _common 프롬프트에 통합됨."""
    if task_name and task_name != "default":
        task_context = f"[현재 작업: {task_name}]"
        if task_desc:
            task_context += f" {task_desc}"
        return task_context
    return ""


def build_connected_services_note() -> str:
    """연결된 외부 서비스(env로 감지) + 사용할 스킬을 간결히 안내.
    프롬프트 조립 시 시스템 레이어에 주입됨. 매 메시지 재조립되므로 새 연결도 다음 턴부터 인지됨."""
    import os
    lines = []
    if os.environ.get("GOOGLE_REFRESH_TOKEN"):
        accounts = os.environ.get("GOOGLE_ACCOUNTS", "")
        acc_note = f" 연결된 계정: {accounts} — 특정 계정은 `gmail.py --account <email>`." if accounts and "," in accounts else ""
        lines.append(f"- **Google**: 메일은 `/gmail`, 일정은 `/google-calendar` 스킬 사용.{acc_note} (드라이브/닥스/시트 스킬은 아직 미배포 — 필요 시 직접 API 호출)")
    if os.environ.get("SLACK_BOT_TOKEN"):
        lines.append("- **Slack**: `/slack` 스킬로 채널/DM 읽기·요약·전송. 비공개 채널은 봇 초대 필요.")
    if os.environ.get("NOTION_API_TOKEN"):
        lines.append("- **Notion**: `/notion-api` 스킬로 페이지 읽기/작성/검색.")
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        lines.append("- **Telegram**: `/telegram` 스킬로 메시지 전송.")
    if not lines:
        return ""
    return ("## 연결된 외부 서비스\n"
            "아래 서비스가 이 유저 계정에 연결되어 있습니다. 관련 요청 시 해당 스킬을 쓰면 됩니다(토큰은 환경변수로 자동 주입됨).\n"
            + "\n".join(lines))


# ── 담당자 명부 (프로젝트 + 활성 브랜치) ─────────────────────────────
# 프로젝트/브랜치 프롬프트에 {담당자 명부} 를 적어두면 이 블록으로 치환된다.
# 손으로 적어둔 목록은 금방 낡고, 낡은 목록은 라우팅 실패 → "일단 부사장에게 전달"
# 같은 떠넘기기를 만든다. (2026-08-18 비서실 실사고: 목록에 evolution이 없어
# "자가성장이 어느 프로젝트냐"고 되물음 / 브랜치는 목록에 아예 없어 매번 curl 4회)
ROSTER_PLACEHOLDER = "{담당자 명부}"
_ROSTER_TTL = 300  # 초 — 한 턴에 API를 두 번 이상 부르지 않도록
_ROSTER_BRANCH_LIMIT = 40  # 활성 브랜치가 수백 개인 계정도 있어 상한을 둔다
_roster_cache: dict = {"text": None, "at": 0.0}


def build_agent_roster() -> str:
    """지금 이 순간의 프로젝트 + 활성 브랜치 명부를 마크다운으로 만든다 (5분 캐시)."""
    import time
    from daemon.api import api_request
    from daemon.globals import config

    now = time.time()
    cached = _roster_cache.get("text")
    if cached and now - _roster_cache.get("at", 0) < _ROSTER_TTL:
        return cached

    api_key = config.get("api_key", "")
    if not api_key:
        return cached or ""

    try:
        proj_res = api_request(api_key, "GET", "/api/projects", timeout=10) or {}
        project_rows = list(proj_res.get("projects") or [])
        names = {}
        proj_lines = []
        for p in project_rows:
            pid = p.get("id")
            if not pid:
                continue
            name = p.get("name") or pid
            names[pid] = name
            proj_lines.append(f"- `{pid}` — {name}")

        # 활성 브랜치는 수백 개가 될 수 있어 전부 넣으면 프롬프트가 폭발한다.
        # 최근 쓴 순으로 상한만 싣고, 나머지는 조회 방법을 안내한다.
        branch_lines = []
        dropped = 0
        br_res = api_request(api_key, "GET", "/api/branches?all_active=1", timeout=10) or {}
        rows = [b for b in (br_res.get("branches") or []) if b.get("id")]
        # session_started_at은 "세션을 처음 연 시각"이라 몇 달 전 값일 수 있다.
        # 최근성은 세 타임스탬프 중 가장 최근 것으로 판단한다 (ISO 문자열 비교로 충분).
        rows.sort(
            key=lambda b: max(
                b.get("updated_at") or "",
                b.get("session_started_at") or "",
                b.get("created_at") or "",
            ),
            reverse=True,
        )
        dropped = max(0, len(rows) - _ROSTER_BRANCH_LIMIT)
        for b in rows[:_ROSTER_BRANCH_LIMIT]:
            pid = b.get("project_id", "")
            owner = names.get(pid, pid)
            branch_lines.append(
                f"- `branch:{b['id']}` — #{b.get('branch_number')} {b.get('title', '')} ({owner} 브랜치)"
            )

        if not proj_lines and not branch_lines:
            return cached or ""

        parts = [
            "## 담당자 명부 (자동 주입 — 지금 이 순간의 실제 목록)",
            "릴레이·핸드오프 대상은 **여기서만** 고른다. 여기 없는 대상은 존재하지 않는다 (추측 금지).",
        ]
        if proj_lines:
            parts.append("### 프로젝트 — 릴레이 `to_project`, 핸드오프 `[[voice/handoff:id:용건]]`\n" + "\n".join(proj_lines))
        if branch_lines:
            tail = (f"\n- (그 외 활성 브랜치 {dropped}개는 최근 사용순에서 밀려 생략 — "
                    f"`GET $API_URL/api/branches?project_id=프로젝트ID` 로 확인)") if dropped else ""
            parts.append("### 활성 브랜치 (최근 사용순) — 릴레이 `to_branch_id`(숫자), 핸드오프 `[[voice/handoff:branch:숫자:용건]]`\n"
                         + "\n".join(branch_lines) + tail)
        text = "\n".join(parts)
        _roster_cache["text"] = text
        _roster_cache["at"] = now
        return text
    except Exception as e:
        logger.error(f"build_agent_roster failed: {e}")
        return cached or ""


def apply_roster_placeholder(text: str) -> str:
    """프롬프트 안의 {담당자 명부} 를 실시간 명부로 치환. 플레이스홀더가 없으면 그대로 반환."""
    if not text or ROSTER_PLACEHOLDER not in text:
        return text
    roster = build_agent_roster()
    if not roster:
        roster = "## 담당자 명부\n(조회 실패 — `GET $API_URL/api/projects` 와 `GET $API_URL/api/branches?all_active=1` 로 직접 확인할 것)"
    return text.replace(ROSTER_PLACEHOLDER, roster)
