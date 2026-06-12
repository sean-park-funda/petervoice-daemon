# 16. 에이전트용 API 레퍼런스

각 프로젝트의 Claude 에이전트가 피터보이스 API를 직접 호출하여 사용할 수 있는 기능 모음.

## 인증

모든 API 요청에 `X-Api-Key` 헤더 필요:

```bash
API_URL=$(python3 -c "import json; c=json.load(open('/Users/sean/.claude-daemon/config.json')); print(c.get('api_url', 'https://peter-voice.vercel.app'))")
API_KEY=$(python3 -c "import json; print(json.load(open('/Users/sean/.claude-daemon/config.json'))['api_key'])")
```

```bash
curl -s "$API_URL/api/..." -H "X-Api-Key: $API_KEY"
```

---

## 1. 대화 검색

과거 대화 기록에서 키워드로 검색. Full-Text Search 지원.

### 현재 프로젝트 대화 검색

```bash
curl -s "$API_URL/api/messages/search?q=검색어&project=프로젝트ID" \
  -H "X-Api-Key: $API_KEY"
```

### 전체 프로젝트 대화 검색

```bash
curl -s "$API_URL/api/messages/search?q=검색어" \
  -H "X-Api-Key: $API_KEY"
```

### 파라미터

| 파라미터 | 필수 | 기본값 | 설명 |
|---------|------|-------|------|
| `q` | O | - | 검색어 (공백 구분 → AND 조건) |
| `project` | X | 전체 | 프로젝트 ID |
| `limit` | X | 30 | 결과 수 (최대 100) |
| `offset` | X | 0 | 페이지네이션 오프셋 |

### 응답

```json
{
  "messages": [
    {
      "id": 123,
      "type": "user",
      "text": "메시지 내용...",
      "project": "cocktail",
      "created_at": "2026-03-20T09:00:00Z"
    }
  ],
  "total": 42
}
```

---

## 2. 에이전트 간 통신 (Relay)

다른 프로젝트의 에이전트에게 메시지 전달.

```bash
curl -X POST "$API_URL/api/relay/message" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "to_project": "대상 프로젝트ID",
    "from_project": "내 프로젝트ID",
    "text": "전달할 메시지",
    "attachments": ["/절대/경로/파일.md"]
  }'
```

### 사용 가능한 프로젝트 목록

**하드코딩 금지** — `GET /api/projects`로 동적 조회할 것. 프로젝트는 수시로 추가/삭제됨.

---

## 3. 파일 업로드 (로컬 → 웹 링크)

로컬 파일을 업로드하여 공유 가능한 웹 링크 생성.

```bash
curl -s -X POST "$API_URL/api/files/upload" \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@/경로/파일.png"
```

### 응답

```json
{
  "url": "https://...supabase.co/storage/v1/object/public/...",
  "name": "파일.png",
  "size": 12345,
  "type": "image/png"
}
```

> 50MB 제한. 반환된 URL을 유저에게 공유.

---

## 4. 하트비트 태스크 (자율 반복)

### 태스크 등록

```bash
curl -X POST "$API_URL/api/tasks" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"project":"프로젝트ID","interval_min":30,"max_runs":20}'
```

### 태스크 조회

```bash
curl -s "$API_URL/api/tasks?project=프로젝트ID" \
  -H "X-Api-Key: $API_KEY"
```

### 태스크 완료 처리

```bash
curl -X PATCH "$API_URL/api/tasks/TASK_ID" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status":"done"}'
```

---

## 5. 시크릿/환경변수 조회

데몬이 OS에 싱크하는 환경변수를 API로도 조회 가능.

```bash
curl -s "$API_URL/api/secrets?raw=true" \
  -H "X-Api-Key: $API_KEY"
```

> `raw=true`는 X-Api-Key 인증 시에만 동작. 마스킹 없이 값 전체 + OAuth 토큰 포함.

---

## 6. 프로젝트 브랜치 생성

현재 대화에서 새 프로젝트를 분기.

```bash
curl -X POST "$API_URL/api/projects/branch" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "new-project-id",
    "name": "새 프로젝트",
    "session_summary": "현재 대화 요약",
    "parent_project": "원본 프로젝트ID"
  }'
```

---

## 7. 프로젝트 목록 조회

```bash
curl -s "$API_URL/api/projects" \
  -H "X-Api-Key: $API_KEY"
```

> `getAnyUserFromRequest()` 사용 — 세션 쿠키와 X-Api-Key 모두 지원.

---

## 요약 — 에이전트가 할 수 있는 것

| 기능 | API | 인증 |
|------|-----|------|
| 대화 검색 (프로젝트별/전체) | `GET /api/messages/search` | X-Api-Key |
| 에이전트 간 메시지 | `POST /api/relay/message` | X-Api-Key |
| 파일 업로드 | `POST /api/files/upload` | Bearer or X-Api-Key |
| 하트비트 태스크 | `GET/POST/PATCH /api/tasks` | X-Api-Key |
| 시크릿 조회 | `GET /api/secrets?raw=true` | X-Api-Key |
| 프로젝트 브랜치 | `POST /api/projects/branch` | X-Api-Key |
