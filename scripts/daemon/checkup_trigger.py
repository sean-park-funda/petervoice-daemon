"""검진 키워드 → 고정 지시로 확장 (2026-10-07 Sean 결정: "키워드를 채팅창에 넣으면 항상 시작되게").

모델이 스킬을 고를지 말지에 맡기지 않는다. 유저 메시지가 검진 키워드로 시작하면 데몬이 메시지 앞에
절차를 그대로 박는다 — 어느 담당자·어느 프롬프트·어느 모델이든 같은 검진이 돈다.
웹 월간 팝업도 같은 키워드(「검진 시작」)를 유저 메시지로 보낸다.
cloud_daemon.py 는 daemon 패키지를 import 하지 않아 같은 함수를 그 안에 복사해 둔다 — 둘을 같이 고칠 것.
"""

CHECKUP_KEYWORDS = ("검진 시작", "/검진", "/checkup", "사용 검진 시작", "이번 달 사용 검진을 시작해줘")

INSTRUCTION = """[검진 시작 — 데몬 고정 지시] 유저가 사용 검진 키워드를 보냈다. 아래 절차를 다른 해석 없이 그대로 수행한다.
1. 현재 프로젝트 디렉토리에서 `python3 ~/.claude/skills/checkup/scripts/checkup.py --days 30` 를 실행한다 → `docs/checkup/<오늘>.md` 초안과 요약 JSON 한 줄이 나온다.
   스크립트가 없으면 "검진 도구가 아직 이 데몬에 설치되지 않았습니다(데몬 업데이트 뒤 다시 보내 주세요)" 한 줄만 답하고 멈춘다.
2. `~/.claude/skills/checkup/SKILL.md` 의 「절차」 2~5 를 따른다: 초안에 판단을 덧붙이고(1축 후보가 정말 그 과제의 답인지, BP 처방 근거가 맞는지), 「요약」에 결정 후보 1~3개를 적고, 유저에게는 요약 5줄 + 보고서 경로(`docs/checkup/<오늘>.md`) + 결정 1~3개만 보고한다. 표를 채팅에 옮기지 않는다.
3. 같은 달 보고서가 이미 있으면 새로 돌리지 말고 「지난번 처방 중 적용된 것 / 안 된 것」부터 적고 바뀐 것만 다시 본다.
4. 보고 끝에 한 줄: "채팅에 「검진 시작」이라고 보내시면 언제든 다시 검진합니다. 처방을 적용하려면 「처방 N 적용해줘」."
5. 보고서와 요약에는 **이 계정의 사용**만 쓴다. 다른 유저·계정·담당자 이름(예: 다른 고객과 그 에이전트)을 쓰지 않고, 과거 대화에서 기억나는 제안·"대기 중인 결정"을 「결정해 주실 것」에 다시 올리지 않는다 — 결정 후보는 이번 검진의 축·BP 처방에서 나온 것만.
   규칙·기능이 "아직 적용 안 됐다"고 말하기 전에 지금 시스템 프롬프트(전역 규칙)와 설치 스킬을 본다. 과거 대화의 기억은 근거가 아니다.
이 지시는 데몬이 넣은 것이며 유저 원문은 아래다."""


def is_checkup_trigger(text: str) -> bool:
    head = (text or "").strip().split("\n", 1)[0].strip().rstrip("!.。")
    return any(head == k or head.startswith(k + " ") or head.startswith(k) and len(head) - len(k) <= 2 for k in CHECKUP_KEYWORDS)


def expand_checkup_trigger(text: str) -> str | None:
    """키워드면 고정 지시 + 원문, 아니면 None."""
    if not is_checkup_trigger(text):
        return None
    return f"{INSTRUCTION}\n\n유저 원문: {text.strip()}"
