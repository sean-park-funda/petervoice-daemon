# 03. Claude 데몬 (claude_daemon.py)

Python 데몬. 유저 로컬 머신에서 실행되며, 웹 UI와 Claude Code CLI를 연결하는 브릿지.

## 소스 코드 레포지토리

- **데몬 코드**: `sean-park-funda/petervoice-daemon` (PUBLIC)
- **웹앱 코드**: `sean-park-funda/sonolbot_web` (PRIVATE)
- 데몬과 웹앱은 별도 레포로 분리됨 (2026-04-02~)
- 고객 머신에는 `petervoice-daemon` 레포만 clone됨
- deploy token 불필요 (PUBLIC 레포)

### 개발 환경 디렉토리 (Sean)

```
~/Projects/peter-voice/peter-voice-daemon/  ← petervoice-daemon (데몬, 고객과 동일 구조)
~/Projects/peter-voice/peter-voice-web/     ← sonolbot_web (웹앱, Vercel 배포)
```

- 데몬 수정 → `peter-voice-daemon/`에서 커밋/푸시 → AutoUpdater가 고객에 자동 반영
- 웹앱 수정 → `peter-voice-web/`에서 커밋/푸시 → Vercel 자동 배포

## 구성 요소

```
claude_daemon.py + scripts/daemon/
├── Worker               — 메시지 폴링 + Claude Code CLI 실행 (메인)
│   └── kanban 폴링      — 칸반 메시지 폴링 + 카드별 에이전트 세션
├── SecretsSyncer        — 환경변수 DB→로컬 (60초)
├── SkillsSyncer         — 스킬 업데이트 전용 (5분)
├── AutoUpdater          — Git 기반 자동 업데이트 (5분)
├── SessionHealthChecker — 세션 수명 관리 (2시간) + Stall 감지 (30분)
├── HeartbeatThread      — 반복 작업 스케줄링 (1분)
├── ManagerThread        — 자율 프로젝트 점검 (설정 가능)
├── SummonManager       — 다중 에이전트 리뷰 세션 관리
└── cloudflared 헬스체크 — 메인 루프 내 함수 (60초)

칸반 관련 모듈:
└── daemon/kanban.py     — 칸반 세션 관리, 프롬프트 빌드, Claude 실행
```

## 데이터 접근 방식

데몬은 **Supabase에 직접 접근하지 않는다.** 모든 DB 작업은 PeterVoice 웹 API(`/api/bot/*`)를 프록시로 사용한다.

```
데몬 → GET/POST /api/bot/* (Authorization: Bearer api_key)
  → Vercel 서버 → Supabase (service_role key)
```

**이유:** 고객 머신에 `service_role_key`를 배포하면 RLS 바이패스로 전체 DB 접근 가능 → 보안 위험

**주요 API 엔드포인트:**
| 엔드포인트 | 용도 |
|-----------|------|
| `GET /api/bot/poll` | 새 메시지 폴링 |
| `POST /api/bot/reply` | 응답 전송 |
| `GET /api/bot/prompt` | 프로젝트 프롬프트 조회 |
| `GET /api/bot/system-prompt` | `_petervoice_system` 시스템 프롬프트 조회 |
| `GET /api/bot/conversation` | 최근 대화 조회 |
| `GET /api/secrets?raw=true` | 환경변수 조회 |
| `GET /api/branches` | 브랜치 목록/관리 |
| `GET /api/bot/skills` | 스킬 목록 |
| `GET /api/bot/tasks` | HeartBeat 작업 조회 |
| `GET/POST /api/bot/kanban/*` | 칸반 관련 |

## 설정 (config.json)

위치: `~/.claude-daemon/config.json`

```json
{
  "api_key": "pv_xxxxxxxx",
  "api_url": "https://peter-voice.vercel.app",
  "bot_name": "Peter",

  "max_concurrent": 3,
  "session_ttl_hours": 24,
  "poll_interval_sec": 3,
  "stream_interval_sec": 2.0,

  "claude_model": "claude-sonnet-4-5-20250514",
  "claude_effort": "medium",
  "claude_stdout_timeout_sec": 600,
  "claude_hard_timeout_sec": 900,
  "claude_hard_timeout_with_tools_sec": 1800,

  "rewriter_enabled": false,
  "rewriter_model": "haiku",

  "project_dirs": {
    "peter-voice": "/Users/sean/Projects/peter-voice"
  },

  "accounts": {
    "account-b": {
      "config_dir": "~/.claude-account-b/.claude"
    }
  },

  "manager": {
    "enabled": false,
    "interval_minutes": 60,
    "projects": ["peter-voice", "thegrim"],
    "quiet_hours": [0, 7]
  }
}
```

## 파일 구조

```
~/.claude-daemon/
├── config.json          — 데몬 설정
├── sessions.json        — 활성 세션 목록
├── queue.json           — 미처리 메시지 큐 (crash recovery)
├── daemon.log           — 로그 (일별 로테이션, 7일 보관)
├── .env.secrets         — 환경변수 파일 (0o600)
├── manager_state.json   — ManagerThread 상태
├── pending_resets.json  — 세션 리셋 대기열
├── prompts/             — 시스템 프롬프트 캐시
├── workflows/           — ManagerThread 워크플로우
├── downloads/           — 파일 첨부 다운로드
└── projects/            — 자동 생성 프로젝트 디렉토리
```

## Worker — 메시지 처리

### 메시지 폴링

```
매 3초 → GET /api/bot/poll
  ↓ pending 메시지 수신
중복 제거 (processed_ids 셋, 최대 1000개)
  ↓
ThreadPoolExecutor에 제출 (프로젝트별 직렬, 프로젝트 간 병렬)
```

### Claude Code CLI 실행

```python
claude -p "{메시지}" \
  --resume "{session_id}" \
  --model "{모델}" \
  --output-format stream-json \
  --max-turns 50 \
  --verbose
```

- `--resume`: 세션 유지 (없으면 새 세션 생성)
- `--output-format stream-json`: JSON 라인 스트리밍
- 작업 디렉토리: 프로젝트별 `directory` 설정

### 스트리밍 응답 처리

```
Claude CLI stdout (JSON 라인)
  ↓ content_block_delta → 텍스트 누적
  ↓ assistant 이벤트 → 툴 사용 감지 (🔧 Bash, 🔧 Read 등)
  ↓ 2초마다 → POST /api/bot/reply (is_final=false) → 스트리밍 표시
  ↓ result 이벤트 → 최종 응답
POST /api/bot/reply (is_final=true)
  + 툴 로그 별도 메시지 (subtype='tool_log')
```

### 타임아웃

| 타임아웃 | 기본값 | 설명 |
|----------|--------|------|
| stdout_timeout | 600초 (10분) | 출력 없음 → kill |
| hard_timeout | 900초 (15분) | 절대 시간 제한 |
| hard_timeout_with_tools | 1800초 (30분) | 툴 사용 시 확장 |

### 컨텍스트 오버플로 처리

```
stderr에 "context" 포함 감지
  ↓ 세션 컨텍스트 자동 저장
  ↓ 세션 리셋
  ↓ 재시도 (최대 2회)
```

### 특수 명령어

| 명령어 | 설명 |
|--------|------|
| `/restart` | 데몬 재시작 |
| `/reset`, `/새세션` | 세션 리셋 (컨텍스트 요약 저장) |
| `/status` | 봇 상태, 세션 정보, 모델 표시 |
| `/prompt` | 현재 시스템 프롬프트 표시 |
| `/rewriter` | 음성 친화 후처리 토글 |
| `/manager run` | 매니저 즉시 실행 |
| `/do <작업>` | 딥 태스크 큐잉 |

## 세션 관리

### 프롬프트 결합 순서

```
1. _petervoice_system  — 피터보이스 시스템 가이드 (모든 유저 공유, user_id=0)
2. _common             — 유저별 공통 프롬프트 (유저의 모든 프로젝트 공유)
3. project prompt      — 프로젝트별 프롬프트
4. session context     — 이전 세션 요약 + 최근 대화
```

- `_petervoice_system`은 `GET /api/bot/system-prompt`로 조회
- 나머지는 `GET /api/bot/prompt?project=...`으로 조회

### 세션 키 형식
`"project:task"` (예: `"peter-voice:default"`)

### 세션 수명주기

```
1. 새 메시지 도착
2. get_session_id(project) 호출
   - 세션 존재 + TTL 내 → 기존 세션 재사용
   - 세션 없음 또는 TTL 만료 → None (새 세션 생성)
3. Claude CLI 실행 → 세션 ID 반환
4. update_session(project, sid) → sessions.json 저장
5. TTL 만료 시 → save_session_context() → 요약 저장
```

### 세션 컨텍스트 연속성

```
이전 세션 요약 (GET /api/bot/session-summary)
  + 최근 메시지 (GET /api/bot/conversation)
  → 새 세션의 시스템 프롬프트에 주입
  → Claude가 이전 대화 맥락을 유지
```

### 멀티 계정

```json
"accounts": {
  "account-b": {
    "config_dir": "~/.claude-account-b/.claude"
  }
}
```
- 프로젝트별 `account` 필드 → 해당 계정의 config_dir 사용
- `CLAUDE_CONFIG_DIR` 환경변수로 Claude CLI에 전달
- 계정 변경 시 세션 자동 리셋

## SecretsSyncer — 환경변수 싱크

```
매 60초 → GET /api/secrets?raw=true (API 프록시)
  ↓ 유저 시크릿 + OAuth 토큰 (환경변수 형태로)
  ↓
~/.claude-daemon/.env.secrets 작성 (퍼미션 0o600)
  ↓
os.environ에 주입 → Claude 서브프로세스 상속
```

포함되는 환경변수:
- 유저가 직접 설정한 시크릿
- `GOOGLE_REFRESH_TOKEN`, `GOOGLE_ACCESS_TOKEN`
- `NOTION_ACCESS_TOKEN`, `NOTION_API_TOKEN`
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (앱 자격증명)

## DocsSyncer — 제거됨

DocsSyncer는 제거되었다. docs는 DB 싱크 없이 Home Portal API가 로컬 파일을 직접 서빙한다.
`docs_state.json`은 레거시 파일로 남아 있지만 사용되지 않는다.

## HeartbeatThread — 반복 작업

```
매 60초 → GET /api/bot/tasks (API 프록시)
  ↓ 활성 시간대 확인 (active_hours)
  ↓ max_runs 확인
  ↓ 프로젝트 busy 확인
  ↓
"[heartbeat] HEARTBEAT.md를 확인하고 할 일이 있으면 처리해줘"
  → POST /api/bot/reply로 주입
  ↓
next_run_at 갱신, run_count 증가
```

## ManagerThread — 자율 점검

### 사이클

```
1. 딥 태스크 큐 확인 (/do 명령)
2. 재시도 큐 확인 (실패한 프로젝트)
3. 라운드 로빈 프로젝트 점검
   → Scout (1턴: 문제/개선점 식별)
   → Triage (AUTO/ASK/SKIP 결정)
   → Execute (승인 시 실행, 멀티턴 지원)
```

### 워크플로우 설정 (`~/.claude-daemon/workflows/*.md`)

```yaml
---
project: peter-voice
scout:
  focus: [bugs, performance]
  auto_fix: false
agent:
  autonomous: false
  max_turns: 3
---
프로젝트별 점검 가이드라인...
```

## AutoUpdater — Git 기반 자동 배포

파일: `scripts/daemon/syncers/auto_updater.py`

### 배포 구조

```
개발 (Sean Mac Mini)
│
├─ 웹앱 코드 변경 (sonolbot_web, PRIVATE)
│   └─ git push → GitHub → Vercel 자동 배포 ✅
│
└─ 데몬 코드 변경 (petervoice-daemon, PUBLIC)
    └─ git push → GitHub
        └─ 각 머신의 AutoUpdater가 5분마다 git fetch
            └─ 새 커밋 감지 시 git pull → 데몬 자동 재시작 ✅
```

**핵심: 각 레포에 `git push`만 하면 자동 배포.**
- 웹앱: `sonolbot_web` → Vercel
- 데몬: `petervoice-daemon` → 고객 머신 AutoUpdater

### 동작 흐름 (5분 주기)

```
1. git fetch origin main
2. 로컬 HEAD vs origin/main HEAD 비교
3. 다르면:
   a. git pull --ff-only origin main (fast-forward만 허용)
   b. requirements.txt 변경 감지 시 → pip install -r requirements.txt
   c. shutdown_event.set() → launchd가 10초 후 자동 재시작
4. 같으면: 패스
```

### 안전장치

- **fast-forward only**: 로컬 수정이 있으면 pull 거부 → 충돌 방지
- **연속 실패 3회**: 자동 일시 중단 + 로그 경고
- **30초 지연 시작**: 데몬 초기화 완료 후 첫 체크

### config.json 설정

```json
{
  "auto_update_enabled": true,   // 자동 업데이트 ON/OFF (신규 설치 기본 true)
  "update_branch": "main"        // 따라갈 브랜치
}
```

### 배포 절차

| 시나리오 | 방법 |
|----------|------|
| 일반 배포 | `git push` → 끝. 5분 내 모든 머신 자동 반영 |
| 즉시 배포 | `./scripts/push-daemon-ssh.sh` (SSH로 git pull + 재시작) |
| 긴급 복구 | SSH 접속 → `git pull --ff-only` → `launchctl stop` |

### push-daemon-ssh.sh (수동 배포)

파일: `scripts/push-daemon-ssh.sh`

```bash
./scripts/push-daemon-ssh.sh            # 실제 배포
./scripts/push-daemon-ssh.sh --dry-run  # 확인만
```

고객 목록 형식: `"user@host:os:service_name:repo_path:ssh_opts"`

AutoUpdater가 켜진 후에는 긴급 상황에서만 사용.

### 기존 고객 활성화

`~/.claude-daemon/config.json`에 추가:
```json
"auto_update_enabled": true,
"update_branch": "main"
```

### 확인 명령어

```bash
# AutoUpdater 로그
tail -f ~/.claude-daemon/daemon.log | grep updater

# 원격과 버전 차이 확인
git fetch origin main && git log --oneline HEAD..origin/main
```

### 폐기된 방식

- `deploy-daemon.sh` + `daemon_releases` 테이블: Supabase에 단일 파일 업로드 방식 → Git 기반으로 대체, 삭제됨
- `daemon.zip` 배포: 초기에 ZIP으로 데몬 배포 → `git clone petervoice-daemon`으로 대체
- `sonolbot_web` 모노레포: 웹+데몬 통합 레포 → `petervoice-daemon` (PUBLIC) + `sonolbot_web` (PRIVATE)으로 분리
- Supabase 직접 접근: `supabase_url`/`supabase_key`로 REST API 직접 호출 → API 프록시(`/api/bot/*`)로 전환

## SessionHealthChecker — 세션 건강 관리 + Stall Detection

파일: `scripts/daemon/health.py`

세션 수명 관리와 중단된 대화 감지를 하나의 스레드에서 수행한다.

### 듀얼 Tick 루프

SessionHealthChecker는 두 가지 주기로 동작한다:

| 주기 | 기능 | 설명 |
|------|------|------|
| 30분 | Stall 감지 | 모든 세션의 최근 대화 스니펫을 session-manager에게 전송 |
| 2시간 | Health 체크 | 세션 TTL 관리, 리셋 제안, 좀비 세션 정리 |

초기 대기 30분 후 루프 시작. 60초마다 tick하며 각 주기 도달 시 실행.

### Stall 감지 흐름

Python(데몬)은 **필터링이나 판단을 하지 않는다**. 모든 세션의 스니펫을 수집해서 session-manager에게 보내고, 판단은 session-manager(Haiku 모델의 Claude Code 세션)가 한다.

```
30분마다 (_check_stalls):
  1. 모든 활성 세션의 최근 대화 5건 스니펫 수집 (각 400자 제한)
  2. [stall-check 리포트]로 묶어서 session-manager 프로젝트에 메시지 전송
     (subtype: "stall_check_report")
  3. session-manager가 대화 맥락을 읽고 지능적으로 판단:
     - 자연스럽게 끝난 대화 → 무시
     - "잠시만요" 후 30분+ 침묵 → nudge 필요
     - 유저가 보류 요청 → 무시
  4. nudge 필요 시 session-manager가 [stall-check] 릴레이를 해당 프로젝트에 전송
  5. 깨울 대상 없으면 "없음" 한 마디로 응답 (토큰 절약)
```

**왜 Python이 판단하지 않는가**: 대부분의 대화는 봇 메시지로 끝난다. "완료했습니다"도 "잠시만요"도 둘 다 봇 메시지가 마지막이다. 기계적 임계값으로는 정상 종료와 중단을 구분할 수 없고, 대화 맥락을 이해하는 LLM만이 판단 가능하다.

### [stall-check] 릴레이 수신

모든 에이전트의 `_common` 프롬프트에 `[stall-check]` 수신 가이드가 포함됨:
1. 릴레이 메시지의 맥락 읽기 (어떤 작업이 중단되었는지)
2. 필요시 이전 대화 조회
3. 미완료 작업이 있으면 이어서 진행
4. 완료되었거나 유저 입력 필요 시 현황 보고
5. 불필요한 작업은 하지 않기 (오탐 가능)

### session-manager 프로젝트

session-manager는 **Haiku 모델**의 Claude Code 세션으로, 세션 건강 관리와 stall 판단을 담당한다.

**자동 생성**: `_ensure_session_manager()`가 첫 실행 시 프로젝트 존재 여부를 확인하고, 없으면 자동 생성한다:
1. `GET /api/projects` → session-manager 존재 여부 확인 (결과 캐싱)
2. 없으면 → `POST /api/projects`로 프로젝트 생성
3. `PUT /api/projects`로 모델을 haiku로 설정
4. `PUT /api/prompts`로 세션 관리 + stall 감지 프롬프트 자동 주입
   (프롬프트는 `health.py`의 `_SESSION_MANAGER_PROMPT` 클래스 변수에 내장)

## 멀티 계정 (Claude Code Account Switching)

Claude Code Max 플랜의 주간 사용량 한도를 관리하기 위해, 프로젝트별로 다른 Claude Code 계정을 선택할 수 있다.

### 왜 필요한가

Claude Code Max 플랜은 주간 사용량 제한이 있다. 프로젝트가 많으면 한 계정으로 일주일을 버티기 어렵다. 두 번째 Claude Code 계정(다른 이메일로 가입)을 추가하고, 프로젝트를 나눠서 배분하면 실질적으로 사용량이 2배가 된다.

### 현재 환경 비교: Sean vs Willy

**Sean (2계정 운영 중)**

```
~/.claude/                      ← 기본 계정 (default)
~/.claude-account-b/.claude/    ← 두 번째 계정 (secondary)
~/.claude-daemon/config.json    ← accounts 설정 있음
```

```json
// Sean의 config.json (accounts 부분)
{
  "accounts": {
    "secondary": {
      "config_dir": "~/.claude-account-b/.claude"
    }
  }
}
```

> **Q: 계정이 2개인데 왜 accounts에는 1개만?**
>
> 기본 계정(`~/.claude/`)은 Claude Code가 원래 사용하는 경로라서 등록할 필요가 없다.
> accounts에는 **기본이 아닌 추가 계정만** 등록한다.
>
> - 프로젝트 설정이 `default` → `~/.claude/`를 그냥 씀 (accounts 조회 안 함)
> - 프로젝트 설정이 `secondary` → accounts에서 `config_dir` 찾아서 환경변수로 전달

프로젝트 분배 현황 (41개 프로젝트):
```
default    계정: general, to-do, tl, danguen-hunter, ship-flip, dokbo 등 (22개)
secondary  계정: peter-voice, cocktail, thegrim, signalflow, evolution 등 (19개)
```
→ 사용량이 많은 핵심 프로젝트를 secondary에 분산

**Willy (1계정 — 아직 추가 안 함)**

```
~/.claude/                      ← 기본 계정뿐
~/.claude-daemon/config.json    ← accounts 설정 없음
```

프로젝트: general, willy, crewariworks 등 — 전부 default 계정 사용

### 계정 추가 가이드 (Willy 기준 step-by-step)

#### 1단계: 두 번째 Claude Code 구독

- 다른 이메일로 Anthropic 계정 가입
- Claude Code Max 플랜 구독
- 이 계정의 이메일/비밀번호를 기억해둘 것

#### 2단계: 두 번째 계정용 폴더 만들기

Mac 터미널에서:

```bash
mkdir -p ~/.claude-account-b/.claude
```

#### 3단계: 두 번째 계정으로 Claude Code 인증

```bash
# 두 번째 계정 폴더를 지정해서 Claude CLI 실행
CLAUDE_CONFIG_DIR=~/.claude-account-b/.claude claude

# → 브라우저가 열리면 두 번째 이메일로 로그인
# → 인증 완료 후 claude를 종료해도 됨 (/exit)
```

인증이 끝나면 `~/.claude-account-b/.claude/` 안에 `.claude.json`, `credentials.json` 등이 생긴다.

#### 4단계: config.json에 계정 등록

`~/.claude-daemon/config.json`을 열어서 `accounts` 항목 추가:

```json
{
  "api_key": "pv_xxxx",
  "api_url": "https://peter-voice.vercel.app",
  "bot_name": "Peter",

  "accounts": {
    "secondary": {
      "config_dir": "~/.claude-account-b/.claude"
    }
  },

  ... (나머지 설정)
}
```

- `"secondary"` — 계정 이름. 웹 UI에서 이 이름으로 표시됨. 원하는 이름 사용 가능
- `"config_dir"` — 3단계에서 인증한 폴더 경로
- 기본 계정(`~/.claude/`)은 여기에 넣지 않는다 — Claude Code가 알아서 쓰는 경로이므로 추가 계정만 등록

#### 5단계: 웹 UI에서 프로젝트 배정

1. 웹 UI에서 프로젝트 설정 (톱니바퀴) 열기
2. "계정" 드롭다운에서 `secondary` 선택
3. 저장

사용량이 많은 프로젝트를 secondary로 옮기면 된다. 예시:

```
default 유지:    general (기본), system-admin
secondary 이동:  willy (메인 업무), crewariworks (개발)
```

#### 6단계: 확인

프로젝트에 아무 메시지나 보내보면 된다. 데몬 로그에서 확인 가능:

```bash
tail -20 ~/.claude-daemon/daemon.log | grep account
# → "project=willy, ... account=secondary" 같은 로그가 보이면 성공
```

### 작동 원리

```
유저가 메시지 전송
  → 데몬이 해당 프로젝트 설정 조회 (매번 API로 최신값 fetch)
  → account 값 확인 (null이면 "default")
  → config.json의 accounts에서 config_dir 조회
  → CLAUDE_CONFIG_DIR 환경변수를 설정한 채로 Claude CLI 실행
  → Claude CLI가 해당 폴더의 인증 정보를 사용
```

프로젝트의 계정 설정이 바뀌면 데몬이 자동으로:
1. 현재 세션 컨텍스트 저장 (대화 요약)
2. 세션 리셋 — 이전 계정의 세션은 새 계정에서 무효
3. 다음 메시지부터 새 계정으로 새 세션 시작

### 웹 UI

- 프로젝트 설정 모달에 "계정" 드롭다운
- 프로젝트 목록에서 계정이 지정된 프로젝트는 보라색 뱃지 표시
- 브랜치 생성 시 부모 프로젝트의 계정을 자동 계승

### 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| 계정 전환 후 이전 대화 맥락이 없다 | 정상 — 계정이 바뀌면 세션이 리셋됨 | 이전 대화 요약이 자동 주입되므로 맥락은 유지됨 |
| "authentication required" 에러 | config_dir 폴더에서 인증을 안 했음 | 3단계 다시 수행: `CLAUDE_CONFIG_DIR=... claude` |
| 드롭다운에 secondary가 안 보인다 | config.json에 accounts를 안 넣었음 | 4단계 확인 후 데몬 재시작 |
| 계정을 바꿨는데 반영이 안 된다 | — | 다음 메시지부터 반영됨 (설정은 매번 fetch) |
| 두 계정 다 한도가 찼다 | 주간 리셋 대기 | 리셋까지 대기, 또는 3번째 계정 추가 |
| config_dir 경로에 오타 | 폴더를 못 찾아서 인증 실패 | `ls ~/.claude-account-b/.claude/` 로 폴더 존재 확인 |

---

## launchd 관리

### 시작/중지

```bash
# 데몬 시작
launchctl load ~/Library/LaunchAgents/com.petervoice.claude-daemon.plist

# 데몬 중지 (10초 내 자동 재시작)
launchctl stop com.petervoice.claude-daemon

# 지연 재시작 (현재 응답 완료 후)
(sleep 5 && launchctl stop com.petervoice.claude-daemon) &
```

### 재시작 트리거

| 트리거 | 메커니즘 |
|--------|----------|
| `/restart` 명령 | restart_requested → exit(1) → launchd 재시작 |
| AutoUpdater | 새 버전 감지 → 교체 → exit(1) |
| 웹 UI Force Restart | user_status.force_restart → exit(1) |
| Worker 스레드 사망 | 메인 스레드 watchdog → exit(1) |
| cloudflared 프로세스 사망 | 60초마다 헬스체크 → `_ensure_home_portal()` 자동 복구 |

### 주의사항

- **절대 `pkill`, `kill`, `killall`로 직접 종료하지 말 것** → PID 파일 정리 실패
- 반드시 `launchctl stop`을 사용
- 지연 재시작으로 현재 응답 전달 보장

## Cloudflare 터널 자동 복구

데몬 메인 루프의 watchdog이 60초마다 Cloudflare 터널 상태를 점검하고 자동 복구한다.

### 터널 토큰 복구 (`_fetch_tunnel_token_from_cf`)

`config.json`에 `cloudflare_tunnel_id`는 있지만 `cloudflare_tunnel_token`이 누락된 경우:
1. `CLOUDFLARE_API_TOKEN` 환경변수로 Cloudflare API 호출
2. 계정 ID 조회 → 기존 터널 토큰 가져오기
3. 복구된 토큰을 `config.json`에 저장
4. 실패 시에만 새 터널을 생성

### cloudflared 프로세스 헬스체크 (`_check_cloudflared_health`)

메인 루프에서 60초(`watchdog_tick % 60`)마다 실행:
1. `pgrep -f "cloudflared.*tunnel.*run"`으로 프로세스 생존 확인
2. 프로세스가 죽어 있으면 `_ensure_home_portal()` 재호출하여 자동 복구
3. `home_portal_enabled=false`인 경우 체크 스킵

### 복구 시나리오

| 상황 | 동작 |
|------|------|
| `cloudflared` plist 삭제됨 | 헬스체크가 감지 → plist 재생성 + `launchctl bootstrap` |
| `cloudflare_tunnel_token` 누락 | Cloudflare API로 기존 토큰 복구 → config 저장 |
| `cloudflared` 크래시 | 헬스체크가 감지 → `_ensure_home_portal()` 재실행 |
| `CLOUDFLARE_API_TOKEN` 없음 | 토큰 복구 불가 → 새 터널 생성 시도 |
