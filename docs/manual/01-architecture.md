# 01. 아키텍처 개요

## 시스템 구성도

```
┌─────────────────────────────────────────────────────────────────┐
│                        사용자 (웹/모바일)                          │
│                    브라우저 (Chrome, Safari)                       │
└──────────────┬──────────────────────────────────┬───────────────┘
               │ HTTPS                            │ WebSocket
               ▼                                  ▼
┌──────────────────────────┐     ┌────────────────────────────────┐
│   Vercel (Next.js 15)    │     │      Supabase Realtime         │
│                          │     │  (user_status, messages,       │
│  ┌─ app/          Pages  │     │   documents 실시간 구독)        │
│  ├─ app/api/    API Routes│     └────────────────────────────────┘
│  ├─ components/    React  │                  │
│  ├─ lib/       Utilities  │                  │
│  └─ hooks/     React Hooks│                  │
└──────────┬───────────────┘                  │
           │ REST API                          │
           ▼                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Supabase (PostgreSQL)                          │
│                                                                   │
│  users, messages, projects, prompts, secrets, oauth_tokens,       │
│  documents, todos, daily_goals, tasks, user_status, skills,       │
│  log_cache, watchdog_status, watchdog_commands, daemon_releases,  │
│  teams, team_members, kanban_cards, kanban_comments,              │
│  kanban_messages                                                   │
└──────────────────────────────┬───────────────────────────────────┘
                               │ REST API (service_role_key)
                               │
┌──────────────────────────────▼───────────────────────────────────┐
│              유저 로컬 머신 (Mac Mini M2 Pro 등)                    │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │              claude_daemon.py (Python)                       │ │
│  │                                                              │ │
│  │  Worker          — 메시지 폴링 + Claude Code CLI 실행        │ │
│  │  (+ 칸반 폴링)   — 칸반 메시지 폴링 + 카드별 에이전트 세션  │ │
│  │  SecretsSyncer   — 환경변수 DB→로컬 싱크 (60초)              │ │
│  │  SkillsSyncer    — 스킬 업데이트 전용 싱크 (5분)             │ │
│  │  AutoUpdater     — 데몬 자동 업데이트 (5분)                  │ │
│  │  SessionHealthChecker — 세션 수명 관리 (2시간) + Stall (30분)│ │
│  │  HeartbeatThread — 자율 반복 작업 스케줄링 (1분)              │ │
│  │  ManagerThread   — 자율 프로젝트 점검/제안 (설정 가능)        │ │
│  │  SummonManager   — 다중 에이전트 리뷰 세션 관리              │ │
│  │  cloudflared 헬스체크 — 메인 루프 내 함수 (60초)              │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                          │                                        │
│                          ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │              Claude Code CLI                                 │ │
│  │  claude -p --resume <session_id> --model <model>            │ │
│  │  → 프로젝트 디렉토리에서 코드 읽기/수정/실행                   │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ~/.claude-daemon/          — 데몬 설정, 세션, 상태               │
│  ~/Projects/<project>/      — 프로젝트 코드베이스                 │
│  ~/.claude/skills/          — Claude Code 스킬                    │
└───────────────────────────────────────────────────────────────────┘
```

## 기술 스택

| 계층 | 기술 | 버전 |
|------|------|------|
| **프론트엔드** | Next.js (App Router) | 15.5.12 |
| | React | 19 |
| | Tailwind CSS | 3.4.17 |
| | Framer Motion | 애니메이션 |
| | Lucide React | 아이콘 |
| | next-themes | 다크/라이트 모드 |
| **백엔드** | Next.js API Routes | Serverless |
| | Supabase JS Client | @supabase/supabase-js |
| **데이터베이스** | Supabase (PostgreSQL) | - |
| **음성** | Deepgram Nova-2 | STT (한국어) |
| | Edge TTS (node-edge-tts) | TTS (ko-KR-InJoonNeural) |
| | Web Speech API | 브라우저 STT 폴백 |
| **인증** | HMAC-SHA256 세션 토큰 | 자체 구현 |
| | Google/Notion OAuth 2.0 | 외부 서비스 연동 |
| **데몬** | Python 3 | claude_daemon.py |
| | Claude Code CLI | `claude -p --resume` |
| **배포** | Vercel | 웹앱 |
| | launchd | 데몬 프로세스 관리 |

## 디렉토리 구조

프로젝트는 데몬과 웹앱 두 레포로 분리되어 있다 (2026-04-02~).

```
peter-voice/                       # 작업 디렉토리
├── peter-voice-daemon/            # petervoice-daemon 레포 (PUBLIC)
│   ├── scripts/
│   │   ├── claude_daemon.py       # 메인 데몬 엔트리포인트
│   │   ├── home-portal.js         # Home Portal (맥미니 대시보드)
│   │   ├── publish.py             # 로컬 퍼블리싱 CLI
│   │   ├── web_reply.py           # 웹 API 통신 유틸
│   │   └── daemon/                # 데몬 모듈
│   │       ├── worker.py          # 메시지 폴링 + Claude Code CLI 실행
│   │       ├── health.py          # SessionHealthChecker (세션 수명 + Stall 감지)
│   │       ├── heartbeat.py       # HeartbeatThread (자율 반복 작업)
│   │       ├── kanban.py          # 칸반 세션 관리
│   │       ├── summon.py          # SummonManager (다중 에이전트 리뷰)
│   │       ├── branches.py        # 브랜치 관리
│   │       ├── sessions.py        # 세션 생성/관리
│   │       ├── prompts.py         # 프롬프트 빌드
│   │       ├── config.py          # 설정 로드
│   │       ├── globals.py         # 전역 변수
│   │       ├── api.py             # API 클라이언트
│   │       ├── queue.py           # 메시지 큐
│   │       ├── tasks.py           # 태스크 관리
│   │       ├── utils.py           # 유틸리티
│   │       ├── claude_runner.py   # Claude Code CLI 래퍼
│   │       ├── supabase.py        # Supabase 통신
│   │       ├── site_manager.py    # 퍼블리싱 사이트 관리
│   │       ├── syncers/           # 동기화 모듈
│   │       │   ├── auto_updater.py  # AutoUpdater (데몬 자동 업데이트)
│   │       │   ├── secrets.py       # SecretsSyncer (환경변수 싱크)
│   │       │   └── skills.py        # SkillsSyncer (스킬 업데이트)
│   │       └── manager/           # 매니저 모듈
│   │           ├── thread.py      # ManagerThread (자율 멀티턴)
│   │           └── http_server.py # 상태 모니터링 API
│   ├── skills/                    # 번들 스킬
│   └── requirements.txt
│
├── peter-voice-web/               # sonolbot_web 레포 (PRIVATE)
│   ├── app/                       # Next.js App Router
│   │   ├── page.tsx               # 랜딩 페이지
│   │   ├── chat/page.tsx          # 메인 채팅 인터페이스
│   │   ├── settings/page.tsx      # 설정 페이지
│   │   ├── admin/page.tsx         # 관리자 대시보드
│   │   ├── agents/page.tsx        # 에이전트 대시보드
│   │   ├── kanban/page.tsx        # 칸반 보드
│   │   ├── reviews/page.tsx       # 코드 리뷰
│   │   ├── portal/               # Home Portal 관련
│   │   ├── login/page.tsx         # 로그인
│   │   ├── signup/page.tsx        # 회원가입
│   │   ├── layout.tsx             # 루트 레이아웃
│   │   └── api/                   # API 라우트
│   │       ├── auth/              # 인증 (login, signup, logout, OAuth)
│   │       ├── bot/               # 데몬 통신 (poll, reply, heartbeat)
│   │       ├── messages/          # 채팅 메시지 CRUD
│   │       ├── projects/          # 프로젝트 관리
│   │       ├── branches/          # 브랜치 관리
│   │       ├── secrets/           # 환경변수 관리
│   │       ├── prompts/           # 시스템 프롬프트
│   │       ├── todos/             # 할일 관리
│   │       ├── daily-goals/       # 오늘의 목표
│   │       ├── tasks/             # 하트비트 태스크
│   │       ├── kanban/            # 칸반 카드/댓글/메시지
│   │       ├── teams/             # 팀 멤버 관리
│   │       ├── users/             # 유저 검색
│   │       ├── agents/            # 에이전트 대시보드 API
│   │       ├── relay/             # 에이전트 간 릴레이
│   │       ├── summon/            # 에이전트 소환
│   │       ├── repos/             # Git 리포 등록
│   │       ├── reviews/           # 코드 리뷰
│   │       ├── calendar/          # Google 캘린더
│   │       ├── files/             # 파일 업로드
│   │       ├── stt/               # 음성인식 (Deepgram)
│   │       ├── tts/               # 음성합성 (Edge TTS)
│   │       ├── skills/            # 스킬 마켓
│   │       ├── logs/              # 데몬 로그
│   │       ├── tunnel/            # 터널 관리
│   │       ├── admin/             # 관리자 API
│   │       ├── onboarding/        # 고객 온보딩
│   │       ├── version/           # 버전 정보
│   │       └── ...
│   ├── components/                # React 컴포넌트
│   │   ├── ChatWindow.tsx         # 메인 채팅 컨테이너
│   │   ├── MessageBubble.tsx      # 메시지 렌더링
│   │   ├── MessageInput.tsx       # 입력 필드
│   │   ├── VoiceModeOverlay.tsx   # 음성모드 오버레이
│   │   ├── TodoCalendarSidebar.tsx # 할일/캘린더 사이드바
│   │   ├── DocumentsPanel.tsx     # 문서 패널
│   │   ├── SkillsPanel.tsx        # 스킬 마켓 패널
│   │   ├── SecretsPanel.tsx       # 환경변수 관리
│   │   ├── SystemStatusPanel.tsx  # 시스템 상태/설정
│   │   ├── LogsViewer.tsx         # 로그 뷰어
│   │   ├── PromptEditorModal.tsx  # 프롬프트 편집기
│   │   ├── ContextUsageWidget.tsx # 컨텍스트 사용량 표시
│   │   ├── DirectClaude.tsx       # Direct Claude 연결
│   │   ├── NewBranchModal.tsx     # 브랜치 생성 모달
│   │   ├── ThemeProvider.tsx      # 테마 제공자
│   │   ├── kanban/                # 칸반 보드 컴포넌트
│   │   ├── reviews/               # 코드 리뷰 컴포넌트
│   │   └── ...
│   ├── hooks/                     # React 커스텀 훅
│   ├── lib/                       # 유틸리티 라이브러리
│   ├── supabase/migrations/       # DB 마이그레이션
│   └── public/                    # 정적 파일
│
└── docs/                          # 프로젝트 문서 (Home Portal이 직접 서빙)
```

## 핵심 데이터 흐름

### 1. 사용자 메시지 → 봇 응답

```
사용자 입력 (웹 UI)
  ↓ POST /api/messages
Supabase messages 테이블 (processed=false)
  ↓ 데몬 Worker.poll() — 3초마다
메시지 수신 + 프로젝트별 락 획득
  ↓ run_claude()
Claude Code CLI 서브프로세스 실행
  ↓ 스트리밍 응답
POST /api/bot/reply (is_final=false) — 2초마다 중간 결과
  ↓ 최종 응답
POST /api/bot/reply (is_final=true)
  ↓ Supabase Realtime
웹 UI 실시간 업데이트
```

### 2. 파일 첨부

```
웹 UI에서 파일 선택/드래그/붙여넣기
  ↓ POST /api/files/upload (JSON 모드)
Supabase Storage signed URL 발급
  ↓ PUT (직접 업로드, Vercel 4.5MB 제한 우회)
메시지에 파일 URL 포함
  ↓ 데몬이 메시지 처리 시
파일 다운로드 → 로컬 저장 → Claude에 경로 전달
```

### 3. 환경변수 싱크

```
웹 UI SecretsPanel → POST /api/secrets → DB 저장
  ↓ SecretsSyncer (60초마다)
GET /api/secrets?raw=true (OAuth 토큰 포함)
  ↓
~/.claude-daemon/.env.secrets 파일 (0o600)
  ↓
os.environ에 주입 → Claude Code 서브프로세스 상속
```

### 4. 문서 서빙

```
Claude Code가 {project}/docs/*.md 생성/수정
  ↓
Home Portal API (맥미니 :3000)가 로컬 파일을 직접 서빙
  ↓ 웹 UI가 Home Portal API로 문서 목록/내용 조회
웹 UI DocumentsPanel에 실시간 반영
```

> DocsSyncer는 제거됨 — docs는 DB 싱크 없이 Home Portal이 로컬 파일을 직접 서빙한다.

### 5. 칸반 에이전트 세션

```
팀원이 카드 Chat 탭에서 메시지
  ↓ POST /api/kanban/{id}/messages
kanban_messages (processed=false)
  ↓ 데몬 칸반 폴링 (kanban_enabled=true 프로젝트만)
카드별 격리 세션 (CLAUDE_CONFIG_DIR=~/.claude-daemon/kanban/card-{id}/)
  ↓ Claude Code CLI 실행
응답 → kanban_messages (type=bot)
  ↓ Supabase Realtime
웹 UI 자동 갱신
```

## 인증 구조

| 주체 | 인증 방식 | 헤더/쿠키 |
|------|-----------|-----------|
| 웹 UI 사용자 | 세션 쿠키 | `peter_voice_session` (httpOnly) |
| Claude 데몬 | API 키 | `X-Api-Key: <users.api_key>` |
| 데몬 (일부 구 API) | Bearer 토큰 | `Authorization: Bearer <api_key>` |
| 관리자 | 세션 쿠키 + role='admin' | 관리자 권한 체크 |

## 실시간 기능 (Supabase Realtime)

| 테이블 | 구독 대상 | 용도 |
|--------|-----------|------|
| `user_status` | streaming_text, is_working | 봇 작업 상태 + 스트리밍 텍스트 |
| `messages` | INSERT | 새 메시지 실시간 수신 |
| `documents` | INSERT, UPDATE, DELETE | 문서 실시간 반영 |
| `kanban_cards` | INSERT, UPDATE, DELETE | 칸반 보드 자동 갱신 |
