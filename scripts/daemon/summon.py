"""Summon Manager: 에이전트 소환 세션 처리.

프로젝트 에이전트가 소환을 요청하면, 비판자(Critic) + 전문가 에이전트를
임시 Claude 프로세스로 실행하고, 턴 기반 대화를 진행한다.
"""

import json
import subprocess
import threading
import time
from pathlib import Path

import daemon.globals as g
from daemon.globals import (
    CLAUDE_CMD, PROMPTS_DIR, SECRETS_ENV_PATH,
    config, shutdown_event, logger,
)
from daemon.api import api_request
from daemon.supabase import get_project_dir

SUMMON_POLL_INTERVAL = 10  # seconds

# ─── 에이전트 프롬프트 ────────────────────────────────

CRITIC_PROMPT = """당신은 **비판자(Critic)** 에이전트입니다. 소환 세션의 사회자이자 감독관 역할입니다.

## 역할
1. **진행(Facilitate)**: 프로젝트 에이전트의 작업물을 평가하고, 전문가에게 의견을 요청
2. **비판(Challenge)**: "이걸로 충분한가?", "예외 케이스는?", "사용자 관점에서 직관적인가?"
3. **중재(Mediate)**: 전문가와 프로젝트 에이전트의 의견이 다를 때 방향 잡기
4. **종료 결정(Conclude)**: 더 이상 개선할 게 없다고 판단되면 세션 종료

## 응답 규칙
- 매 턴마다 구체적인 개선 포인트를 제시하거나, 충분하다고 판단하면 종료를 선언
- 종료 시 반드시 `[SUMMON_COMPLETE]` 태그로 시작하는 최종 요약을 작성
- 요약에는: (1) 주요 개선 사항, (2) 최종 결론, (3) 남은 과제를 포함
- 한국어로 답변

## 종료 판단 기준
- 작업물의 완성도가 충분하다고 판단될 때
- 더 이상 실질적 개선이 어렵다고 판단될 때
- 같은 피드백이 반복될 때
"""

EXPERT_PROMPTS = {
    "designer": """당신은 **UI/UX 디자이너** 전문가 에이전트입니다.

## 전문 영역
- UI/UX 설계, 사용자 경험 개선, 시각 디자인
- 접근성(Accessibility), 반응형 디자인
- 디자인 시스템, 컴포넌트 구조

## 응답 규칙
- 구체적이고 실행 가능한 디자인 제안을 할 것
- 코드 수정이 필요하면 구체적 코드를 제시
- 한국어로 답변
""",
    "marketer": """당신은 **마케팅 전략가** 전문가 에이전트입니다.

## 전문 영역
- 마케팅 전략, 포지셔닝, 메시징
- 콘텐츠 기획, 랜딩페이지 최적화
- 고객 여정, 전환율 개선

## 응답 규칙
- 타겟 고객 관점에서 분석할 것
- 데이터 기반의 구체적 제안을 할 것
- 한국어로 답변
""",
    "code-reviewer": """당신은 **시니어 코드 리뷰어** 전문가 에이전트입니다.

## 전문 영역
- 코드 품질, 아키텍처, 성능 최적화
- 보안 취약점 탐지, OWASP Top 10
- 테스트 전략, 리팩토링

## 응답 규칙
- 구체적인 코드 라인을 지적하고 개선안을 제시
- 심각도(Critical/Warning/Info)를 표시
- 한국어로 답변
""",
}


def _run_claude_once(prompt: str, system_prompt: str, cwd: str) -> str:
    """단일 Claude CLI 호출 (세션 없이)."""
    prompt_file = PROMPTS_DIR / f"_summon_sys_{threading.current_thread().name}.md"
    prompt_file.write_text(system_prompt, encoding="utf-8")

    cmd = [
        CLAUDE_CMD, "-p",
        "--output-format", "json",
        "--append-system-prompt-file", str(prompt_file),
        "--verbose",
        "--dangerously-skip-permissions",
    ]

    model = config.get("claude_model")
    if model:
        cmd.extend(["--model", model])

    env = dict(__import__("os").environ)
    if SECRETS_ENV_PATH.exists():
        for line in SECRETS_ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k] = v

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=cwd,
            env=env,
        )
        if result.returncode != 0:
            logger.error(f"[summon] Claude failed: {result.stderr[:500]}")
            return f"[에러] Claude 실행 실패: {result.stderr[:200]}"

        # JSON output 파싱
        try:
            data = json.loads(result.stdout)
            return data.get("result", result.stdout[:3000])
        except json.JSONDecodeError:
            return result.stdout[:3000]
    except subprocess.TimeoutExpired:
        return "[에러] Claude 응답 시간 초과 (5분)"
    except Exception as e:
        return f"[에러] {e}"


def _update_session(session_id: int, **kwargs):
    """소환 세션 업데이트."""
    api_key = config.get("api_key", "")
    body = {}
    for k, v in kwargs.items():
        if k == "message":
            body["message"] = v
        else:
            body[k] = v
    api_request(api_key, "PATCH", f"/api/summon/{session_id}", body=body)


def _post_chat_message(project: str, text: str, subtype: str = "summon"):
    """소환 상태를 프로젝트 채팅에 시스템 메시지로 표시."""
    api_key = config.get("api_key", "")
    api_request(api_key, "POST", "/api/bot/reply", body={
        "project": project,
        "text": text,
        "subtype": subtype,
    })


def _run_summon_session(session: dict):
    """소환 세션 실행 (별도 스레드)."""
    session_id = session["id"]
    host_project = session["host_project"]
    guest_agents = session["guest_agents"]
    context = session.get("context_summary") or ""
    max_rounds = session.get("max_rounds", 20)

    logger.info(f"[summon] Starting session {session_id}: {guest_agents} for {host_project}")

    # 상태: active
    _update_session(session_id, status="active")

    # 프로젝트 디렉토리
    try:
        cwd = get_project_dir(host_project)
    except Exception:
        cwd = str(Path.home())

    # 비판자 프롬프트 구성
    experts = [a for a in guest_agents if a != "critic"]
    expert_names = ", ".join(experts) if experts else "없음"
    critic_system = CRITIC_PROMPT + f"\n\n## 이번 소환 정보\n- 프로젝트: {host_project}\n- 참여 전문가: {expert_names}\n- 최대 라운드: {max_rounds}\n"

    # 초기 메시지
    _post_chat_message(host_project, f"🔮 소환 세션 시작 — {', '.join(guest_agents)} (최대 {max_rounds}라운드)")

    # 턴 루프
    conversation_log = []
    if context:
        conversation_log.append(f"[프로젝트 컨텍스트]\n{context}")

    for round_num in range(1, max_rounds + 1):
        if shutdown_event.is_set():
            break

        # 세션 상태 확인 (취소 여부)
        api_key = config.get("api_key", "")
        check = api_request(api_key, "GET", f"/api/summon/{session_id}")
        if check and check.get("session", {}).get("status") == "cancelled":
            logger.info(f"[summon] Session {session_id} cancelled by user")
            _post_chat_message(host_project, "🔮 소환 세션이 취소되었습니다.")
            return

        _update_session(session_id, current_round=round_num)

        # 1. 전문가 의견 수집 (있는 경우)
        expert_opinions = []
        for expert in experts:
            expert_prompt = EXPERT_PROMPTS.get(expert, f"당신은 {expert} 전문가입니다. 한국어로 답변하세요.")
            expert_input = f"현재 대화 맥락:\n\n{''.join(conversation_log[-6:])}\n\n위 맥락을 보고 {expert} 관점에서 의견을 제시하세요. (라운드 {round_num})"

            opinion = _run_claude_once(expert_input, expert_prompt, cwd)
            expert_opinions.append(f"[{expert}] {opinion}")

            _update_session(session_id, message={
                "round": round_num,
                "agent": expert,
                "content": opinion,
            })

        # 2. 비판자 턴
        all_context = "\n\n".join(conversation_log[-6:])
        expert_section = "\n\n".join(expert_opinions) if expert_opinions else "(전문가 없음 — 비판자 단독)"
        critic_input = f"""## 현재까지의 대화
{all_context}

## 전문가 의견 (라운드 {round_num})
{expert_section}

---
위 내용을 검토하고:
1. 개선이 필요하면 구체적 피드백을 제시하세요.
2. 충분하다고 판단되면 `[SUMMON_COMPLETE]`로 시작하는 최종 요약을 작성하세요.
라운드 {round_num}/{max_rounds}"""

        critic_response = _run_claude_once(critic_input, critic_system, cwd)

        _update_session(session_id, message={
            "round": round_num,
            "agent": "critic",
            "content": critic_response,
        })

        conversation_log.append(f"[라운드 {round_num}]\n{expert_section}\n\n[비판자] {critic_response}")

        # 상태 업데이트
        _post_chat_message(host_project, f"🔮 소환 라운드 {round_num}/{max_rounds} 완료")

        # 3. 완료 체크
        if "[SUMMON_COMPLETE]" in critic_response:
            summary = critic_response.split("[SUMMON_COMPLETE]", 1)[1].strip()
            _update_session(session_id,
                status="completed",
                current_round=round_num,
                result_summary=summary,
            )

            # 결과를 docs/에 저장
            try:
                docs_dir = Path(cwd) / "docs" / "summon"
                docs_dir.mkdir(parents=True, exist_ok=True)
                result_path = docs_dir / f"summon-{session_id}.md"
                full_log = f"# 소환 세션 #{session_id} 결과\n\n"
                full_log += f"**프로젝트**: {host_project}\n"
                full_log += f"**참여 에이전트**: {', '.join(guest_agents)}\n"
                full_log += f"**라운드**: {round_num}\n\n---\n\n"
                full_log += "\n\n---\n\n".join(conversation_log)
                full_log += f"\n\n---\n\n## 최종 요약\n\n{summary}"
                result_path.write_text(full_log, encoding="utf-8")

                _update_session(session_id, result_doc_path=str(result_path))
            except Exception as e:
                logger.warning(f"[summon] Failed to save result doc: {e}")

            _post_chat_message(host_project,
                f"🔮 소환 완료 ({round_num}라운드) — {', '.join(guest_agents)}\n\n{summary[:500]}")
            logger.info(f"[summon] Session {session_id} completed after {round_num} rounds")
            return

    # max_rounds 초과
    _update_session(session_id,
        status="completed",
        result_summary="최대 라운드 도달로 자동 종료",
    )
    _post_chat_message(host_project, f"🔮 소환 세션 종료 (최대 {max_rounds}라운드 도달)")
    logger.info(f"[summon] Session {session_id} hit max rounds")


class SummonManager(threading.Thread):
    """소환 세션 폴링 및 실행 관리."""

    def __init__(self):
        super().__init__(daemon=True, name="summon-manager")
        self._active_sessions: dict[int, threading.Thread] = {}

    def _poll_and_run(self):
        api_key = config.get("api_key", "")
        if not api_key:
            return

        result = api_request(api_key, "GET", "/api/bot/summon", timeout=10)
        if not result or not result.get("session"):
            return

        session = result["session"]
        sid = session["id"]

        # 이미 실행 중이면 스킵
        if sid in self._active_sessions and self._active_sessions[sid].is_alive():
            return

        # 새 세션 실행
        t = threading.Thread(target=_run_summon_session, args=(session,), daemon=True, name=f"summon-{sid}")
        t.start()
        self._active_sessions[sid] = t

        # 완료된 세션 정리
        self._active_sessions = {k: v for k, v in self._active_sessions.items() if v.is_alive()}

    def run(self):
        logger.info("[summon] Manager started")
        shutdown_event.wait(10)  # 초기 대기

        while not shutdown_event.is_set():
            try:
                self._poll_and_run()
            except Exception as e:
                logger.error(f"[summon] Poll error: {e}")

            shutdown_event.wait(SUMMON_POLL_INTERVAL)

        logger.info("[summon] Manager stopped")
