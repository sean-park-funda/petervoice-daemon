# 09. 일정 및 할일

## 구성 요소

| 기능 | 데이터 소스 | 컴포넌트 |
|------|-------------|----------|
| 일정 (캘린더) | Google Calendar API | TodoCalendarSidebar |
| 할일 | Supabase todos 테이블 | TodoCalendarSidebar |
| 오늘의 목표 | Supabase daily_goals 테이블 | TodoCalendarSidebar |

## 일정 — Google 캘린더

### 연동 흐름

```
1. 유저가 Google OAuth 연결 (설정 → Google 연결)
2. oauth_tokens에 refresh_token 저장
3. 웹 UI 사이드바 → GET /api/calendar
4. refresh_token으로 access_token 갱신
5. Google Calendar API에서 7일간 일정 조회
6. 사이드바에 일정 표시
```

### API

```
GET /api/calendar
→ { events: [{ summary, start, end, location, description }] }
```

- 조회 범위: 오늘 ~ 7일 후
- Google OAuth 미연결 시: 빈 배열 반환
- 5분마다 자동 갱신

### UI 표시

사이드바 상단에 미니 캘린더 + 선택 날짜의 일정 목록

## 할일 — Todos

### 데이터 구조

```sql
todos
├── id (UUID)
├── user_id
├── project_id    — 프로젝트 분류
├── parent_id     — 서브태스크 (자기참조)
├── title         — 할일 제목
├── memo          — 메모
├── status        — 'todo' | 'in_progress' | 'done'
├── priority      — 0(낮음), 1(보통), 2(높음)
├── due_date      — 마감일
├── sort_order    — 정렬 순서
└── completed_at  — 완료 시각
```

### API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | /api/todos?project=xxx&done=true | 조회 (프로젝트 필터, 완료 포함) |
| POST | /api/todos | 생성 |
| PATCH | /api/todos | 수정 (상태 변경, 제목 수정 등) |
| DELETE | /api/todos | 삭제 |

### 서브태스크

```
메인 할일 (parent_id = null)
├── 서브태스크 1 (parent_id = 메인할일ID)
├── 서브태스크 2
└── 서브태스크 3
```

- 메인 할일에서 서브태스크 추가/완료 가능
- 일부 서브태스크만 완료한 경우 진행률 표시

### UI 기능

- **프로젝트 필터**: 드롭다운으로 프로젝트별 필터링
- **우선순위 표시**: 🔴 높음, 🟡 보통, ⚪ 낮음
- **프로젝트 뱃지**: 프로젝트별 색상 뱃지
- **액션 아이콘** (항상 표시):
  - ✅ 완료 토글
  - ✏️ 수정
  - 🗑 삭제
- **할일 추가**: 제목, 프로젝트 선택 가능
- **완료된 할일 보기**: 하단 토글 → 최신순 표시

### PC 레이아웃

사이드바 폭: 기본의 2배 (넓게)
- 좌측 30%: 미니 캘린더
- 우측 70%: 할일 목록 + 오늘의 목표

## 오늘의 목표 — Daily Goals

### 개념

할일 목록에서 오늘 집중할 항목을 선택하여 별도 표시.

### 데이터 구조

```sql
daily_goals
├── id (UUID)
├── user_id
├── date        — 날짜 (기본: 오늘)
├── todo_id     — 연결된 할일 (FK → todos)
├── sort_order  — 순서
├── completed   — 완료 여부
└── UNIQUE (user_id, date, todo_id)
```

### 동작

```
할일 목록에서 "오늘의 목표"로 지정
  → daily_goals에 추가 (date=today)
  → 할일 목록에서는 숨김
  → 오늘의 목표 섹션에서 표시

다음날 아침:
  → 전날의 목표 중 미완료 → 자동으로 할일로 복귀
  → 완료된 항목은 그대로 유지
```

### API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | /api/daily-goals?date=2026-03-22 | 날짜별 목표 조회 |
| POST | /api/daily-goals | 목표 추가 |
| PATCH | /api/daily-goals | 완료 토글, 순서 변경 |
| DELETE | /api/daily-goals | 목표에서 제외 (할일로 복귀) |

### UI 표시

- 오늘의 목표 섹션에서도 서브태스크 표시
- 프로젝트 뱃지 표시
- ❌ 버튼: 목표에서 제외 → 할일로 복귀

## 대화로 할일 관리

Claude 봇과 대화하면서 할일을 관리할 수 있음:
- "이거 할일에 추가해줘" → POST /api/todos
- "오늘 할 일 보여줘" → GET /api/daily-goals
- "이 할일 완료 처리해줘" → PATCH /api/todos
