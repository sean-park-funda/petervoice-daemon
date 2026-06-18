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
        lines.append("- **Google** (Gmail/캘린더/드라이브/닥스/시트): `/gmail`·`/google-calendar`·`/google-docs`·`/google-sheets`·`/google-drive` 스킬 사용.")
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
