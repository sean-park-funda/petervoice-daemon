# 11. 에이전트 간 통신 (Relay)

## 개요

서로 다른 프로젝트의 Claude 봇이 메시지를 주고받는 시스템. 현재 **Relay(단방향 전달)**이 구현되어 있으며, Consult(상의)와 Delegate(위임)은 설계 단계.

## Relay — 단방향 메시지 전달

### 흐름

```
프로젝트 A의 Claude (또는 유저)
  ↓ POST /api/relay/message
  ↓ { to_project, from_project, text, attachments }
messages 테이블에 "[relay from:A]\n메시지" 형태로 주입
  ↓ 데몬이 프로젝트 B의 메시지로 폴링
프로젝트 B의 Claude가 메시지 처리
  ↓ 응답 (필요시 역방향 relay)
```

### API

```
POST /api/relay/message
인증: X-Api-Key 또는 세션 쿠키

Body:
{
  "to_project": "peter-voice",     // 필수: 대상 프로젝트
  "from_project": "evolution",     // 필수: 발신 프로젝트
  "text": "전달할 메시지",          // 필수: 본문
  "attachments": ["/path/to/file"] // 선택: 첨부 파일 경로
}

응답:
{
  "success": true,
  "message_id": 15832,
  "to_project": "peter-voice",
  "from_project": "evolution"
}
```

### 메시지 포맷

```
[relay from:evolution]
전달할 메시지 본문

📎 첨부 문서 (반드시 읽을 것):
- /path/to/attached/document.md
```

### 검증

- 대상 프로젝트 존재 여부 확인
- 존재하지 않으면 404 + 사용 가능한 프로젝트 목록 반환

### 사용 가능한 프로젝트 목록

프로젝트 목록은 유저마다 다르므로 **하드코딩하지 않고 API로 동적 조회**해야 한다.

```bash
# 에이전트가 릴레이 전송 전에 자기 프로젝트 목록을 조회
curl -s "$API_URL/api/projects" -H "Authorization: Bearer $API_KEY"
# → { "projects": [{"id": "peter-voice", "name": "피터보이스"}, ...] }
```

- `GET /api/projects`는 Bearer 토큰 인증 지원 (v2026-03-30~)
- `_common` 프롬프트에 하드코딩된 프로젝트 목록은 폐기됨
- 릴레이 API 자체도 존재하지 않는 프로젝트로 보내면 404 + 유효 목록 반환

## 봇의 Relay 호출 방법

`_common` 프롬프트에 정의된 방법:

```bash
API_URL=$(python3 -c "import json; c=json.load(open('/Users/sean/.claude-daemon/config.json')); print(c.get('api_url'))")
API_KEY=$(python3 -c "import json; print(json.load(open('/Users/sean/.claude-daemon/config.json'))['api_key'])")

curl -s -X POST "$API_URL/api/relay/message" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "to_project": "대상 프로젝트명",
    "from_project": "현재 프로젝트명",
    "text": "전달할 메시지"
  }'
```

**주의**: `Authorization: Bearer` 가 아니라 `X-Api-Key` 헤더를 사용해야 함.

## Relay 수신 처리

수신 봇의 `_common` 프롬프트에 정의:

```
[relay from:프로젝트명] 접두어 → 다른 에이전트가 보낸 메시지
📎 첨부 문서 → Read 도구로 읽고 맥락 파악
응답은 일반 유저 응답처럼 처리하되, 발신 맥락 고려
```

## 브랜치 릴레이

### 브랜치→프로젝트/브랜치 릴레이

브랜치 에이전트도 릴레이를 보낼 수 있다. `from_branch_id`와 `to_branch_id` 파라미터로 브랜치 간 직접 통신 가능.

```
POST /api/relay/message
{
  "from_project": "peter-voice",
  "from_branch_id": 79,        // 발신 브랜치 (선택)
  "to_project": "peter-voice",
  "to_branch_id": 54,          // 수신 브랜치 (선택, null이면 메인)
  "text": "메시지"
}
```

### 릴레이 가이드 자동 주입

`branches.py`의 `_build_branch_relay_guide()`가 모든 브랜치 에이전트 프롬프트에 릴레이 방법을 자동 주입:
- 프로젝트 목록 조회 방법
- 브랜치 목록 조회 방법
- 메시지 전송 예시 (from_branch_id 포함)
- 수신 시 처리 가이드

이로써 브랜치 에이전트가 별도 안내 없이도 다른 프로젝트/브랜치에 릴레이를 보낼 수 있다.

## 서버 가드 — 왕복 깊이·수신 건수 (2026-10-26 교체)

릴레이는 받는 세션에 턴을 열고, 그 턴이 또 릴레이로 끝나면 사람 없이 연쇄가 이어진다. 서버(`POST /api/relay/message`)가 두 가지 상한으로 끊는다. 둘 다 **사람 입력이 들어오면 0 으로** 돌아가고, 하트비트·예약 작업·`[stall-check]`·`[매니저]` 같은 자동 입력은 세지도 끊지도 않는다.

| 가드 | 기준 | 상한 | 응답 |
|---|---|---|---|
| 왕복(핑퐁) 깊이 | **이번 릴레이의 두 세션 사이**에 사람 입력 없이 오간 릴레이 수 (`pair_depth`) | 다른 프로젝트 사이 5 (6홉 허용), 같은 프로젝트 안(메인 ↔ 자기 브랜치·형제 브랜치·자기 자신) 11 (12홉 허용) | 429 `relay_depth_exceeded` + `depth`·`pair_depth`·`limit`·`pair`·`scope` |
| 수신 건수 | 받는 세션이 사람 입력 없이 **최근 60분**에 받은 릴레이 수 | 60 | 429 `relay_count_exceeded` + `chain`·`limit` |

- 총괄이 담당 A → B → C 로 **상대를 바꿔 가며** 지시·보고를 주고받는 흐름은 길이와 무관하게 막히지 않는다(10/22~10/26 의 「총 연쇄 깊이 5」는 이 흐름을 왕복 3번 만에 막아 교체했다). 막히는 것은 같은 두 세션 사이의 왕복이다.
- 보내는 세션의 마지막 「사람 또는 릴레이」 입력이 **6시간 넘게 지난 것**이면 새 연쇄로 시작한다(하루 한 번 같은 상대에게 요청·회신하는 감시 담당은 매일 0→1).
- 성공 응답에 `relay_depth`(총 깊이, 참고값)와 `pair_depth` 가 붙는다. 저장 메시지의 `metadata.relay`(version 2: `depth`·`pair_depth`·`pairs`·`path`)는 서버만 쓴다. 요청 body 의 depth·metadata 는 무시된다.
- 429 를 받으면 **같은 턴에서 재시도하지 않는다.** 깊이 초과는 `pair` 의 두 세션 중 보내는 쪽에 사람이 한 줄 쓰면(또는 6시간 조용하면) 풀리고, 건수 초과는 받는 세션에 사람 글이 들어오거나 오래된 건이 1시간을 넘기면 풀린다. 다른 담당 경유로 같은 내용을 돌리는 것은 우회다 — 파일(`docs/`)로 남기고 유저에게 보고한다.
- 조회 실패·손상된 기록은 503 `relay_guard_unavailable` (전송 보류, 자동 재시도 금지).
- 상세·실측·이력: 공유지 위키 `hazard/peter-voice-relay-chain-block`, peter-voice `docs/plans/2026-10-26-relay-pairwise-depth.md`.

## 미래 패턴 (설계 단계)

### 패턴 B: Consult (상의)

에이전트 간 멀티턴 대화. 토픽, max_turns 지정.

```
[CONSULT from:A topic:"PocketBase 도입" max_turns:6]
초기 질문/컨텍스트
  ↔ 양방향 멀티턴 (2~10턴)
[CONSULT:DONE]
결론 + 액션 아이템
```

### 패턴 C: Delegate (위임)

작업 요청 + 완료 보고.

```
[DELEGATE from:A]
작업 내용 + 참고 문서
  → 수신 봇이 작업 수행
[DELEGATE:DONE]
완료 결과 + PR 링크 등
```

### 내재화 계획 (Phase 2)

데몬 ManagerThread 기반:
- `agent_messages` 테이블: 에이전트 간 메시지
- `agent_threads` 테이블: 스레드 상태 관리
- 웹 UI: 에이전트 간 대화 조회, 승인/거부 버튼
