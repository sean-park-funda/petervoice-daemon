# 32. 엔진 선택 (Claude Code / Codex / Gemini)

## 개요

Peter Voice 데몬은 **Claude Code CLI**, **OpenAI Codex CLI**, 그리고 2026-10-14부터 **Google Gemini CLI**(제3 엔진, 파일럿) 세 가지 엔진을 지원합니다.  
프로젝트별 또는 브랜치별로 엔진을 다르게 설정할 수 있습니다.

## 엔진 종류

| 엔진 | CLI | 모델 기본값 | 적합한 경우 |
|------|-----|-----------|-----------|
| `claude` | `@anthropic-ai/claude-code` | config `claude_model`, 없으면 `claude-opus-5-5`(러너 폴백) | 기본값, 코드 이해/수정 |
| `codex` | `@openai/codex` | OpenAI 기본값 | 맥 데몬 전용. 웹은 클라우드 유저의 새 프로젝트에 `codex` 를 기본값으로 저장하지만(2026-06-10 a7b37d6) **클라우드 데몬은 engine 을 보지 않고 항상 Claude 로 실행**한다 — 설정값과 실제 엔진이 다름(2026-10-15 확인) |
| `gemini` | `@google/gemini-cli` | gemini-4-argon | 맥미니(셀프호스팅) 전용 파일럿. 클라우드 데몬은 미지원 |

## 설정 방법

### 프로젝트 설정

웹 UI 프로젝트 설정 → **Engine** 드롭다운:
- `claude` (기본)
- `codex`

또는 API로 직접 설정:
```bash
curl -X PATCH "$API_URL/api/projects" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id": "프로젝트_ID", "engine": "codex"}'
```

### 브랜치별 override

브랜치는 부모 프로젝트 엔진을 상속하지만, 개별 브랜치에 다른 엔진을 지정할 수 있습니다.  
설정 우선순위: **브랜치 engine** > 프로젝트 engine > 기본값(`claude`)

### config.json 글로벌 기본값

```json
{
  "codex_default_model": "codex-mini-latest"
}
```

`codex_default_model`이 설정되면 Codex 엔진 사용 시 해당 모델을 기본으로 사용합니다.

## 클라우드 유저 기본값

`provision_type = "cloud"`인 유저는 **새 프로젝트 생성 시 engine이 자동으로 `codex`**로 설정됩니다.

## 엔진별 동작 차이

### Claude Code CLI (`run_claude`)
- 세션 ID: `claude --resume {session_id}`로 대화 이어가기
- 출력: 스트리밍 JSONL 이벤트 파싱
- 툴 사용 로그 포함

### Codex CLI (`run_codex`)
- 세션 ID: Codex 세션 UUID
- 출력: JSONL result 이벤트 파싱
- OPENAI_API_KEY 환경변수 필요
- **지침(시스템 가이드·공통·역할·세션 맥락) 전달 = stdin** (2026-11-23, 아래 절)

#### Codex 지침은 프로젝트 폴더에 쓰지 않는다 (2026-11-23)

예전에는 턴마다 전체 프롬프트를 프로젝트 폴더 `AGENTS.md` 의 표지 블록
(`<!-- PETERVOICE-DAEMON-PROMPT-START -->` ~ `END`)에 썼다. 디스크에 남은 스냅샷이 다른 세션·지식 그래프·git 으로 새고
(2026-09-19 사고), 같은 폴더를 쓰는 브랜치끼리 서로 덮어썼다. 지금은 제미나이와 같이 **stdin 으로 대화에 넣는다.**

| 턴 | 지침 | 실행 모양 | 로그 `ctx=` |
|---|---|---|---|
| 새 세션 첫 턴 | 넣음 (세션 맥락 포함) | `codex exec … -` + stdin | `new` |
| 재개, 지침 그대로 | 안 넣음 (세션이 기억) | `codex exec … resume <sid> "<메시지>"` | `same` |
| 재개, 표식 없는 세션 (AGENTS.md 시절에 만든 세션) | 넣음 | `… resume <sid> -` + stdin | `legacy` |
| 재개, 지침이 바뀜 + 마지막 주입 뒤 6시간 지남 | 넣음 (「앞선 지침을 대체」 머리말) | `… resume <sid> -` + stdin | `refresh` |
| 재개, 지침이 바뀜 + 6시간 안 됨 | 안 넣음 | argv | `throttled` |

- stdin 덩어리: 머리말 한 줄 + `<petervoice_system_instructions>…</petervoice_system_instructions>` + 유저 메시지.
  지침은 argv 에 실리지 않는다(`ps` 로 안 보인다).
- 「지침이 바뀌었나」는 세션 맥락을 뺀 본문의 해시로 본다. 표식은 `~/.claude-daemon/codex_sessions.json` 의
  `ctx_hash`·`ctx_fed_at` — **턴이 끝까지 돈(`turn.completed`) 경우에만** 남긴다(깨진 턴이면 다음 턴에 다시 넣는다).
- 최소 간격은 config `codex_ctx_refresh_min_sec`(기본 21600). 0 이면 바뀐 다음 턴에 바로 넣는다. 급하면 `/reset`.
- **예전 블록은 걷어낸다.** 코덱스는 실행할 때마다(resume 포함) 작업 폴더의 `AGENTS.md` 를 다시 읽는다(0.171 실측) —
  블록을 남기면 낡은 지침을 끝없이 읽는다. 그래서 코덱스 턴을 띄우기 직전에 **데몬 표지 블록만** 지운다. 유저가 직접 쓴 내용과
  줄바꿈 방식은 그대로 두고, 블록을 빼면 빈 파일이 될 때만 파일을 지운다. 표지는 줄 전체가 표지일 때만 인정하고(본문에서 언급만 한
  파일은 안 건드림), 끝 표지가 없으면 지우지 않고 로그에 `has no end marker` 만 남긴다. 지우지 못하면(읽기 전용 등) 로그에
  `Failed to strip daemon block` — 그 폴더의 코덱스는 낡은 블록과 새 지침을 함께 읽으니 손으로 블록을 지운다.
  코덱스를 돌리지 않는 폴더의 옛 `AGENTS.md` 는 건드리지 않는다.
- 한계: 코덱스가 긴 대화를 자동 압축하면 stdin 으로 넣은 지침이 요약될 수 있다(제미나이와 같음) → `/reset`.
- 시험: `python3 scripts/test_codex_stdin_context.py`

## 자동 업데이트

`AutoUpdater`가 5분마다 두 CLI 모두 최신 버전으로 자동 업데이트합니다:

- **Claude Code**: `npm install -g @anthropic-ai/claude-code@{latest}`
  - Apple Silicon(arm64)에서는 네이티브 패키지(`@anthropic-ai/claude-code-darwin-arm64`) 먼저 설치
- **Codex**: `npm install -g @openai/codex@{latest}`
  - `codex` 명령이 없으면 (미설치) 업데이트 건너뜀

업데이트 로그:
```
[updater] Updating Claude CLI: 1.2.3 → 1.3.0
[updater] Updating Codex CLI: 0.8.1 → 0.9.0
```

## 주의사항

- Codex 엔진 사용 시 `OPENAI_API_KEY` 환경변수가 설정되어 있어야 합니다
- Codex CLI가 설치되지 않은 경우 해당 프로젝트 메시지는 오류 처리됨
- 엔진 전환은 다음 메시지부터 적용됩니다 (현재 진행 중인 세션에는 미적용)

## Gemini CLI (제3 엔진, 2026-10-14~ 파일럿)

- 설정: 프로젝트/브랜치의 `engine` 을 `gemini` 로. 웹 UI 드롭다운에는 아직 없어 API 로 설정한다 —
  `PATCH /api/projects {"id":"…","engine":"gemini","model":"gemini-4-argon"}` 또는 `PATCH /api/branches/{id}` 같은 body.
- 필요: `npm install -g @google/gemini-cli` + 시크릿 `GEMINI_API_KEY`. 둘 중 하나가 없으면 그 턴에 안내 메시지로 답하고 끝난다.
- 모델: 브랜치 `model` > 프로젝트 `model` > config `gemini_default_model` > 기본 `gemini-4-argon`. `gemini-` 로 시작하지 않는 코드(옛 claude/gpt 잔재)는 무시.
- 세션: `~/.claude-daemon/gemini_sessions.json` 에 프로젝트/브랜치별 UUID, 다음 턴은 `--resume <uuid>`. `/reset`(`/새세션`)으로 초기화. 세션 파일이 사라져 재개에 실패하면 자동으로 새 세션 1회 재시도.
- 컨텍스트 주입: 시스템 가이드·공통·역할·세션 맥락을 **프로젝트 폴더에 파일로 쓰지 않고** 새 세션 첫 턴에 stdin 으로 넘긴다(코덱스도 2026-11-23 부터 같은 방식). 재개 턴은 메시지만 보낸다 → 세션이 살아 있는 동안의 프롬프트 수정은 `/reset` 뒤에 반영된다.
- 출력: `--output-format stream-json` 을 줄 단위로 읽어 도구 로그(🔧)와 본문을 웹에 스트리밍한다.
- 스킬: Gemini CLI 는 에이전트 스킬 표준(SKILL.md)을 `~/.gemini/skills/`·`~/.agents/skills/`·워크스페이스 `.gemini/skills/` 에서 읽는다. 피터보이스 스킬을 쓰려면 `~/.gemini/skills/<name> → ~/.claude/skills/<name>` 심링크.
- 상태: 하트비트 payload 의 `gemini_available`(설치+키, 5분 캐시).
- 자동 업데이트 대상 아님 — `AutoUpdater` 는 Claude·Codex 만 갱신한다. 검증된 버전은 0.55.1.
- 실행 로그 예: `Gemini: project=branch:1024, dir=…, session=new, model=gemini-4-argon, context=stdin 61234B`

