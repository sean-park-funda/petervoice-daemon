# 31. 웹 터미널 (WebSocket Terminal)

## 개요

브라우저에서 맥미니 터미널을 직접 사용하는 기능입니다. xterm.js + WebSocket + node-pty로 구현되어 있으며, 프로젝트별 tmux 세션에 연결됩니다.

## 접근 방법

### 독립 페이지
`/terminal` — 전용 터미널 페이지

### 채팅 내 토글
채팅 헤더 오른쪽 **터미널 아이콘** 클릭 → 채팅과 터미널을 나란히 표시 (리사이즈 가능한 스플리터).

## 아키텍처

```
브라우저 (xterm.js)
  ↓ WebSocket (wss://[tunnelUrl]/terminal?key=...&dir=...)
Home Portal (맥미니 :3000, node-pty + ws)
  ↓ pty (가상 터미널)
tmux 세션 (프로젝트별 지속)
```

### Home Portal WebSocket 서버

`home-portal.js`가 HTTP 서버와 같은 포트에서 WebSocket(`/terminal` 경로)을 서빙합니다.

**의존성:** `node-pty`, `ws` npm 패키지. 미설치 시 터미널 기능 비활성.

### 연결 파라미터

| 파라미터 | 값 | 설명 |
|---------|---|------|
| `key` | `{username}::{projectId}` | tmux 세션 식별자 |
| `dir` | 프로젝트 절대 경로 | 터미널 시작 디렉토리 |
| `Authorization` | `Bearer {api_key}` | 인증 헤더 |

### tmux 세션 관리

- 세션 키 `{username}::{projectId}` (콜론 → 언더스코어로 정규화)
- 기존 세션이 있으면 재사용 (`tmux attach`)
- 없으면 신규 생성 (`tmux new-session`)
- 브라우저 탭을 닫아도 tmux 세션은 유지 → 다시 접속 시 이전 상태 복원

## 테마

| 테마 | 설정 위치 | 연동 |
|------|---------|------|
| 다크 | 기본값 | 배경 `#0d0d0d`, 전경 `#e8e8e8` |
| 라이트 | 설정 페이지 | 배경 `#ffffff`, 전경 `#1a1a1a` |

터미널 테마는 **앱 테마(다크/라이트)와 독립적**으로 설정 가능합니다.  
`localStorage["terminal-theme"]`에 저장되며, 설정 페이지 변경이 StorageEvent로 즉시 반영됩니다.

## 프로젝트 전환

사이드바에서 프로젝트를 전환하면 터미널도 해당 프로젝트의 tmux 세션으로 재연결됩니다.  
브랜치 세션(`branch:N` 형식)도 지원합니다.

## 주의사항

- Home Portal이 실행 중이어야 합니다 (`node home-portal.js`)
- `node-pty`와 `ws` 패키지가 설치되어 있어야 합니다
- Cloudflare Tunnel이 연결되어 있어야 브라우저에서 WebSocket 접근 가능
- Auto-accept permissions (`--dangerously-skip-permissions`) 옵션이 터미널 연결 시 자동 적용됨

## 의존성 설치

```bash
cd ~/Projects/peter-voice/peter-voice-daemon/scripts
npm install node-pty ws
```
