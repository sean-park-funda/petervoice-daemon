# Peter Voice 종합 매뉴얼

Peter Voice — 음성 기반 AI 어시스턴트 플랫폼.
웹 UI(채팅/음성)로 메시지를 보내면 로컬 Mac의 Claude Code CLI가 프로젝트 코드를 읽고 수정하는 개발 도우미.

## 매뉴얼 구조

| # | 문서 | 설명 |
|---|------|------|
| 01 | [아키텍처 개요](./01-architecture.md) | 전체 시스템 구조, 기술 스택, 데이터 흐름 |
| 02 | [데이터베이스](./02-database.md) | Supabase 테이블 스키마, RLS, 마이그레이션 |
| 03 | [Claude 데몬](./03-daemon.md) | claude_daemon.py, launchd, 세션 관리, 매니저 스레드 |
| 04 | [웹 UI — 채팅](./04-ui-chat.md) | 채팅 인터페이스, 메시지 버블, 툴로그, 자동스크롤, 파일첨부 |
| 05 | [웹 UI — 음성모드](./05-ui-voice.md) | STT, TTS, 음성인식, 자동전송, 음성모드 상태관리 |
| 06 | [프로젝트 관리](./06-projects.md) | 멀티프로젝트, 드롭다운, 하이라이트, unread 추적, 배포 URL |
| 07 | [인증 및 멀티유저](./07-auth-multiuser.md) | 로그인, OAuth(Google/Notion), 유저별 토큰, 플랫폼 인증 구조 |
| 08 | [환경변수 및 시크릿](./08-secrets.md) | SecretsPanel, 유저별 환경변수, OS env 싱크 |
| 09 | [일정 및 할일](./09-schedule-todo.md) | 구글 캘린더 연동, 할일 관리, 서브태스크, 오늘의 목표 |
| 10 | [문서 관리](./10-documents.md) | docs 폴더, DB 싱크, 마크다운 프리뷰, 문서 참조 |
| 11 | [에이전트 간 통신](./11-relay.md) | Relay API, Consult, Delegate 패턴 |
| 12 | [하트비트](./12-heartbeat.md) | 자율 반복 작업, 태스크 등록, 인터벌 관리 |
| 13 | [파일 공유](./13-file-sharing.md) | 로컬 파일 → 웹 링크 업로드, 스토리지 관리 |
| 14 | [스킬 관리](./14-skills.md) | 스킬 설치, DB 싱크, _common 프롬프트 |
| 15 | [디자인 및 테마](./15-design-theme.md) | 다크/라이트 모드, 헤더/사이드바 레이아웃 |
| 16 | [에이전트용 API](./16-agent-api.md) | 대화/문서 검색, 릴레이, 파일업로드, 하트비트 등 에이전트 호출 가능 API |
| 17 | [브랜치 세션 & 칸반 보드](./17-kanban.md) | 브랜치 독립 세션, 부모 맥락 계승, 칸반 상태 관리, 팀 협업, 필터/검색/통계 |
| 18 | [매니저](./18-manager.md) | 자율 멀티턴 작업 관리, /do 명령, 자율 순회, 워크플로우 |
| 19 | [프롬프트 관리](./19-prompts.md) | 시스템 프롬프트, 멀티유저 분리, 에이전트 메모, 3-Layer 구조 |
| 20 | [로컬 퍼블리싱](./20-local-publishing.md) | Cloudflare Tunnel 기반 맥미니 직접 호스팅, site_manager, publish CLI |
| 21 | [Home Portal](./21-home-portal.md) | 맥미니 대시보드, JWT 세션 인증, 프로젝트 브라우저, 트러블슈팅 |
| 22 | [에이전트 소환](./22-summon.md) | 다중 에이전트 리뷰, 비판자+전문가 협업, @멘션, 라운드 루프 |
| 23 | [코드 리뷰](./23-code-reviews.md) | /reviews 페이지, Git diff 조회, 리포 등록, 리뷰 스레드 |
| 24 | [에이전트 대시보드](./24-agent-dashboard.md) | /agents 페이지, 프롬프트 편집, 관리자 유저 선택 |
| 25 | [고객 온보딩](./25-onboarding.md) | 자동 프로비저닝, 전문가 프리셋, 번들 스킬 배포 |
| 26 | [장기 작업 & 협업](./26-long-tasks-collaboration.md) | /do 멀티턴, HeartBeat, Stall Detection, Relay, Summon 협업 패턴 |
| 27 | [고객 매니지먼트](./27-customer-management.md) | 고객 현황, 채팅 기록 조회, Tailscale SSH 원격 접속, 장애 복구 |
| 28 | [문서의 기억화](./28-doc-memory.md) | 🚧 계획 — docs 인덱싱, doc-search 스킬, 문서 기반 자동 응답 |
| 29 | [크로스유저 에이전트 연결](./29-cross-user-connections.md) | 다른 유저 에이전트 간 친구 기반 통신, 양방향 수신함, /api/relay/external, 핸드오프 |
| 30 | [홈포탈 & 로컬 퍼블리싱 종합](./30-homeportal-localpublish-guide.md) | Home Portal + Local Publishing + Cloudflare Tunnel + Local-First 아키텍처 통합 가이드 |
| 31 | [웹 터미널](./31-terminal-page.md) | xterm.js WebSocket 터미널, Home Portal node-pty, 프로젝트별 tmux 세션 |
| 32 | [엔진 선택 (Claude vs Codex)](./32-codex-engine.md) | Claude Code CLI / OpenAI Codex CLI 선택, 프로젝트·브랜치별 설정, 자동 업데이트 |
| 33 | [사용량 대시보드](./33-usage-dashboard.md) | 토큰/비용 분석, 모델별·프로젝트별·일별 집계, 캐시 효율 확인 |

---

## 대화 이력 기반 기능 분류

이 매뉴얼은 2026-02-16 ~ 2026-04-14 기간의 실제 개발 대화를 분석하여 작성되었습니다.

### 작성 방법
1. 대화 이력에서 Peter Voice 자체 기능 관련 대화 추출 및 분류
2. 현재 코드베이스 확인으로 실제 구현 상태 반영
3. 각 섹션별 개별 문서로 상세 내용 기술
