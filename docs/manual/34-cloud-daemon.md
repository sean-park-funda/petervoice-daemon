# 34. 클라우드 데몬 (멀티테넌트)

> 2026-07-23 도입. 계획 원문: peter-voice 프로젝트 `docs/plans/2026-07-23-cloud-first-signup.md`

## 개요

가입하면 누구나 즉시 쓰는 **클라우드 기반 피터보이스**의 실행 인프라.
공용 호스트 1대에서 데몬 프로세스 1개(`cloud_daemon.py`)가 `daemon_target=cloud` 유저 전원을 서비스한다.
유저는 웹 설정 없이 채팅에서 `클로드 로그인`(또는 `로그인`·`재로그인`·`연결` 등, 공백·대소문자 무시) 만 입력하면 본인 클로드 구독으로 AI 비서가 활성화된다.

## 유저 모델

`users.daemon_target` 이 유저 종류의 단일 진실:

| 값 | 의미 | 데몬 위치 |
|----|------|----------|
| `cloud` (가입 기본) | 클라우드 유저 | 공용 호스트 multi 데몬 |
| `self` | 맥미니 프로비저닝 완료 | 본인 맥 (기존 claude_daemon.py, 무변경) |
| `none` | 독립(웹+DB 자체 서빙) / 시스템 | 본인 인프라 |

- `user_type`(lite/full)은 은퇴 수순 — 라우팅/분류에 쓰지 말 것
- 자체호스팅 승인(approve-user) 시 `cloud→self` 자동 전환 (이중응답 방지)
- "라이트 유저" 개념은 폐지 — 클로드 미연결 클라우드 유저가 같은 역할 (초대 프로젝트 대화만 가능)

## 인프라

- **호스트**: Lightsail `petervoice-golden-snapshot-ubuntu` (3.36.88.139) 재활용
- **서비스**: `pv-cloud.service` (데몬), `pv-portal.service` (docs 포탈, home-portal.js 클라우드 모드 --port 8899)
- **설정**: `/etc/pv-cloud/config.json` — api_url, host_key, users_root, max_concurrent, isolate_users
- **유저 디렉토리**: `/srv/pv/users/<id>/` — `claude/`(클로드 토큰, 700), `workspace/<project>/docs/`
- **세션 상태**: `/srv/pv/state/<id>.json` (데몬 소유, 프로젝트별 --resume 세션ID)

## 메시지 흐름 (통합 폴링)

1. 데몬이 `GET /api/cloud/poll` (X-Host-Key 인증)을 2~3초 간격 폴링 — **클라우드 유저 전원의 pending 을 요청 1개로**
2. 서버는 셀프 유저와 동일한 클레임(markMessagesFetched) 적용 → 이중 소비 방지
3. 유저 로스터(`GET /api/cloud/roster`)로 user_id↔api_key 매핑 (60초 갱신, 미스 시 즉시 재동기화)
4. 응답/처리마킹/heartbeat 는 해당 유저의 api_key 로 기존 `/api/bot/*` 호출

푸시(webhook)는 채택하지 않음 — 보정 폴링이 어차피 필요해 이중관리가 되기 때문. 유저 수와 무관하게 폴링 부하는 요청 1개/2~3초.

## 유저 격리 (보안)

- 유저별 unix 계정 `pv<id>` 를 첫 메시지 때 자동 생성, 홈 700
- claude 실행/로그인은 `sudo -u pv<id>` 강등 — 파일시스템 권한으로 교차 접근 차단
- sudoers: `/etc/sudoers.d/pv-cloud` (useradd/chown/mkdir/install/setfacl + pvusers 강등 env/claude, SETENV, runcwd)
- **시크릿 주입**: 턴마다 해당 유저 api_key 로 `/api/secrets?raw=true` 조회(60초 캐시) → `sudo --preserve-env=<이름들>` 로 env 전달. **값은 argv 에 절대 노출 금지** (/proc cmdline 교차 열람 방지)
- **ACL**: workspace 에 `u:ubuntu:rwx` — 포탈/데몬(ubuntu)과 claude(pv<id>)가 모두 접근, 다른 pv 유저는 차단. claude 토큰 폴더는 ubuntu 도 접근 불가
- 스킬: `/srv/pv/shared/skills` (레포 skills/ 복사본) → 유저 `claude/skills` 심링크

## 클로드 로그인 (온보딩 = 재로그인)

- 미연결 유저의 메시지 → "클로드 로그인 필요" 안내 응답
- 로그인 트리거 입력 → `claude auth login --claudeai` 를 pty 로 실행, **완전한 OAuth URL**(redirect_uri 포함 ~450자)을 캡처해 채팅으로 전달
  - ⚠️ `setup-token` 은 redirect_uri 없는 불완전 URL — 쓰지 말 것. URL 이 잘리지 않게 출력 종료+redirect_uri 포함 확인 후 전송
- 유저가 코드 붙여넣기 → pty 주입 → 성공 시 자격증명이 유저 claude/ 에 저장
- 연결 여부는 컬럼화하지 않음 — 데몬이 실행 시점에 감지 (신규 온보딩 = 토큰만료 재로그인과 동일 플로우)

## 문서탭 (docs)

```
브라우저 → 웹 /api/cloud/portal/[...] (세션 인증 + dir=본인 workspace 접두사 강제)
        → 호스트 pv-portal:8899 (home-portal.js PV_CLOUD_MODE, host_key, /api/docs* 화이트리스트)
        → /srv/pv/users/<id>/workspace/<project>/docs
```

- `/api/portal/token` 이 클라우드 유저에게 `tunnelUrl="/api/cloud/portal"` 반환 → 기존 문서탭 코드가 그대로 동작
- 클라우드 유저 `projects.directory` = `/srv/pv/users/<id>/workspace/<project>`
- 방화벽: 8899는 웹 박스 IP(54.116.190.85)만 허용

## 운영

- 상태 확인: /admin → 프로비저닝 탭 → "클라우드 데몬" 카드 (heartbeat 기반 생존 판정)
- 호스트 SSH: `ssh -i ~/.claude-daemon/cloudhost/lightsail-default.pem ubuntu@3.36.88.139`
- 로그: `sudo journalctl -u pv-cloud.service -f` / `pv-portal.service`
- 배포: 데몬 레포 push → 호스트에서 `git pull` + `sudo systemctl restart pv-cloud.service pv-portal.service` (AutoUpdater 미적용 호스트)
- host_key: 웹 박스 `.env.local` 의 `CLOUD_HOST_KEY` = 호스트 config.json 의 `host_key` (로컬 백업: `~/.claude-daemon/cloudhost/host_key.txt`)
- 골든 스냅샷 원본 백업: `golden-backup-before-cloudhost-20260723`

## 미완 항목

- 유저 삭제/셀프 전환 시 pv<id> 계정·파일 회수 (현재 무해하게 잔존)
- 스킬 마켓의 유저별 설치 스킬 동기화 (현재 번들 스킬만 공유)
- 채팅 인라인 로그인 카드 UI (현재는 대화형 안내로 충분히 동작)
