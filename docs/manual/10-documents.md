# 10. 문서 관리

## 개요

프로젝트별 `docs/` 폴더의 파일을 웹 UI에서 직접 열람합니다. **Local-first 아키텍처** — DB를 거치지 않고 브라우저가 Home Portal을 통해 맥미니의 로컬 파일을 직접 읽습니다.

## 아키텍처

```
웹 브라우저 (peter-voice.vercel.app)
  ↓ Bearer JWT (5분 TTL)
Home Portal (맥미니 :3000)
  ↓ 로컬 파일시스템
{project_dir}/docs/ ← 모든 파일
```

**DB 동기화 없음.** 파일 생성/수정 즉시 웹에 반영됩니다.

### 인증 흐름

1. 웹 UI → `POST /api/portal/token` → Vercel이 JWT 발급 (HMAC-SHA256, 5분 TTL)
2. 웹 UI → Home Portal API에 `Authorization: Bearer {jwt}` 헤더로 요청
3. Home Portal이 JWT 검증 후 로컬 파일 서빙

토큰은 클라이언트에서 4분간 캐시 (`portalCacheRef`).

## 지원 파일 형식

마크다운뿐 아니라 **모든 파일**을 표시합니다:

| 타입 | 확장자 | 뷰어 |
|---|---|---|
| doc | .md, .mdx | 마크다운 렌더링 (GFM, 이미지 인라인) |
| image | .png, .jpg, .gif, .webp, .svg | 이미지 뷰어 (인증 blob URL) |
| code | .py, .js, .ts, .tsx, .css 등 | Prism 신택스 하이라이팅 + 줄번호 |
| text | .txt, .csv, .json, .yaml 등 | 코드 뷰어 (하이라이팅) |
| pdf | .pdf | iframe PDF 뷰어 |
| video | .mp4, .webm, .mov | HTML5 비디오 플레이어 |
| audio | .mp3, .wav, .m4a, .flac | HTML5 오디오 플레이어 |
| file | 기타 | 파일 정보 표시 (이름, 크기) |

## 로컬 파일 구조

```
{project_dir}/
└── docs/
    ├── design-plan.md       → type: doc
    ├── screenshot.png       → type: image
    ├── config.json          → type: text
    ├── report.pdf           → type: pdf
    ├── demo.mp4             → type: video
    ├── notes/               → type: folder (자동 감지)
    │   ├── meeting-1.md
    │   └── meeting-2.md
    └── images/
        └── logo.png
```

## 웹 UI — DocumentsPanel

채팅 헤더의 **문서 아이콘** 📄을 클릭하면 우측 사이드 패널이 열립니다.

### 사이드바 (파일 트리)

```
┌──────────────────────────────────┐
│ 📄 문서                    [닫기] │
├──────────────────────────────────┤
│ ▾ notes                          │
│   ● meeting-1                    │
│   ○ meeting-2                    │
│ ● design-plan                    │
│ 🖼 screenshot                    │
│ ⟨⟩ config                        │
│ 📕 report                        │
│ ▶ demo                           │
│                                  │
│ [+ 폴더]                         │
└──────────────────────────────────┘
```

아이콘: ●/○ 문서, 🖼 이미지, ⟨⟩ 코드, ☰ 텍스트, 📕 PDF, ▶ 비디오, ♪ 오디오, 📎 기타

### 정렬

사이드바 상단의 드롭다운으로 파일 정렬 기준을 변경할 수 있습니다:

| 옵션 | 동작 |
|------|------|
| 최신순 | 최근 변경된 파일 먼저 (기본값) |
| 오래된순 | 오래된 파일 먼저 |
| 이름순 | 가나다/ABC 순 |

- 폴더는 항상 파일보다 위에 표시
- 폴더 내부 파일도 같은 기준으로 정렬
- 선택한 정렬은 localStorage에 저장되어 다음 방문에도 유지

### 파일 프리뷰

파일 클릭 시 우측에 뷰어가 열립니다. 텍스트 파일은 Prism 신택스 하이라이팅, 바이너리는 인증된 blob URL로 렌더링합니다.

### 문서 생성

사이드바 하단 **[+ 문서]** 버튼 → 파일명 입력 → `POST /api/docs/write`로 생성. 생성된 파일이 즉시 사이드바에 표시됨.

### 인라인 편집

문서 뷰어 상단의 **[편집]** 버튼 클릭 → textarea 인라인 편집기 표시. 저장/취소 버튼으로 변경 적용. `POST /api/docs/write` API 사용.

### 다운로드 (MD / DOCX / PDF)

문서 뷰어 상단의 **다운로드 버튼(⬇)** 클릭 → 3가지 포맷 선택:

| 포맷 | 설명 |
|------|------|
| MD | 원본 마크다운 파일 그대로 |
| DOCX | Word 문서 변환 (마크다운 테이블 → Word 표) |
| PDF | PDF 변환 (나눔고딕 폰트 base64 내장, 한글 정상 출력) |

- **PDF 한글 지원**: `pdf-lib` + 나눔고딕 base64 내장으로 Vercel 서버리스에서도 한글 출력
- **테이블 변환**: 마크다운 테이블이 DOCX/PDF에서 실제 표로 변환됨

### 파일 드래그 앤 드롭 이동

사이드바에서 파일/폴더를 **드래그하여 다른 폴더 안으로 이동** 가능:
- 폴더 위에 드롭 → 해당 폴더 안으로 이동
- 루트 영역에 드롭 → 최상위로 이동
- 자기 자신이나 하위 폴더로의 이동은 자동 방지

### 문서 멘션 (전체 경로)

사이드바에서 파일을 **채팅 입력란으로 드래그** 시 `@상위폴더/하위폴더/파일명` 형태의 전체 경로로 멘션이 삽입됨.

### 기능 버튼

| 버튼 | 동작 |
|------|------|
| 다운로드(⬇) | MD/DOCX/PDF 포맷 선택 다운로드 |
| 복사 | 다른 프로젝트의 docs/로 파일 복사 (Home Portal `fs.cpSync`) |
| 이동 | 다른 프로젝트의 docs/로 파일 이동 (Home Portal `fs.renameSync`) |
| 삭제 | 파일 또는 폴더 삭제 (확인 다이얼로그 → Home Portal `fs.rmSync`) |
| 대화에 넣기 | 채팅 입력란에 `@파일명 ` 삽입 |

### 폴더 생성

사이드바 하단 "**+ 폴더**" 버튼 → 이름 입력 → `POST /api/docs/mkdir`

### 드래그 앤 드롭 업로드

파일을 문서 패널 위에 끌어다 놓으면 현재 폴더에 업로드됩니다.
- 50MB 제한
- 한글 파일명 지원 (UTF-8 multipart 파싱)
- 업로드 중 오버레이 표시

## Home Portal API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/docs?dir={docsDir}` | 파일/폴더 트리 목록 |
| GET | `/api/docs/read?dir=&path=` | 텍스트 파일 내용 읽기 |
| GET | `/api/docs/file?dir=&path=` | 바이너리 파일 서빙 (MIME 감지) |
| POST | `/api/docs/mkdir` | 폴더 생성 |
| POST | `/api/docs/upload` | 파일 업로드 (multipart) |
| POST | `/api/docs/copy` | 파일/폴더 복사 (다른 프로젝트로) |
| POST | `/api/docs/move` | 파일/폴더 이동 (다른 프로젝트로) |
| POST | `/api/docs/delete` | 파일/폴더 삭제 (폴더는 recursive) |
| POST | `/api/docs/write` | 텍스트 파일 생성/수정 |

### 경로 보안

- `validateDocsDir(dir)` — 경로 순회 방지, 홈 디렉토리 범위 확인
- `dir` 파라미터는 웹 UI가 DB `projects.directory` + `/docs`로 조합하여 전달

## 프로젝트 디렉토리 resolve

DB의 `projects.directory` 컬럼이 프로젝트별 절대 경로를 저장합니다.

```
Vercel: POST /api/portal/token
  → projects 테이블에서 {projectId: directory} 매핑 반환
  → 웹 UI가 projectDirs[currentProject] + "/docs" 로 docsDir 구성
  → Home Portal에 docsDir 쿼리 파라미터로 전달
```

## PC/모바일 레이아웃

### PC (대화면)

```
┌──────────────────────────────────────────────────────┐
│                    헤더                               │
├──────────┬─┬─────────────────────────────────────────┤
│          │↔│                                         │
│  채팅창   │ │          문서 프리뷰                     │
│  (좌측)   │ │          (우측, 리사이즈 가능)            │
│          │ │                                         │
├──────────┴─┴─────────────────────────────────────────┤
│                  입력란                               │
└──────────────────────────────────────────────────────┘
```

드래그로 좌우 비율 조절 가능 (20~80%). 설정 자동 저장.

### 모바일 (소화면)

문서 패널이 전체 화면으로 표시됩니다. 백드롭 클릭으로 닫기.

## 봇의 문서 작성

`_common` 프롬프트 지시:

```
## 내부 문서 관리
- 프로젝트 디렉토리의 docs/ 폴더에 .md 파일로 작성
- Home Portal이 로컬 파일을 직접 서빙하므로 API 호출 불필요
- 폴더 구조화 가능 (예: docs/notes/, docs/specs/)
- 파일명이 문서 제목이 됨
```

파일 작성 즉시 웹 UI에서 확인 가능합니다 (Home Portal이 실시간 서빙).

## 파일 탐색기 고급 기능 (branch-139)

### 우클릭 컨텍스트 메뉴

파일 또는 폴더에서 **우클릭** → 컨텍스트 메뉴 표시:

| 항목 | 동작 |
|------|------|
| 이름 변경 | 인라인 입력 필드로 전환 |
| 새 서브폴더 | 해당 폴더 안에 하위 폴더 생성 |
| 삭제 | 확인 후 삭제 |
| 핀 추가/해제 | 핀 목록에 등록/제거 |

### 다중 선택

- **Cmd(Mac) / Ctrl(Windows) + 클릭** — 파일/폴더 개별 선택
- 여러 항목 선택 후 하단 **삭제 버튼** → 일괄 삭제 (확인 다이얼로그)
- 선택 해제: ESC 또는 빈 영역 클릭

### 서브폴더 생성

폴더에서 우클릭 → "새 서브폴더" 또는 사이드바 **+ 폴더** 버튼에서 슬래시(`/`)로 계층 표현:
```
notes/meeting    →  notes/ 폴더 안에 meeting 폴더 생성
```

### 문서 핀 기능

자주 쓰는 문서를 **핀**으로 고정하면 사이드바 상단에 항상 표시됩니다.

- 파일 우클릭 → "핀 추가" (또는 핀 아이콘 클릭)
- 핀 목록은 `localStorage["docsPanelPinnedDocs"]`에 저장 (브라우저별, 로그인 유지)
- **크로스 프로젝트 핀 가능** — 다른 프로젝트의 문서도 핀으로 등록하면 현재 프로젝트 사이드바에서 바로 접근
- 핀된 다른 프로젝트 문서는 `프로젝트명/파일명` 형태로 표시

## Mermaid 다이어그램 렌더링

마크다운 내 ````mermaid` 코드 블록이 SVG 다이어그램으로 자동 렌더링됨.

- **웹앱** (`DocumentsPanel.tsx`): mermaid npm 패키지 + dynamic import
- **홈포탈** (`home-portal.js`): mermaid CDN + `marked.parse()` 후 변환
- flowchart, sequence, class, state, ER, gantt, pie 등 지원
