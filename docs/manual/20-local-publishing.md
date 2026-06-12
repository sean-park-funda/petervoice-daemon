# 20. 로컬 퍼블리싱

맥미니에서 프로젝트를 빌드하고 인터넷에 직접 공개하는 기능.
GitHub/Vercel 없이, 맥미니 자체가 웹서버가 되어 Cloudflare Tunnel로 HTTPS 제공.

## 개요

```
유저: "이 프로젝트 퍼블리싱해줘"
  ↓
에이전트: npm install → next build → launchd 서비스 등록 → Cloudflare 라우팅
  ↓
결과: https://sean-myshop.peter-voice.site 에서 접속 가능
```

## URL 형식

```
https://{username}-{project}.peter-voice.site
```

- `peter-voice.site` — 피터보이스가 소유한 도메인 (Cloudflare DNS)
- 1단계 서브도메인만 사용 (Cloudflare Free 플랜 SSL 제약)
- 예: `sean-peterdemo.peter-voice.site`, `kim-portfolio.peter-voice.site`

## 아키텍처

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

### 구성 요소

| 컴포넌트 | 위치 | 역할 |
|---------|------|------|
| `cloudflared` | 맥미니 (brew) | Cloudflare Tunnel 커넥터 |
| `site_manager.py` | `scripts/daemon/` | 빌드, 포트 할당, launchd 관리 |
| `publish.py` | `scripts/` | CLI 래퍼 (에이전트가 bash로 호출) |
| `/api/tunnel/*` | Vercel | Cloudflare API 대행 (DNS, ingress) |
| `cloudflare-tunnel.ts` | `lib/` | Cloudflare API 헬퍼 |
| `local-publish` 스킬 | DB → 자동 싱크 | 에이전트 사용 가이드 |

## 지원 프레임워크

| 프레임워크 | 감지 기준 | 빌드 명령 | 서빙 명령 |
|-----------|----------|----------|----------|
| Next.js | `package.json`에 `next` | `next build` | `next start -p {port}` |
| Vite | `package.json`에 `vite` | `vite build` | `vite preview --port {port}` |
| 정적 사이트 | `index.html` 존재 | 불필요 | `npx serve -l {port} -s .` |

## 사용법 (에이전트 CLI)

### 퍼블리시

```bash
python3 ~/Projects/peter-voice/scripts/publish.py publish <project_id> <project_dir> --username <username>
```

예시:
```bash
python3 ~/Projects/peter-voice/scripts/publish.py publish peterdemo /Users/sean/Projects/peterdemo --username sean
# → https://sean-peterdemo.peter-voice.site
```

### 재빌드

```bash
python3 ~/Projects/peter-voice/scripts/publish.py rebuild <project_id>
```

### 언퍼블리시

```bash
python3 ~/Projects/peter-voice/scripts/publish.py unpublish <project_id> --username <username>
```

### 상태 확인

```bash
python3 ~/Projects/peter-voice/scripts/publish.py status
```

## 동작 상세

### publish 흐름

1. **프레임워크 감지** — `package.json` 분석
2. **포트 할당** — 3001~3099 범위에서 미사용 포트 자동 선택
3. **빌드** — `npm install` + 프레임워크별 빌드 명령
4. **launchd 서비스 등록** — `~/Library/LaunchAgents/com.petervoice.site.{project}.plist`
   - `KeepAlive: true` (크래시 시 자동 재시작)
   - `.env.local` 환경변수 자동 로드
5. **Cloudflare 라우팅** — 서버 API(`/api/tunnel/add-route`) 호출
   - DNS CNAME 레코드 생성
   - Tunnel ingress 규칙 추가
   - `cloudflared`가 자동 반영 (재시작 불필요)
6. **상태 저장** — `~/.petervoice-sites/sites.json`

### rebuild 흐름

1. 프로젝트 재빌드 (`npm install` + `next build`)
2. `launchctl kickstart` 로 서비스 재시작
3. DNS/ingress 변경 없음 (기존 라우팅 유지)

### unpublish 흐름

1. launchd 서비스 언로드 + plist 삭제
2. 서버 API(`/api/tunnel/remove-route`) 호출
   - DNS CNAME 레코드 삭제
   - Tunnel ingress 규칙 제거
3. 상태를 `stopped`로 업데이트

## 서버 API (Vercel)

### POST /api/tunnel/create

유저 온보딩 시 Cloudflare Tunnel 생성.

```json
// Request
{ "username": "sean" }

// Response
{ "tunnelId": "xxx", "tunnelToken": "base64...", "message": "터널 pv-sean 생성 완료" }
```

### POST /api/tunnel/add-route

퍼블리시 시 DNS + ingress 추가.

```json
// Request
{ "username": "sean", "project": "myshop", "port": 3001, "tunnelId": "xxx" }

// Response
{ "hostname": "sean-myshop.peter-voice.site", "url": "https://...", "dnsRecordId": "xxx" }
```

### DELETE /api/tunnel/remove-route

언퍼블리시 시 DNS + ingress 제거.

```json
// Request
{ "username": "sean", "project": "myshop", "tunnelId": "xxx" }

// Response
{ "ok": true, "message": "sean-myshop.peter-voice.site 언퍼블리시 완료" }
```

## Cloudflare 설정

### 크리덴셜

| 항목 | 저장 위치 |
|------|-----------|
| 도메인: `peter-voice.site` | Cloudflare DNS |
| `CLOUDFLARE_API_TOKEN` | Vercel 환경변수 + 피터보이스 시크릿 |
| `CLOUDFLARE_ACCOUNT_ID` | Vercel 환경변수 |
| `CLOUDFLARE_ZONE_ID` | Vercel 환경변수 |
| `cloudflare_tunnel_id` | 맥미니 `config.json` |
| `cloudflare_tunnel_token` | 맥미니 `config.json` |

### 터널 구조

- 맥미니당 1개 터널 (`pv-{username}`)
- 터널 내 ingress 규칙으로 서브도메인 → 포트 매핑
- 원격 관리 모드: `cloudflared`가 API 변경을 자동 반영

### DNS 구조

```
sean-myshop.peter-voice.site  CNAME  {tunnel_id}.cfargotunnel.com  (proxied)
sean-blog.peter-voice.site    CNAME  {tunnel_id}.cfargotunnel.com  (proxied)
```

- 프로젝트별 개별 CNAME 레코드 (와일드카드 아님)
- Cloudflare Free 플랜: zone당 200 DNS 레코드 제한

## 사전 조건

맥미니에서 퍼블리싱을 사용하려면:

1. `cloudflared` 설치됨 (`brew install cloudflared`)
2. `cloudflared` 터널 프로세스 실행 중
3. `config.json`에 `cloudflare_tunnel_id` 설정됨
4. `node`, `npm` 설치됨
5. 프로젝트에 `package.json` 또는 `index.html` 존재

`local-publish` 스킬이 이 조건들을 자동 체크하고 미충족 시 설치/설정을 시도함.

## 제한사항

- 포트 범위: 3001~3099 (동시 최대 99개 사이트)
- Cloudflare Free DNS 레코드: 200개 (Pro 플랜 시 3,500개)
- 맥미니가 꺼지면 사이트 접속 불가
- 최초 DNS 전파: 1~2분 소요
- 2단계 서브도메인(`app.sean.peter-voice.site`) SSL 불가 (Free 플랜)

## 파일 위치

```
peter-voice-web/ (sonolbot_web)
├── app/api/tunnel/
│   ├── create/route.ts         — 터널 생성 API
│   ├── add-route/route.ts      — DNS + ingress 추가 API
│   └── remove-route/route.ts   — DNS + ingress 제거 API
└── lib/
    └── cloudflare-tunnel.ts    — Cloudflare API 헬퍼

peter-voice-daemon/ (petervoice-daemon)
├── scripts/
│   ├── publish.py              — CLI 래퍼
│   └── daemon/
│       └── site_manager.py     — 빌드, 포트, launchd 관리
```
