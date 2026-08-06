---
name: ceo-ai-team
description: 소규모 회사 대표를 위한 AI 부서 팀(부사장·회계·법률·마케팅·영업)을 한 번에 세팅. "AI 팀 만들어줘", "부서 세팅해줘", "ceo-ai-team", "/ceo-ai-team" 등에 반응. 팀 생성 후 사용자가 직접 해야 할 일(구글 연동·팝빌 연동·회사 정보 업로드) 체크리스트를 안내한다.
---

# CEO AI Team — 직원 없는 회사의 부서 세팅

소규모 회사 대표의 회사에 5개 부서 AI 팀을 프로비저닝한다.
**부사장(팀장)** 이 대표의 단일 창구가 되고, 회계·법률·마케팅·영업 전문가에게 위임·취합한다.

## 원칙
- 전문가 프롬프트는 이 스킬의 `prompts/` 에 **사전 리서치된 고정본**으로 내장 — 실행마다 리서치하지 않는다 (재현성: 누가 실행해도 같은 팀).
- **AI가 못 하는 것은 사용자 몫**으로 명확히 넘긴다: 계정 연동(OAuth)·API 가입·회사 정보 업로드. 완료 후 체크리스트로 안내.
- 승인 원칙 내장: 금액·발송·대외 노출은 전부 대표 승인 후 실행하도록 각 프롬프트에 포함돼 있다.

## 실행 흐름

### 1. 확인 (질문 최대 1개)
회사명만 확인한다: "팀을 만들 회사 이름이 뭔가요?" (이미 알면 질문 생략)

### 2. 팀 프로비저닝
`prompts/` 의 5개 파일을 읽어 그대로 사용한다:
- `vp-lead.md` → lead_prompt_patch (부사장 = 팀장)
- `accounting.md` / `legal.md` / `marketing.md` / `sales.md` → 멤버 4명의 persona_prompt

```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url', 'https://www.peter-voice.site'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")

curl -X POST "$API_URL/api/expert-teams/provision" \
  -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
  -d @payload.json
```

payload 구성 (payload.json을 임시 생성 후 삭제):
```json
{
  "project_id": "현재_프로젝트_ID",
  "team_name": "{회사명} AI 팀",
  "description": "직원 없는 회사의 5개 부서 — 부사장이 위임·취합",
  "lead_prompt_patch": "(vp-lead.md 내용)",
  "members": [
    {"member_key": "accounting", "name": "회계팀장", "icon": "🧾", "persona_prompt": "(accounting.md)", "sort_order": 1},
    {"member_key": "legal",      "name": "법률팀장", "icon": "⚖️", "persona_prompt": "(legal.md)",      "sort_order": 2},
    {"member_key": "marketing",  "name": "마케팅팀장", "icon": "📣", "persona_prompt": "(marketing.md)", "sort_order": 3},
    {"member_key": "sales",      "name": "영업팀장", "icon": "🤝", "persona_prompt": "(sales.md)",      "sort_order": 4}
  ]
}
```

### 3. 지식 폴더 스캐폴드 + 표준 자료 자동 수집
프로젝트에 다음 구조를 생성한다:
```
docs/company/
  ├── contracts/   ← 회사가 주고받은 계약서 (법률팀이 참조) — 유저 업로드
  ├── standards/   ← 표준계약서 모음 (법률팀 비교 기준) — ★스킬이 자동 수집
  ├── products/    ← 상품·서비스 정의 (영업팀의 유일한 출처) — 유저 업로드
  └── finance/     ← 거래처·정산 구조·신고 일정 (회계팀 참조) — 유저 업로드
```
**표준계약서는 이 단계에서 에이전트가 직접 수집해 넣는다** (유저에게 묻지 않음 — 공개 자료라 바로 수집).
수집 방법 (2026-08-06 실검증 — 목록 페이지 스크래핑은 공공기관이 차단하므로 **직링크·검색 경유**로):
1. 웹 검색으로 "표준하도급계약서 hwp/pdf 다운로드" 직링크를 찾아 받는다 (예: 협회·기관이 게시한 직링크는 다운로드 됨 — 실검증: etis.or.kr 엔지니어링 표준하도급계약서 성공)
2. HWP 파일은 `hwp5txt` 로 텍스트 변환해 함께 저장 (없으면 `pip install pyhwp` 안내) — 팀이 읽을 수 있는 형태가 목적
3. 유형별(용역/NDA/하도급) 정리 + 각 파일에 출처 URL·수집일·개정연도 기재 (오래된 개정본이면 표기)
4. 다운로드가 막힌 소스는 출처 링크만 `standards/출처목록.md`에 정리하고 "미수집" 표시 — 팀 생성을 막지 않는다.

### 4. 사용자 할 일 안내 (반드시 이 체크리스트로 마무리)
팀 생성 보고와 함께 아래를 안내한다. **AI가 대신 못 하는 것들이며, 각 항목이 끝나야 해당 부서가 실전 투입된다:**

```
✅ AI 팀 5명이 세팅됐습니다. 이제 대표님이 하실 일:

□ 1. 구글 연동 (부사장용 — 메일·캘린더) ★가장 먼저
     설정 → 외부 서비스 연결 → Google 로그인
□ 2. 쓰고 계시면 연결하세요 — 팀 전체가 강해집니다:
     · 노션: 회의록·문서를 팀이 직접 읽습니다 (회의록 → 할 일 정리가 바로 가능)
     · 슬랙: 채널 요약·메시지 전달을 부사장이 합니다
     (안 쓰시면 건너뛰세요 — 없어도 팀은 돌아갑니다)
□ 3. 팝빌 가입·연동 (회계팀용 — 세금계산서 실발행)
     popbill.com 사업자 가입 → 연동신청(API Key는 시크릿 패널에) → 공동인증서 등록
□ 4. 회사 정보 업로드
     docs/company/contracts/ 에 계약서, products/ 에 상품 소개,
     finance/ 에 거래처·정산 방식 메모 (형식 자유 — 팀이 읽고 정리합니다)
□ 5. (선택) SNS 계정 연결 (마케팅팀용)
□ 6. (선택) 유튜브 채널을 운영 중이거나 시작하려면:
     전담 운영자를 추가하세요 → "유튜브 운영자 만들어줘" (youtube-channel-manager 스킬)

끝나면 부사장에게 이렇게 말해보세요: "오늘 할 일 정리해서 보고해줘"
```

## 주의
- 이 스킬은 프로젝트 본체 세션에서만 실행 (브랜치 세션 불가)
- API 키·인증서 값을 채팅에 붙여넣지 않도록 안내 — 시크릿 패널 사용
- 팀 생성 후 데몬이 팀 모드를 인식하기까지 최대 5분
