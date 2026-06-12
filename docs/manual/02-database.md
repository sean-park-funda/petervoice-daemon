# 02. 데이터베이스 스키마

Supabase (PostgreSQL) 기반. 총 30개+ 테이블.

## 테이블 목록

| # | 테이블 | 용도 | RLS |
|---|--------|------|-----|
| 1 | users | 사용자 계정 | - |
| 2 | messages | 채팅 메시지 | - |
| 3 | user_status | 데몬/봇 실시간 상태 | - |
| 4 | projects | 프로젝트 설정 | - |
| 5 | prompts | 프로젝트별 시스템 프롬프트 | - |
| 6 | secrets | 유저별 환경변수 | - |
| 7 | oauth_tokens | OAuth 인증 토큰 | ✅ |
| 8 | project_views | 프로젝트 마지막 조회 시각 | - |
| 9 | log_cache | 데몬 로그 캐시 | - |
| 10 | watchdog_status | 데몬 헬스 모니터링 | - |
| 11 | watchdog_commands | 데몬 명령 큐 | - |
| 12 | direct_claude_messages | Direct Claude 대화 이력 | - |
| 13 | direct_claude_sessions | Direct Claude 세션 | - |
| 14 | documents | 계층형 문서/폴더 | - |
| 15 | todos | 할일 (서브태스크 지원) | - |
| 16 | daily_goals | 오늘의 목표 | - |
| 17 | skills | Claude Code 스킬 | ✅ |
| 18 | tasks | 하트비트 반복 작업 | ✅ |
| 19 | teams | 팀 (칸반 협업) | ✅ |
| 20 | team_members | 팀 멤버 + 역할 | ✅ |
| 21 | kanban_cards | 칸반 카드 | ✅ |
| 22 | kanban_comments | 카드 댓글 | ✅ |
| 23 | kanban_messages | 카드별 에이전트 대화 | ✅ |
| 24 | branches | 프로젝트 하위 브랜치 세션 | - |
| 25 | summon_sessions | 에이전트 소환 세션 | - |
| 26 | summon_messages | 소환 라운드별 메시지 | - |
| 27 | project_repos | 프로젝트별 Git 리포 경로 | - |
| 28 | code_reviews | 코드 리뷰 세션 | - |
| 29 | review_threads | 리뷰 스레드 (파일/라인) | - |
| 30 | review_thread_messages | 스레드 내 메시지 | - |
| 31 | onboarding_queue | 고객 온보딩 대기열 | - |
| 32 | onboarding_ssh_keys | 온보딩 SSH 키 | - |

## 상세 스키마

### 1. users — 사용자 계정

```sql
CREATE TABLE users (
  id          SERIAL PRIMARY KEY,
  username    VARCHAR(50) UNIQUE NOT NULL,
  password    VARCHAR(255) NOT NULL,  -- bcrypt 해시
  role        VARCHAR(20) DEFAULT 'user',  -- 'admin' | 'user'
  api_key     VARCHAR(255) UNIQUE,  -- 데몬 인증용
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);
```

### 2. messages — 채팅 메시지

```sql
CREATE TABLE messages (
  id          SERIAL PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  project     VARCHAR(50) DEFAULT 'general',
  type        TEXT NOT NULL,  -- 'user' | 'bot'
  text        TEXT NOT NULL,
  files       JSONB DEFAULT '[]',  -- [{name, url, size, type}]
  processed   BOOLEAN DEFAULT FALSE,
  reply_to    INTEGER[],  -- 응답 대상 메시지 ID 배열
  subtype     TEXT,  -- 'tool_log' 등
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW(),
  fetched_at  TIMESTAMPTZ  -- 데몬이 가져간 시각
);

-- 인덱스
CREATE INDEX idx_messages_pending ON messages(user_id, processed, type, project)
  WHERE processed = FALSE AND type = 'user';
CREATE INDEX idx_messages_user_project ON messages(user_id, project, created_at DESC);
```

**핵심 동작:**
- 사용자 메시지: `type='user'`, `processed=false` → 데몬이 poll → 처리 후 `processed=true`
- 봇 응답: `type='bot'`, `reply_to=[원본_메시지_id]`
- 툴 로그: `type='bot'`, `subtype='tool_log'` → UI에서 접어서 표시

### 3. user_status — 데몬/봇 실시간 상태 (Realtime 활성)

```sql
CREATE TABLE user_status (
  user_id          INTEGER PRIMARY KEY REFERENCES users(id),
  is_working       BOOLEAN DEFAULT FALSE,
  current_task     TEXT,
  message_ids      INTEGER[],
  started_at       TIMESTAMPTZ,
  last_heartbeat   TIMESTAMPTZ,
  context_usage    JSONB,  -- {inputTokens, totalTokens, contextTokens, model}
  streaming_text   TEXT,   -- JSON: 프로젝트별 스트리밍 텍스트
  active_project   VARCHAR(100),
  project_started_at TIMESTAMPTZ,
  streaming_project VARCHAR(100),
  force_restart    BOOLEAN DEFAULT FALSE,
  stop_requested   BOOLEAN DEFAULT FALSE,
  stop_requested_at TIMESTAMPTZ
);
```

**Realtime 구독**: 프론트엔드가 `streaming_text`, `is_working` 변경을 실시간 수신

### 4. projects — 프로젝트 설정

```sql
CREATE TABLE projects (
  user_id        INTEGER NOT NULL REFERENCES users(id),
  id             VARCHAR(50) NOT NULL,
  name           VARCHAR NOT NULL,
  context        TEXT,
  sort_order     INTEGER DEFAULT 0,
  directory      VARCHAR(255),  -- 로컬 디렉토리 경로
  deploy_url     TEXT,          -- Vercel 배포 URL
  chrome         BOOLEAN DEFAULT FALSE,  -- 브라우저 자동화 사용
  account        TEXT,          -- Claude 계정 (멀티 계정 지원)
  model          TEXT,          -- Claude 모델 오버라이드
  session_summary TEXT,         -- 세션 리셋 시 컨텍스트 요약
  parent_project TEXT,          -- 브랜치된 프로젝트의 부모
  created_at     TIMESTAMPTZ DEFAULT NOW(),
  updated_at     TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, id)
);
```

### 5. secrets — 유저별 환경변수

```sql
CREATE TABLE secrets (
  id         SERIAL PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  key        VARCHAR(100) NOT NULL,
  value      TEXT NOT NULL,
  category   VARCHAR(50) DEFAULT 'custom',  -- 'google', 'notion', 'api', 'custom'
  memo       VARCHAR(200),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, key)
);
```

### 6. oauth_tokens — OAuth 인증 토큰 (RLS 적용)

```sql
CREATE TABLE oauth_tokens (
  user_id       INTEGER NOT NULL REFERENCES users(id),
  provider      TEXT NOT NULL,  -- 'google' | 'notion'
  access_token  TEXT,
  refresh_token TEXT,           -- Google만
  scope         TEXT,
  extra         JSONB,          -- {email, workspace_name, bot_id 등}
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, provider)
);
-- RLS: 유저 본인 토큰만 접근 가능
```

### 7. documents — 계층형 문서/폴더 (Realtime 활성)

```sql
CREATE TABLE documents (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    INTEGER NOT NULL REFERENCES users(id),
  project_id VARCHAR(50),
  parent_id  UUID REFERENCES documents(id),  -- 폴더 계층
  title      TEXT NOT NULL,
  content    TEXT DEFAULT '',
  type       VARCHAR(10) DEFAULT 'doc',  -- 'doc' | 'folder'
  sort_order INTEGER DEFAULT 0,
  pinned     BOOLEAN DEFAULT FALSE,
  file_path  TEXT,  -- 데몬 싱크용 (로컬 경로)
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 유니크 인덱스: 프로젝트 내 파일 경로 중복 방지
CREATE UNIQUE INDEX idx_documents_user_project_filepath
  ON documents(user_id, project_id, file_path)
  WHERE file_path IS NOT NULL;
```

### 8. todos — 할일 (서브태스크 지원)

```sql
CREATE TABLE todos (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      INTEGER NOT NULL REFERENCES users(id),
  project_id   VARCHAR(50),
  parent_id    UUID REFERENCES todos(id),  -- 서브태스크
  title        TEXT NOT NULL,
  memo         TEXT DEFAULT '',
  status       VARCHAR(20) DEFAULT 'todo',  -- 'todo' | 'in_progress' | 'done'
  priority     INTEGER DEFAULT 1,  -- 0=낮음, 1=보통, 2=높음
  due_date     DATE,
  sort_order   INTEGER DEFAULT 0,
  completed_at TIMESTAMPTZ,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  updated_at   TIMESTAMPTZ DEFAULT NOW()
);
```

### 9. daily_goals — 오늘의 목표

```sql
CREATE TABLE daily_goals (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    INTEGER NOT NULL REFERENCES users(id),
  date       DATE DEFAULT CURRENT_DATE,
  todo_id    UUID NOT NULL REFERENCES todos(id),
  sort_order INTEGER DEFAULT 0,
  completed  BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, date, todo_id)
);
```

### 10. tasks — 하트비트 반복 작업 (RLS 적용)

```sql
CREATE TABLE tasks (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      INTEGER NOT NULL REFERENCES users(id),
  project      VARCHAR(50) NOT NULL,
  interval_min INTEGER DEFAULT 30,
  status       VARCHAR(20) DEFAULT 'active',  -- 'active' | 'paused' | 'done'
  active_hours TEXT,  -- "09:00-23:00" 형식
  next_run_at  TIMESTAMPTZ DEFAULT NOW(),
  max_runs     INTEGER,
  run_count    INTEGER DEFAULT 0,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- 프로젝트당 활성 태스크 1개 제한
CREATE UNIQUE INDEX idx_tasks_project_active
  ON tasks(user_id, project) WHERE status = 'active';
```

### 11. skills — Claude Code 스킬 (RLS 적용)

```sql
CREATE TABLE skills (
  id          TEXT PRIMARY KEY,  -- 예: "branch-project"
  content     TEXT NOT NULL,     -- SKILL.md 내용
  description TEXT,
  enabled     BOOLEAN DEFAULT TRUE,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);
-- RLS: 공개 읽기, 인증된 사용자만 쓰기
```

### 기타 테이블

- **project_views** `(user_id, project, last_viewed_at)` — unread 추적용
- **log_cache** `(user_id, lines[], log_file)` — 데몬 로그 캐시 (최근 200줄)
- **watchdog_status** `(user_id, gateway, system, context, ts)` — 데몬 헬스
- **watchdog_commands** `(user_id, command, payload, status, result)` — 데몬 명령 큐
- **direct_claude_messages** `(user_id, project, role, text)` — Direct Claude 대화
- **direct_claude_sessions** `(user_id, project, session_id, pending_command_id)` — 세션 관리

### 20. teams — 팀 (칸반 협업)

```sql
CREATE TABLE teams (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  slug        TEXT NOT NULL UNIQUE,
  owner_id    INTEGER NOT NULL REFERENCES users(id),
  description TEXT DEFAULT '',
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);
```

### 21. team_members — 팀 멤버

```sql
CREATE TABLE team_members (
  team_id    INTEGER REFERENCES teams(id) ON DELETE CASCADE,
  user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK (role IN ('owner','admin','editor','viewer')),
  created_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (team_id, user_id)
);
```

### 22. kanban_cards — 칸반 카드

```sql
CREATE TABLE kanban_cards (
  id                   SERIAL PRIMARY KEY,
  owner_id             INTEGER NOT NULL REFERENCES users(id),
  project_id           TEXT NOT NULL,
  team_id              INTEGER REFERENCES teams(id),
  title                TEXT NOT NULL,
  description          TEXT DEFAULT '',
  acceptance_criteria  TEXT DEFAULT '',
  status               TEXT NOT NULL DEFAULT 'idea'
    CHECK (status IN ('idea','dev','paused','review','done','archived')),
  created_by           INTEGER REFERENCES users(id),
  approved_by          INTEGER,
  assigned_to          INTEGER,
  session_id           TEXT,
  session_started_at   TIMESTAMPTZ,
  session_summary      TEXT,
  result_commits       TEXT[] DEFAULT '{}',
  result_prs           TEXT[] DEFAULT '{}',
  result_notes         TEXT DEFAULT '',
  priority             TEXT DEFAULT 'normal'
    CHECK (priority IN ('urgent','high','normal','low')),
  labels               TEXT[] DEFAULT '{}',
  due_date             DATE,
  sort_order           INTEGER DEFAULT 0,
  created_at           TIMESTAMPTZ DEFAULT now(),
  updated_at           TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_kanban_cards_project ON kanban_cards(project_id, status);
```

### 23. kanban_comments — 카드 댓글

```sql
CREATE TABLE kanban_comments (
  id         SERIAL PRIMARY KEY,
  card_id    INTEGER NOT NULL REFERENCES kanban_cards(id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  text       TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

### 24. kanban_messages — 카드별 에이전트 대화

```sql
CREATE TABLE kanban_messages (
  id          SERIAL PRIMARY KEY,
  card_id     INTEGER NOT NULL REFERENCES kanban_cards(id) ON DELETE CASCADE,
  sender_id   INTEGER REFERENCES users(id),
  type        TEXT NOT NULL CHECK (type IN ('user','bot','system')),
  text        TEXT NOT NULL,
  files       JSONB DEFAULT '[]',
  processed   BOOLEAN DEFAULT false,
  sender_name TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_kanban_messages_card ON kanban_messages(card_id, created_at);
CREATE INDEX idx_kanban_messages_pending ON kanban_messages(processed) WHERE processed = false;
```

**projects 테이블 칸반 확장 컬럼:**
```sql
ALTER TABLE projects ADD COLUMN kanban_enabled BOOLEAN DEFAULT false;
ALTER TABLE projects ADD COLUMN team_id INTEGER REFERENCES teams(id);
ALTER TABLE projects ADD COLUMN max_concurrent_kanban INTEGER DEFAULT 3;
```

## 관계도

```
users (1) ──┬── (N) messages
            ├── (N) projects ──── (N) branches
            ├── (N) secrets
            ├── (N) oauth_tokens
            ├── (N) documents ──── (N) documents (parent_id, 자기참조)
            ├── (N) todos ──────── (N) todos (parent_id, 서브태스크)
            ├── (N) daily_goals ── (1) todos
            ├── (N) tasks
            ├── (1) user_status
            ├── (1) log_cache
            └── (1) watchdog_status

teams (1) ──┬── (N) team_members ── (1) users
            └── (N) kanban_cards ──┬── (N) kanban_comments
                                   └── (N) kanban_messages

projects ── (0..1) teams (team_id FK)
         ── (N) project_repos ── (N) code_reviews ── (N) review_threads ── (N) review_thread_messages
         ── (N) summon_sessions ── (N) summon_messages
```

## 마이그레이션 관리

```bash
# 새 마이그레이션 작성
cat > supabase/migrations/$(date +%Y%m%d%H%M%S)_description.sql << 'EOF'
ALTER TABLE foo ADD COLUMN IF NOT EXISTS bar text;
EOF

# 적용
supabase db push --linked

# 스키마 확인
supabase db dump --linked 2>/dev/null | grep -A10 'CREATE TABLE foo'
```

## lib/db.ts 함수 목록

| 카테고리 | 함수 | 설명 |
|----------|------|------|
| **메시지** | getMessages() | 메시지 조회 (시간 범위, 프로젝트 필터) |
| | createMessage() | 메시지 생성 |
| | markMessageProcessed() | 처리 완료 표시 |
| | getPendingMessages() | 미처리 메시지 조회 |
| | markMessagesFetched() | 데몬 fetch 시각 기록 |
| **상태** | getUserStatus() | 봇 상태 조회 |
| | updateUserStatus() | 상태 업데이트 |
| | updateHeartbeat() | 하트비트 갱신 |
| | updateStreamingText() | 스트리밍 텍스트 갱신 |
| **프로젝트** | getProjects() | 전체 프로젝트 조회 |
| | createProject() | 프로젝트 생성 |
| | updateProject() | 프로젝트 수정 |
| **인증** | createUser() | 사용자 생성 |
| | updateUserPassword() | 비밀번호 변경 |
| **시크릿** | listSecrets() | 환경변수 목록 |
| | createSecret() | 환경변수 생성 |
| **OAuth** | getOAuthToken() | 토큰 조회 |
| | upsertOAuthToken() | 토큰 저장/갱신 |
| **문서** | getDocuments() | 문서 목록 |
| | createDocument() | 문서 생성 |
| | searchDocuments() | 문서 검색 |
| **할일** | getTodos() | 할일 목록 |
| | createTodo() | 할일 생성 |
| **태스크** | getTasks() | 태스크 목록 |
| | createTask() | 태스크 생성 |

### lib/kanban.ts 함수 목록

| 카테고리 | 함수 | 설명 |
|----------|------|------|
| **접근 제어** | checkKanbanAccess() | 프로젝트 오너/팀 역할 기반 권한 체크 |
| | isValidTransition() | 상태 전이 규칙 검증 |
| **카드** | getKanbanCards() | 카드 목록 (아카이브 포함 옵션) |
| | getKanbanCard() | 단일 카드 조회 |
| | createKanbanCard() | 카드 생성 |
| | updateKanbanCard() | 카드 수정 |
| | updateCardStatus() | 상태 변경 (전이 규칙 적용) |
| | deleteKanbanCard() | 카드 삭제 |
| **댓글** | getKanbanComments() | 댓글 조회 (username 포함) |
| | createKanbanComment() | 댓글 작성 |
| **메시지** | getKanbanMessages() | 에이전트 대화 조회 |
| | createKanbanMessage() | 메시지 생성 |

### lib/teams.ts 함수 목록

| 카테고리 | 함수 | 설명 |
|----------|------|------|
| **팀** | createTeam() | 팀 생성 (owner 자동 추가) |
| | getTeam() | 팀 조회 |
| | getTeamsByUser() | 내 팀 목록 |
| | updateTeam() | 팀 수정 |
| | deleteTeam() | 팀 삭제 |
| **멤버** | getTeamMembers() | 멤버 목록 (username/email 포함) |
| | addTeamMember() | 멤버 추가 |
| | updateMemberRole() | 역할 변경 |
| | removeTeamMember() | 멤버 제거 |
| **권한** | getTeamRole() | 유저의 팀 역할 조회 |
| | hasMinRole() | 최소 역할 충족 여부 |
| | requireTeamRole() | 역할 체크 (실패 시 throw) |
| **검색** | searchUsers() | 이메일/이름으로 유저 검색 |
