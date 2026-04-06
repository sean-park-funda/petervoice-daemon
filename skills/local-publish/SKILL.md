---
name: local-publish
description: 현재 프로젝트를 맥미니에서 로컬 빌드 후 인터넷에 퍼블리싱. "퍼블리싱해줘", "사이트 올려줘", "publish this", "사이트 내려줘", "재빌드" 등에 반응. Cloudflare Tunnel + launchd 기반.
---

# Local Publish — 맥미니 로컬 퍼블리싱

프로젝트를 맥미니에서 빌드하고, Cloudflare Tunnel을 통해 인터넷에 공개합니다.
URL 형식: `https://{username}-{project}.peter-voice.site`

## 퍼블리시 전 사전 체크 (반드시 순서대로 실행)

### Step 1: publish.py 경로 확인
```bash
# 경로는 맥미니마다 다를 수 있음
PUBLISH_SCRIPT=""
for p in "$HOME/Projects/peter-voice/scripts/publish.py" "$HOME/peter-voice/scripts/publish.py"; do
  if [ -f "$p" ]; then PUBLISH_SCRIPT="$p"; break; fi
done
echo "${PUBLISH_SCRIPT:-NOT_FOUND}"
```
**NOT_FOUND이면**: peter-voice 코드가 설치되지 않은 상태. Sean(관리자)에게 문의 필요.

### Step 2: cloudflared 설치 확인
```bash
export PATH=/opt/homebrew/bin:$PATH
which cloudflared || brew install cloudflared
```

### Step 3: cloudflare_tunnel_id 확인 → 없으면 API로 자동 생성
```bash
python3 -c "
import json, pathlib
c = json.loads((pathlib.Path.home() / '.claude-daemon' / 'config.json').read_text())
tid = c.get('cloudflare_tunnel_id', '')
print(tid if tid else 'NOT_SET')
"
```

**NOT_SET이면** 아래 스크립트로 터널을 자동 생성:
```python
import json, pathlib, urllib.request

config_path = pathlib.Path.home() / ".claude-daemon" / "config.json"
c = json.loads(config_path.read_text())

if c.get("cloudflare_tunnel_id"):
    print(f"이미 설정됨: {c['cloudflare_tunnel_id']}")
else:
    api_url = c.get("api_url", "https://peter-voice.vercel.app")
    api_key = c["api_key"]
    username = c.get("bot_name", "user").lower().replace(" ", "-")

    data = json.dumps({"username": username}).encode()
    req = urllib.request.Request(
        f"{api_url}/api/tunnel/create",
        data=data,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read())

    c["cloudflare_tunnel_id"] = result["tunnelId"]
    c["cloudflare_tunnel_token"] = result["tunnelToken"]
    config_path.write_text(json.dumps(c, indent=2, ensure_ascii=False))
    print(f"터널 생성 완료: {result['tunnelId']}")
```

### Step 4: cloudflared 프로세스 실행 확인 → 안 돌면 launchd 서비스로 등록
```bash
pgrep -f "cloudflared.*tunnel.*run" && echo "RUNNING" || echo "NOT_RUNNING"
```

**NOT_RUNNING이면** launchd 서비스로 등록 (재부팅 후에도 자동 실행):
```python
import json, pathlib, subprocess, time

config_path = pathlib.Path.home() / ".claude-daemon" / "config.json"
c = json.loads(config_path.read_text())
token = c.get("cloudflare_tunnel_token", "")

if not token:
    print("ERROR: cloudflare_tunnel_token이 config에 없습니다. Step 3을 먼저 실행하세요.")
else:
    # cloudflared 바이너리 경로
    r = subprocess.run(["which", "cloudflared"], capture_output=True, text=True)
    if r.returncode != 0:
        cf_bin = "/opt/homebrew/bin/cloudflared"
    else:
        cf_bin = r.stdout.strip()

    # launchd plist 생성
    plist_path = pathlib.Path.home() / "Library" / "LaunchAgents" / "com.cloudflare.cloudflared.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = pathlib.Path.home() / ".claude-daemon"

    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        '    <key>Label</key><string>com.cloudflare.cloudflared</string>\n'
        '    <key>ProgramArguments</key>\n    <array>\n'
        f'        <string>{cf_bin}</string>\n'
        '        <string>tunnel</string>\n'
        '        <string>--no-autoupdate</string>\n'
        '        <string>--protocol</string>\n'
        '        <string>http2</string>\n'
        '        <string>run</string>\n'
        '        <string>--token</string>\n'
        f'        <string>{token}</string>\n'
        '    </array>\n'
        '    <key>RunAtLoad</key><true/>\n'
        '    <key>KeepAlive</key><true/>\n'
        f'    <key>StandardOutPath</key><string>{log_dir}/cloudflared-stdout.log</string>\n'
        f'    <key>StandardErrorPath</key><string>{log_dir}/cloudflared-stderr.log</string>\n'
        '</dict>\n</plist>'
    )

    plist_path.write_text(plist)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    time.sleep(3)

    r = subprocess.run(["pgrep", "-f", "cloudflared.*tunnel.*run"], capture_output=True)
    if r.returncode == 0:
        print("cloudflared launchd 서비스 등록 및 실행 완료")
    else:
        print("ERROR: cloudflared 시작 실패. 로그: " + str(log_dir / "cloudflared-stderr.log"))
```

### Step 5: node/npm 설치 확인
```bash
which node || brew install node
```

### Step 6: 프로젝트에 package.json 또는 index.html 확인
- 둘 다 없으면 → "퍼블리싱 가능한 프로젝트가 아닙니다" 안내

**위 6단계 모두 OK면 퍼블리시 진행!**

## 퍼블리시 실행

```bash
python3 "$PUBLISH_SCRIPT" publish <project_id> <project_dir> --username <username>
```

- `project_id`: 프로젝트명 (URL에 사용됨)
- `project_dir`: 프로젝트 절대 경로
- `--username`: config의 bot_name에서 추출

username 추출:
```bash
python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('bot_name','user').lower().replace(' ','-'))"
```

## 재빌드 (코드 수정 후)
```bash
python3 "$PUBLISH_SCRIPT" rebuild <project_id>
```
유저에게 "새로고침하면 반영됩니다" 안내.

## 언퍼블리시
```bash
python3 "$PUBLISH_SCRIPT" unpublish <project_id> --username <username>
```

## 상태 확인
```bash
python3 "$PUBLISH_SCRIPT" status
```

## 지원 프레임워크

| 프레임워크 | 감지 기준 | 빌드 | 서빙 |
|-----------|----------|------|------|
| Next.js | package.json에 `next` | `next build` | `next start -p {port}` |
| Vite | package.json에 `vite` | `vite build` | `vite preview --port {port}` |
| 정적 사이트 | `index.html` 존재 | 불필요 | `npx serve -l {port} -s .` |

## 에이전트 행동 가이드

- "퍼블리싱해줘" → 사전 체크 6단계 → publish 실행 → URL 안내
- 코드 수정 후 → rebuild → "새로고침하세요"
- "사이트 내려줘" → unpublish
- 빌드 실패 → 에러 로그 확인 후 수정 시도
- **publish.py 경로는 맥미니마다 다를 수 있으므로 반드시 Step 1에서 찾은 경로를 사용**

## 트러블슈팅

### cloudflared QUIC 연결 실패
증상: `CRYPTO_ERROR 0x178 (remote): tls: no application protocol`
해결: `--protocol http2` 옵션 추가 (Step 4의 plist에 이미 포함)

### Error 1033 (Ares Timedout)
증상: 사이트 접속 시 Cloudflare Error 1033
원인: cloudflared가 실행 안 됨
해결: Step 4 실행하여 cloudflared 서비스 시작

### 503 Service Unavailable
증상: cloudflared 실행 중인데 503
원인: 터널 ingress 규칙 미설정
해결: publish 재실행 — publish.py가 서버 API로 ingress 자동 설정함

### DNS 전파 지연
최초 퍼블리시 후 1~2분간 접속 불가할 수 있음. 기다리면 해결.

## 주의사항

- 포트: 3001~3099 (최대 99개)
- 맥미니 꺼지면 접속 불가
- 맥미니마다 별도 터널 자동 생성됨
- **~/peter-voice/ 디렉토리의 코드를 수정하지 말 것** — 자동 업데이트로 관리됨
