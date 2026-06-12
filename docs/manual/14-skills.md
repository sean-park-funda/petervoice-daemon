# 14. 스킬 관리

## 개요

스킬은 AI 비서의 능력을 확장하는 플러그인입니다. **스킬 마켓**에서 원하는 스킬을 골라 설치하면, 내 맥미니에 자동으로 적용됩니다.

## 구조

```
~/.claude/skills/
├── gmail/
│   └── SKILL.md
├── weather/
│   └── SKILL.md
└── ...
```

각 스킬은 `SKILL.md` 파일 하나로 구성됩니다. Claude Code가 자동으로 인식하여 관련 상황에서 활용합니다.

## 내 스킬 vs 스킬 마켓

| | 내 스킬 | 스킬 마켓 |
|---|---|---|
| **위치** | 내 맥미니 `~/.claude/skills/` | 서버 DB (`skills` 테이블) |
| **보이는 곳** | 웹 UI → 스킬 패널 → "내 스킬" 탭 | 웹 UI → 스킬 패널 → "스킬 마켓" 탭 |
| **설치** | 마켓에서 "설치" 클릭 | — |
| **제거** | "제거" 클릭 → 로컬에서 삭제 | — |

**핵심: 로컬 디스크가 내 스킬 목록입니다.** 별도의 user_skills 테이블 없이, `~/.claude/skills/`에 있는 것이 곧 내 스킬입니다.

## 웹 UI 스킬 패널

채팅 헤더의 **퍼즐 아이콘** 🧩을 클릭하면 스킬 패널이 열립니다.

### 내 스킬 탭
- 현재 설치된 스킬 카드 목록
- 한글 제목, 설명, 카테고리/태그 뱃지
- 카드 클릭 → SKILL.md 상세 내용 렌더링
- 채팅 삽입 버튼 (예: `/gmail ` 자동 입력)

### 스킬 마켓 탭
- 서버에 등록된 전체 스킬 목록
- 검색으로 필터링
- **설치** 버튼 → DB에서 SKILL.md를 가져와 로컬에 저장
- **제거** 버튼 → 로컬에서 삭제
- 설치 여부가 카드에 표시됨

## DB 구조

### skills 테이블 (마켓)

```sql
skills
├── id              — 스킬 식별자 (예: "gmail")
├── content         — SKILL.md 전체 내용
├── description     — 영문 설명
├── enabled         — 마켓 노출 여부
├── title_ko        — 한글 제목 (예: "Gmail")
├── description_ko  — 한글 설명 (예: "이메일을 보내고 검색합니다")
├── tags            — 태그 배열 (예: ["이메일", "Gmail"])
├── icon            — 이모지 아이콘 (예: "✉️")
├── category        — 카테고리 (예: "communication")
└── RLS: 공개 읽기, 인증된 사용자 쓰기
```

## 설치/제거 흐름

```
[스킬 마켓 탭] → "설치" 클릭
    ↓
1. Vercel API: GET /api/skills/content?id=gmail
   → skills 테이블에서 SKILL.md content 반환
    ↓
2. Home Portal: POST /api/skills/install
   → ~/.claude/skills/gmail/SKILL.md 로컬 저장
    ↓
3. Claude Code가 자동 인식 → 즉시 사용 가능
```

```
[내 스킬 탭] → "제거" 클릭
    ↓
Home Portal: POST /api/skills/uninstall
    → ~/.claude/skills/gmail/ 폴더 삭제
```

## 자동 동기화 (SkillsSyncer)

데몬의 SkillsSyncer가 5분마다 **업데이트 전용** 동기화를 수행합니다:

```
매 5분:
1. GET /api/bot/skills (enabled=true만)
2. 로컬에 이미 설치된 스킬만 대상 → SKILL.md 내용이 다르면 갱신
3. 로컬에 없는 스킬은 건드리지 않음 (마켓 UI에서 설치)
4. 로컬에만 있고 DB에 없는 스킬 → 유지 (유저 커스텀 스킬)
```

**핵심: SkillsSyncer는 자동 설치/삭제를 하지 않습니다.** 이미 설치된 스킬의 내용 업데이트만 합니다. 설치/제거는 유저가 마켓 UI에서 직접 합니다.

### 일회성 정리 (cleanup-v1)

이전 버전에서 SkillsSyncer가 모든 마켓 스킬을 자동 설치하는 방식이었기 때문에, 기존 유저 머신에 불필요한 스킬이 대량 설치되어 있습니다. 이를 정리하기 위한 일회성 마이그레이션:

1. 데몬 시작 시 `~/.claude/skills/.cleanup-v1-done` 플래그 확인
2. 없으면 → DB 마켓 스킬 목록 조회 → 해당 ID의 로컬 스킬 폴더 삭제
3. 유저가 직접 만든 커스텀 스킬(마켓에 없는 것)은 보존
4. `.cleanup-v1-done` 플래그 생성 → 다시 실행 안 함

## API 엔드포인트

| 엔드포인트 | 용도 |
|---|---|
| `GET /api/skills/market` | 마켓 스킬 목록 (한글 메타 포함) |
| `GET /api/skills/content?id=` | 스킬 SKILL.md content |
| `GET /api/bot/skills` | 데몬 동기화용 (api_key 인증) |
| Home Portal `GET /api/skills` | 로컬 설치 스킬 목록 |
| Home Portal `GET /api/skills/read?id=` | 로컬 SKILL.md 본문 |
| Home Portal `POST /api/skills/install` | 스킬 설치 (content → 로컬 저장) |
| Home Portal `POST /api/skills/uninstall` | 스킬 제거 (로컬 삭제) |

## 번들 스킬 (자동 배포)

데몬 레포(`petervoice-daemon`)의 `skills/` 폴더에 포함된 스킬은 AutoUpdater를 통해 **전 고객에게 자동 배포**됨.

### 번들 스킬 목록 (7종)

| 스킬 | 설명 |
|------|------|
| `notion-api` | Notion 페이지 CRUD |
| `pdf` | PDF 읽기/합치기/분할 |
| `gmail` | 이메일 발송/검색 |
| `google-calendar` | 일정 관리 |
| `local-publish` | Cloudflare 터널 기반 로컬 퍼블리싱 |
| `weather` | 날씨 조회 |
| `find-skills` | 스킬 검색/설치 도움 |

### 배포 흐름

```
데몬 레포 skills/ 폴더에 스킬 추가/수정
  → git push
  → AutoUpdater가 5분 내 git pull
  → 고객 머신의 ~/.claude/skills/에 자동 반영
```

번들 스킬은 마켓 UI 설치와 무관하게 레포에 포함되어 있으므로, 신규 고객도 데몬 설치 시 자동으로 받게 됨.

## 카테고리

| 카테고리 | 한글명 | 예시 |
|---|---|---|
| communication | 소통 | Gmail |
| productivity | 생산성 | 구글 캘린더, Word 문서 |
| development | 개발 | Next.js 인증, Supabase 가이드 |
| search | 검색 | 구글 검색, 네이버 플레이스 |
| media | 미디어 | 유튜브 요약, 숏폼 영상 제작 |
| ai | AI | 이미지 생성, AI 도구 모음 |
| utility | 유틸 | 날씨, 파일 공유 |
| finance | 금융 | ETF 시나리오, 네이버 정산 |
| automation | 자동화 | 브라우저 자동화 |
| design | 디자인 | Apple 디자인 스타일 |

## 스킬 마켓에 새 스킬 등록

`/deploy-skill` 스킬을 사용하거나 직접 DB에 등록:

```bash
# Supabase REST API
curl -X POST "$SUPABASE_URL/rest/v1/skills" \
  -H "apikey: $SUPABASE_KEY" \
  -H "Authorization: Bearer $SUPABASE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: resolution=merge-duplicates" \
  -d '{
    "id": "my-skill",
    "content": "---\nname: my-skill\ndescription: ...\n---\n# My Skill\n...",
    "description": "English description",
    "title_ko": "내 스킬",
    "description_ko": "한글 설명",
    "tags": ["태그1", "태그2"],
    "icon": "🔧",
    "category": "utility",
    "enabled": true
  }'
```

**중요**: 한글 메타데이터(`title_ko`, `description_ko`, `tags`, `icon`)를 반드시 포함해야 마켓에서 보기 좋게 표시됩니다.
