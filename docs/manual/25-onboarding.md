# 25. 고객 온보딩 시스템 (PeterVoice Biz)

## 개요

신규 고객 가입부터 데몬 설치, 프로젝트 설정까지 자동으로 처리하는 온보딩 파이프라인.
웹에서 가입 → 관리자 승인 → 맥미니에 자동 설치/설정.

## 온보딩 흐름

```
[Phase 0] 고객이 웹에서 가입 (role: pending) → 관리자가 /admin 에서 승인 (role: user, onboarding_queue: active)
          승인 시 자동 생성: api_key · provisioning_configs(Tailscale 키) · 온보딩/일반 프롬프트 · 설치 명령
[Phase 1] 고객 맥미니 터미널에 설치 명령 1줄 실행 (사람 손 필요, 15~25분)
          curl …/api/install?key=… | bash → Xcode CLT · Homebrew · Tailscale · SSH 원격로그인 ON · 서버에 보고
          (install-config 는 재실행 안전 — "1회용" 이라는 옛 설명은 폐기)
[Phase 2] onboarding_daemon.py 가 큐를 감지하고 SSH 로 들어가 전부 자동 (~10분)
          node · python · git · claude CLI → 데몬 git clone → 로그인 → config → launchd → 프로젝트·스킬 배포
[Phase 3] 상태 completed → 고객이 '일반' 프로젝트에서 대화 시작
```

**반자동이다.** 사람이 반드시 개입하는 지점은 Phase 0 의 승인 클릭과 Phase 1 의 설치 명령 실행뿐이고, 그 뒤는 데몬이 한다.
(2026-09-17 정정: 이전 판은 데몬이 Tailscale 설치까지 하는 것으로 적었고, 하단 표에는 "미구현"으로 적혀 있었다 — 둘 다 사실과 달랐다. 실제 운영 중이며 2026-07-06 고객 건이 이 경로로 `install_progress 10/10 completed` 처리됐다.)

## 온보딩 데몬 (onboarding_daemon.py)

관리자(Sean) 맥미니에서 실행되는 별도 데몬. 온보딩 큐를 폴링하여 신규 고객을 자동 프로비저닝.

### 주요 설정

| 설정 | 값 | 설명 |
|------|---|------|
| MAX_CONCURRENT | 10 | 동시 온보딩 처리 수 |
| 상태 전이 | active → installing → completed | 중간 단계 최소화 |

### 세션 자동 정리

온보딩 완료 시 `installing → completed` 직행 처리. 이전의 `building → verifying → cleanup` 중간 단계는 제거됨.

## 전문가 프리셋 시스템

고객에게 제공되는 AI 전문가 프리셋 (7종):

| 프리셋 | 설명 |
|--------|------|
| 기본 어시스턴트 | 범용 AI 비서 |
| 마케터 | 마케팅 전략/콘텐츠 |
| 디자이너 | UI/UX 디자인 |
| AI 피디 | AI 영상/이미지 제작 |
| 웹 개발자 | 로컬 퍼스트 웹 개발 |
| 콘텐츠 작가 | 글쓰기/블로그 |
| 데이터 분석가 | 데이터 분석/시각화 |

### 사이드바 동적 연동

`ChatWindow.tsx`에서 `EXPERT_PRESETS` 배열을 동적 import. 새 프리셋 추가 시 `expert-presets.ts` 파일만 수정하면 사이드바 UI에 자동 반영.

## 웹 개발자 로컬 퍼스트 스택

Vercel/Supabase 클라우드 의존 없이 고객 맥미니에서 직접 실행:
- **로컬 퍼블리싱**: `publish.py` + Cloudflare Tunnel
- **로컬 DB**: PGlite (PostgreSQL WASM)
- **자동 URL**: `{username}-{project}.peter-voice.site`

## 번들 스킬 자동 배포

데몬 레포의 `skills/` 폴더에 포함된 7종 스킬이 AutoUpdater를 통해 전 고객에게 자동 배포:
notion-api, pdf, gmail, google-calendar, local-publish, weather, find-skills

## approve-user API

관리자 승인 시 `onboarding_queue`에 `"active"` 상태로 등록. 승인 즉시 온보딩 시작.

```
PATCH /api/admin/approve-user
  → users.role: "pending" → "user"
  → onboarding_queue 레코드 생성 (status: "active")
```

## 관련 파일

| 레포 | 파일 | 설명 |
|------|------|------|
| 온보딩 데몬 (Sean 맥, 별도) | 실행본 `~/.claude-daemon-onboarding/onboarding_daemon.py` · launchd `com.petervoice.onboarding-daemon` · 소스 petervoice-biz 레포 `scripts/onboarding_daemon.py` | 온보딩 자동화 데몬 — **운영 중** (2026-09-17 정정. 이 데몬 레포에는 들어 있지 않다. 실행본과 레포 소스가 다를 수 있으니 실행본이 기준) |
| 웹 (sonolbot_web) | `app/api/admin/approve-user/route.ts` | 승인 API |
| 데몬 (petervoice-daemon) | `scripts/daemon/syncers/auto_updater.py` | 데몬/스킬 자동 업데이트 |
| 웹 | `lib/expert-presets.ts` | 전문가 프리셋 정의 |
