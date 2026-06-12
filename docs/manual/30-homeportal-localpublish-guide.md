# 30. 홈포탈 & 로컬 퍼블리싱 종합 가이드

홈포탈(Home Portal)과 로컬 퍼블리싱(Local Publishing)은 피터보이스의 **Local-First 아키텍처**를 구현하는 핵심 인프라입니다.
고객의 맥미니를 웹서버로 활용하여, 클라우드 의존 없이 문서 서빙·사이트 호스팅·시스템 모니터링을 제공합니다.

---

## 1. 전체 아키텍처 개요

```
┌─────────────────────────────────────────────────────────────────┐
│                       인터넷 (사용자 브라우저)                       │
└───────────┬──────────────────────────────┬──────────────────────┘
            │ HTTPS                        │ HTTPS
            ▼                              ▼
┌───────────────────────┐    ┌──────────────────────────────────┐
│  Vercel (웹앱)         │    │  Cloudflare Edge (CDN + SSL)     │
│  peter-voice.vercel.app│    │  *.peter-voice.site              │
│                        │    └──────────────┬───────────────────┘
│  - 채팅 UI             │                   │ Cloudflare Tunnel
│  - API Routes          │                   ▼
│  - JWT 발급            │    ┌──────────────────────────────────┐
└────────────────────────┘    │       고객 맥미니 (24h 가동)       │
                              │                                   │
                              │  ┌─ Home Portal (:3000)           │
                              │  │   문서 서빙, 시스템 상태, Git API │
                              │  │                                │
                              │  ├─ 퍼블리시 사이트 (:3001~3099)   │
                              │  │   Next.js / Vite / 정적 사이트  │
                              │  │                                │
                              │  └─ cloudflared (Tunnel 프로세스)  │
                              │      → Cloudflare Edge 연결       │
                              │                                   │
                              │  ┌─ Claude 데몬                   │
                              │  │   Worker, Heartbeat, Manager   │
                              │  └───────────────────────────────│
                              └───────────────────────────────────┘
```

### 핵심 원칙

| 원칙 | 설명 |
|------|------|
| **Local-First** | 데이터(문서, 환경변수, 세션)는 맥미니에 원본 보관. Supabase에 복제하지 않음 |
| **Zero Cloud Dependency** | 문서/사이트 서빙에 Vercel·Supabase 불필요. Cloudflare Tunnel만 사용 |
| **중앙 API 중개** | Cloudflare API 키는 Vercel에만 보관. 맥미니에 API 키 배포 안 함 |
| **자동 복구** | cloudflared 60초 워치독, launchd KeepAlive, 터널 토큰 자동 재발급 |

---

## 2. Home Portal

### 2.1 개요

맥미니의 상태와 프로젝트를 웹에서 관리하는 **경량 Node.js 대시보드** (포트 3000).
`{username}.peter-voice.site`에서 접속합니다.

```
sean.peter-voice.site
  ├── Published Sites — 퍼블리시된 사이트 목록, 재빌드/중지 버튼
  ├── Projects — ~/Projects/ 폴더 브라우저
  └── System — 업타임, 디스크, 메모리, cloudflared/데몬 상태
```

### 2.2 인증 체계

외부(Cloudflare 터널) 접속 시 **JWT + 세션 쿠키** 인증.
로컬(localhost) 접속은 인증 불필요.

```
[피터보이스 웹앱]                    [Home Portal (맥미니)]
  │                                     │
  │ 1. "Home Portal" 클릭               │
  │ → GET /portal/redirect              │
  │ ← 302 + JWT (5분 만료)              │
  │                                     │
  │ 2. 브라우저 리다이렉트               │
  │    sean.peter-voice.site?auth=JWT ──→│
  │                                     │ 3. JWT 검증 (HMAC-SHA256)
  │                                     │ 4. Set-Cookie: pv_session (24h)
  │                                     │ 5. 302 → / (깨끗한 URL)
  │                                     │
  │ 이후: 쿠키 또는 Authorization: Bearer JWT
```

#### 보안 설정

| 항목 | 값 | 이유 |
|------|---|------|
| JWT 만료 | 5분 | 일회용 입장권 |
| 세션 쿠키 만료 | 24시간 | 매일 재인증 |
| SameSite | **Lax** | 크로스사이트 리다이렉트에서 쿠키 전달 필요 (Strict는 차단됨) |
| HttpOnly | true | JS 접근 차단 |
| Secure | true | HTTPS만 허용 |
| JWT secret | 유저 api_key | Vercel(DB)과 맥미니(config.json) 양쪽이 공유 |

> **주의**: `SameSite=Strict`는 `vercel.app → peter-voice.site` 리다이렉트 체인에서 쿠키를 차단합니다. 반드시 `Lax`를 사용하세요.

> **주의**: Vercel DB의 `users.api_key`와 맥미니 `config.json`의 `api_key`가 불일치하면 JWT 검증 실패. DB에서 api_key 재생성 시 config.json도 동기화 필요.

#### 크로스 도메인 직접 통신 (문서 API 등)

웹 UI(`peter-voice.vercel.app`)에서 Home Portal(`sean.peter-voice.site`)로 `fetch()` 호출 시:
- `SameSite=Lax` 쿠키는 서브리소스 요청(fetch)에서 전송되지 않음
- **해결**: `Authorization: Bearer {단기JWT}` 헤더로 인증
- 웹 UI가 `POST /api/portal/token`에서 5분 JWT를 발급받아 사용
- 토큰은 클라이언트에서 4분간 캐시 (만료 전 자동 갱신)

```
Home Portal CORS 설정:
  Access-Control-Allow-Origin: https://peter-voice.vercel.app
  Access-Control-Allow-Credentials: true
  Access-Control-Allow-Headers: Content-Type, Authorization, X-Api-Key
```

### 2.3 문서 서빙 (Local-First)

Home Portal의 **가장 중요한 역할** — 프로젝트의 `docs/` 폴더를 직접 서빙합니다.

#### 이전 방식 (폐기)
```
docs/*.md → DocsSyncer (30초마다) → Supabase documents 테이블 → 웹 UI
```

#### 현재 방식
```
docs/*.md → Home Portal API (Cloudflare Tunnel) → 웹 UI (직접 fetch)
```

**장점**:
- DB 싱크 지연 제거 → 파일 변경 즉시 반영
- Supabase 스토리지/트래픽 절감
- 바이너리(이미지/동영상) 직접 서빙 가능 (Vercel 6MB 제한 없음)
- 데이터 소스가 맥미니 파일 하나로 통일 → 버전 충돌 없음

#### 문서 API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/docs?dir={docsDir}` | 파일/폴더 트리 목록 (계층 구조) |
| GET | `/api/docs/read?dir=&path=` | 텍스트 파일 내용 읽기 |
| GET | `/api/docs/file?dir=&path=` | 바이너리 파일 서빙 (MIME 감지) |
| POST | `/api/docs/mkdir` | 폴더 생성 |
| POST | `/api/docs/upload` | 파일 업로드 (multipart, 50MB 제한) |
| POST | `/api/docs/copy` | 파일/폴더 복사 |
| POST | `/api/docs/move` | 파일/폴더 이동 |
| POST | `/api/docs/delete` | 파일/폴더 삭제 |
| POST | `/api/docs/write` | 텍스트 파일 생성/수정 |

#### 지원 파일 형식

| 타입 | 확장자 | 웹 UI 뷰어 |
|---|---|---|
| doc | .md, .mdx | 마크다운 렌더링 (GFM, Mermaid 다이어그램 포함) |
| image | .png, .jpg, .gif, .webp, .svg | 이미지 뷰어 |
| code | .py, .js, .ts, .tsx, .css 등 | Prism 신택스 하이라이팅 |
| pdf | .pdf | iframe PDF 뷰어 |
| video | .mp4, .webm, .mov | HTML5 비디오 플레이어 |
| audio | .mp3, .wav, .m4a | HTML5 오디오 플레이어 |

#### 갱신 전략

- 문서 탭 진입 시 **1회 자동 fetch** + **새로고침 버튼**
- Supabase Realtime 구독 제거 (에이전트가 수정 완료 후 사용자가 탭 여는 패턴이 현실)
- 맥미니 꺼져 있으면 → "오프라인" 표시 (Supabase fallback 안 함 — stale 데이터 혼란 방지)

### 2.4 Git API (코드 리뷰용)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/git/repos` | 맥미니의 git 리포 자동 탐색 |
| GET | `/api/git/branches?repo=` | 브랜치 목록 |
| GET | `/api/git/commits?repo=&branch=` | 커밋 히스토리 |
| GET | `/api/git/diff?repo=&commit=` | 단일 커밋 diff |
| GET | `/api/git/diff-range?repo=&from=&to=` | 커밋 범위 diff |

웹앱의 `/reviews` 페이지에서 코드 리뷰 시 사용.

### 2.5 프로젝트 디렉토리 탐색

Home Portal은 **두 디렉토리**를 모두 스캔합니다:
- `~/Projects/` — 사용자가 직접 배치한 프로젝트
- `~/.claude-daemon/projects/` — 데몬이 자동 생성한 프로젝트

프로젝트 디렉토리 결정 우선순위:
```
1. Supabase projects.directory (웹 UI 설정에서 지정한 경로)
2. config.json의 project_dirs 매핑
3. 자동 생성: ~/.claude-daemon/projects/{project_id}/
```

### 2.6 타이틀 표시

Host 헤더에서 username 추출:
- `sean.peter-voice.site` → **"sean's Mac Mini"**
- `joory.peter-voice.site` → **"joory's Mac Mini"**

### 2.7 파일 위치

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

---

## 3. 로컬 퍼블리싱

### 3.1 개요

맥미니에서 프로젝트를 빌드하고 인터넷에 직접 공개하는 기능.
GitHub/Vercel 없이, 맥미니 자체가 웹서버가 되어 **Cloudflare Tunnel로 HTTPS 제공**.

```
유저: "이 프로젝트 퍼블리싱해줘"
  ↓
에이전트: npm install → next build → launchd 서비스 등록 → Cloudflare 라우팅
  ↓
결과: https://sean-myshop.peter-voice.site 에서 접속 가능
```

### 3.2 URL 형식

```
https://{username}-{project}.peter-voice.site
```

- `peter-voice.site` — 피터보이스 소유 도메인 (Cloudflare DNS)
- **1단계 서브도메인만 사용** — Cloudflare Free 플랜에서 2단계 서브도메인(`*.sean.peter-voice.site`) SSL 미지원
- 예: `sean-peterdemo.peter-voice.site`, `kim-portfolio.peter-voice.site`

### 3.3 아키텍처

```
[맥미니]
  ┌─ launchd: com.petervoice.site.{project}
  │    → next start -p 3001 (프로젝트별 포트)
  │
  ├─ launchd: com.petervoice.site.{project2}
  │    → next start -p 3002
  │
  └─ cloudflared (터널 프로세스)
       → sean-project1.peter-voice.site → localhost:3001
       → sean-project2.peter-voice.site → localhost:3002
       → Cloudflare Edge (HTTPS, CDN)
       → 인터넷
```

#### 구성 요소

| 컴포넌트 | 위치 | 역할 |
|---------|------|------|
| `cloudflared` | 맥미니 (brew) | Cloudflare Tunnel 커넥터 |
| `site_manager.py` | `scripts/daemon/` | 빌드, 포트 할당, launchd 관리 |
| `publish.py` | `scripts/` | CLI 래퍼 (에이전트가 bash로 호출) |
| `/api/tunnel/*` | Vercel | Cloudflare API 대행 (DNS, ingress) |
| `cloudflare-tunnel.ts` | `lib/` | Cloudflare API 헬퍼 |
| `local-publish` 스킬 | DB → 자동 싱크 | 에이전트 사용 가이드 |

### 3.4 지원 프레임워크

| 프레임워크 | 감지 기준 | 빌드 명령 | 서빙 명령 |
|-----------|----------|----------|----------|
| Next.js | `package.json`에 `next` | `next build` | `next start -p {port}` |
| Vite | `package.json`에 `vite` | `vite build` | `vite preview --port {port}` |
| 정적 사이트 | `index.html` 존재 | 불필요 | `npx serve -l {port} -s .` |

로컬 DB가 필요한 앱은 **PGlite (PostgreSQL WASM)** 사용 가능 — Supabase 이전 시 SQL 변환 비용 0.

### 3.5 사용법 (에이전트 CLI)

```bash
# 퍼블리시
python3 ~/Projects/peter-voice/scripts/publish.py publish <project_id> <project_dir> --username <username>
# → https://sean-peterdemo.peter-voice.site

# 재빌드
python3 ~/Projects/peter-voice/scripts/publish.py rebuild <project_id>

# 언퍼블리시
python3 ~/Projects/peter-voice/scripts/publish.py unpublish <project_id> --username <username>

# 상태 확인
python3 ~/Projects/peter-voice/scripts/publish.py status
```

### 3.6 동작 흐름

#### publish 흐름
```
1. 프레임워크 감지 — package.json 분석
2. 포트 할당 — 3001~3099 범위에서 미사용 포트 자동 선택
3. 빌드 — npm install + 프레임워크별 빌드 명령
4. launchd 서비스 등록 — ~/Library/LaunchAgents/com.petervoice.site.{project}.plist
   - KeepAlive: true (크래시 시 자동 재시작)
   - .env.local 환경변수 자동 로드
5. Cloudflare 라우팅 — 서버 API(/api/tunnel/add-route) 호출
   - DNS CNAME 레코드 생성
   - Tunnel ingress 규칙 추가
   - cloudflared가 자동 반영 (재시작 불필요)
6. 상태 저장 — ~/.petervoice-sites/sites.json
```

#### rebuild 흐름
```
1. 프로젝트 재빌드 (npm install + next build)
2. launchctl kickstart 로 서비스 재시작
3. DNS/ingress 변경 없음 (기존 라우팅 유지)
```

#### unpublish 흐름
```
1. launchd 서비스 언로드 + plist 삭제
2. 서버 API(/api/tunnel/remove-route) 호출
   - DNS CNAME 레코드 삭제
   - Tunnel ingress 규칙 제거
3. 상태를 stopped로 업데이트
```

---

## 4. Cloudflare Tunnel 인프라

### 4.1 왜 Cloudflare Tunnel인가

Tailscale Funnel과의 비교:

| | Tailscale Funnel | Cloudflare Tunnel |
|---|---|---|
| 앱 수 제한 | **최대 3개** | **무제한** |
| 서브도메인 라우팅 | 불가 (path만) | **완전 지원** |
| 커스텀 도메인 | 불가 (.ts.net 고정) | **지원** |
| API 제어 | 최소 | **Full REST API** |
| 비용 | 무료 | **무료** |

### 4.2 터널 구조

- **맥미니당 1개 터널** (`pv-{username}`)
- 터널 내 **ingress 규칙**으로 서브도메인 → 포트 매핑
- **원격 관리 모드**: cloudflared가 API 변경을 자동 반영 (재시작 불필요)

```
[맥미니 A: sean]                        [맥미니 B: kim]
  cloudflared (pv-sean)                  cloudflared (pv-kim)
    ingress rules:                         ingress rules:
      sean.peter-voice.site → :3000         kim.peter-voice.site → :3000
      sean-app1 → :3001                     kim-shop → :3001
      sean-app2 → :3002                     kim-blog → :3002
```

### 4.3 DNS 구조

```
sean.peter-voice.site       CNAME  {tunnel_id}.cfargotunnel.com  (proxied)
sean-myshop.peter-voice.site  CNAME  {tunnel_id}.cfargotunnel.com  (proxied)
```

- 프로젝트별 개별 CNAME 레코드 (와일드카드 아님)
- Cloudflare Free 플랜: zone당 200 DNS 레코드 제한

### 4.4 Cloudflare 크리덴셜

| 항목 | 저장 위치 |
|------|-----------|
| 도메인: `peter-voice.site` | Cloudflare DNS |
| `CLOUDFLARE_API_TOKEN` | Vercel 환경변수 + 피터보이스 시크릿 |
| `CLOUDFLARE_ACCOUNT_ID` | Vercel 환경변수 |
| `CLOUDFLARE_ZONE_ID` | Vercel 환경변수 |
| `cloudflare_tunnel_id` | 맥미니 `config.json` |
| `cloudflare_tunnel_token` | 맥미니 `config.json` |

API Token 권한: `Account/Cloudflare Tunnel/Edit`, `Zone/DNS/Edit`, `Zone/Zone/Read`

### 4.5 서버 API (Vercel → Cloudflare 중개)

맥미니에 Cloudflare API 키를 배포하지 않고, Vercel이 대행합니다.

| API | 용도 | 호출 시점 |
|-----|------|-----------|
| `POST /api/tunnel/create` | 터널 생성 | 유저 온보딩 시 1회 |
| `POST /api/tunnel/add-route` | DNS + ingress 추가 | 퍼블리시 시 |
| `DELETE /api/tunnel/remove-route` | DNS + ingress 제거 | 언퍼블리시 시 |

### 4.6 자동 복구 메커니즘

데몬 메인 루프에서 **60초마다** cloudflared 상태 확인:

| 상황 | 자동 복구 |
|------|-----------|
| cloudflared 프로세스 사망 | launchd 재시작 (plist 복원 포함) |
| `cloudflare_tunnel_token` 누락 | Cloudflare API에서 토큰 재발급 (`CLOUDFLARE_API_TOKEN` 필요) |
| config.json에서 tunnel_id 삭제 | 경고 로그 (수동 복구 필요) |

> **인사이트**: Cloudflare Tunnel 토큰을 직접 조립하면 secret 인코딩이 달라 연결 실패합니다. 반드시 공식 `/token` API를 사용하세요.

---

## 5. 데몬의 Home Portal 자동 프로비저닝

### 5.1 시작 흐름

`claude_daemon.py`의 `_ensure_home_portal()` 함수가 데몬 시작 시 자동 실행:

```python
def _ensure_home_portal():
    # 1. 자기 username 조회
    me = api_request("GET", "/api/bot/me")
    username = me["username"]

    # 2. Home Portal launchd plist 확인/생성
    # 3. cloudflared 프로세스 확인
    # 4. 서버에 tunnel_url 등록 (멱등)
    tunnel_url = f"https://{username}.peter-voice.site"
    api_request("PATCH", "/api/bot/status", body={"tunnel_url": tunnel_url})
```

### 5.2 config.json 관련 설정

```json
{
  "cloudflare_tunnel_id": "abc-123-...",
  "cloudflare_tunnel_token": "base64...",
  "home_portal_enabled": true
}
```

### 5.3 사전 조건

맥미니에서 퍼블리싱을 사용하려면:

1. `cloudflared` 설치됨 (`brew install cloudflared`)
2. cloudflared 터널 프로세스 실행 중
3. `config.json`에 `cloudflare_tunnel_id` 설정됨
4. `node`, `npm` 설치됨
5. 프로젝트에 `package.json` 또는 `index.html` 존재

`local-publish` 스킬이 이 조건들을 자동 체크하고 미충족 시 설치/설정을 시도합니다.

---

## 6. Local-First 아키텍처 전환 현황

### 6.1 전환 완료

| 항목 | 이전 | 이후 | 상태 |
|------|------|------|------|
| **문서** | DocsSyncer → Supabase → 웹 UI | Home Portal API → 웹 UI 직접 | ✅ 완료 |
| **Home Portal 인증** | URL에 API Key 노출 | JWT + 세션 쿠키 | ✅ 완료 |
| **프로젝트 경로** | config.json `project_dirs` | DB `projects.directory` | ✅ 완료 |

### 6.2 미전환 (향후 계획)

| 항목 | 현재 | 계획 | 우선순위 |
|------|------|------|----------|
| **환경변수** | SecretsSyncer → Supabase → 로컬 | Home Portal API 직접 CRUD | 높음 (보안 개선) |
| **세션 요약** | PATCH /api/bot/session-summary | 로컬 파일 | 낮음 |
| **스킬** | SkillsSyncer → Supabase | Git 레포 포함 | 낮음 (현재 잘 작동) |
| **프롬프트** | Supabase → 세션 시작 시 fetch | 로컬 파일 | 비추 (웹 편집 편의성) |

### 6.3 제거 불가한 중앙 데이터

| 데이터 | 이유 |
|--------|------|
| 채팅 메시지 | 웹 UI ↔ 데몬 통신의 핵심 |
| 칸반/태스크 | 웹 UI에서 생성, 여러 프로젝트 간 공유 |
| 하트비트 상태 | 웹 UI에서 봇 온라인 표시 |
| 유저 인증 | 중앙 관리 필수 |

---

## 7. 트러블슈팅

### Home Portal

| 증상 | 원인 | 해결 |
|------|------|------|
| "인증 필요" (토큰 없음) | JWT 발급 실패 | Vercel 로그에서 `/portal/redirect` 에러 확인 |
| "인증 필요" (토큰 있음) | api_key 불일치 | DB `users.api_key`와 config.json `api_key` 비교 |
| 다른 유저의 콘텐츠 표시 | DNS가 잘못된 터널 가리킴 | Cloudflare DNS CNAME 대상 터널 확인 |
| 프로젝트 폴더 안 보임 | `~/.claude-daemon/projects/`에 생성됨 | 프로젝트 디렉토리를 `~/Projects/`로 이동/설정 |
| SameSite 쿠키 미전달 | SameSite=Strict 사용 | SameSite=Lax로 변경 |
| cloudflared PATH 감지 실패 | `which cloudflared` 실패 | `/opt/homebrew/bin/cloudflared` 직접 경로 체크 |

### 로컬 퍼블리싱

| 증상 | 원인 | 해결 |
|------|------|------|
| 터널 끊김 | cloudflared 프로세스 크래시 | 데몬이 60초마다 자동 감지 및 복구 |
| 터널 토큰 누락 | config에서 삭제됨 | 데몬이 Cloudflare API로 자동 복구 |
| 사이트 접속 불가 | 맥미니 꺼짐 | 맥미니 전원 확인 (장기: Cloudflare Workers fallback 페이지) |
| DNS 전파 지연 | 최초 DNS 설정 | 1~2분 대기 |
| SSL 오류 | 2단계 서브도메인 사용 | 1단계만 사용 (`sean-app.peter-voice.site`) |
| 팝업 차단 | fetch 후 window.open | 클릭 즉시 window.open('about:blank') 후 URL 변경 |

### cloudflared

| 증상 | 원인 | 해결 |
|------|------|------|
| 연결 실패 (토큰 오류) | 토큰 직접 조립 시 secret 인코딩 불일치 | 공식 `/token` API 사용 |
| plist 없음 | 수동 삭제 또는 미설치 | 데몬 워치독이 자동 복원 |
| 한 호스트가 잘못된 터널로 라우팅 | DNS가 다른 터널 가리킴 | DNS CNAME + ingress 규칙 모두 변경 |

---

## 8. 제한사항

| 항목 | 제한 |
|------|------|
| 동시 퍼블리시 사이트 | 포트 3001~3099 (최대 99개) |
| Cloudflare Free DNS 레코드 | 200개 (Pro 3,500개) |
| 맥미니 의존성 | 꺼지면 사이트/문서 접속 불가 |
| SSL | Cloudflare Free는 1단계 서브도메인만 지원 |
| 파일 업로드 | 50MB 제한 |
| 최초 DNS 전파 | 1~2분 소요 |

---

## 9. 고객 온보딩 시 설정

### install.sh 퍼블리싱 단계

```bash
# 1. cloudflared 설치
brew install cloudflared

# 2. 사이트 디렉토리 생성
mkdir -p ~/.petervoice-sites/logs

# 3. 서버 API로 터널 생성
TUNNEL_RESULT=$(curl -s -X POST "$API_URL/api/tunnel/create" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"$USERNAME\"}")

# 4. config.json에 tunnel_id 저장

# 5. cloudflared 서비스 설치 + Home Portal 시작
python3 scripts/publish.py home-portal --username "$USERNAME"
```

데몬 시작 시 `_ensure_home_portal()`이 자동으로 Home Portal + 터널 URL 등록을 수행하므로, install.sh에서 초기 설정만 하면 이후는 자동입니다.

---

## 10. 핵심 결정사항 기록 (의사결정 로그)

| 시기 | 결정 | 이유 |
|------|------|------|
| 2026-03 Week 7 | Cloudflare Tunnel 채택 (Tailscale Funnel 탈락) | 앱 3개 제한, 커스텀 도메인 불가 |
| 2026-03 Week 7 | `peter-voice.site` 도메인 구매 | 유저 사이트 전용 도메인 필요 |
| 2026-03 Week 7 | 1단계 서브도메인 구조 (`sean-app.peter-voice.site`) | Free 플랜 2단계 SSL 미지원 |
| 2026-03 Week 7 | Vercel이 Cloudflare API 중개 | 맥미니에 API 키 배포 불필요 |
| 2026-03 Week 7 | DocsSyncer 비활성화 → Home Portal 직접 서빙 | 실시간 반영 + DB 부하 감소 |
| 2026-03 Week 7 | SameSite=Lax 사용 (Strict 아님) | 크로스사이트 리다이렉트 쿠키 차단 문제 |
| 2026-03 Week 7 | username 기반 URL (bot_name 아님) | bot_name 중복 가능("Peter" 다수) |
| 2026-04 Week 8 | PGlite 채택 (로컬 퍼블리싱 앱 DB) | Supabase 이전 시 SQL 변환 비용 0 |
| 2026-04 Week 9 | 시스템 프롬프트 비대화 방지 | 상세 절차는 스킬로 분리, 프롬프트에 한 줄만 |
| 2026-04 | 터널 토큰 자동 복구 메커니즘 | 60초 워치독 기반, Cloudflare API로 토큰 재발급 |

---

## 11. 관련 문서 참조

| 문서 | 내용 |
|------|------|
| [01-architecture.md](01-architecture.md) | 전체 시스템 아키텍처 |
| [03-daemon.md](03-daemon.md) | Claude 데몬 상세 (cloudflared 헬스체크 포함) |
| [10-documents.md](10-documents.md) | 문서 관리 상세 (DocumentsPanel, 뷰어 등) |
| [20-local-publishing.md](20-local-publishing.md) | 로컬 퍼블리싱 상세 |
| [21-home-portal.md](21-home-portal.md) | Home Portal 상세 |
| [25-onboarding.md](25-onboarding.md) | 고객 온보딩 시스템 |
| [plans/local-first-architecture.md](../plans/local-first-architecture.md) | Local-First 전환 계획 (Phase 0~3) |
| [plans/local-publishing.md](../plans/local-publishing.md) | 로컬 퍼블리싱 원본 설계서 |
| [plans/home-portal-auth.md](../plans/home-portal-auth.md) | Home Portal 인증 설계 |
| [plans/docs-local-first.md](../plans/docs-local-first.md) | 문서 Local-First 전환 계획 |
