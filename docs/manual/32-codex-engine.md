# 32. 엔진 선택 (Claude Code vs Codex)

## 개요

Peter Voice 데몬은 **Claude Code CLI**와 **OpenAI Codex CLI** 두 가지 엔진을 지원합니다.  
프로젝트별 또는 브랜치별로 엔진을 다르게 설정할 수 있습니다.

## 엔진 종류

| 엔진 | CLI | 모델 기본값 | 적합한 경우 |
|------|-----|-----------|-----------|
| `claude` | `@anthropic-ai/claude-code` | claude-sonnet-4-6 | 기본값, 코드 이해/수정 |
| `codex` | `@openai/codex` | OpenAI 기본값 | 클라우드 유저 기본값 |

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
