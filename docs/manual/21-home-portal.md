# 21. Home Portal

맥미니의 상태와 프로젝트를 웹에서 관리하는 대시보드.
`{username}.peter-voice.site`에서 접속.

## 개요

```
sean.peter-voice.site
  ├── Published Sites — 퍼블리시된 사이트 목록, 재빌드/중지 버튼
  ├── Projects — ~/Projects/ 폴더 브라우저
  └── System — 업타임, 디스크, 메모리, cloudflared/데몬 상태
```

## 인증

외부(Cloudflare 터널) 접속 시 JWT 세션 인증 사용.
로컬(localhost) 접속은 인증 불필요.

### 흐름

```
[피터보이스 웹앱]                    [Home Portal (맥미니)]
  │                                     │
  │ 1. "Home Portal" 클릭               │
  │ → GET /portal/redirect              │
  │ ← 302 + JWT (5분 만료)              │
  │                                     │
  │ 2. 브라우저 리다이렉트 ──────────→   │
  │    sean.peter-voice.site?auth=JWT   │
  │                                     │ 3. JWT 검증 (HMAC-SHA256)
  │                                     │ 4. Set-Cookie: pv_session (24h)
  │                                     │ 5. 302 → / (깨끗한 URL)
  │                                     │
  │                                     │ 이후 요청은 쿠키로 인증
```

### 핵심 보안 설정

| 항목 | 값 | 이유 |
|------|---|------|
| JWT 만료 | 5분 | 일회용 입장권 |
| 세션 쿠키 만료 | 24시간 | 매일 재인증 |
| SameSite | **Lax** | 크로스사이트 리다이렉트 체인에서 쿠키 전달 필요 |
| HttpOnly | true | JS 접근 차단 |
| Secure | true | HTTPS만 허용 |
| JWT secret | 유저 api_key | Vercel(DB)과 맥미니(config.json) 양쪽이 공유 |

> **주의**: Vercel DB의 `users.api_key`와 맥미니 `config.json`의 `api_key`가 반드시 일치해야 JWT 검증이 성공한다. 불일치 시 "인증 필요" 오류 발생.

## 프로젝트 디렉토리

### Home Portal이 보여주는 것

Home Portal의 Projects 섹션은 **`~/Projects/`와 `~/.claude-daemon/projects/` 두 디렉토리**를 모두 스캔한다. (기존에는 `~/Projects/`만 탐색했으나 이중 탐색으로 확장됨)

### 프로젝트가 생성되는 위치

데몬은 프로젝트 디렉토리를 다음 우선순위로 결정한다:

```
1. Supabase projects.directory (웹 UI 설정에서 지정한 경로)
2. config.json의 project_dirs 매핑
3. 자동 생성: ~/.claude-daemon/projects/{project_id}/
```

3번으로 자동 생성된 프로젝트는 `~/Projects/`가 아닌 `~/.claude-daemon/projects/`에 위치하므로 **Home Portal에 보이지 않는다.**

### 해결 방법

프로젝트가 Home Portal에 보이려면:
- 웹 UI 프로젝트 설정에서 디렉토리를 `~/Projects/{project_id}/`로 지정
- 또는 에이전트가 프로젝트 생성 시 `~/Projects/` 하위에 배치

퍼블리시된 사이트는 디렉토리 위치와 무관하게 **Published Sites** 섹션에 항상 표시된다 (`~/.petervoice-sites/sites.json`에서 읽음).

## 타이틀 표시

Home Portal 타이틀은 요청의 Host 헤더에서 추출:
- `sean.peter-voice.site` → **"sean's Mac Mini"**
- `joory.peter-voice.site` → **"joory's Mac Mini"**
- 로컬 접속 시 → OS 유저명 사용

## 파일 위치

```
peter-voice-daemon/ (petervoice-daemon)
└── scripts/
    └── home-portal.js         — 경량 Node.js 웹서버 (포트 3000)

peter-voice-web/ (sonolbot_web)
├── lib/
│   └── jwt.ts                 — JWT 생성/검증 유틸리티
└── app/portal/
    └── redirect/route.ts      — 서버사이드 JWT 발급 + 리다이렉트
```

## Cloudflare 라우팅

```
{username}.peter-voice.site → Cloudflare Tunnel → localhost:3000 (Home Portal)
{username}-{project}.peter-voice.site → Cloudflare Tunnel → localhost:300X (퍼블리시된 사이트)
```

- 맥미니당 1개 터널, Home Portal은 항상 포트 3000
- DNS CNAME이 해당 맥미니의 터널을 가리켜야 함
- **주의**: 한 호스트명이 여러 터널 ingress에 등록되면 DNS가 가리키는 터널로 라우팅됨. 터널 이동 시 ingress + DNS 모두 변경 필요.

## Git API (코드 리뷰용)

Home Portal은 코드 리뷰 기능을 위한 Git API 5개를 제공한다:

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/git/repos` | 맥미니의 git 리포 자동 탐색 |
| GET | `/api/git/branches?repo=` | 리포의 브랜치 목록 |
| GET | `/api/git/commits?repo=&branch=` | 커밋 히스토리 |
| GET | `/api/git/diff?repo=&commit=` | 단일 커밋 diff |
| GET | `/api/git/diff-range?repo=&from=&to=` | 커밋 범위 diff |

이 API들은 웹앱의 `/reviews` 페이지에서 코드 리뷰 시 사용됨.

## 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| "인증 필요" (토큰 없음) | JWT 발급 실패 | Vercel 로그에서 `/portal/redirect` 에러 확인 |
| "인증 필요" (토큰 있음) | api_key 불일치 | DB `users.api_key`와 config.json `api_key` 비교 |
| 다른 유저의 콘텐츠 표시 | DNS가 잘못된 터널 가리킴 | Cloudflare DNS CNAME 대상 터널 확인 |
| 프로젝트 폴더 안 보임 | `~/.claude-daemon/projects/`에 생성됨 | 프로젝트 디렉토리를 `~/Projects/`로 이동/설정 |
| SameSite 쿠키 미전달 | SameSite=Strict 사용 | SameSite=Lax 사용 (크로스사이트 리다이렉트 허용) |
| 터널 끊김 / cloudflared 죽음 | cloudflared 프로세스 크래시 또는 plist 삭제 | 데몬이 60초마다 자동 감지 및 복구 (수동 개입 불필요) |
| 터널 토큰 누락 (config.json) | config에서 `cloudflare_tunnel_token` 삭제됨 | 데몬이 Cloudflare API로 자동 복구 (`CLOUDFLARE_API_TOKEN` 필요) |
