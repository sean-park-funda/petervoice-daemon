# 12. 하트비트 (자율 반복 작업)

## 개요

프로젝트별로 반복 작업을 등록하면 데몬이 정해진 인터벌마다 해당 프로젝트의 Claude 봇에게 "할 일 확인"을 지시하는 시스템.
데몬은 타이머 역할만 하고, 작업 판단과 실행은 Claude 봇이 담당.

## 아키텍처

```
유저: "자율적으로 돌려"
  ↓ Claude 봇
1. docs/HEARTBEAT.md 작성 (작업 체크리스트)
2. POST /api/tasks (태스크 등록)
  ↓
HeartbeatThread (데몬, 매 60초 체크)
  ↓ due 태스크 발견
"[heartbeat] HEARTBEAT.md를 확인하고 할 일이 있으면 처리해줘"
  → messages 테이블에 주입 (inject_system_message)
  ↓ Worker가 폴링
Claude Code CLI 실행
  ↓ HEARTBEAT.md 읽고 다음 미완료 항목 처리
  ↓ 완료 시 [x] 마킹
  ↓ 전부 완료 → 태스크 status='done'으로 변경
```

## DB 스키마 (tasks 테이블)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | uuid | PK |
| user_id | integer | 소유자 |
| project | varchar(50) | 프로젝트 ID |
| interval_min | integer (default 30) | 실행 간격 (분) |
| status | varchar(20) | `active` / `paused` / `done` |
| active_hours | text | `"09:00-23:00"` 또는 NULL |
| next_run_at | timestamptz | 다음 실행 시각 |
| max_runs | integer | 최대 실행 횟수 (NULL = 무제한) |
| run_count | integer (default 0) | 현재까지 실행 횟수 |
| created_at | timestamptz | 생성 시각 |

인덱스:
- `idx_tasks_heartbeat`: (user_id, status, next_run_at) — 폴링 최적화
- `idx_tasks_project_active`: (user_id, project) WHERE status='active' — 프로젝트당 1개 제한

## API

### 사용자 API: `/api/tasks`

인증: 쿠키 또는 API 키

| 메서드 | Body | 설명 |
|--------|------|------|
| GET | ?project=xxx | 태스크 조회 |
| POST | `{project, interval_min?, max_runs?, active_hours?}` | 태스크 생성 (201) |
| PATCH | `{id, status?, interval_min?, max_runs?, active_hours?, next_run_at?}` | 태스크 수정 |
| DELETE | `{id}` | 태스크 삭제 |

에러:
- 400: project 필수값 누락
- 409: 동일 프로젝트에 active 태스크 이미 존재

### 데몬 API: `/api/bot/tasks`

인증: API 키 (verifyApiKey)

| 메서드 | 설명 |
|--------|------|
| GET | due 태스크 조회 (status=active, next_run_at <= now) |
| PATCH | 태스크 갱신 (next_run_at, run_count 등) |

### 프로젝트당 1개 제한

동일 프로젝트에 `status='active'` 태스크는 1개만 허용 (DB UNIQUE 인덱스).

## HeartbeatThread (데몬)

파일: `peter-voice-daemon/scripts/daemon/heartbeat.py`

### 동작 주기

매 60초마다:
1. `status='active'` AND `next_run_at <= now()` 태스크 조회
2. 각 태스크에 대해:
   - **활성 시간 확인**: active_hours 범위 밖이면 스킵
   - **최대 실행 확인**: run_count >= max_runs이면 `status='done'`
   - **프로젝트 busy 확인**: 이미 작업 중이면 5분 연기
   - **메시지 주입**: `inject_system_message(project, msg, prefix="[heartbeat]")`
   - **스케줄 갱신**: `next_run_at = now + interval_min`
   - **카운터 증가**: `run_count += 1`

### 메시지 주입 (inject_system_message)

```python
POST /api/bot/message
Body: {
    "project": project,
    "text": "[heartbeat] HEARTBEAT.md를 확인하고 할 일이 있으면 처리해줘. 없으면 '할 일 없음'이라고 답해.",
    "type": "user",
    "processed": False
}
```

Worker가 다음 폴링에서 이 메시지를 감지하여 Claude Code CLI 실행.

### 활성 시간 (active_hours)

포맷: `"HH:MM-HH:MM"`

```
"09:00-23:00"  → 오전 9시 ~ 밤 11시
"22:00-06:00"  → 밤 10시 ~ 오전 6시 (야간 작업, 자정 넘김 지원)
```

NULL이면 24시간 활성.

## HEARTBEAT.md

프로젝트 `docs/HEARTBEAT.md`에 작업 체크리스트 작성:

```markdown
# 반복 작업

- [x] 데이터 수집 #1
- [x] 분석 실행 #2
- [ ] 결과 리포트 생성
- [ ] 알림 발송
```

### Claude 봇의 처리

1. `[heartbeat]` 접두어 메시지 수신
2. `docs/HEARTBEAT.md` 읽기
3. 다음 미완료(`[ ]`) 항목 처리
4. 완료 시 `[x]`로 마킹
5. 전부 완료 시:
   - HEARTBEAT.md 정리 또는 비우기
   - `PATCH /api/tasks/{id} {"status": "done"}`

## 사용 시나리오

### 자율 모니터링

```
유저: "서버 상태를 30분마다 확인해줘"
  → HEARTBEAT.md:
    - [ ] CPU/메모리 확인
    - [ ] 에러 로그 확인
    - [ ] 비정상이면 알림
  → 태스크: interval=30, max_runs=48 (24시간)
```

### 배치 작업

```
유저: "이 데이터를 하나씩 처리해줘"
  → HEARTBEAT.md:
    - [ ] 항목 1 처리
    - [ ] 항목 2 처리
    - [ ] 항목 3 처리
  → 태스크: interval=30, max_runs=10
```

### 일회성 예약 작업

```
유저: "새벽 3시에 배치 돌려줘"
  → HEARTBEAT.md: 작업 내용
  → 태스크: interval=30, max_runs=1, active_hours="03:00-03:30"
```

## 비용 관리

| 제어 항목 | 방법 |
|-----------|------|
| interval_min | 최소 30분 (짧을수록 비용 급증) |
| max_runs | 설정 필수 (무한 루프 방지) |
| active_hours | 불필요 시간 제외 |
| HEARTBEAT.md 비우기 | 봇이 "할 일 없음" 즉시 종료 → 짧은 토큰 소비 |

예상 비용 (1회 heartbeat):
- Opus: ~$0.225 (5K input + 2K output 기준)
- Sonnet: ~$0.045

## _common 프롬프트 가이드

```
## HeartBeat (자율 반복 작업)

유저가 "자율적으로 돌려" 등 요청 시:
1. docs/HEARTBEAT.md에 체크리스트 작성
2. POST /api/tasks로 태스크 등록 (interval_min 최소 30, max_runs 필수)
3. 유저에게 인터벌, max_runs 확인

[heartbeat] 메시지 수신 시:
1. docs/HEARTBEAT.md 읽기
2. 다음 미완료 항목 처리
3. 완료 마킹
4. 전부 완료 → status='done' 변경
```

## 구현 상태

### Phase 1 (완료)
- tasks 테이블 및 인덱스
- /api/tasks, /api/bot/tasks REST API
- HeartbeatThread 구현 및 데몬 통합
- inject_system_message 함수
- 활성 시간 필터링 (야간 범위 포함)
- max_runs 자동 완료
- _common 프롬프트 안내

### Phase 2 (미구현)
- 지수 백오프 재시도
- lightContext 패턴 (토큰 절약)
- 웹 UI 태스크 관리 페이지
