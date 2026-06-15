# 2026-06-15 박태준 피터보이스 먹통 (TCP 포트 고갈)

**발생**: 2026-06-15 04:44 UTC (마지막 heartbeat 기준)  
**복구**: 2026-06-15 18:34 KST  
**고객**: 박태준 (user_id=22, Tailscale IP: 100.76.237.77, OS 유저: 777inked777)  
**증상**: 피터보이스 응답 없음, 웹 UI에서 메시지 전송해도 답 없음

---

## 타임라인

| 시각 (KST) | 사건 |
|-----------|------|
| ~04:44 UTC | 마지막 heartbeat 기록됨 |
| 오후 | 박태준이 Sean에게 "피터보이스 먹통"이라고 연락 |
| 18:03 | 데몬 로그: `Poll error #519, wait 30s` — 이미 519번 연속 실패 중 |
| 18:17 | `launchctl stop`으로 데몬 중단 시도 (문제 발생, 아래 참고) |
| 18:22 | git pull + 데몬 재기동 (PATH 문제로 claude not found) |
| 18:23 | 올바른 PATH로 재기동, 네트워크 복구 확인 |
| 18:34 | 박태준 맥미니 직접 로그인 → Keychain 복구 → 완전 정상화 |

---

## 근본 원인

### 1. TCP 포트 고갈 (PRIMARY)

데몬이 `urllib.request`로 매 API 호출마다 새 TCP 연결을 생성했음.  
3초 간격 × 4개 엔드포인트 = 수일에 걸쳐 TIME_WAIT 소켓 누적.

```
TIME_WAIT 소켓 수: 15,055개
가용 ephemeral 포트: 49152~65535 = 16,384개
→ 92% 점유, 신규 TCP connect() 불가 (errno 49: EADDRNOTAVAIL)
```

**특이사항**: ICMP ping은 정상 (패킷 레벨), TCP만 실패 (소켓 레벨)

### 2. macOS 커널 소스 IP 선택 버그 (SECONDARY)

WiFi 껐다 켜서 IP가 .168 → .100으로 바뀌었지만,  
커널의 소스 IP 선택 캐시가 유효하지 않은 .168을 계속 선택함.  
→ 명시적 `bind('192.168.100.100', 0)`하면 작동, 미바인딩 시 즉시 실패.

---

## 진단 과정에서 만난 함정들

### SSH 비번을 몰라서 한참 헤맨 것
**실제**: 비번 불필요. 기본 `~/.ssh/id_ed25519` 키로 passwordless 접속 가능.  
`ssh -o StrictHostKeyChecking=no 777inked777@100.76.237.77`  
→ 4월 대화 기록에 이 형태로 나와있었음. 다음부터는 먼저 키 인증 시도할 것.

### `launchctl stop`을 SSH에서 실행 → 데몬 미복구
SSH 세션에서 `launchctl stop com.petervoice.daemon` 실행 시 데몬는 종료되지만,  
`gui/501` 도메인 LaunchAgent는 SSH 세션에서 제어 불가 (Domain does not support specified action).  
→ launchd가 재시작 못 함. `pkill -f claude_daemon.py` 후 nohup으로 직접 기동해야 함.

```bash
# SSH에서 데몬 재시작하는 올바른 방법
pkill -f claude_daemon.py
cd ~/peter-voice
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin nohup .venv/bin/python scripts/claude_daemon.py >> ~/.claude-daemon/daemon.log 2>&1 &
```

**PATH 필수**: SSH의 기본 PATH(`/usr/bin:/bin:...`)에는 `/opt/homebrew/bin` 없음  
→ `claude` 명령을 못 찾아 `[Errno 2] No such file or directory: 'claude'` 발생

### "Not logged in" 오류
SSH에서 nohup으로 띄운 데몬은 macOS Keychain 접근 불가.  
Claude CLI OAuth 토큰이 Keychain에 저장되어 있어 GUI 세션 없이는 읽지 못함.  
→ 박태준이 맥미니에 직접 로그인하자 `seeded ~/.claude/.credentials.json from Keychain` 로그 출력되며 자동 복구.

### sudo 없음
`777inked777` 계정은 sudo 권한 없음.  
`sysctl`, `pfctl`, `route delete`, `ifconfig down` 등 커널 수준 작업 불가.

---

## 적용한 수정 (코드)

### `scripts/daemon/api.py` — urllib → requests.Session (commit: 8f79aa5)

```python
# 수정 전: 매 호출마다 새 TCP 연결
urllib.request.urlopen(req)

# 수정 후: 연결 재사용 (keep-alive)
requests.Session()  # pool_connections=4, pool_maxsize=10
```

### `scripts/daemon/api.py` — source_address 명시 바인딩 (commit: f805ea5)

macOS 커널 버그 우회를 위해 현재 인터페이스 IP를 자동 감지해 소켓에 명시 바인딩.

```python
class _SourceBoundAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        if self._source_address:
            kwargs["source_address"] = (self._source_address, 0)
        super().init_poolmanager(*args, **kwargs)

def _get_primary_ip() -> str:
    for iface in ("en0", "en1", "en2"):
        result = subprocess.run(["ipconfig", "getifaddr", iface], ...)
        if ip: return ip
```

IP 변경 시 (DHCP 갱신 등) 자동으로 세션 재빌드.

---

## 재발 방지

| 항목 | 조치 |
|------|------|
| TCP 포트 고갈 | requests.Session 연결 재사용으로 TIME_WAIT 최소화 |
| 소스 IP 선택 버그 | source_address 명시 바인딩 |
| SSH launchctl 제한 | 프롬프트에 주의사항 기록 완료 |
| 박태준 SSH 방법 | 프롬프트에 기록 완료 (키 인증, PATH 필수) |

---

## 참고: 고객 Supabase 조회 방법

박태준(user_id=22) 메시지 조회 또는 직접 삽입:

```python
SB_URL = 'https://gfzprzvynxixekmsadqe.supabase.co'
SB_KEY = '...'  # ~/Projects/peter-voice/peter-voice-web/.env.local의 SUPABASE_SERVICE_ROLE_KEY

# 메시지 삽입 (테스트용)
msg = {'user_id': 22, 'project': 'general', 'type': 'user', 'text': '...', 'processed': False}
# POST /rest/v1/messages
```
