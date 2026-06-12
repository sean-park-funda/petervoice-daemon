# 07. 인증 및 멀티유저

## 인증 방식

### 1. 세션 쿠키 (웹 UI)

```
로그인: POST /api/auth/login
  ↓ bcrypt 비밀번호 검증
  ↓ HMAC-SHA256 세션 토큰 생성
  ↓ httpOnly 쿠키 설정 (peter_voice_session, 30일)
  ↓ 이후 모든 API 요청에 쿠키 자동 포함
```

- 토큰 페이로드: `{userId, username, role, iat}`
- 서명: `SESSION_SECRET` 환경변수 (HMAC-SHA256)
- secure 플래그: production에서만

### 2. API 키 (데몬)

```
데몬 요청: X-Api-Key: <users.api_key>
  ↓ getBotUserFromRequest()
  ↓ users 테이블에서 api_key 매칭
  ↓ 유저 정보 반환
```

- API 키 형식: `pv_` 접두사 + 랜덤 문자열
- 캐시: 5분 TTL (DB 조회 최소화)

### 3. Bearer 토큰 (일부 구 API)

```
Authorization: Bearer <api_key>
  ↓ verifyApiKey()
  ↓ 5분 캐시
```

## 인증 함수 (`lib/auth.ts`)

| 함수 | 용도 |
|------|------|
| `getUserFromRequest(req)` | 쿠키에서 유저 추출 |
| `getBotUserFromRequest(req)` | X-Api-Key에서 유저 추출 |
| `getAnyUserFromRequest(req)` | 쿠키 또는 API 키 (둘 다 시도) |
| `authenticateUser(username, password)` | 로그인 검증 |
| `createSessionToken(user)` | 세션 토큰 생성 |
| `parseSessionToken(token)` | 토큰 파싱/검증 |

## OAuth 연동

### Google OAuth 2.0

```
설정 → "Google 연결" 클릭
  ↓ GET /api/auth/google → Google 동의 화면
  ↓ 사용자 승인
  ↓ GET /api/auth/google/callback
  ↓ authorization_code → access_token + refresh_token
  ↓ oauth_tokens 테이블 저장
  ↓ 이메일 정보도 extra 필드에 저장
```

- 스코프: `calendar.readonly`, `userinfo.email`
- refresh_token으로 access_token 자동 갱신
- Vercel 환경변수: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`

### Notion OAuth

```
설정 → "Notion 연결" 클릭
  ↓ GET /api/auth/notion → Notion 동의 화면
  ↓ 사용자 승인
  ↓ GET /api/auth/notion/callback
  ↓ authorization_code → permanent access_token
  ↓ oauth_tokens 테이블 저장
  ↓ workspace_name, bot_id도 extra 필드에 저장
```

- Notion은 refresh_token 없음 (영구 access_token)
- Vercel 환경변수: `NOTION_OAUTH_CLIENT_ID`, `NOTION_OAUTH_CLIENT_SECRET`

### 연결 상태 확인

```
GET /api/auth/connections
→ { google: { connected, email }, notion: { connected, workspace_name } }
```

### 연결 해제

```
DELETE /api/auth/google  또는  DELETE /api/auth/notion
→ oauth_tokens에서 삭제
```

## 멀티유저 아키텍처

### 데이터 격리

- **모든 테이블에 user_id**: 유저별 데이터 완전 분리
- **RLS**: oauth_tokens, tasks, skills 테이블에 적용
- **API 레벨 필터링**: 나머지 테이블은 코드에서 user_id 필터

### 유저당 데몬

```
유저 A (Mac Mini) → claude_daemon.py (api_key=pv_aaa)
유저 B (VPS)      → claude_daemon.py (api_key=pv_bbb)
```

- 각 유저는 자체 머신에서 데몬 실행
- 동일 Vercel 앱 + Supabase 공유
- api_key로 유저 식별

### 관리자 기능

```
role='admin' 유저만:
  GET  /api/admin/dashboard  — 전체 유저/프로젝트/상태 대시보드
  POST /api/admin/users      — 유저 생성
  DELETE /api/admin/users    — 유저 삭제
  PATCH /api/admin/prompts   — 모든 유저의 프롬프트 수정
```

## 인증 매트릭스

| API 경로 | 쿠키 | API 키 | 관리자 |
|----------|------|--------|--------|
| /api/auth/* | ✅ | - | - |
| /api/messages | ✅ | ✅ | - |
| /api/projects | ✅ | - | - |
| /api/secrets | ✅ | ✅ (raw) | - |
| /api/documents | ✅ | ✅ | - |
| /api/todos | ✅ | ✅ | - |
| /api/tasks | ✅ | ✅ | - |
| /api/files/upload | ✅ | ✅ | - |
| /api/bot/* | - | ✅ | - |
| /api/relay/message | ✅ | ✅ | - |
| /api/calendar | ✅ | - | - |
| /api/stt, /api/tts | ✅ | - | - |
| /api/admin/* | ✅ | - | ✅ |

## 플랫폼 인증 설계

### 자격증명 구분

| 종류 | 저장 위치 | 범위 |
|------|-----------|------|
| 앱 자격증명 (CLIENT_ID/SECRET) | Vercel 환경변수 | 전체 앱 공통 |
| 유저 OAuth 토큰 | DB oauth_tokens | 유저별 |
| 유저 환경변수 | DB secrets | 유저별 |

### 데몬으로의 전달

```
GET /api/secrets?raw=true (API 키 인증)
  → 유저 시크릿 + OAuth 토큰 (환경변수 형태)
  → 데몬이 os.environ에 주입
  → Claude Code가 환경변수로 접근
```

OAuth 토큰도 환경변수로 변환되어 전달:
- `GOOGLE_REFRESH_TOKEN` → 캘린더 스킬에서 사용
- `NOTION_ACCESS_TOKEN` → 노션 스킬에서 사용
