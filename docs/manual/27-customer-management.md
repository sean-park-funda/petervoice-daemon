# 27. 고객 매니지먼트

고객 계정 관리, 채팅 기록 조회, 원격 장애 복구 등 운영 업무 가이드.

---

## 27.1 고객 현황 파악

### 고객 목록 조회

Supabase REST API로 전체 고객 목록을 확인한다.

```bash
# 고객 목록 (id, username, role, plan, customer_name, created_at)
curl -s "$SUPABASE_URL/rest/v1/users?select=id,username,role,customer_name,plan,created_at&order=created_at.desc" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
```

### 고객 상태 확인 (실시간)

`user_status` 테이블로 데몬 동작 상태를 확인한다.

```bash
# 특정 고객의 데몬 상태
curl -s "$SUPABASE_URL/rest/v1/user_status?select=is_working,current_task,last_heartbeat,active_project,context_usage&user_id=eq.{USER_ID}" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
```

주요 필드:
| 필드 | 설명 |
|------|------|
| `is_working` | 현재 작업 중 여부 |
| `last_heartbeat` | 마지막 하트비트 시각 (오래되면 데몬 다운 의심) |
| `active_project` | 현재 활성 프로젝트 |
| `context_usage` | 컨텍스트 사용률 (높으면 세션 리셋 임박) |
| `streaming_text` | 실시간 스트리밍 텍스트 |

---

## 27.2 고객 채팅 기록 조회

### 최근 대화 조회

```bash
# 특정 고객의 특정 프로젝트 최근 메시지 (최대 50건)
curl -s "$SUPABASE_URL/rest/v1/messages?select=type,text,created_at&user_id=eq.{USER_ID}&project=eq.{PROJECT_ID}&order=created_at.desc&limit=20" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
```

- `type=user`: 고객이 보낸 메시지
- `type=bot`: AI 응답
- `subtype=tool_log`: 도구 실행 로그 (디버깅용)

### 기간별 조회

```bash
# 특정 날짜 범위 메시지
curl -s "$SUPABASE_URL/rest/v1/messages?select=type,text,created_at&user_id=eq.{USER_ID}&created_at=gte.2026-04-01T00:00:00Z&created_at=lt.2026-04-16T00:00:00Z&order=created_at.asc&limit=50" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
```

### 미처리 메시지 확인 (장애 징후)

고객이 메시지를 보냈는데 봇 응답이 없다면 데몬 장애 가능성이 높다.

```bash
# 미처리(unprocessed) 유저 메시지 조회
curl -s "$SUPABASE_URL/rest/v1/messages?select=id,text,created_at&user_id=eq.{USER_ID}&type=eq.user&processed=is.false&order=created_at.desc" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
```

미처리 메시지가 쌓여 있으면 → **27.4 장애 복구** 절차 진행.

### 고객 프로젝트 목록

```bash
curl -s "$SUPABASE_URL/rest/v1/projects?select=id,name,directory,deploy_url&user_id=eq.{USER_ID}&order=sort_order.asc" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
```

---

## 27.3 Tailscale SSH 원격 접속

### 사전 요구

- Sean의 Mac Mini가 Tailscale 네트워크에 연결되어 있어야 함
- 고객 머신도 같은 tailnet에 가입되어 있어야 함 (온보딩 시 자동 설정)
- SSH 키가 Sean Mac Mini의 `~/.ssh/`에 존재해야 함

### 고객 머신 접속

```bash
# 기본 접속
ssh user@{TAILSCALE_IP}

# 타임아웃 설정 (연결 불가 시 빠르게 실패)
ssh -o ConnectTimeout=5 user@{TAILSCALE_IP}

# 특정 SSH 키 사용
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519_migration user@{TAILSCALE_IP}
```

### 고객별 접속 정보

`push-daemon-ssh.sh` 스크립트에 고객별 접속 정보가 관리된다:
```
"user@IP:OS:service_name:repo_path:ssh_opts"
```

예시:
| 고객 | IP | OS | 서비스명 | 레포 경로 |
|------|----|----|----------|-----------|
| willy | 100.125.150.24 | Mac | com.petervoice.daemon | ~/peter-voice |
| jennc | 100.119.200.43 | Windows | ClaudeDaemon | C:/PeterVoice/peter-voice |

### 알려진 고객 정보

| 고객명 | 이름 | Tailscale IP | OS 유저 | SSH 방식 | Claude 계정 | 비고 |
|--------|------|-------------|---------|----------|-------------|------|
| karl | 안영수 | 100.86.21.105 | karl | sshpass -p 'karl1234' (password auth) | dev.ceo@ptjcomics.com | 박태준과 계정 공유 |
| — | 박태준 | 100.76.237.77 | 777inked777 | SSH 키 인증 (기본 id_ed25519) | dev.ceo@ptjcomics.com | 안영수와 계정 공유; sudo 없음 |

> **주의**: `dev.ceo@ptjcomics.com` 계정을 두 사람이 공유한다. Claude CLI 재로그인 시 이 계정으로 로그인해야 하며, Sean 계정(`sungjunpark@ptjcomics.com`)으로 덮어쓰지 말 것.

### 연결 확인

```bash
# 핑 테스트
ssh -o ConnectTimeout=5 -o BatchMode=yes user@IP "echo ok"

# Tailscale 상태로 온라인 여부 확인
tailscale status | grep {고객이름 또는 IP}
```

---

## 27.4 장애 복구

### 장애 진단 순서

1. **채팅 기록 확인** — 미처리 메시지 있는지 (27.2)
2. **데몬 상태 확인** — `user_status.last_heartbeat`가 오래됐는지 (27.1)
3. **Tailscale 연결** — SSH 접속 가능한지 (27.3)
4. **원격 로그 확인** — 데몬 로그 분석
5. **복구 조치** — 상황에 따라 아래 절차 진행

### Mac 고객 복구

```bash
# 1. SSH 접속
ssh user@{TAILSCALE_IP}

# 2. 데몬 로그 확인 (최근 20줄)
tail -20 ~/.claude-daemon/daemon.log

# 3. 데몬 프로세스 확인
launchctl list | grep petervoice

# 4. 코드 업데이트
cd ~/peter-voice && git pull --ff-only origin main

# 5. 의존성 업데이트 (변경 시)
.venv/bin/pip install -r requirements.txt

# 6. 데몬 재시작 (고객 설치본 라벨 = com.petervoice.daemon, 일부 구/개발 머신만 claude-daemon)
launchctl stop com.petervoice.daemon 2>/dev/null || launchctl stop com.petervoice.claude-daemon
# launchd가 자동으로 10초 내 재시작

# 7. 재시작 확인
sleep 15 && tail -5 ~/.claude-daemon/daemon.log
```

### Windows 고객 복구

```bash
# 1. SSH 접속
ssh user@{TAILSCALE_IP}

# 2. 데몬 로그 확인
type %USERPROFILE%\.claude-daemon\daemon.log | more

# 3. 코드 업데이트
cd C:\PeterVoice\peter-voice && git pull --ff-only origin main

# 4. 의존성 업데이트
pip install -r requirements.txt

# 5. 데몬 재시작 (NSSM)
nssm restart ClaudeDaemon

# 6. 재시작 확인
timeout 15 && type %USERPROFILE%\.claude-daemon\daemon.log
```

### 일괄 업데이트 (전체 고객)

```bash
# push-daemon-ssh.sh 사용 — 모든 고객에게 최신 코드 배포
cd ~/Projects/peter-voice-daemon
./scripts/push-daemon-ssh.sh
```

이 스크립트는:
- 모든 등록된 고객 머신에 순차 접속
- `git pull --ff-only` 실행
- 변경 있으면 의존성 설치 + 데몬 재시작
- 접속 불가 머신은 건너뜀
- 결과를 "Already up to date" / "New code pulled" / 오류로 보고

---

## 27.5 흔한 장애 유형과 대응

### 데몬이 응답하지 않음

**증상**: 고객 메시지가 미처리 상태로 쌓임, `last_heartbeat`가 오래됨

**원인 & 대응**:
| 원인 | 확인 방법 | 대응 |
|------|-----------|------|
| 데몬 크래시 | `launchctl list \| grep petervoice` 결과 없음 | `launchctl start` 또는 재설치 |
| 컨텍스트 오버플로 | 로그에 "context" 에러 | 자동 복구됨, 안 되면 재시작 |
| Claude CLI 에러 | 로그에 API 에러 | API 키/네트워크 확인 |
| 머신 꺼짐 | SSH 연결 불가, Tailscale 오프라인 | 고객에게 연락 |
| 네트워크 차단 | SSH OK인데 API 호출 실패 | 방화벽/프록시 확인 |

### 세션이 반복 리셋됨

**증상**: 고객이 "맥락을 자꾸 잊어버린다"고 보고

**확인**: 로그에서 "session reset", "context overflow" 검색
```bash
ssh user@IP "grep -i 'session\|context\|reset' ~/.claude-daemon/daemon.log | tail -20"
```

**대응**: 프롬프트 크기 최적화, 불필요한 스킬 제거

### API 키 문제

**증상**: 로그에 401/403 에러

**확인**:
```bash
# 고객의 API 키 확인
curl -s "$SUPABASE_URL/rest/v1/users?select=api_key&id=eq.{USER_ID}" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"

# 고객 머신의 설정과 대조
ssh user@IP "python3 -c \"import json; print(json.load(open('/Users/USER/.claude-daemon/config.json'))['api_key'])\""
```

**대응**: DB의 키와 고객 config의 키가 다르면 고객 config 수정

---

## 27.6 모니터링 체크리스트

정기 점검 항목:

- [ ] 전체 고객 `last_heartbeat`가 최근 5분 이내인지
- [ ] 미처리 메시지가 쌓인 고객이 없는지
- [ ] Tailscale 네트워크에서 오프라인 머신이 없는지
- [ ] 데몬 로그에 반복 에러가 없는지
- [ ] 코드 업데이트가 모든 고객에게 배포되었는지

```bash
# 전체 고객 데몬 상태 한눈에 보기
curl -s "$SUPABASE_URL/rest/v1/user_status?select=user_id,is_working,last_heartbeat,active_project" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
```

---

## 관련 문서

- [02. 데이터베이스](./02-database.md) — 테이블 스키마 상세
- [03. Claude 데몬](./03-daemon.md) — 데몬 구조, 세션 관리
- [25. 고객 온보딩](./25-onboarding.md) — 신규 고객 프로비저닝
