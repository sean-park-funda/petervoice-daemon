#!/bin/bash
# 헤드리스 chromium 을 CDP(9222)와 함께 기동한다 (이미 떠 있으면 no-op).
# 에이전트(브라우저 자동화 시작 시)와 데몬(인계 중 컨테이너 재생성 후)이 같은 스크립트를 쓴다.
# 프로필($HOME/.pv-browser)은 유저 홈에 있어 로그인 세션(쿠키)이 컨테이너 재시작에도 유지된다.
#
# 크롬(151+)은 CDP 를 127.0.0.1 에만 바인딩한다(원격 바인딩 플래그 무시 — 보안 변경).
# 컨테이너에서는 포트 퍼블리시가 eth0 IP 로 DNAT 되므로, cdp-proxy.js(node 표준 라이브러리)가
# eth0:9222 → 127.0.0.1:9222 브리지를 함께 뜬다.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# CDP 포트: 데몬이 주입한 PV_CDP_PORT 우선 (컨테이너=9222 고정, 비컨테이너 클라우드=19000+uid,
# 맥 설치형=9222 기본). 포탈이 유저별로 유도하는 포트와 일치해야 한다.
CDP_PORT="${PV_CDP_PORT:-9222}"

started=""

# ── chromium ──
# "이미 떠 있음" 판정은 pgrep 이 아니라 CDP 응답으로 한다 — pgrep -f 는 SSH/부모 셸의
# 명령줄 문자열에 패턴이 들어 있으면 자기 자신을 매칭한다 (뉴넥스에서 실측 오탐)
if ! curl -sf -m 2 "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
  BIN=""
  HEADLESS_FLAG="--headless=new"
  # 맥에 GUI 콘솔 세션이 있으면 헤드리스를 쓰지 않는다 (일반 모드).
  # 구글은 헤드리스 크롬의 로그인을 차단한다("브라우저 또는 앱이 안전하지 않을 수 있습니다") —
  # 헤드리스로 뜨면 구글/유튜브/애드센스/슬랙(구글 연동) 세션을 아예 복구할 수 없다.
  # 클라우드 컨테이너·리눅스는 화면이 없으므로 헤드리스를 유지한다.
  if [ "$(uname)" = "Darwin" ] && [ -n "$(stat -f%Su /dev/console 2>/dev/null)" ] \
     && [ "$(stat -f%Su /dev/console 2>/dev/null)" != "root" ]; then
    HEADLESS_FLAG=""
  fi
  if [ "$(uname)" = "Darwin" ]; then
    # macOS (설치형/맥미니): 설치된 Chrome/Chromium 우선, 없으면 playwright 캐시
    for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
             "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
      [ -x "$c" ] && BIN="$c" && break
    done
    if [ -z "$BIN" ]; then
      BIN="$(ls -d "$HOME"/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium 2>/dev/null | sort | tail -1)"
    fi
  else
    # Linux: playwright 가 받아둔 바이너리 우선 (chrome-linux/ 또는 chrome-linux64/ 레이아웃),
    # 다음 headless-shell, 마지막으로 시스템 chromium
    BIN="$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | sort | tail -1)"
    if [ -z "$BIN" ]; then
      BIN="$(ls -d "$HOME"/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell 2>/dev/null | sort | tail -1)"
      HEADLESS_FLAG=""  # headless-shell 은 항상 헤드리스
    fi
    if [ -z "$BIN" ] && command -v chromium >/dev/null 2>&1; then BIN=chromium; fi
    if [ -z "$BIN" ] && command -v chromium-browser >/dev/null 2>&1; then BIN=chromium-browser; fi
  fi
  if [ -z "$BIN" ]; then
    echo "chromium not found — run: npx -y playwright install --with-deps chromium (or install Chrome)" >&2
    exit 1
  fi

  mkdir -p "$HOME/.pv-browser"
  # 헤드리스가 아니면(맥 GUI) 크롬 창이 고객 화면에 실제로 보인다. 빈 about:blank 창이
  # 갑자기 뜨면 무슨 창인지 알 수 없으므로, 정체를 알리는 랜딩 탭으로 띄운다.
  START_URL="about:blank"
  if [ -z "$HEADLESS_FLAG" ] && [ "$(uname)" = "Darwin" ]; then
    cat > "$HOME/.pv-browser/landing.html" << 'LANDING'
<!doctype html><meta charset="utf-8"><title>피터보이스 공유 브라우저</title>
<style>body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;
justify-content:center;font-family:-apple-system,BlinkMacSystemFont,sans-serif;color:#374151;
background:#f9fafb}h1{font-size:20px;margin:0 0 8px}p{font-size:13px;color:#6b7280;margin:2px 0}</style>
<h1>피터보이스 공유 브라우저</h1>
<p>AI 비서가 웹사이트 작업에 사용하는 창입니다. 닫지 말고 그대로 두세요.</p>
<p>로그인 화면이 뜨면 직접 로그인해 주시면 비서가 이어서 진행합니다.</p>
LANDING
    START_URL="file://$HOME/.pv-browser/landing.html"
  fi
  # set -m: 백그라운드 잡을 자기 프로세스 그룹으로 분리한다. 없으면 chromium 이 이 스크립트를
  # 부른 부모(에이전트 턴 셸/데몬)의 프로세스 그룹에 남아, 부모 정리(타임아웃 킬, 서비스 재시작,
  # 프로세스 그룹 킬)에 브라우저가 같이 죽는다 — 2026-08-18 저녁 chromium 원인불명 사망 후 예방 조치.
  set -m
  # shellcheck disable=SC2086
  nohup "$BIN" $HEADLESS_FLAG \
    --remote-debugging-port="$CDP_PORT" \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --window-size=1280,800 \
    --user-data-dir="$HOME/.pv-browser" \
    "$START_URL" >/dev/null 2>&1 &
  started="chromium"
fi

# ── CDP 브리지 (컨테이너 전용 — 홈이 /home/agent 일 때만) ──
if [ "$HOME" = "/home/agent" ] && ! pgrep -f "node .*cdp-proxy.js" >/dev/null 2>&1; then
  nohup node "$SCRIPT_DIR/cdp-proxy.js" >/dev/null 2>&1 &
  started="$started cdp-proxy"
fi

if [ -z "$started" ]; then
  echo "already running"
  exit 0
fi

for _ in $(seq 1 20); do
  if curl -sf "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
    echo "started:$started"
    exit 0
  fi
  sleep 0.5
done
echo "failed to start" >&2
exit 1
