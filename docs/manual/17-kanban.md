# 17. 브랜치 세션 & 칸반 보드

## 개요

프로젝트 하위에 **브랜치**를 만들어 독립된 AI 세션에서 작업을 수행하고, 선택적으로 **칸반 보드**로 상태를 관리하는 기능.

**핵심 구조:**
- **브랜치** = 프로젝트 하위의 독립 대화 세션 (범용 기본 단위)
- **칸반** = 브랜치에 상태(idea→dev→review→done)를 입히는 선택적 뷰/레이어

```
프로젝트 (메인 대화)
 ├── 브랜치 #1 "로그인 버그 수정"           ← 순수 브랜치 (active/archived)
 ├── 브랜치 #2 "결제 UI 리팩터" [dev]      ← 칸반 연결 브랜치
 └── 브랜치 #3 "이미지 리사이즈" [review]   ← 칸반 연결 브랜치
```

---

## 브랜치 세션

### 핵심 개념

브랜치는 프로젝트 메인 대화를 오염시키지 않으면서 특정 작업을 독립 세션에서 집중 처리하기 위한 단위.

**특징:**
- **독립 세션**: 각 브랜치는 고유한 Claude Code 세션(`session_id`)을 가짐
- **부모 맥락 계승**: 프로젝트 대화에서 분기 시 에이전트가 핵심 맥락을 자동 요약하여 전달 (`parent_context`)
- **프로젝트 리소스 공유**: 부모 프로젝트와 동일한 코드 디렉토리, 시스템 프롬프트, 환경변수 공유
- **2단계 상태**: `active` (진행 중) / `archived` (완료)

### 브랜치 데이터 구조

```sql
branches
├── id (PK, SERIAL)
├── user_id, project_id              -- 소속 프로젝트
├── branch_number                    -- 프로젝트 내 자동증가 번호
├── title, description               -- 작업 제목/설명
├── session_id                       -- Claude Code 세션 ID
├── session_started_at               -- 세션 시작 시각
├── parent_context                   -- 부모 대화 맥락 요약 (최대 2000자)
├── parent_message_id                -- 분기 기준 메시지 ID
├── status                           -- 'active' | 'archived'
├── sort_order
├── created_at, updated_at
└── UNIQUE (user_id, project_id, branch_number)
```

### 브랜치 만드는 4가지 방법

| 방법 | 설명 | parent_context |
|------|------|----------------|
| **에이전트에게 요청** | "이 작업 브랜치로 빼서 따로 하자" | 에이전트가 대화에서 자동 요약 (가장 풍부) |
| **사이드바 [+] 버튼** | 프로젝트 옆 [+] 클릭 → NewBranchModal | NULL (빈 브랜치) |
| **칸반 보드에서 카드 생성** | 기존 NewCardModal 플로우 | NULL (카드 description이 대체) |
| **메시지에서 분기** (예정) | 특정 메시지 시점에서 분기 | 서버가 haiku로 자동 요약 |

### 에이전트를 통한 브랜치 생성

부모 프로젝트 에이전트의 시스템 프롬프트(`_petervoice_system`)에 브랜치 생성 가이드가 포함되어 있음.

유저가 "브랜치로 빼자", "따로 작업하자", "이거 분리해서" 등을 말하면:

1. 에이전트가 대화에서 관련 핵심 맥락을 요약 (합의 사항, 기술적 결정, 참고 파일, 제약 조건)
2. `POST /api/branches` 호출
3. 유저에게 "브랜치 #{number} '{title}'을 만들었습니다. 사이드바에서 전환하세요." 안내

**parent_context 작성 규칙:**
- 마크다운 형식, ## 섹션으로 구분 (배경 / 합의 사항 / 제약)
- 2000자 이내
- 포함: 합의된 방향, 기술적 결정, 참고 파일/코드, 제약 조건
- 제외: 인사말, 진행 경과, 관련 없는 대화

### 사이드바 [+] 버튼

프로젝트명 우측에 표시. 클릭하면 NewBranchModal 열림:
- **제목** (필수)
- **설명** (선택)

생성 후 자동으로 해당 브랜치 대화(`branch:{id}`)로 전환.

### 사이드바 표시 규칙

| 조건 | 사이드바 표시 |
|------|-------------|
| 순수 브랜치 (active) | 🔀 #N 제목 |
| 순수 브랜치 (archived) | 숨김 |
| 칸반 연결 + dev 상태 | 🔀 #N 제목 [dev] |
| 칸반 연결 + dev 이외 (idea/review/done) | **숨김** |

**요점**: 칸반 연결 브랜치는 `dev` 상태일 때만 사이드바에 표시. 사이드바가 과도하게 복잡해지는 것을 방지.

### 메시지 라우팅

브랜치 메시지는 `messages.project` 컬럼에 `"branch:{branch_id}"` 형태로 저장.
기존 프로젝트 메시지(`"peter-voice"`)와 완전히 분리됨.

---

## 브랜치 에이전트 세션

### 프롬프트 구조 (4 레이어)

```
Layer 1: _petervoice_system    ← 피터보이스 시스템 가이드 (릴레이, 파일공유, 칸반 API 등)
Layer 2: _common               ← 유저별 공통 프롬프트 (역할, 규칙, 환경변수)
Layer 3: 프로젝트 프롬프트       ← 부모 프로젝트 고유 프롬프트 (기술 스택, 코딩 규칙)
Layer 4: 브랜치/카드 규칙        ← 브랜치 작업 규칙 (아래 참조)
```

**프로젝트 세션과의 차이:** 브랜치 세션은 `_petervoice_system`을 포함하므로, 기존 칸반 세션에서 누락되었던 릴레이, 파일 공유 등의 기능 안내가 모두 포함됨.

### 첫 메시지 주입 (새 세션 시작 시)

유저의 첫 메시지 앞에 prepend:

```
# 브랜치 #3: 이미지 리사이즈 기능 추가

작업 목표: Sharp 라이브러리로 이미지 리사이즈 구현

## 이 브랜치의 배경 (부모 프로젝트 대화에서 캡처)
## 배경
로그인 페이지에서 이미지 업로드 시 원본 그대로 저장 중.
(... parent_context 내용 ...)

위 맥락을 바탕으로 작업을 시작하세요.
맥락이 부족하면 먼저 질문하세요.
```

### 순수 브랜치 규칙

칸반 카드가 없는 브랜치의 에이전트 규칙:

```
- 이 브랜치의 작업에 집중하세요.
- 커밋 메시지 앞에 [branch-{id}]를 붙이세요.
- 작업 범위를 임의로 넓히지 마세요.
- 작업 종료 시: 변경 요약 보고 → 브랜치를 archived로 변경
```

### 칸반 연결 브랜치 규칙

기존 `build_kanban_prompt()` 규칙을 그대로 사용 (카드 상태 변경 API, 리뷰 절차 등 포함).
차이점: `session_id`는 `branches` 테이블에서 읽고, `_petervoice_system`이 포함됨.

### 프로젝트 세션 vs 브랜치 세션 비교

| 항목 | 프로젝트 세션 | 브랜치 세션 |
|------|--------------|-------------|
| 세션 키 | `"project:task"` | DB `branches.session_id` |
| 이전 세션 요약 | O (새 세션 시 주입) | X (parent_context가 대체) |
| `_petervoice_system` | O | **O** (기존 칸반에서는 누락) |
| `_common` 프롬프트 | O | O |
| 프로젝트 프롬프트 | O | O |
| 브랜치/카드 정보 | N/A | O (첫 메시지에 주입) |
| 작업 디렉토리 | 프로젝트 디렉토리 | 동일 (부모 프로젝트) |
| 컨텍스트 오버플로 자동 리셋 | O | **O** (run_claude 통합) |
| 세션 TTL 관리 | O | O (SessionHealthChecker) |

### 데몬 연동

**세션 키 포맷**: `"branch:{branch_id}:default"` (sessions.json)

**메시지 플로우:**
```
유저 메시지 → POST /api/messages (project="branch:{id}")
  → messages 테이블
  → 데몬 폴링 감지
  → branch 정보 조회 (GET /api/bot/branch?id=N)
  → 실제 project_id 확인 → project_dir 결정
  → build_branch_prompt() + build_branch_context()
  → Claude CLI 실행 (프로젝트 디렉토리에서)
  → 응답 → messages 테이블
  → Supabase Realtime → UI 갱신
```

**세션 생성 후:** `PATCH /api/bot/branch` 로 `session_id` + `session_started_at`을 DB에 저장.

---

## 칸반 보드

### 활성화

프로젝트 설정(ChatWindow 설정 모달)에서 "칸반 보드" 토글로 활성화.
활성화 시 자동으로 팀이 생성되고 현재 유저가 owner로 등록됨.

**접근 경로:**
- 채팅 UI 설정 드롭다운 → "칸반 보드" 링크
- 직접 URL: `/kanban?project={projectId}`

### 브랜치와의 관계

칸반 카드는 브랜치 위의 **관리 레이어**. 칸반 카드를 만들면 브랜치가 자동으로 함께 생성되고, `kanban_cards.branch_id`로 연결됨.

```
브랜치 #2 "결제 UI 리팩터" ─┐
                            ├─ 칸반 카드
                            │  ├── status: dev → review → done
                            │  ├── priority: high
                            │  ├── labels: [리팩터, 결제]
                            │  ├── acceptance_criteria
                            │  └── due_date
                            └──────────
```

**모든 칸반 카드 생성 시 브랜치가 자동 생성됨:**
- `POST /api/kanban` → 브랜치 생성 → 칸반 카드 생성 → `branch_id` 연결
- `POST /api/kanban/ideas` (에이전트 API) → 동일

### 상태 전이 규칙

```
idea ──→ dev ──→ review ──→ done ──→ archived
              ↕                ↓
           paused         dev (수정요청)
```

| From | To | 설명 |
|------|----|------|
| idea | dev | 승인 = 개발 시작, 브랜치 에이전트 세션 생성 |
| dev | paused | 일시중지 (세션 보존) |
| dev | review | 리뷰 요청 |
| paused | dev | 재개 |
| review | dev | 수정 요청 |
| review | done | 완료 승인 |
| done | archived | 아카이브 |

**금지**: done → dev (새 카드 생성 필요), archived → * (불변)

**사이드바 연동**: `dev` 상태의 칸반 카드에 연결된 브랜치만 사이드바에 active로 표시. idea/review/done 상태의 카드는 칸반 보드에서만 확인.

### 카드 데이터 구조

```sql
kanban_cards
├── id, owner_id, project_id, team_id
├── branch_id               -- FK → branches(id), 자동 생성 및 연결
├── title, description, acceptance_criteria
├── status                   -- idea/dev/paused/review/done/archived
├── priority                 -- urgent/high/normal/low
├── labels[]                 -- 자유 라벨
├── due_date
├── created_by, approved_by, assigned_to
├── session_id               -- (레거시, 브랜치의 session_id 사용 권장)
├── session_started_at, session_summary
├── result_commits[], result_prs[], result_notes
└── sort_order, created_at, updated_at
```

### 팀 & 접근 제어

칸반은 팀 기반 협업을 지원. 프로젝트 오너는 항상 전체 권한.

#### 역할 계층

| 역할 | 카드 조회 | 카드 생성 | 카드 수정 | 상태 변경 | 삭제 | 팀 관리 |
|------|----------|----------|----------|----------|------|---------|
| owner | O | O | O | O | O | O |
| admin | O | O | O | O | O | O |
| editor | O | O | O | O | X | X |
| viewer | O | O | X | X | X | X |

#### 팀 관리 UI (TeamMembersPanel)

칸반 보드 헤더의 Users 아이콘 → 팀 멤버 관리 모달

- **유저 검색**: 이메일/이름으로 검색 (2자 이상)
- **초대**: 역할 선택 후 초대 (admin/editor/viewer)
- **역할 변경**: 드롭다운으로 변경 (owner 제외)
- **멤버 제거**: 확인 후 삭제 (owner 제외)

#### 관련 테이블

```sql
teams (id, name, slug, owner_id, description)
team_members (team_id, user_id, role, created_at)
```

### 보드 UI (KanbanBoard)

#### 카드 드래그 앤 드롭 (순서 변경)

`@dnd-kit` 라이브러리로 카드를 드래그하여 재배치 가능:
- **같은 컬럼 내**: 카드 순서 변경 → `sort_order` DB 저장
- **다른 컬럼 간**: 카드를 끌어서 다른 컬럼에 드롭 → 상태 자동 변경 (상태 전이 규칙 적용)
- 터치 디바이스 지원

#### 컬럼별 정렬 모드

각 컬럼 헤더에 **정렬 버튼** 추가 — 컬럼별로 독립적인 정렬 기준 선택 가능:

| 모드 | 설명 |
|------|------|
| 수동 | 드래그 순서 유지 (기본) |
| 최신순 | 최근 생성된 카드 먼저 |
| 오래된순 | 오래된 카드 먼저 |
| 우선순위순 | urgent → high → normal → low |

#### 컬럼 구성

| 컬럼 | 상태 | 색상 |
|------|------|------|
| 아이디어 | idea | 회색 |
| 개발 중 | dev | 파란색 |
| 리뷰 | review | 노란색 |
| 완료 | done | 초록색 |
| 아카이브 | archived | 회색 (토글로 표시/숨김) |

#### 헤더 버튼

| 아이콘 | 기능 |
|--------|------|
| BarChart3 | 통계 바 토글 |
| Filter | 필터 바 토글 |
| Archive | 아카이브 컬럼 토글 |
| Users | 팀 멤버 관리 (team_id 있을 때만) |
| RefreshCw | 수동 새로고침 |
| Plus | 새 카드 생성 |

#### 필터 & 검색

- **텍스트 검색**: 카드 제목/설명에서 검색
- **우선순위 필터**: 전체/긴급/높음/보통/낮음
- **라벨 필터**: 현재 카드들의 라벨 목록에서 선택
- **초기화**: X 버튼으로 모든 필터 해제

#### 통계 바 (KanbanStats)

활성화 시 보드 상단에 표시:
- 전체 카드 수, 상태별 카운트
- 평균 리드타임 (done 카드의 생성~완료 기간, 일 단위)
- 주간 처리량 (최근 7일 완료 카드 수)

#### Supabase Realtime

`kanban_cards` 테이블의 변경을 구독. 다른 사용자가 카드를 변경하면 자동 새로고침.

#### 칸반→채팅 직접 이동 버튼

`dev`, `review`, `done` 상태 카드에 **말풍선 아이콘(💬)** 표시. 클릭 시 `/chat?project=branch:{branch_id}` 로 이동하여 해당 브랜치 대화로 직접 진입. URL 파라미터 기반 프로젝트 전환이 자동 동기화됨.

#### 자동 코드 리뷰 시스템

`dev → review` 상태 전환 시 자동으로 코드 리뷰가 진행됨:

1. `kanban.py`에서 review 상태 전환 감지
2. 개발 완료 보고 작성 (변경 파일, 커밋 등)
3. `code-reviewer` 프로젝트에 릴레이로 자동 의뢰
4. code-reviewer가 6개 기준(보안, 아키텍처, 테스트 등)으로 리뷰 수행
5. 리뷰 결과가 칸반 카드에 자동 반영

#### 리뷰 결과 뱃지

review 컬럼의 카드에 리뷰 결과가 뱃지로 표시됨:

| 뱃지 | 의미 |
|------|------|
| ✅ 리뷰 통과 | 문제 없음 |
| ⚠️ 조건부 통과 | 경미한 이슈 있음 |
| ❌ 수정 필요 | 심각한 이슈 발견 |
| 🔄 리뷰 중... | 리뷰 진행 중 |

### 카드 상세 패널 (CardDetailPanel)

카드 클릭 시 모달 형태로 표시. 3개 탭:

#### Info 탭
- 설명, 수락 기준 (전체 텍스트)
- 우선순위, 마감일, 라벨
- 생성 일시

#### Chat 탭 (에이전트 대화)
- dev/paused 상태에서만 메시지 전송 가능
- 채팅 버튼 클릭 시 `branch:{branch_id}` 대화로 이동
- 메시지 타입: user, bot, system
- 시스템 메시지: 상태 변경 알림 등

#### Comments 탭 (팀 토론)
- 모든 역할에서 댓글 작성 가능
- 사용자명 + 작성 시각 표시

### 새 카드 생성 (NewCardModal)

- **제목** (필수)
- **설명** (선택)
- **수락 기준** (선택) — 완료 조건
- **우선순위** (low/normal/high/urgent)
- **마감일** (date picker)
- **라벨** (자유 입력, Enter로 추가)

카드 생성 시 **브랜치가 자동으로 함께 생성**되고, `kanban_cards.branch_id`로 연결됨.

---

## API 엔드포인트

### 브랜치

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | /api/branches?project_id=X | 프로젝트별 브랜치 목록 |
| GET | /api/branches?all_active=1 | 전체 active 브랜치 (사이드바용, 칸반 dev만 포함) |
| POST | /api/branches | 브랜치 생성 (kanban 옵션으로 칸반 카드 동시 생성 가능) |
| GET | /api/branches/[id] | 브랜치 상세 |
| PATCH | /api/branches/[id] | 브랜치 수정 (title, description, status 등) |
| DELETE | /api/branches/[id] | 브랜치 삭제 |
| POST | /api/branches/[id]/kanban | 기존 브랜치에 칸반 카드 연결 |

### 데몬용 브랜치 API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | /api/bot/branch?id=N | 브랜치 + 칸반 카드 정보 조회 |
| PATCH | /api/bot/branch | session_id 업데이트 (session_started_at 자동 설정) |

### 칸반 카드

| 메서드 | 경로 | 설명 | 권한 |
|--------|------|------|------|
| GET | /api/kanban?project=X | 보드 조회 | viewer+ |
| POST | /api/kanban | 카드 생성 (**브랜치 자동 생성**) | viewer+ |
| PATCH | /api/kanban/[id] | 카드 수정 | editor+ |
| DELETE | /api/kanban/[id] | 카드 삭제 | admin+ |
| PATCH | /api/kanban/[id]/status | 상태 변경 (전이 규칙 검증) | editor+ |
| POST | /api/kanban/ideas | 에이전트용 아이디어 등록 (**브랜치 자동 생성**) | 인증 |

### 댓글 & 메시지

| 메서드 | 경로 | 설명 | 권한 |
|--------|------|------|------|
| GET | /api/kanban/[id]/comments | 댓글 조회 | viewer+ |
| POST | /api/kanban/[id]/comments | 댓글 작성 | viewer+ |
| GET | /api/kanban/[id]/messages | 에이전트 대화 조회 | viewer+ |
| POST | /api/kanban/[id]/messages | 에이전트에게 메시지 | editor+ |

### 팀

| 메서드 | 경로 | 설명 | 권한 |
|--------|------|------|------|
| GET | /api/teams | 내 팀 목록 | 인증 |
| POST | /api/teams | 팀 생성 | 인증 |
| PATCH | /api/teams/[id] | 팀 수정 | owner |
| DELETE | /api/teams/[id] | 팀 삭제 | owner |
| GET | /api/teams/[id]/members | 멤버 목록 | viewer+ |
| POST | /api/teams/[id]/members | 멤버 추가 | admin+ |
| PATCH | /api/teams/[id]/members/[uid] | 역할 변경 | admin+ |
| DELETE | /api/teams/[id]/members/[uid] | 멤버 제거 | admin+ |
| GET | /api/users/search?q=X | 유저 검색 | 인증 |

---

## 컴포넌트 구조

```
components/
├── NewBranchModal.tsx        — 브랜치 생성 모달 (사이드바 [+] 버튼)
└── kanban/
    ├── KanbanBoard.tsx       — 메인 보드 (필터, Realtime, 컬럼 배치)
    ├── KanbanColumn.tsx      — 단일 컬럼 (카드 리스트)
    ├── KanbanCard.tsx        — 카드 (우선순위, 라벨, 액션 버튼)
    ├── CardDetailPanel.tsx   — 카드 상세 모달 (Info/Chat/Comments)
    ├── NewCardModal.tsx      — 카드 생성 (라벨, 마감일 포함)
    ├── TeamMembersPanel.tsx  — 팀 멤버 관리 (검색, 초대, 역할)
    └── KanbanStats.tsx       — 통계 바 (리드타임, 처리량)
```

## 관련 lib 모듈

- `lib/branches.ts` — 브랜치 CRUD, 칸반 카드 조인, 사이드바용 필터링
- `lib/kanban.ts` — 카드 CRUD, 상태 전이 검증, 접근 제어
- `lib/teams.ts` — 팀 CRUD, 역할 체크, 유저 검색
- `scripts/daemon/branches.py` — 데몬 브랜치 지원 (fetch_branch, update_branch_session, build_branch_prompt, build_branch_context)
- `scripts/daemon/claude_runner.py` — `branch:` 프로젝트 감지 및 브랜치 세션 처리

---

## 마이그레이션 (기존 칸반 → 브랜치)

브랜치 도입 시 기존 칸반 카드 데이터가 자동 마이그레이션됨:

1. 기존 `kanban_cards` → `branches` 테이블에 1:1 대응 레코드 생성
2. `kanban_cards.branch_id` 설정
3. `messages.project` 컬럼의 `"kanban:{card_id}"` → `"branch:{branch_id}"`로 변환
4. 데몬의 `sessions.json` 키도 `"kanban:{card_id}:default"` → `"branch:{branch_id}:default"`로 변환 필요

마이그레이션 SQL: `supabase/migrations/20260405100000_branches.sql`
