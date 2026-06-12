# 23. 코드 리뷰 페이지 (/reviews)

## 개요

프로젝트의 Git 리포지토리 커밋을 웹에서 직접 리뷰하는 기능. 로컬 맥미니의 Git 리포를 Home Portal API로 접근하여 커밋 히스토리, diff 조회, 리뷰 스레드 관리를 제공.

## 접근 경로

- URL: `/reviews`
- 채팅 메뉴에서 진입 가능

## 아키텍처

```
웹 브라우저 (/reviews)
  ↓ Vercel API
  ↓
Home Portal (맥미니 :3000)
  ↓ Git CLI (git log, git diff)
  ↓
로컬 Git 리포지토리
```

## DB 테이블

| 테이블 | 설명 |
|--------|------|
| `project_repos` | 프로젝트별 등록된 Git 리포 경로 |
| `code_reviews` | 리뷰 세션 (커밋 범위, 상태, 리뷰어) |
| `review_threads` | 리뷰 스레드 (파일, 라인, 코멘트) |
| `review_thread_messages` | 스레드 내 메시지 (토론) |

## API (Vercel)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/repos` | 등록된 리포 목록 |
| POST | `/api/repos` | 리포 등록 |
| DELETE | `/api/repos` | 리포 삭제 |
| GET | `/api/reviews` | 리뷰 목록 |
| POST | `/api/reviews` | 리뷰 생성 |
| GET | `/api/reviews/[id]` | 리뷰 상세 + 스레드 |

## API (Home Portal)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/git/repos` | 맥미니의 git 리포 자동 탐색 |
| GET | `/api/git/branches` | 리포의 브랜치 목록 |
| GET | `/api/git/commits` | 커밋 히스토리 |
| GET | `/api/git/diff` | 단일 커밋 diff |
| GET | `/api/git/diff-range` | 커밋 범위 diff |

## 리포 등록 UI

리뷰 페이지에서 **[+ 리포 추가]** 버튼:
- 경로 직접 입력
- 또는 프로젝트 폴더 목록 클릭으로 git 리포 탐색
- Home Portal의 `/api/git/repos`가 `~/Projects/` 하위를 자동 스캔하여 git 리포 후보 제시
- 등록된 리포는 `/api/repos`에 저장, 삭제도 가능

## 관련 파일

| 위치 | 파일 | 설명 |
|------|------|------|
| 웹 | `app/reviews/page.tsx` | 리뷰 페이지 메인 |
| 웹 | `app/api/repos/route.ts` | 리포 CRUD |
| 웹 | `app/api/reviews/route.ts` | 리뷰 CRUD |
| 웹 | `app/api/reviews/[id]/route.ts` | 리뷰 상세 |
| 포탈 | `scripts/home-portal.js` | Git API (repos, branches, commits, diff) |
