# 32. 엔진 선택 (Claude Code / Codex / Gemini)

## 개요

Peter Voice 데몬은 **Claude Code CLI**, **OpenAI Codex CLI**, 그리고 2026-10-14부터 **Google Gemini CLI**(제3 엔진, 파일럿) 세 가지 엔진을 지원합니다.  
프로젝트별 또는 브랜치별로 엔진을 다르게 설정할 수 있습니다.

## 엔진 종류

| 엔진 | CLI | 모델 기본값 | 적합한 경우 |
|------|-----|-----------|-----------|
| `claude` | `@anthropic-ai/claude-code` | claude-sonnet-4-6 | 기본값, 코드 이해/수정 |
| `codex` | `@openai/codex` | OpenAI 기본값 | 클라우드 유저 기본값 |
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
- 컨텍스트 주입: 시스템 가이드·공통·역할·세션 맥락을 **프로젝트 폴더에 파일로 쓰지 않고** 새 세션 첫 턴에 stdin 으로 넘긴다(코덱스 AGENTS.md 와 다른 점). 재개 턴은 메시지만 보낸다 → 세션이 살아 있는 동안의 프롬프트 수정은 `/reset` 뒤에 반영된다.
- 출력: `--output-format stream-json` 을 줄 단위로 읽어 도구 로그(🔧)와 본문을 웹에 스트리밍한다.
- 스킬: Gemini CLI 는 에이전트 스킬 표준(SKILL.md)을 `~/.gemini/skills/`·`~/.agents/skills/`·워크스페이스 `.gemini/skills/` 에서 읽는다. 피터보이스 스킬을 쓰려면 `~/.gemini/skills/<name> → ~/.claude/skills/<name>` 심링크.
- 상태: 하트비트 payload 의 `gemini_available`(설치+키, 5분 캐시).
- 자동 업데이트 대상 아님 — `AutoUpdater` 는 Claude·Codex 만 갱신한다. 검증된 버전은 0.55.1.
- 실행 로그 예: `Gemini: project=branch:1024, dir=…, session=new, model=gemini-4-argon, context=stdin 61234B`

