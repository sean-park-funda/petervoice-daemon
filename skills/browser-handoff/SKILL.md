---
name: browser-handoff
description: 브라우저 자동화 중 로그인/인증 벽에 막혔을 때 사용자에게 원격 로그인을 넘기는 스킬. 웹사이트 로그인이 필요한 작업, "로그인해서 데이터 가져와줘", 인증이 필요한 사이트 자동화, 로그인 세션 만료 시 사용. 사용자가 폰/PC에서 서버 브라우저 화면을 직접 보고 로그인해주면 세션(쿠키)을 이어받아 작업을 계속한다.
---

# 브라우저 세션 인계 (Browser Session Handoff)

서버(클라우드 컨테이너)에는 화면이 없어서 로그인·2FA·CAPTCHA는 직접 못 푼다.
이 스킬로 **사용자에게 브라우저 화면을 원격으로 넘겨** 로그인을 대신 받아온다.

## 0. 브라우저 준비 (최초 1회 + 세션마다)

```bash
# chromium 이 없으면 설치 (최초 1회, 유저 홈에 저장되어 유지됨)
npx -y playwright install --with-deps chromium

# CDP(9222) 포함 헤드리스 브라우저 기동 (이미 떠 있으면 no-op)
bash /srv/pv/shared/skills/browser-handoff/scripts/start-browser.sh
```

**중요**: 브라우저 자동화는 반드시 이 브라우저에 **CDP로 붙어서** 해야 한다.
그래야 사용자가 로그인해준 세션을 그대로 이어받는다.

```bash
npx -y agent-browser --cdp 9222 open https://example.com
npx -y agent-browser --cdp 9222 snapshot -i
npx -y agent-browser --cdp 9222 click @e1
```

(자체적으로 새 브라우저를 띄우는 `agent-browser open`(--cdp 없이)을 쓰면 인계와 세션이 분리된다 — 금지)

## 1. 로그인 벽을 만나면

로그인 폼, "로그인이 필요합니다", 2FA 입력, CAPTCHA 등을 만나면 **직접 뚫으려 하지 말고**:

```bash
RESP=$(curl -s -X POST "$API_URL/api/browser-handoff" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project": "'"$(basename "$PWD")"'",
    "url": "현재 로그인이 필요한 페이지 URL",
    "reason": "쿠팡윙 로그인 (주문 데이터 조회용)"
  }')
MARKER=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['marker'])")
```

- `reason`: 사용자에게 보이는 한 줄 설명 — **어느 사이트, 무슨 작업 때문인지** 명확히
- `project`: 현재 프로젝트 ID (작업 디렉토리 이름)

## 2. 턴 종료 (기다리지 말 것)

응답 텍스트에 마커를 포함하고 **턴을 끝낸다**. 사용자를 기다리며 sleep 하지 말 것.

```
쿠팡윙에 로그인이 필요합니다. 아래 버튼을 눌러 대신 로그인해 주세요.
로그인이 끝나면 제가 이어서 진행합니다. (15분 안에 부탁드려요)
[[browser/handoff:여기에_marker의_id]]
```

마커는 채팅에서 로그인 버튼 카드로 렌더링된다. 브라우저는 데몬이 살려두니 걱정하지 않아도 된다.

## 3. 완료 후 이어받기

사용자가 로그인을 마치면 `[relay from:browser-handoff]` 로 시작하는 완료 메시지가 도착한다.
그때 **같은 CDP 브라우저(--cdp 9222)** 로 이어서 작업하면 로그인 세션이 살아 있다.

- 만료 메시지(⏱️)가 와도 로그인이 이미 되어 있을 수 있다 — 먼저 페이지를 열어 확인할 것
- 로그인 세션은 `$HOME/.pv-browser` 프로필에 저장되어 다음 턴·컨테이너 재시작에도 유지된다

## 안 되는 것 (사용자에게 솔직히 안내)

- 패스키·공동인증서·하드웨어 키 등 기기 귀속 인증 → "이 인증은 클라우드에서 불가능해요.
  내 컴퓨터에 피터를 설치하면 가능합니다" 안내 (`[HEAVY_TASK]` 아님)
- 일부 은행·포털은 데이터센터 IP 로그인 자체를 차단할 수 있다 — 실패 시 원인을 그대로 보고
