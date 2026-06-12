# 19. 프롬프트 관리

## 개요

에이전트의 동작을 결정하는 시스템 프롬프트는 Supabase `prompts` 테이블에 저장된다.
PK는 `(user_id, project)` — 유저별, 프로젝트별로 분리.

---

## 프롬프트 종류

| project 값 | 이름 | 용도 |
|-------------|------|------|
| `_petervoice_system` | 플랫폼 프롬프트 | 모든 유저에게 동일한 플랫폼 지식 (user_id=0) |
| `_common` | 공통 프롬프트 | 유저별 공통 적용 (역할, 환경, 규칙) |
| `peter-voice` 등 | 프로젝트 프롬프트 | 프로젝트별 고유 맥락 (경로, URL, 메모) |

## 주입 순서

데몬이 Claude에게 보내는 시스템 프롬프트:

```
1. sys_prompt            — 빌드 타임 컨텍스트 (task 정보)
2. _petervoice_system    — 플랫폼 시스템 프롬프트 (user_id=0, 모든 유저 공통)
3. _common               — 유저별 공통 프롬프트
4. project prompt        — 프로젝트 프롬프트
5. session_context       — 이전 세션 요약 (세션 만료 시)
```

코드: `scripts/daemon/claude_runner.py`의 `run_claude()` 함수

---

## API

### 웹 API (권장)

모든 프롬프트 읽기/쓰기는 웹 API를 사용할 것. user_id가 인증 토큰에서 자동 결정됨.

```bash
API_URL="https://peter-voice.vercel.app"
API_KEY=$(python3 -c "import json; print(json.load(open('~/.claude-daemon/config.json'))['api_key'])")

# 읽기
curl -s "$API_URL/api/prompts?project=_common" \
  -H "Authorization: Bearer $API_KEY"

# 쓰기
curl -s -X PUT "$API_URL/api/prompts" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"project": "_common", "content": "..."}'
```

- Cookie 인증 (웹 UI) 또는 Bearer 토큰 (데몬/에이전트) 모두 지원
- `getAnyUserFromRequest()`로 인증 → user_id 자동 분리

### 어드민 API

관리자가 다른 유저의 프롬프트를 수정할 때:

```
PATCH /api/admin/prompts
Body: { "user_id": 6, "project": "_common", "content": "..." }
```

### UI

프로젝트 설정 모달 → "프롬프트" / "공통 프롬프트" 탭에서 편집 가능.

---

## 멀티유저 프롬프트 분리

### 현재 구조

각 유저가 자신만의 `_common`과 프로젝트 프롬프트를 가짐:

```
prompts 테이블:
  (user_id=1, project="_common")  → Sean의 공통 프롬프트
  (user_id=6, project="_common")  → Willy의 공통 프롬프트
  (user_id=1, project="peter-voice") → Sean의 PV 프롬프트
  (user_id=6, project="크루어리워크스")  → Willy의 프롬프트
```

### 주의: Supabase 직접 접근 금지

**버그 이력**: 에이전트가 Supabase service_role key로 프롬프트를 PATCH할 때 `user_id` 필터 없이 `project=eq._common`만 사용 → **모든 유저의 _common이 Sean 내용으로 덮어씌워지는 버그** 발생.

**규칙**:
- 프롬프트 읽기/쓰기는 반드시 **웹 API**(`/api/prompts`)를 사용
- Supabase REST API를 직접 사용하지 말 것
- 웹 API는 Bearer 토큰에서 user_id를 자동 추출하므로 유저 간 충돌 불가

### 신규 유저

현재 신규 유저 생성 시 `_common` 프롬프트가 자동 생성되지 않음. 첫 접속 시 빈 상태. 어드민이 수동으로 세팅하거나, 유저가 UI에서 직접 작성해야 함.

---

## 프롬프트 아키텍처 — 3-Layer 구조

현재 3레이어로 분리 구현됨:

| 레이어 | project 값 | 유저별? | 설명 |
|--------|-----------|---------|------|
| 플랫폼 | `_petervoice_system` | X (user_id=0) | 릴레이 API, 칸반 API, 데몬 규칙 등 |
| 유저 공통 | `_common` | O | 머신 스펙, 환경변수, 응답 스타일 |
| 프로젝트 | `{project}` | O | 프로젝트별 맥락 (경로, URL, 메모) |

---

## 에이전트의 프롬프트 자체 수정 기능

에이전트가 자기 프로젝트 프롬프트를 직접 읽고 수정할 수 있음.

```bash
# 읽기
curl -s "$API_URL/api/prompts?project=$PROJECT" \
  -H "Authorization: Bearer $API_KEY"

# 쓰기
curl -s -X PUT "$API_URL/api/prompts" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"project": "프로젝트_ID", "content": "전체 프롬프트 내용"}'
```

**규칙**: 기존 내용을 함부로 삭제하지 말 것. 섹션 추가/수정은 자유.

---

## 파일 구조

```
app/api/prompts/route.ts     — GET/PUT (유저 자신의 프롬프트)
app/api/admin/prompts/route.ts — PATCH (관리자가 다른 유저 프롬프트 수정)
lib/db.ts                    — getPrompt(), upsertPrompt()
scripts/daemon/claude_runner.py — 프롬프트 조합 및 주입
scripts/daemon/supabase.py   — fetch_prompt_from_supabase()
```
