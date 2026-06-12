# 18. 매니저 — 자율 멀티턴 작업 관리

## 개요

에이전트는 기본적으로 **1턴 대화** 구조다. 사용자가 메시지를 보내면 에이전트가 한 번 응답하고 끝. 복잡한 작업은 사람이 계속 붙어서 대화를 이어가야 한다.

**매니저는 이 반복을 자동화한다.** 특정 에이전트와 여러 턴의 대화를 주고받으면서 결과물의 완성도를 자율적으로 판단하고, 충분히 올라갈 때까지 진행한다.

---

## 아키텍처

```
사용자 ─── "/do 유닛테스트 작성" ──→ 매니저 스레드
                                        │
                                        ├─ Turn 1: 지시 주입 → 에이전트 응답 대기
                                        ├─ Turn 2: "아직 미완" → 다음 단계 지시
                                        ├─ Turn 3: "거의 다 됨" → 보완 지시
                                        └─ Turn N: "DONE" 판정 → 사용자에게 요약 보고
```

매니저는 데몬 내부의 **독립 스레드**(`ManagerThread`)로 동작한다. 대상 프로젝트의 메시지 테이블에 직접 메시지를 주입하고, 에이전트의 응답을 폴링하는 방식으로 대화한다.

---

## 핵심 매커니즘

### 1. 메시지 주입과 응답 대기

매니저가 다른 에이전트와 대화하는 방법:

1. Supabase messages 테이블에 `[매니저] 질문내용`을 user 타입으로 INSERT
2. 대상 프로젝트의 worker가 이 메시지를 픽업 → Claude Code 실행
3. 매니저는 5초 간격으로 Supabase를 폴링하며 bot 응답을 대기
4. `reply_to`에 주입한 메시지 ID가 포함된 bot 응답을 수집
5. 최대 600초(설정 가능) 대기 후 타임아웃

**포인트**: 별도 API가 아니라 기존 메시지 큐를 그대로 활용. 대상 에이전트 입장에서는 사용자가 말을 건 것과 동일.

### 2. 체크포인트 기반 멀티턴 루프

매 턴마다 매니저가 판단하는 것이 아니라, **CHECKPOINT_INTERVAL (5턴)** 간격으로만 매니저가 개입한다:

```
Turn 1: 유저 원문 그대로 전달 → 에이전트 응답
Turn 2~4: "이어서 다음 항목을 처리하세요" → 에이전트 응답 (매니저 안 거침)
Turn 5 [체크포인트]: 매니저 Claude가 결과 판단
  → DONE → 종료
  → CONTINUE → "이어서 다음 항목을 처리하세요" 주입
  → QUESTION: 질문 → 에이전트에게 질문 전달
Turn 6~9: 직접 주입 (매니저 안 거침)
Turn 10 [체크포인트]: 매니저 판단 → ...반복
```

**첫 턴 동작**: 매니저가 작업을 단계별로 분해하지 않는다. 유저 원문을 그대로 대상 에이전트에 전달한다. 에이전트가 전문가이므로 스스로 계획하고 실행한다.

**체크포인트 판단**: 매니저 Claude에게 감독관 역할로 3가지 중 선택시킴:
1. `DONE` — 작업 완료, 종료 보고
2. `CONTINUE` — 잘 진행 중, 계속
3. `QUESTION: 질문` — 방향 의심, 에이전트에게 질문 전달

**유저 보고**: 매 턴이 아닌 **체크포인트 턴에서만** 유저에게 진행 보고 메시지 전송.

### 3. 완료 판정

| 조건 | 동작 |
|------|------|
| 체크포인트에서 "DONE" 판정 | 정상 종료, 요약 보고 |
| max_turns 도달 | 강제 종료, 현재까지 결과 보고 |
| 3턴 연속 응답 타임아웃 | 작업 중단, 유저에게 보고 |
| 단일 응답 타임아웃 (600초) | 리트라이 메시지 주입 후 계속 |
| 대상 프로젝트 busy (사용자 작업 중) | 30초 대기 후 재시도 |

---

## 사용 모드

### 모드 1: Deep Task (`/do` 명령)

사용자가 직접 작업을 요청:

```
/do auth 모듈 유닛테스트 작성해줘
```

**`/do N 작업내용`으로 max_turns 지정 가능** (예: `/do 30 유닛테스트 작성해줘`)

**흐름:**
1. worker가 작업을 매니저의 `task_queue`에 등록
2. 매니저 스레드가 깨어남 (`manager_wake_event`)
3. 유저 원문을 그대로 대상 에이전트에 전달 (첫 턴)
4. 5턴마다 체크포인트에서 매니저가 DONE/CONTINUE/QUESTION 판단
5. 체크포인트에서만 사용자에게 진행 보고
6. 완료 시 전체 요약 보고

**설정:**
- `max_turns`: 기본 50 (`/do N`으로 오버라이드 가능)
- `CHECKPOINT_INTERVAL`: 5 (매 5턴마다 매니저 판단)
- 최근 대화 10건을 컨텍스트로 함께 전달

### 모드 2: 자율 순회 (프로젝트 점검)

매니저가 설정된 간격으로 프로젝트를 자율 점검:

```
1. Scout — 프로젝트 상태 질문 (버그? 미완성 기능?)
2. Triage — 응답 분석 후 판단 (AUTO / ASK / SKIP)
3. Suggest — 사용자 승인 대기 또는 자율 실행
4. Execute — 멀티턴으로 작업 수행
```

**Triage 분기:**
- **AUTO**: 긴급 버그 → 즉시 자율 수행 (`auto_fix: true`)
- **ASK**: 개선/기능 → 사용자에게 제안, 승인 대기 (최대 60분)
- **SKIP**: 할 일 없음 → 다음 프로젝트로

---

## 설정

### config.json

```json
{
  "manager": {
    "enabled": true,
    "project_id": "manager",
    "interval_minutes": 30,
    "max_wait_sec": 600,
    "poll_interval_sec": 5,
    "suggestion_wait_min": 60,
    "quiet_hours": [0, 7],
    "projects": []
  }
}
```

| 항목 | 기본값 | 설명 |
|------|--------|------|
| `enabled` | false | 매니저 전체 ON/OFF |
| `projects` | - | 자율 순회 대상. **`[]`이면 자율 순회 비활성화, `/do`만 동작** |
| `interval_minutes` | 30 | 자율 순회 주기 |
| `max_wait_sec` | 600 | 에이전트 응답 대기 타임아웃 |
| `poll_interval_sec` | 5 | Supabase 폴링 간격 |
| `suggestion_wait_min` | 60 | 사용자 승인 대기 시간 |
| `quiet_hours` | [0, 7] | 비활동 시간대 |

### 자율 순회 대상 결정 우선순위

| `projects` 값 | 동작 |
|----------------|------|
| `[]` (빈 배열) | **자율 순회 완전 비활성화.** workflows/ 파일이 있어도 무시. `/do`만 동작. |
| `["signalflow", ...]` | 명시된 프로젝트만 순회 |
| 키 자체가 없음 | workflows/ → 활성 세션 순으로 폴백 |

> **중요**: `projects: []`는 최상위 킬스위치. 이 값이 빈 배열이면 `~/.claude-daemon/workflows/`에 파일이 있어도 무시된다.

### 워크플로우 파일 (~/.claude-daemon/workflows/{project}.md)

자율 순회가 활성화된 경우 프로젝트별 상세 설정:

```yaml
---
project: signalflow
description: "SignalFlow - 주식/ETF 비교 분석"
scout:
  focus:
    - 미완성 기능
    - UI/UX 개선
    - 버그 및 에러
  auto_fix: false
  max_wait_sec: 600
agent:
  autonomous: true      # 사용자 승인 없이 자율 실행
  max_turns: 10         # 멀티턴 최대 횟수
  stall_timeout_sec: 300
retry:
  max_backoff_sec: 300
---
```

---

## 상태 관리

### 상태 파일 (~/.claude-daemon/manager_state.json)

```json
{
  "last_run": "2026-03-29T14:30:00",
  "run_count": 325,
  "current_phase": "idle",
  "next_project_idx": 0,
  "hints": [],
  "retry_queue": {},
  "task_queue": []
}
```

**Phase 값:**
`idle` → `scouting` → `triaging` → `suggesting` → `executing` → `idle`
또는 `deep_task:{project}` (Deep Task 실행 중)

### 리트라이

실패 시 지수 백오프로 재시도:
- 1차: 10초 후, 2차: 20초 후, 3차: 40초 후, ...최대 `max_backoff_sec`(기본 300초)까지

---

## 모니터링

HTTP 상태 API (포트 7777):

```
GET  /api/manager/state       → 전체 상태
GET  /api/manager/{project}   → 프로젝트별 상태
POST /api/manager/refresh     → 강제 깨우기
```

---

## 사용자 가시성

매니저 채팅에서 매 턴마다 다음 정보가 보고된다:

| 항목 | 내용 |
|------|------|
| 지시 원문 | 에이전트에게 보낸 지시 전문 |
| 결과 요약 | Claude가 1줄로 요약한 진행상황 |
| 응답 앞부분 | 에이전트 응답의 첫 500자 |

### 메시지 흐름

```
[사용자]  /do 프로비저닝 개선해줘
  │
  ▼
[worker]  task_queue에 등록 → "매니저에 작업을 등록했습니다" 응답
  │
  ▼
[매니저 Claude]  작업을 단계로 분해 → 1단계 지시 생성
  │
  ├─ _inject_message() → [대상 프로젝트]  "[매니저] 프로비저닝 스크립트를 분석해줘..."
  │                           ▼
  │                    에이전트 작업 수행 → 응답
  │                           ▼
  │                    매니저가 응답 수집
  │
  ├─ Turn 2~4: "이어서 다음 항목을 처리하세요" → 직접 주입 (매니저 판단 안 거침)
  │
  └─ Turn 5 [체크포인트] → _post_to_user() → [매니저 채팅]
       "**[5/50] → peter-voice**
        CONTINUE (또는 DONE / QUESTION)"
  │
  ├─ DONE → 종료 보고
  ├─ CONTINUE → "이어서 다음 항목을 처리하세요" → 반복
  └─ QUESTION → 질문을 에이전트에 전달 → 반복
```

---

## 활용 가이드

### 핵심 패턴: "완성도를 높이는 자율 토론"

```
1. 유저 원문을 대상 에이전트에 그대로 전달 (첫 턴)
2. 비체크포인트 턴: "이어서 다음 항목을 처리하세요" 직접 주입
3. 체크포인트 턴 (매 5턴): 매니저가 DONE/CONTINUE/QUESTION 판단
4. CONTINUE면 계속, QUESTION이면 질문 전달 → 2로 돌아감
5. DONE이면 요약 후 종료
```

### 사용 예시

```
/do 칵테일 프로젝트의 성능 최적화 해줘
```

매니저가 자율적으로:
1. "현재 성능 병목이 어디인지 확인해줘" → 에이전트 분석
2. "쿼리 N+1 문제가 있네. 수정해줘" → 에이전트 수정
3. "수정 후 성능 테스트 결과 보여줘" → 에이전트 실행
4. "응답시간 200ms→50ms. DONE." → 사용자에게 요약 보고

### 설계 원칙

1. **기존 메시지 큐 활용** — 별도 통신 프로토콜 없이 Supabase 메시지 테이블로 대화
2. **Claude가 판단** — 완료 여부, 다음 단계 모두 Claude에게 위임
3. **폴링 기반** — 웹소켓 없이 단순 REST 폴링 (5초 간격)
4. **사용자 개입 가능** — 자율 실행 중에도 사용자가 직접 대화하면 매니저가 양보

---

## 파일 구조

```
scripts/daemon/manager/
├── thread.py          # 핵심 로직 — 사이클, 멀티턴, Deep Task
├── http_server.py     # 상태 모니터링 API (포트 7777)
└── __init__.py

scripts/daemon/
├── worker.py          # /do 명령 처리, 매니저 메시지 필터링
├── globals.py         # manager_wake_event, _manager_instance
└── supabase.py        # 메시지 주입/조회

~/.claude-daemon/
├── manager_state.json # 상태 영속화
└── workflows/         # 프로젝트별 워크플로우 정의
    └── disabled/      # 비활성화된 워크플로우
```
