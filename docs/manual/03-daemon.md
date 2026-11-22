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
├── SessionHealthChecker — 세션 수명 관리 (2시간) (Stall 감지는 2026-07-16 제거 — 26장)
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
| `GET /api/bot/conversation` | 최근 대화 조회 (최신순 `limit`건, 더 이전은 응답 `next_before` 를 `before` 로) |
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
  "human_reserved_lanes": 0,
  "merge_auto_messages": true,
  "merge_auto_max": 10,
  "boot_grace_sec": 180,
  "boot_auto_lanes": 1,
  "boot_grace_uptime_sec": 600,
  "claude_spawn_retry_sec": 120,
  "cli_update_boot_grace_sec": 900,
  "cli_update_max_defer_sec": 21600,
  "cli_update_retry_sec": 21600,
  "cli_install_timeout_sec": 900,
  "cli_repair_retry_sec": 1800,
  "session_ttl_hours": 24,
  "poll_interval_sec": 3,
  "stream_interval_sec": 2.0,

  "claude_model": "claude-opus-5-5",
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

> **동시 실행 칸(`max_concurrent`)은 실제로 도는 턴만 센다 (2026-11-03).** 같은 담당(프로젝트·브랜치)에 몰린 메시지는 칸을 쥐지 않고 디스패처 대기열(`daemon/dispatch.py`)에서 기다리며, 담당당 한 번에 한 턴만 돈다. 사람이 친 메시지가 자동 메시지(`[relay from:` · `[heartbeat]` 등)보다 먼저 시작한다. `human_reserved_lanes` 는 사람 메시지 전용 예비 칸 수 (기본 `max_concurrent // 4`, 최대 2 — 3칸이면 0). 로그 `[queue] msg #N → … started after Ns wait` 가 5초 넘게 기다린 메시지를 남긴다. 배경: 11/03 Sean 맥 12칸이 실제 실행 3 + 잠금 대기 9 로 차서 사람 메시지가 16분 기다렸다. **예외(2026-11-16, axl 작업대 카드 839 E):** 작업대 실무자 글 — 릴레이 머리 `[relay from:…]` 바로 뒤(줄바꿈 뒤)에 `[작업대 대화] ` 가 오는 메시지 — 는 릴레이 길로 와도 **사람 메시지**로 센다(`dispatch.py` `WORKBENCH_HUMAN`). 우선순위·예비 칸·부팅 완충은 사람과 같고, 자동 메시지 묶기에는 안 들어간다. `[시험 자리] `·`(○○ 시험) ` 표식이 앞에 붙은 글과 `[작업대 확인]`·`[작업 중지]` 는 자동 그대로. 배경: 11/03 11:35~12:13 맥 전체 칸 부족 때 실무자 글 4건이 예비 칸을 못 쓰고 3분 넘게 기다렸다.
>
> **쌓인 자동 메시지는 한 턴에 묶인다 (2026-11-12, Sean 「둘 다 해」).** 자동 턴을 시작할 때 같은 담당에게 대기 중인 릴레이·알림(`[relay from:` · `[외부 relay` · `[자료수신` · `[stall-check`)이 더 있으면 도착순으로 한 통에 묶어 한 턴으로 돈다 — 머리 `[자동 메시지 N건 묶음]`, 통마다 `━━━ i/N · #id · 시각(KST) ━━━`. 「A 가 맡아」 뒤에 온 「A 말고 B」 를 같은 턴에 보게 하고 턴 수를 줄인다(11/09 revenue-lab 부산진 중복 답). 사람 메시지·하트비트(`[heartbeat]`, 「할 일 없음」 조용한 회차 판정이 입력 1건 기준)·`[매니저]`·첨부·댓글(reply_to)·암호화 메시지는 묶지 않는다. 묶음 턴은 메시지 전부를 처리완료로 표시하고 답의 `reply_to` 에 전부를 넣는다(영구 큐에도 묶음 하나로). `merge_auto_messages`(기본 true, false 면 끔) · `merge_auto_max`(기본 10통, 본문 합 60,000자 상한). 릴레이의 `metadata.supersede` 키가 같은 옛 대기 메시지는 새 메시지가 들어오는 순간 대기열에서 빠지고 처리완료로 표시된다(서버 쪽 규칙은 11장 「대기 중 메시지 합치기·대체」). 이미 시작된 턴은 막지 못한다. 로그 `[queue] merged N auto msgs → <담당>: #a,#b` · `[queue] msg #N → <담당> dropped: superseded by #M`. 시험 `scripts/test_worker_dispatch.py`.
>
> **부팅 직후 완충 (2026-11-16).** 기계가 막 켜진 뒤(데몬 기동 시 uptime < `boot_grace_uptime_sec`, 기본 600초) 데몬이 시작하면 `boot_grace_sec`(기본 180초) 동안 **자동 턴**(릴레이·하트비트·복구 재개)을 `boot_auto_lanes`(기본 1)개까지만 동시에 돈다. 사람 메시지는 평소대로 바로 간다. 배포 재시작(따뜻한 기계)에는 걸리지 않는다. 배경: 11/15 23:53 커널 패닉 → 자동로그인 → 로컬 사이트 45개·감시 크롬 50개·whisper 4개·업데이터 npm 과 함께 중단된 긴 턴 3개가 즉시 재개돼 2분 뒤 WindowServer 워치독 크래시(16GB 맥미니). 로그 `[queue] boot grace: machine up Ns → auto turns capped at 1 for 180s`. `boot_grace_sec: 0` 이면 끔. 같은 날 정정: 재시작 복구 머리말(`[안내: 직전 턴이 데몬 재시작으로 중단되었습니다…]`)은 분류 전에 벗긴다 — 전엔 복구된 릴레이가 사람 메시지로 분류돼 우선순위·예비 칸·묶기를 건너뛰었다.
>
> **실행 파일 공백 재시도 (2026-11-16).** claude 를 띄울 때 `FileNotFoundError`(업데이터가 npm 으로 CLI 를 교체하는 수십 초 동안 `/opt/homebrew/bin/claude` 심링크가 끊김)가 나면 바로 오류를 돌려주지 않고 3초 간격으로 `claude_spawn_retry_sec`(기본 120초)까지 경로를 다시 찾아 재시도한다(`daemon/utils.popen_retry_missing_binary`). 로그 `[spawn] … missing — updater reinstalling? retrying up to 120s`. 11/10~15 Sean 맥에서 이 공백에 걸린 턴 16건이 `(Claude 실행 오류: [Errno 2] …)` 로 끝났다. **공백이 공백으로 끝나지 않을 때(2026-11-17)**: 실행 파일이 계속 없으면 업데이터가 다음 5분 점검에서 「복구」(아래 CLI 교체 시점 절)를 돌려 다시 깐다. 업데이터가 CLI 를 설치하는 동안(`globals.cli_install_inflight`)에는 이 재시도가 설치가 끝날 때까지(최대 `cli_install_timeout_sec`) 기다린다.

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
| `/advisor <fable\|opus\|sonnet\|off>` | 조언 모델을 **이 대화(프로젝트·브랜치)에만** 켜고 끈다. `/advisor` 만 치면 지금 값 조회 |

#### `/advisor` 는 데몬이 받는다 (2026-11-23)

Claude Code 의 `/advisor <모델>` 은 `-p` 에서도 동작하고 값을 `~/.claude/settings.json`(`advisorModel`)에 저장한다. 그 파일은
그 기기의 모든 세션이 읽으므로 채팅 한 줄이 하트비트를 포함한 전 담당의 턴에 조언 모델을 붙인다(조언 모델이 대화를 읽는 비용은
캐시되지 않는다). 그래서 데몬이 가로챈다.

- **설정·해제**(`/advisor opus`, `/advisor off`)는 CLI 로 보내지 않는다(대소문자 무관). 값은 `~/.claude-daemon/advisor.json` 에 담당 키
  (`프로젝트id` 또는 `branch:N`)별로 저장한다. **상속 없음** — 프로젝트에서 켜도 그 브랜치에는 안 붙는다.
- 그 담당의 턴에만 `--advisor <모델>` 플래그가 붙는다(플래그는 그 세션 한정, 실행 로그 `[claude] project=… advisor=opus`).
  붙이지 않는 턴: 한도 폴백으로 모델을 바꿔 다시 띄운 턴, 팀 세션, 데모, 하트비트 정찰 턴(정찰은 러너가 다르다).
- **조회**(`/advisor`)도 데몬이 답한다(이 담당의 값 + 기기 설정에 남은 값이 있으면 그 경고). CLI 로 보내면 새 세션·브랜치
  첫 턴에서는 맥락이 앞에 붙어 슬래시 명령으로 읽히지 않기 때문이다.
- **예외 하나**: 이 장치 전에 CLI 가 기기 설정(`settings.json` 의 `advisorModel`)에 저장해 둔 값이 남아 있는 기기에서는
  `/advisor off` 를 CLI 로 흘려보낸다 — 그 값은 CLI 만 지울 수 있기 때문이다(데몬은 설정 파일을 읽기만 한다).
- 받는 값은 `fable`·`opus`·`sonnet`·`off` 뿐이다. 전체 모델 id 나 다른 값은 사용법만 답하고 저장하지 않는다
  (CLI 는 `--advisor` 에 모르는 값·`off` 가 오면 rc=1 로 죽는다 — 2.1.330 실측).
- 조언 모델이 본 모델보다 낮으면 CLI 가 경고만 하고 쓰지 않는다(턴은 정상).
- **안전망**: `--advisor` 때문에 CLI 가 뜨지 못하면(구형 CLI 가 플래그를 모름 등) 값을 지우고 플래그 없이 한 번 다시 띄운 뒤,
  답 맨 앞에 「조언 모델 … 을 이 기기의 Claude 가 받지 못해 껐습니다」 한 줄을 붙인다.
- 조언 호출은 도구줄에 `🔧 Advisor` 로 보인다.
- 한계: **클라우드 데몬(`cloud_daemon.py`)에는 이 가로채기가 없다**(러너가 따로다). 터미널에서 직접 `/advisor` 를 치면
  예전처럼 기기 전체에 저장된다 — 되돌리려면 터미널에서 `/advisor off`.
- 시험: `python3 scripts/test_advisor_scope.py`

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
- **브랜치도 `account` 를 가질 수 있다**(2026-11-18, 카드 823): 브랜치 → 부모 브랜치 → 루트 프로젝트 순으로 첫 값. 없으면 지금처럼 루트 계정
- `CLAUDE_CONFIG_DIR` 환경변수로 Claude CLI에 전달
- 계정 변경 시 세션 자동 리셋
- `accounts.<라벨>` 에 `email`(그 폴더의 주인)·`relogin: "terminal"`(채팅 재로그인 대신 터미널 안내만)을 적을 수 있다 — 아래 「팀원 방」 절
- **자동 비우기(`daemon/autoreset.py`, 2시간 주기)** 임계: 수명 48h · 메시지 500 · 전사 10MB · **문맥 25만 토큰**(2026-10-31 추가 — 전사 꼬리의 마지막
  assistant usage = 입력+캐시 쓰기+캐시 읽기. 파일 크기보다 문맥이 비용을 정한다). 컨텍스트 저장이 3회 연속 실패하면 저장 없이 리셋(좀비 세션).
  config: `session_max_context_tokens`(0=끔) · `session_summary_max_failures`.
- **비우기 전 회고(`session_retro`, 2026-10-31)**: 요약 호출 한 번에 「`docs/playbook.md` 에 `## 회고 <날짜> (세션 리셋 전)` 절(잘 된 방법·실패·유저 피드백,
  15줄, 근거 괄호, 대화에 있던 것만) → 인계 요약」을 시킨다. 다음 세션 인계 메모 끝에 플레이북 안내가 붙는다. 끄기 `session_retro.enabled=false`.
- **CLI 자동 압축 창**: config `claude_auto_compact_window`(토큰)를 두면 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 로 Claude Code 가 그 근처에서 같은 세션 안에서
  압축한다(기본 모델 창 1M — 실측 24건 전부 96.5만+에서 압축). 기본 미설정, 값은 Sean 결정.

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
  ↓ 활성 시간대 확인 (active_hours) — 창 밖이면 next_run_at 을 다음 창 시작으로 미루고 로그 1줄
  ↓ max_runs 확인
  ↓ 프로젝트 busy 확인
  ↓
"[heartbeat] HEARTBEAT.md를 확인하고 할 일이 있으면 처리해줘"
  → POST /api/bot/message로 주입 (inject_system_message, api.py)
  ↓
next_run_at = 예정 시각 + interval (깬 시각 기준 아님, 2026-10-21), run_count 증가
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

### Claude·Codex CLI 교체 시점 — `CliUpdatePolicy` (2026-11-16)

매 5분 사이클에 CLI(npm 전역 패키지) 최신 버전도 확인하지만, **설치는 아래 조건에서만** 한다 (`daemon/syncers/auto_updater.py`, 시험 `scripts/test_auto_updater_policy.py`):

- 데몬 시작 뒤 `cli_update_boot_grace_sec`(기본 900초) 안에는 하지 않는다 — 11/15 부팅 32초 뒤 npm 설치·Codex 재설치가 넘친 기계를 더 눌렀다.
- 턴이 도는 중(`globals.turns_inflight > 0`)에는 미룬다. 새 버전을 처음 본 뒤 `cli_update_max_defer_sec`(기본 6시간)가 지나면 그래도 한다. 이유: `npm install -g` 는 옛 패키지를 치운 뒤 새것을 깔아 수십 초 동안 실행 파일이 없다.
- 설치가 실패한 버전은 `cli_update_retry_sec`(기본 6시간) 동안 다시 시도하지 않는다(다른 버전이 나오면 바로). 전엔 같은 버전을 5분마다 283회/일 실패·롤백하며 매번 공백을 만들었다.
- **플랫폼 패키지(`@anthropic-ai/claude-code-darwin-arm64` 등)를 전역에 따로 깔지 않는다.** 본체의 optionalDependencies 로 따라온다. 전역 최상위에 따로 있으면 설치 전에 `npm rm -g` 로 치운다 — 2.1.32x 의 `install.cjs` 는 두 벌이면 "Two copies of the native package" 로 설치를 거절한다(11/10~15 실패 원인). 설치 뒤 실행이 안 되면 공식 처방(본체·플랫폼 둘 다 지우고 재설치)을 한 번만 더 해 보고, 그래도 안 되면 ERROR 로그와 수동 명령을 남긴다.
- **복구는 업데이트와 다른 규칙이다 (2026-11-17).** `claude` 실행 파일이 없거나(`Errno 2`) `--version` 이 두 사이클 연속 실패하면 장애로 보고, 턴 유무·6시간 백오프를 기다리지 않고 **바로 공식 처방으로 다시 깐다**(스트레이 플랫폼 패키지·죽은 npm 의 `.claude-code-XXXX` 찌꺼기·본체를 지우고 `npm i -g @anthropic-ai/claude-code@최신`). 부팅 완충(`cli_update_boot_grace_sec`)과 `cli_repair_retry_sec`(기본 30분, 복구 시도 간격)만 지킨다. 로그 `[updater] Claude CLI binary missing (Errno 2) — repairing now …` → `Claude CLI repaired: 2.1.326`. 배경: 11/15~16 고객 맥 3곳(안형수·박태준·이정욱 테넌트)에서 설치가 아래 타임아웃에 죽어 실행 파일이 사라졌는데, 전 코드는 `claude --version` 실패를 「건너뜀」으로 처리해 30~36시간 동안 아무도 다시 깔지 않았다(사고 기록 `docs/incidents/2026-11-17-claude-cli-missing-npm-timeout.md`).
- **설치 상한은 `cli_install_timeout_sec`(기본 900초, 바닥 300초).** 전엔 180초였고 고객 맥에서는 `npm install -g @anthropic-ai/claude-code` 가 3~5분 걸린다(플랫폼 바이너리 138MB). 상한에 걸려 npm 을 죽이면 옛 패키지는 치워지고 새 패키지의 실행 파일 링크는 안 만들어진 채 남는다 — 그래서 상한을 넉넉히 두고, 데몬 종료 드레인도 설치가 진행 중이면(`globals.cli_install_inflight`) 끝날 때까지 기다린다(자발 종료·재시작 한정. `launchctl stop` 은 plist `ExitTimeOut` 120초 뒤 launchd 가 죽인다). 그래도 죽었으면 위 복구가 30분 안에 다시 깐다.
- Codex 「설치돼 있는데 안 돎 → 재설치」 복구는 부팅 완충 중·첫 관측에서는 하지 않는다(두 사이클 연속 관측 뒤). codex 가 애초에 없는 기계(실행 파일도 npm 전역 목록에도 없음)는 건너뛴다(11/17 — 전엔 매 5분 `Errno 2 'codex'` 경고를 남겼다).
- 로그: `[updater] Claude CLI 2.1.325 available — deferred (3 turn(s) in flight)`(같은 사유는 한 번만) · `… update failed: … — next try for 2.1.325 in 6h` · `removing stray top-level @anthropic-ai/claude-code-darwin-arm64`.

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

## SessionHealthChecker — 세션 건강 리포트 (2시간)

파일: `scripts/daemon/health.py`

> ⚠️ **SessionHealthChecker 의 Stall Detection(30분마다 대화 스니펫을 모아 session-manager 가 미완료 작업을 판단하고 `[stall-check]` nudge 를 보내던 기능)은 2026-07-16 커밋 `f5c69b3` 로 제거됐다.**
> 이유: 매시간 모든 세션의 스니펫을 Claude 에 보내고 연달아 nudge 를 쏴서 워커가 claude 프로세스를 여러 개 동시에 띄웠다.
> 지금 코드에는 `_check_stalls`·`stall_check_report` 가 없다. "대화 맥락을 읽고 깨우는" 자동 복구는 없지만, 워커 스레드 사망 감시·턴 타임아웃 후속 처리 등 다른 자동 복구는 별개로 남아 있다. 유저 대응 절차는 유저 매뉴얼 18장(맥: 데몬 재시작 → 강제 재시작 요청 → 문제 신고 / 클라우드: `데몬 진단` → 문제 신고).

이 스레드에 남은 것은 **2시간 주기 세션 건강 리포트** 다. 초기 대기 30분 후 루프를 시작하고, 60초마다 tick 하며 주기 도달 시 실행한다.

```
2시간마다:
  1. 활성 세션 목록(프로젝트별 메시지 수·미사용 시간·수명) 수집
  2. session-manager 프로젝트에 리포트 전송 (subtype: "session_health_report")
  3. session-manager(Haiku)가 세션 TTL 관리·리셋 제안·좀비 세션 정리를 판단
```

### session-manager 프로젝트

session-manager는 **Haiku 모델**의 Claude Code 세션으로, 세션 건강 관리를 담당한다.

**자동 생성**: `_ensure_session_manager()`가 첫 실행 시 프로젝트 존재 여부를 확인하고, 없으면 자동 생성한다:
1. `GET /api/projects` → session-manager 존재 여부 확인 (결과 캐싱)
2. 없으면 → `POST /api/projects`로 프로젝트 생성
3. `PUT /api/projects`로 모델을 haiku로 설정
4. `PUT /api/prompts`로 세션 관리 프롬프트 자동 주입
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
    },
    "kim": {
      "config_dir": "~/.claude-accounts/kim",
      "email": "kim@example.com",
      "relogin": "terminal"
    }
  },

  ... (나머지 설정)
}
```

- `"secondary"` — 계정 이름(라벨). 프로젝트·브랜치의 `account` 값으로 쓴다. 원하는 이름 사용 가능
- `"config_dir"` — 3단계에서 인증한 폴더 경로
- `"email"` — (선택) 이 폴더의 주인 계정. 값만 적는다(자격증명 아님). 재로그인 안내문에 「누구 계정으로」를 적고, 턴을 띄울 때 폴더의 로그인 계정과 대조해 다르면 그 방에 경고 1회(턴은 돈다)
- `"relogin"` — (선택) `"chat"`(기본, 지금 동작) 또는 `"terminal"`: 그 폴더는 채팅·설정 화면 재로그인을 띄우지 않고 「본인이 터미널에서 로그인하세요」 안내만 보낸다. 팀원 폴더용(아래 절)
- 기본 계정(`~/.claude/`)은 여기에 넣지 않는다 — Claude Code가 알아서 쓰는 경로이므로 추가 계정만 등록
- 저장하면 **재시작 없이** 다음 턴부터 반영된다(2026-11-18부터 config.json 의 `accounts` 를 mtime 으로 다시 읽는다)

#### 5단계: 프로젝트(또는 브랜치)에 계정 라벨 걸기 — API

화면 칸은 없다(2026-11-11 확인 — 설정 모달은 값을 보내기만 하고 「계정 드롭다운」을 그리지 않는다). API 키로 건다:

```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url','https://www.peter-voice.site'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")
# 프로젝트
curl -s -X PATCH "$API_URL/api/projects" -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"id": "willy", "account": "secondary"}'
# 브랜치 (2026-11-18부터 — 값이 없으면 부모 브랜치 → 루트 프로젝트 순으로 상속, 빈 문자열 = 상속으로 되돌림)
curl -s -X PATCH "$API_URL/api/branches/123" -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"account": "kim"}'
```

계정이 바뀐 방은 다음 턴에 세션이 한 번 리셋된다(정상 — 이전 대화 요약이 주입된다). 사용량이 많은 프로젝트를 secondary로 옮기면 된다. 예시:

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
  → 데몬이 계정 라벨을 해석 (브랜치 account → 부모 브랜치 사슬 → 루트 프로젝트 설정, 전부 null이면 "default")
  → config.json의 accounts에서 config_dir 조회 (mtime 감시 — 재시작 없이 반영)
  → CLAUDE_CONFIG_DIR 환경변수를 설정한 채로 Claude CLI 실행
  → Claude CLI가 해당 폴더의 인증 정보를 사용
```

프로젝트의 계정 설정이 바뀌면 데몬이 자동으로:
1. 현재 세션 컨텍스트 저장 (대화 요약)
2. 세션 리셋 — 이전 계정의 세션은 새 계정에서 무효
3. 다음 메시지부터 새 계정으로 새 세션 시작

### 웹 UI

- 계정 라벨 칸은 화면에 없다 — 프로젝트·브랜치 모두 위 5단계의 API 로 건다(수요가 보이면 화면 칸을 함께 만든다)
- 프로젝트 목록에서 계정이 지정된 프로젝트는 보라색 뱃지 표시
- 브랜치는 자기 `account` 가 없으면 부모(브랜치 사슬 → 루트 프로젝트)의 계정을 따른다

### 팀원 방 하나 열기 — 「그 폴더의 로그인은 그 사람 것」 (2026-11-18, 결정 카드 823 기본값 A)

팀 고객의 팀원 방은 브랜치다. 팀원마다 **자기 계정 폴더**를 두고, 그 폴더의 로그인은 본인이 터미널에서 한다(채팅 재로그인 링크를 쓰지 않는다 — Claude 약관 준수 판정 ②·§3(d)). 설정이 없는 폴더·고객은 아무것도 바뀌지 않는다.

1. 소유자 맥에서 폴더를 만들고, 팀원이 **그 자리에서 자기 브라우저로** 로그인한다:
   `mkdir -p ~/.claude-accounts/<이름>` → `CLAUDE_CONFIG_DIR=~/.claude-accounts/<이름> claude auth login`
2. `config.json` 의 `accounts.<이름>` 에 `config_dir`·`email`·`"relogin": "terminal"` 을 적는다(4단계 예시의 `kim`). 재시작 불필요
3. 팀원 방(브랜치)에 라벨을 건다: `PATCH /api/branches/{id}` body `{"account": "<이름>"}` (5단계). 그 방의 세션이 1회 리셋된다(정상). 방을 루트 프로젝트로 따로 만들었다면 `PATCH /api/projects` 의 `account`
4. 팀원은 그 방에서만 말한다. 소유자·다른 담당자는 그 방에 하트비트·릴레이·칸반을 걸지 않는다(안내로 지킨다, 코드 차단 없음)
5. 약 29일 뒤 로그인이 만료되면 데몬이 그 방에 **터미널 안내문**을 보낸다(링크 없음, 데몬 로그 `[relogin] … terminal-only`). 팀원이 1의 명령을 다시 실행하고 방에 말을 걸면 이어진다. 「로그인」「재로그인」을 입력해도 같은 안내만 온다
6. 그 폴더에 다른 사람 계정이 로그인돼 있으면(email 과 다름) 턴은 돌되 방에 경고가 1회 온다 — 맞는 계정으로 다시 로그인하면 사라진다

되돌리기: `accounts.<이름>` 의 `relogin` 칸을 지우면 그 폴더는 채팅 재로그인(지금 동작)으로, 브랜치 `account` 를 `""` 로 PATCH 하면 부모 계정으로 돌아간다.
한계: 팀원 폴더의 로그인 만료일은 사용량 보드(`login_expires_at`)에 안 잡힌다(기본 계정만 잼). 계획서: peter-voice `docs/plans/2026-11-11-team-member-folder-terminal-login.md`.

### 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| 계정 전환 후 이전 대화 맥락이 없다 | 정상 — 계정이 바뀌면 세션이 리셋됨 | 이전 대화 요약이 자동 주입되므로 맥락은 유지됨 |
| "authentication required" 에러 | config_dir 폴더에서 인증을 안 했음 | 3단계 다시 수행: `CLAUDE_CONFIG_DIR=... claude` |
| 라벨을 걸었는데 기본 계정으로 돈다 | config.json `accounts.<라벨>` 이 없거나 라벨 오타 | 4단계 확인(저장 뒤 다음 턴부터 반영, 재시작 불필요). 로그 `account=<라벨>` 로 확인 |
| 팀원 방에 재로그인 링크가 안 오고 터미널 안내만 온다 | 정상 — 그 폴더가 `relogin: "terminal"` | 본인이 터미널에서 `CLAUDE_CONFIG_DIR=… claude auth login` |
| 「다른 계정이 로그인돼 있다」 경고가 방에 온다 | 폴더의 로그인 계정 ≠ `email` | 그 폴더에서 맞는 계정으로 다시 로그인(턴은 그동안도 돈다) |
| 계정을 바꿨는데 반영이 안 된다 | — | 다음 메시지부터 반영됨 (설정은 매번 fetch) |
| 두 계정 다 한도가 찼다 | 주간 리셋 대기 | 리셋까지 대기, 또는 3번째 계정 추가 |
| config_dir 경로에 오타 | 폴더를 못 찾아서 인증 실패 | `ls ~/.claude-account-b/.claude/` 로 폴더 존재 확인 |

---

## launchd 관리

### 시작/중지

launchd 라벨은 설치 세대에 따라 다르다: **고객 설치본 = `com.petervoice.daemon`** (install.sh·willy-install.sh·onboarding_daemon.py 기준), 일부 개발 머신(Sean·Joory) = `com.petervoice.claude-daemon`. `launchctl list | grep petervoice`로 확인.

```bash
# 데몬 시작 (라벨에 맞는 plist 사용)
launchctl load ~/Library/LaunchAgents/com.petervoice.daemon.plist

# 데몬 중지 (10초 내 자동 재시작) — 라벨 폴백 포함
launchctl stop com.petervoice.daemon 2>/dev/null || launchctl stop com.petervoice.claude-daemon

# 지연 재시작 (현재 응답 완료 후)
(sleep 5 && (launchctl stop com.petervoice.daemon 2>/dev/null || launchctl stop com.petervoice.claude-daemon)) &
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
