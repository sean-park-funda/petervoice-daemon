# PeterVoice Daemon (개발 환경)

## 개요
이 레포(petervoice-daemon)는 피터보이스 데몬 코드.
Sean의 개발 환경 — 수정 가능하며, 푸시하면 고객에게 AutoUpdater로 자동 배포됨.

## 구조
- `scripts/claude_daemon.py` — 메인 데몬
- `scripts/daemon/` — 모듈화된 데몬 코드
- `scripts/home-portal.js` — 로컬 HTTP API
- `requirements.txt` — Python 의존성

## 규칙
- 커밋 메시지: 영어
- 푸시 후 고객 반영까지 ~5분 (AutoUpdater)
- 웹앱 코드 작업은 peter-voice-web 프로젝트에서
