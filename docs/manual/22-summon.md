# 22. 에이전트 소환 (Summon)

## 개요

프로젝트 에이전트의 산출물을 **다중 에이전트(비판자 + 전문가)**가 협업하여 리뷰/개선하는 시스템. 사용자가 "소환해줘", "리뷰 좀 받아봐" 등 요청하면 소환 세션이 시작된다.

## 흐름

```
사용자: "디자이너 소환해서 UI 봐줘"
  ↓
프로젝트 에이전트 → POST /api/summon (세션 생성, status: pending)
  ↓
데몬 SummonManager (10초 폴링) → GET /api/bot/summon (pending 세션 감지)
  ↓
세션별 별도 스레드 실행 (status: active)
  ↓
[라운드 루프]
  1. 전문가 Claude CLI 호출 (의견 수집) → 채팅에 발언 표시
  2. 비판자 Claude CLI 호출 (검토/피드백) → 채팅에 발언 표시
  3. 비판자가 [SUMMON_COMPLETE] 선언 시 종료 / max_rounds 도달 시 자동 종료
  ↓
결과: docs/summon/summon-{id}.md 저장, DB 업데이트 (status: completed)
```

## 에이전트 역할

### 비판자 (Critic) — 자동 포함
- 소환 세션의 **사회자이자 감독관**
- 전문가 의견을 수집하고, 개선 포인트를 지시
- `[SUMMON_COMPLETE]` 태그로 세션 종료 결정
- `@전문가명` 태그로 다음 라운드에 호출할 전문가를 지정 가능

### 전문가 (Guest Agents)

| 이름 | 역할 | 전문 영역 |
|------|------|-----------|
| `designer` | UI/UX 디자이너 | UI/UX 설계, 접근성, 반응형, 디자인 시스템 |
| `marketer` | 마케팅 전략가 | 마케팅 전략, 포지셔닝, 콘텐츠, 전환율 |
| `code-reviewer` | 시니어 코드 리뷰어 | 코드 품질, 아키텍처, 보안, 성능 |

## API

### 소환 생성

```
POST /api/summon
인증: Authorization: Bearer 또는 X-Api-Key 또는 세션 쿠키

Body:
{
  "host_project": "peter-voice",          // 필수: 프로젝트 ID
  "guest_agents": ["designer"],           // 필수: 참여 전문가 배열
  "context_summary": "리뷰 대상 설명...",  // 권장: 맥락 요약
  "context_docs": ["/path/to/file.md"],   // 선택: 참조 파일 경로
  "max_rounds": 5                         // 선택: 최대 라운드 (기본 20)
}

Response (201):
{ "session": { "id": 3, "status": "pending", ... } }
```

- 프로젝트당 동시 1개 소환만 가능 (active/pending 상태가 있으면 409)

### 소환 상태 조회

```
GET /api/summon?project=peter-voice
→ { "sessions": [...] }

GET /api/summon/{id}
→ { "session": {...}, "messages": [...] }
```

### 소환 취소

```
DELETE /api/summon/{id}
→ { "ok": true }
```

데몬이 매 라운드 시작 전에 취소 여부를 확인하므로, 현재 라운드 완료 후 중단됨.

### 데몬 폴링 (봇 전용)

```
GET /api/bot/summon
인증: Authorization: Bearer
→ { "session": {...} }  // pending 세션 1개 반환, 없으면 null
```

## DB 스키마

### summon_sessions

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | serial PK | 세션 ID |
| user_id | int FK | 소유 유저 |
| host_project | text | 프로젝트 ID |
| guest_agents | jsonb | 전문가 배열 |
| status | text | pending → active → completed / cancelled |
| current_round | int | 현재 라운드 |
| max_rounds | int | 최대 라운드 (기본 20) |
| context_summary | text | 맥락 요약 |
| context_docs | jsonb | 참조 문서 경로 |
| result_summary | text | 최종 요약 |
| result_doc_path | text | 결과 문서 경로 |
| created_at | timestamptz | 생성 시각 |
| completed_at | timestamptz | 완료 시각 |

### summon_messages

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | serial PK | 메시지 ID |
| session_id | int FK | 소환 세션 |
| round | int | 라운드 번호 |
| agent | text | 발언 에이전트 (designer, critic 등) |
| role | text | agent |
| content | text | 발언 내용 |
| created_at | timestamptz | 생성 시각 |

## 데몬 구현 (summon.py)

### SummonManager
- `threading.Thread`로 10초 주기 폴링
- `/api/bot/summon`에서 pending 세션 가져옴
- 세션별 별도 스레드 (`_run_summon_session`) 실행
- 완료된 스레드 자동 정리

### 라운드 처리 흐름
1. **전문가 호출**: 각 전문가에 대해 `claude -p --output-format json` 1회 호출
   - 첫 라운드: 전체 전문가 호출
   - 2라운드 이후: 비판자의 `@전문가명` 태그로 호출 대상 결정 (태그 없으면 전체)
2. **비판자 호출**: 전문가 의견 + 대화 히스토리를 보고 판단
3. **채팅 표시**: 각 에이전트 발언을 `POST /api/bot/reply` (subtype: summon)로 채팅에 실시간 표시
4. **종료 판단**: `[SUMMON_COMPLETE]` 포함 시 종료, 아니면 다음 라운드

### @전문가 지정 호출
비판자가 응답에 `@designer`, `@code-reviewer` 등을 포함하면, 다음 라운드에서 해당 전문가만 호출됨.
- 파싱: `re.findall(r"@(\w[\w-]*)", critic_response)`
- 매칭 없으면 전체 전문가 호출 (안전한 fallback)

## 웹 UI

### 채팅 표시
- `MessageBubble` 컴포넌트에서 `subtype === "summon"` 감지
- 보라색 배경(`bg-purple-500/10`) + 보라색 테두리(`border-purple-500/30`)
- 글씨는 검정(라이트) / 흰색(다크) — 가독성 확보
- 마크다운 렌더링 지원

### 결과 문서
소환 완료 시 `docs/summon/summon-{id}.md`에 전체 대화 로그 + 최종 요약 저장. docs 폴더이므로 웹 UI 문서 탭에서도 조회 가능.

## 관련 파일

| 레포 | 파일 | 설명 |
|------|------|------|
| 웹 (sonolbot_web) | `app/api/summon/route.ts` | 생성(POST), 목록(GET) |
| 웹 | `app/api/summon/[id]/route.ts` | 상세(GET), 업데이트(PATCH), 취소(DELETE) |
| 웹 | `app/api/bot/summon/route.ts` | 데몬 폴링용 |
| 웹 | `components/MessageBubble.tsx` | 소환 메시지 UI |
| 데몬 (petervoice-daemon) | `scripts/daemon/summon.py` | SummonManager, 에이전트 프롬프트, 라운드 루프 |
| 웹 | `supabase/migrations/20260407100000_summon_sessions.sql` | 테이블 생성 |

## 제한사항 및 향후 개선

| 항목 | 현재 상태 | 비고 |
|------|-----------|------|
| 일일 소환 횟수 제한 | 미구현 | 비용 통제 필요 |
| 전문가 병렬 호출 | 순차 호출 | 3명이면 라운드당 ~15분 |
| 커스텀 전문가 | 3종 하드코딩 | 사용자 정의 전문가 미지원 |
| 메인 대화 컨텍스트 | context_summary만 | 실제 대화 히스토리 미포함 |
| 실시간 스트리밍 | 완료 후 일괄 표시 | 에이전트 응답 중 스트리밍 미지원 |
