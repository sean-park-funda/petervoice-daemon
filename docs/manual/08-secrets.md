# 08. 환경변수 및 시크릿

## 개요

유저별 환경변수를 웹 UI에서 관리하고, 데몬이 로컬 OS 환경변수로 싱크하여 Claude Code가 사용할 수 있게 하는 시스템.

## 구성 요소

```
웹 UI (SecretsPanel)
  ↓ POST/PUT/DELETE /api/secrets
Supabase secrets 테이블
  ↓ SecretsSyncer (60초마다 폴링)
~/.claude-daemon/.env.secrets
  ↓ os.environ 주입
Claude Code CLI 서브프로세스
```

## 웹 UI — SecretsPanel

`components/SecretsPanel.tsx`

### 기능

- 카테고리별 그룹 표시: Google, Notion, API, Custom
- 값 마스킹: 기본 `****` 표시, 클릭으로 원문 표시
- 새 시크릿 추가: 키(자동 대문자), 카테고리, 값, 메모
- 수정/삭제

### 카테고리

| 카테고리 | 용도 | 예시 키 |
|----------|------|---------|
| google | Google API 관련 | GOOGLE_REFRESH_TOKEN |
| notion | Notion API 관련 | NOTION_ACCESS_TOKEN |
| api | 외부 서비스 API 키 | FAL_KEY, GEMINI_API_KEY |
| custom | 기타 | 유저 정의 |

## API 엔드포인트

### `GET /api/secrets`

**쿠키 인증 (웹 UI)**:
- 값 마스킹 (앞 4자리 + **** + 뒤 4자리)

**API 키 인증 + `?raw=true` (데몬)**:
- 원본 값 반환
- OAuth 토큰도 환경변수 형태로 포함:
  - `GOOGLE_REFRESH_TOKEN`, `GOOGLE_ACCESS_TOKEN`
  - `NOTION_ACCESS_TOKEN`, `NOTION_API_TOKEN`
  - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (앱 자격증명)

### `POST /api/secrets` — 생성

```json
{ "key": "FAL_KEY", "value": "xxx", "category": "api", "memo": "fal.ai API" }
```

### `PUT /api/secrets` — 수정

```json
{ "id": 123, "value": "new_value" }
```

### `DELETE /api/secrets` — 삭제

```json
{ "id": 123 }
```

## 데몬 싱크 — SecretsSyncer

### 동작

```python
# 60초마다 실행
def sync_once():
    response = GET /api/secrets?raw=true
    secrets = response.json()  # [{key, value}, ...]

    # .env.secrets 파일 작성 (퍼미션 0o600)
    with open('.env.secrets', 'w') as f:
        for s in secrets:
            f.write(f'{s["key"]}={escape(s["value"])}\n')

    # os.environ에 주입
    for s in secrets:
        os.environ[s['key']] = s['value']
```

### 파일 포맷

```bash
# ~/.claude-daemon/.env.secrets
FAL_KEY=fal-xxxx
GEMINI_API_KEY=AIzaSy...
GOOGLE_REFRESH_TOKEN=1//0xxx...
NOTION_ACCESS_TOKEN=secret_xxx...
```

- 퍼미션: `0o600` (소유자만 읽기/쓰기)
- 특수 문자 이스케이프 처리

## 시스템 프롬프트 연동

데몬이 Claude Code에 전달하는 `_common` 프롬프트에 사용 가능한 환경변수 키 목록이 동적으로 삽입됨:

```
## 사용 가능한 환경변수
- FAL_KEY
- GEMINI_API_KEY
- GOOGLE_REFRESH_TOKEN
- NOTION_ACCESS_TOKEN
...
```

Claude는 키 목록만 보고, 실제 값은 `os.environ`에서 읽음.

## OAuth 토큰 vs 범용 시크릿

| 구분 | 저장 위치 | 관리 | 데몬 접근 |
|------|-----------|------|-----------|
| 범용 시크릿 | secrets 테이블 | SecretsPanel UI | os.environ |
| OAuth 토큰 | oauth_tokens 테이블 | OAuth 연결 UI | /api/secrets?raw=true → os.environ |
| 앱 자격증명 | Vercel 환경변수 | Vercel 대시보드 | /api/secrets?raw=true → os.environ |

→ 데몬 입장에서는 모두 동일하게 환경변수로 접근 가능
