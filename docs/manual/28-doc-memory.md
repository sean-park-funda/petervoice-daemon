# 28. 문서의 기억화 (Doc Memory)

> **상태: 계획 단계** — 아직 구현되지 않음

## 문제

- 프로젝트별 `docs/` 폴더에 매뉴얼, 설계 문서, 회의록 등이 계속 쌓임
- 에이전트는 매번 새 세션으로 시작하므로 이 문서들의 존재를 모름
- 문서를 전부 시스템 프롬프트에 넣으면 컨텍스트 한도 초과
- 현재는 유저가 "XX 문서 읽어봐"라고 직접 지시해야만 참조 가능
- **문서가 두서없이 쌓임** — 에이전트가 급하게 만든 문서, 중복 내용, 오래된 정보, 일관성 없는 이름 등이 검색 품질을 떨어뜨림

**목표**: 문서를 상시 정리하고, 에이전트가 질문을 받으면 관련 문서를 자동으로 찾아 읽고, 문서 기반으로 정확하게 답변하는 구조.

---

## 설계 방향

### 핵심 아이디어

```
유저 질문 → 에이전트가 doc-search 스킬 호출 → 관련 문서 발견 → Read로 읽기 → 답변
```

전체 문서를 프롬프트에 넣는 대신, **인덱스만 유지**하고 필요할 때 검색 → 열람하는 방식.

### 구성 요소

| 구성 | 역할 | 위치 |
|------|------|------|
| **클리너** | 문서 정리/병합/분류/폐기 | 하트비트 또는 수동 실행 |
| **인덱서** | docs/ 폴더를 스캔해 인덱스 파일 생성 | 스킬 스크립트 또는 데몬 |
| **인덱스 저장소** | 문서 메타+요약 구조화 데이터 | 프로젝트별 `docs/.index.json` |
| **검색 스킬** | 키워드/의미 검색 → 관련 문서 경로 반환 | `~/.claude/skills/doc-search/` |
| **시스템 프롬프트 힌트** | "질문에 답할 때 doc-search를 활용하라" | `_petervoice_system` |

---

## 상세 계획

### Phase 0: 문서 클리닝

인덱싱의 전제조건. 정리되지 않은 문서를 인덱싱하면 garbage in, garbage out.

#### 문서가 지저분해지는 원인

에이전트가 작업 중 급하게 만드는 문서는 구조적 문제를 안고 태어난다:

| 문제 | 예시 |
|------|------|
| **중복** | `design-plan.md`와 `design-plan-v2.md`가 80% 동일 |
| **유사하지만 미묘하게 다름** | 같은 주제 문서가 2개인데 세부 내용이 조금씩 다름 |
| **문서 간 모순** | A 문서는 "REST API 사용"이라 하고, B 문서는 "GraphQL로 전환"이라 함 |
| **이름 불일치** | `meeting-notes.md`, `회의록-0415.md`, `mtg_design.md` |
| **위치 혼재** | 매뉴얼이 `docs/` 루트에도, `docs/manual/`에도 있음 |
| **오래된 정보** | 3달 전 설계안이 현재 코드와 불일치 |
| **임시 문서 잔존** | `draft-*.md`, `temp-*.md`, `test-*.md`가 정리 안 됨 |
| **내용 부실** | 제목만 있고 본문이 1-2줄 |

#### 폴더 구조 컨벤션

프로젝트별로 다를 수 있지만, 기본 분류 체계:

```
docs/
├── manual/          ← 사용법, 운영 매뉴얼 (안정적, 장기 유지)
├── plans/           ← 설계/구현 계획 (승인 전 draft → 승인 후 유지 또는 archive)
├── notes/           ← 회의록, 메모 (날짜 기반)
├── reports/         ← 분석 결과, 리포트
├── archive/         ← 완료/폐기 문서 (검색에서 제외)
└── .index.json      ← 인덱스 (자동 생성)
```

#### 파일 네이밍 규칙

```
{카테고리}-{주제}.md           (일반)    예: manual-relay-api.md
{날짜}-{주제}.md               (시간순)  예: 2026-04-15-design-review.md
{번호}-{주제}.md               (순서)    예: 01-architecture.md
```

- 한글 파일명 허용하되 영문 권장 (URL 인코딩 이슈 방지)
- 공백 대신 하이픈(`-`) 사용
- `draft-`, `temp-`, `wip-` 접두사는 임시 문서 표시 → 클리닝 대상

#### 클리닝 작업 정의

| 작업 | 설명 | 자동화 가능? |
|------|------|:---:|
| **중복 탐지** | 제목/내용이 유사한 문서 쌍 찾기 | O |
| **모순 탐지** | 같은 주제의 문서가 서로 다른 내용을 주장 | △ (LLM 보조) |
| **stale 탐지** | N일 이상 수정 안 된 문서 + 코드와 불일치 | O |
| **임시 문서 정리** | `draft-`, `temp-` 접두사 파일 → archive 이동 또는 삭제 | O |
| **빈 문서 정리** | 제목만 있고 본문 10줄 미만 | O |
| **위치 재배치** | 잘못된 폴더에 있는 문서 이동 | △ (규칙 기반 제안) |
| **내용 병합** | 중복/유사 문서를 하나로 통합 | X (LLM 필요) |
| **정확성 검증** | 문서 내용이 현재 코드/설정과 일치하는지 | △ (섹션별 검증) |

#### 유사 문서 & 모순 탐지 (핵심 문제)

단순 중복은 찾기 쉽다. 진짜 위험한 건 **비슷한데 미묘하게 다른 문서**, 그리고 **서로 모순되는 문서**다.

에이전트가 어느 문서를 먼저 읽느냐에 따라 답이 달라지면, 문서가 기억이 아닌 혼란의 원인이 된다.

**발생 패턴:**

| 패턴 | 예시 | 왜 생기는가 |
|------|------|-------------|
| 버전 분기 | `auth-design.md`(REST), `auth-design-v2.md`(GraphQL) | 설계 변경 후 구버전을 안 지움 |
| 관점 차이 | 매뉴얼 03은 "세션 TTL 2시간", 매뉴얼 26은 "TTL 24시간" | 다른 시점에 다른 에이전트가 작성 |
| 부분 업데이트 | A 문서는 새 API 반영, B 문서는 옛 API 참조 | 코드 변경 시 관련 문서 일부만 수정 |
| 계획 vs 현실 | plans/에 "이렇게 하겠다", manual/에 "저렇게 됐다" | 계획과 구현이 달라졌는데 계획 문서가 남음 |

**탐지 방법:**

```
1단계: 주제 클러스터링 (자동)
  - 인덱스의 keywords/headings가 겹치는 문서 쌍 추출
  - 같은 Room(토픽)에 속하는 문서가 2개 이상이면 후보

2단계: 차이점 추출 (자동)
  - 후보 쌍의 공통 헤딩(H2/H3)별 내용 비교
  - 숫자, URL, 명령어, 설정값 등 팩트 요소 diff

3단계: 모순 판정 (LLM 보조)
  - 2단계에서 diff가 발견된 쌍만 LLM에 전달
  - "이 두 문단이 모순인지, 보완 관계인지, 시간순 변경인지 판단해줘"
  - 결과를 리포트에 포함
```

**해결 전략:**

| 유형 | 해결 방법 |
|------|-----------|
| **시간순 변경** (구→신) | 최신 문서만 유지, 구버전은 archive. 최신 문서에 `supersedes: 구문서경로` 메타 표기 |
| **관점 차이** (둘 다 부분적으로 맞음) | 하나로 병합. 충돌 부분은 "현재 상태"로 통일 |
| **계획 vs 현실** | 계획 문서에 `status: implemented` 표기 + 실제 구현 문서 링크. 또는 archive |
| **단순 오류** (한쪽이 틀림) | 코드/설정 확인 후 맞는 쪽으로 통일, 틀린 문서 수정 또는 삭제 |

**문서 메타데이터로 예방:**

문서 상단에 frontmatter를 두면 모순 탐지와 해결이 쉬워진다:

```markdown
---
topic: auth-system
status: active        # active | draft | superseded | archived
supersedes: plans/old-auth-design.md   # 이 문서가 대체한 문서
last_verified: 2026-04-16              # 마지막으로 코드와 대조한 날짜
---
```

- `status: superseded` → 검색 결과에서 후순위 또는 제외
- `supersedes` → 문서 계보 추적 가능
- `last_verified` → 오래되면 클리닝 리포트에서 "재검증 필요" 플래그

**인덱스 반영:**

```json
{
  "path": "manual/07-auth.md",
  "title": "인증 및 멀티유저",
  "status": "active",
  "supersedes": "plans/old-auth-design.md",
  "last_verified": "2026-04-16",
  "topic": "auth-system",
  ...
}
```

검색 시 같은 topic의 문서가 여러 개 매칭되면:
1. `status: active` 우선
2. `superseded` 문서는 결과에서 제외하거나 "⚠️ 대체됨" 표시
3. `last_verified`가 30일 이상 된 문서는 "⚠️ 미검증" 경고

#### 클리닝 리포트 (docs/.cleanup-report.md)

클리너가 실행될 때마다 리포트를 생성한다. 자동 수정이 아닌 **제안** 방식으로, 유저가 판단 후 처리.

```markdown
# 문서 클리닝 리포트
> 생성: 2026-04-16, 대상: peter-voice

## 모순/유사 문서 (1건) ⚠️ 우선 처리
- `manual/03-daemon.md` § 세션관리 ↔ `manual/26-long-tasks.md` § TTL 설정
  - 03: "TTL 기본 2시간"
  - 26: "TTL 2~24시간, 기본 24시간"
  → 코드 확인 필요. 한쪽 수정 후 다른 쪽에 상호 참조 추가

## 대체된 문서 (1건)
- `plans/auth-design-v1.md` → `manual/07-auth.md`로 대체됨
  → 제안: v1에 status: superseded 표기 또는 archive 이동

## 중복 의심 (2건)
- `docs/design-plan.md` ↔ `docs/plans/design-plan-v2.md` — 유사도 85%
  → 제안: v2로 통합 후 구버전 archive

## 미검증 문서 (2건)
- `manual/08-secrets.md` — last_verified 60일 전
  → 제안: 현재 코드와 대조 후 last_verified 갱신

## Stale 문서 (3건)
- `docs/plans/old-auth-design.md` — 90일 미수정, auth 코드와 불일치
  → 제안: archive 이동

## 임시 문서 (1건)
- `docs/draft-kanban-spec.md` — draft 접두사, 45일 미수정
  → 제안: 완성 또는 삭제

## 빈 문서 (1건)
- `docs/notes/idea-voice-ui.md` — 3줄, 메모 수준
  → 제안: 내용 보강 또는 삭제
```

#### 실행 방식

- **수동**: 에이전트에게 "문서 정리해줘" 지시 → 클리닝 리포트 생성 → 유저 확인 후 실행
- **하트비트**: 주기적(주 1회) 자동 스캔 → 리포트만 생성 (자동 삭제/이동 안 함)
- **인덱싱 연계**: 인덱서 실행 시 간단한 위생 점검 (빈 파일, 임시 파일 경고)

### Phase 1: 인덱싱

각 프로젝트의 `docs/` 폴더를 스캔해서 구조화된 인덱스를 생성한다.

#### 인덱스 구조 (docs/.index.json)

```json
{
  "project": "peter-voice",
  "updated_at": "2026-04-16T10:00:00Z",
  "documents": [
    {
      "path": "manual/01-architecture.md",
      "title": "아키텍처 개요",
      "summary": "전체 시스템 구조, 기술 스택(Next.js, Supabase, Claude), 데이터 흐름",
      "keywords": ["아키텍처", "Next.js", "Supabase", "데몬", "데이터흐름"],
      "headings": ["개요", "기술 스택", "시스템 구조", "데이터 흐름"],
      "category": "manual",
      "size_lines": 150,
      "modified_at": "2026-04-10T08:00:00Z",
      "status": "active"
    }
  ],
  "excluded": [
    { "path": "archive/old-auth-design.md", "reason": "archived" },
    { "path": "draft-kanban-spec.md", "reason": "draft" }
  ]
}
```

#### 인덱스 생성 방식

- **방법 A** (간단): 파일명 + 첫 H1 + H2 목록 + 파일 크기로 메타데이터 추출 (파싱만, LLM 불필요)
- **방법 B** (풍부): Claude로 각 문서 요약 + 키워드 추출 (비용 발생, 정확도 높음)
- **권장**: A로 시작, 필요 시 B 추가

#### 인덱스 갱신 시점

- 데몬 시작 시 1회
- 하트비트 주기에 맞춰 갱신 (파일 mtime 비교로 변경분만)
- 에이전트가 `docs/`에 파일 쓴 직후

### Phase 2: 검색 스킬 (doc-search)

`~/.claude/skills/doc-search/SKILL.md` — 에이전트가 `/doc-search 검색어` 형태로 호출.

#### 스킬 동작

```
1. 검색어 수신
2. 모든 프로젝트의 docs/.index.json 로드
3. 키워드 매칭 (title, summary, keywords, headings)
4. 관련도 순으로 상위 N개 문서 경로 반환
5. 에이전트가 Read 도구로 해당 문서 열람
```

#### 검색 방식 (단계별 확장)

| 단계 | 방식 | 장점 | 단점 |
|------|------|------|------|
| v1 | 키워드 매칭 | 구현 간단, 비용 0 | 유의어/맥락 무시 |
| v2 | TF-IDF 또는 BM25 | 정확도 향상 | 인덱싱에 약간의 연산 |
| v3 | 임베딩 벡터 검색 | 의미 기반 검색 | 임베딩 비용, 벡터 DB 필요 |

**v1으로 시작**하고, 검색 품질이 부족하면 단계 올림.

#### 반환 형식

```json
{
  "query": "Tailscale SSH 장애복구",
  "results": [
    {
      "project": "peter-voice",
      "path": "manual/27-customer-management.md",
      "title": "고객 매니지먼트",
      "summary": "...",
      "relevance": 0.85,
      "section_hint": "## 27.3 Tailscale SSH 원격 접속"
    }
  ]
}
```

`section_hint`로 문서 내 관련 섹션까지 안내하면, 에이전트가 전체를 읽지 않고 필요한 부분만 읽을 수 있음.

### Phase 3: 시스템 프롬프트 힌트

`_petervoice_system` 프롬프트에 다음 규칙을 추가한다:

```markdown
## 문서 기반 응답 (Doc Memory)
- 유저 질문에 답할 때, 관련 문서가 있을 수 있으면 `/doc-search` 스킬로 먼저 검색할 것
- 검색 결과가 있으면 해당 문서를 Read로 읽고, 문서 내용을 근거로 답변
- 답변 시 출처 표기: "📄 manual/27-customer-management.md 참조"
- 검색 결과 없으면 기존 방식대로 답변 (강제하지 않음)
- 코드 작업 중 설계 의도가 불명확하면 docs/ 검색으로 확인
- 같은 주제 문서가 2개 이상 검색되면 status 확인: active > draft > superseded 순으로 신뢰
- 문서 내용이 서로 모순되면 코드/설정을 직접 확인하여 현재 사실 기준으로 답변하고, 모순을 유저에게 알릴 것

## 문서 작성/수정 규칙
- 새 문서 작성 전에 기존 문서 검색 → 중복 방지. 기존 문서가 있으면 새로 만들지 말고 기존 문서를 수정
- 기존 문서를 수정할 때, 같은 내용을 다루는 다른 문서가 있으면 함께 업데이트 (상호 참조 유지)
- 설계를 변경했으면 이전 설계 문서에 status: superseded 표기
- 파일명: 영문 하이픈 구분, 폴더 컨벤션 준수 (manual/, plans/, notes/)
- 가능하면 frontmatter 포함 (topic, status, last_verified)
```

#### 힌트가 작동하는 시나리오 예시

| 유저 질문 | 에이전트 동작 |
|-----------|---------------|
| "Tailscale로 고객 접속하는 법?" | doc-search → 27-customer-management.md → 읽고 답변 |
| "칸반 API 어떻게 써?" | doc-search → 16-agent-api.md → 읽고 답변 |
| "지난번 디자인 회의 결론이 뭐였지?" | doc-search → notes/meeting-*.md → 읽고 답변 |
| "이 함수 버그 고쳐줘" | 코드 작업이므로 doc-search 불필요 → 바로 진행 |

### Phase 4: 크로스 프로젝트 검색 (확장)

현재 에이전트는 자기 프로젝트 docs만 접근 가능. 다른 프로젝트 문서가 필요하면:

- **옵션 A**: 인덱스를 중앙 위치에 통합 (예: `~/.claude-daemon/doc-index/`)
- **옵션 B**: Relay로 다른 프로젝트 에이전트에게 "XX 문서 찾아줘" 요청
- **권장**: A — 읽기 전용 인덱스 참조는 프로젝트 격리를 깨지 않음

---

### Phase 5: 상시 클리닝 운영

Phase 0은 초기 정리, Phase 5는 지속적 위생 유지.

#### 문서 생성 시점 규율

에이전트가 문서를 만들거나 수정할 때 지켜야 할 규칙을 `_petervoice_system`에 추가 (Phase 3 참조).

핵심은 **"만들기 전에 검색, 수정 시 연쇄 업데이트"**:
- 새 문서 작성 전에 기존 문서 검색 → 있으면 기존 문서 수정으로 대체
- 내용 변경 시 같은 topic 문서가 있으면 함께 업데이트
- 설계 변경 시 이전 문서에 superseded 표기
- frontmatter로 문서 메타데이터 관리

이 규칙만으로도 모순과 중복을 사전에 크게 줄인다. 사후 정리보다 사전 예방이 비용이 훨씬 낮다.

#### 클리닝 주기

| 주기 | 작업 | 실행 주체 |
|------|------|-----------|
| 문서 생성/수정 시 | 중복 체크, 네이밍 검증, 연쇄 업데이트 | 에이전트 (프롬프트 규칙) |
| 인덱싱 시 | 빈 문서/임시 문서/같은 topic 복수 문서 경고 | 인덱서 |
| 주 1회 | 클리닝 리포트 (모순 탐지 포함) | 하트비트 |
| 월 1회 | stale 문서 전수 점검, last_verified 갱신, archive 정리 | 수동 또는 하트비트 |

---

## 구현 우선순위

```
[0] Phase 0 — 문서 클리닝 (초기 정리)
    └─ 폴더 컨벤션 정의 및 기존 문서 재배치
    └─ 중복/stale/임시 문서 정리
    └─ 클리닝 리포트 스크립트

[1] Phase 1 — 인덱서 (방법 A: 파싱 기반)
    └─ docs/.index.json 생성 스크립트
    └─ 데몬에 자동 갱신 훅
    └─ 인덱싱 시 위생 경고 통합

[2] Phase 2 — doc-search 스킬 (v1: 키워드 매칭)
    └─ SKILL.md 작성
    └─ 검색 스크립트 (bun 또는 python)

[3] Phase 3 — _petervoice_system 힌트 추가
    └─ 문서 검색 활용 규칙
    └─ 문서 작성 규칙 (사전 예방)

[4] Phase 4 — 크로스 프로젝트 (필요 시)
    └─ 중앙 인덱스 통합

[5] Phase 5 — 상시 클리닝 (운영)
    └─ 하트비트 기반 주기적 리포트
    └─ 월간 전수 점검
```

## 외부 도구 검토: MemPalace

> GitHub: https://github.com/mempalace/mempalace (MIT 라이선스)

### MemPalace란

오픈소스 AI 메모리 시스템. 대화 기록과 문서를 로컬에 저장하고 **시맨틱 검색**(의미 기반 검색)으로 찾아주는 도구. LLM 호출 없이 96.6% 검색 정확도(LongMemEval 벤치마크).

### 핵심 개념

```
Wing (프로젝트/사람)
  └─ Room (토픽: auth, billing, deploy)
       └─ Closet (요약 포인터)
            └─ Drawer (원본 내용 — 요약 없이 원문 그대로 저장)
```

피터보이스의 프로젝트 → docs/ 폴더 → 문서 구조와 자연스럽게 대응된다.

### 기술 스택

| 항목 | 내용 |
|------|------|
| 언어 | Python 3.9+ |
| 벡터 DB | ChromaDB (로컬, 플러거블) |
| 임베딩 | 로컬 모델 (~300MB 디스크) |
| 지식 그래프 | SQLite |
| 연동 | MCP 서버 (29개 도구), Claude Code 훅 |
| API 비용 | 코어 검색은 0 (로컬 임베딩) |

### 피터보이스 적합성 분석

#### 장점 (우리가 직접 만들 필요 없는 것)

| 항목 | 자체 구현 | MemPalace 활용 |
|------|-----------|----------------|
| 시맨틱 검색 | Phase 2 v3에서야 가능 | 기본 탑재 |
| 크로스 프로젝트 검색 | Phase 4 별도 구현 | Wing 간 Tunnel로 기본 지원 |
| 대화 기록 검색 | messages 테이블 FTS | `mine --mode convos`로 대화도 인덱싱 |
| 세션 컨텍스트 보존 | session_context 단순 요약 | L0~L3 4계층 메모리 스택 |
| Claude Code 연동 | 스킬로 구현 필요 | MCP 서버 + 훅 기본 제공 |

#### 우려 사항

| 항목 | 상세 |
|------|------|
| **고객 머신 부담** | 임베딩 모델 300MB + ChromaDB. 고객 Mac Mini에 추가 설치 필요 |
| **의존성 증가** | chromadb, sqlite, 임베딩 모델 — 데몬의 requirements.txt 복잡해짐 |
| **구조 미스매치** | 우리는 docs/ 폴더가 이미 정리된 로컬 파일. MemPalace는 비정형 대화를 구조화하는 데 강점 |
| **과잉 기능** | 29개 MCP 도구 중 실제 필요한 건 검색(search)과 마이닝(mine) 정도 |
| **클리닝 미지원** | 문서 정리/중복 탐지/stale 관리는 MemPalace 범위 밖 — Phase 0/5는 여전히 자체 구현 필요 |
| **업데이트 리스크** | 외부 프로젝트 의존 → 호환성 깨질 수 있음 |

### 도입 판단

#### 방안 A: MemPalace 전면 도입

Phase 1(인덱서) + Phase 2(검색 스킬) + Phase 4(크로스 프로젝트)를 MemPalace로 대체.

```
설치: pip install mempalace
초기화: mempalace init {project_dir}
마이닝: mempalace mine {project_dir}/docs/
연동: claude mcp add mempalace -- python -m mempalace.mcp_server
훅: Stop/PreCompact 훅으로 자동 저장
```

- Phase 0(클리닝), Phase 3(프롬프트 힌트), Phase 5(상시 운영)는 그대로 자체 구현
- 데몬에 mine 명령 자동 실행 통합 필요

#### 방안 B: MemPalace 부분 차용

MemPalace를 직접 설치하지 않고, 아이디어만 차용:
- Wing/Room 계층 구조 → 우리 폴더 컨벤션에 반영 (이미 유사)
- L0~L3 메모리 레이어 → wake-up 시 인덱스 요약을 프롬프트에 주입
- ChromaDB 대신 가벼운 방식 유지 (키워드 → BM25 → 필요 시 임베딩)

#### 방안 C: 하이브리드 (권장)

**Sean 개발 환경에서만 MemPalace 사용, 고객에게는 경량 버전 배포.**

```
Sean (개발자):
  MemPalace 풀 설치 → 시맨틱 검색, 대화 기록 마이닝, 지식 그래프
  모든 프로젝트 Wing으로 연결 → 크로스 프로젝트 검색

고객 (일반 유저):
  자체 경량 인덱서 (Phase 1) + 키워드 검색 (Phase 2 v1)
  300MB 임베딩 모델 부담 없음
  필요한 고객만 선택적으로 MemPalace 설치 (스킬 마켓 배포)
```

이유:
- Sean은 수십 개 프로젝트 + 수천 건 대화 → 시맨틱 검색 가치 높음
- 일반 고객은 1~3개 프로젝트 + 문서 수십 개 → 키워드 검색으로 충분
- 고객 머신에 300MB 추가 부담을 강제하지 않음

### MemPalace 도입 시 데몬 통합 포인트

도입한다면 데몬에 연동할 지점:

```python
# claude_daemon.py 또는 별도 syncer

# 1. 데몬 시작 시: 프로젝트별 mine
for project in projects:
    subprocess.run(["mempalace", "mine", project_dir])

# 2. 문서 변경 감지 시: 증분 mine
# (AutoUpdater 패턴 차용 — mtime 비교)

# 3. 세션 시작 시: wake-up으로 컨텍스트 로드
wake_up_context = subprocess.check_output(
    ["mempalace", "wake-up", "--wing", project]
)
# → sys_prompt에 주입

# 4. Claude Code 훅: Stop/PreCompact에서 자동 저장
# settings.json에 hooks 추가
```

---

## 고려사항

- **사전 예방 > 사후 정리**: 프롬프트에 문서 작성 규칙을 넣는 것만으로 오염을 크게 줄일 수 있음. Phase 3의 문서 작성 규칙은 검색 스킬 없이도 독립적으로 먼저 적용 가능
- **자동 삭제 금지**: 클리너는 리포트만 생성하고, 실제 삭제/이동은 유저 승인 후. 에이전트가 만든 문서를 에이전트가 지우면 의도치 않은 손실 위험
- **archive 폴더의 역할**: 삭제가 아닌 격리. 인덱스에서 `excluded`로 표시되어 검색에서 제외되지만, 나중에 필요하면 복원 가능
- **비용**: v1은 LLM 호출 없음 (파싱+키워드). 내용 병합이나 정확성 검증 시에만 LLM 사용. MemPalace 도입 시에도 검색 자체는 무료 (로컬 임베딩)
- **속도**: 인덱스 파일은 작으므로 검색 자체는 ms 단위. Read 도구 호출이 병목. MemPalace의 ChromaDB 검색도 로컬이므로 빠름
- **정확도**: 키워드 매칭은 "프롬프트"를 검색했을 때 "시스템 프롬프트" 문서를 못 찾을 수 있음 → MemPalace의 시맨틱 검색은 이 문제를 해결
- **인덱스 크기**: 프로젝트당 수십 개 문서 수준이면 인덱스가 수 KB — 프롬프트에 인덱스 자체를 넣는 것도 가능 (문서 수가 적을 때)
- **기존 폐기 기능과 차이**: 이전 `/api/documents?search=`는 DB 기반 Full-Text Search였음. 이번에는 로컬 파일 기반 방식으로 DB 의존성 없음

---

## 관련 문서

- [10. 문서 관리](./10-documents.md) — 현재 로컬 파일 서빙 구조
- [14. 스킬 관리](./14-skills.md) — 스킬 설치/동작 방식
- [19. 프롬프트 관리](./19-prompts.md) — _petervoice_system 프롬프트 구조
- [MemPalace GitHub](https://github.com/mempalace/mempalace) — 외부 도구 원본
