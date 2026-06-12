# 25. 고객 온보딩 시스템 (PeterVoice Biz)

## 개요

신규 고객 가입부터 데몬 설치, 프로젝트 설정까지 자동으로 처리하는 온보딩 파이프라인.
웹에서 가입 → 관리자 승인 → 맥미니에 자동 설치/설정.

## 온보딩 흐름

```
1. 고객이 웹에서 가입 (role: pending)
2. 관리자가 승인 (role: user, onboarding_queue: active)
3. onboarding_daemon.py가 감지
4. SSH로 고객 맥미니 접속
5. Tailscale 설치 + 네트워크 연결
6. 데몬 설치 (git clone petervoice-daemon)
7. launchd 등록 + 초기 설정
8. 프로젝트 생성 + 스킬 배포
9. 상태: completed
```

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
| 데몬 | `scripts/onboarding_daemon.py` | 온보딩 자동화 데몬 **(미구현 — 설계만 완료)** |
| 웹 (sonolbot_web) | `app/api/admin/approve-user/route.ts` | 승인 API |
| 데몬 (petervoice-daemon) | `scripts/daemon/syncers/auto_updater.py` | 데몬/스킬 자동 업데이트 |
| 웹 | `lib/expert-presets.ts` | 전문가 프리셋 정의 |
