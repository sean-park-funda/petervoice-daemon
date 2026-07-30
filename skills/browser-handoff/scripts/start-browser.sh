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
if ! pgrep -f "remote-debugging-port=$CDP_PORT" >/dev/null 2>&1; then
  BIN=""
  HEADLESS_FLAG="--headless=new"
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
  # shellcheck disable=SC2086
  nohup "$BIN" $HEADLESS_FLAG \
    --remote-debugging-port="$CDP_PORT" \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --window-size=1280,800 \
    --user-data-dir="$HOME/.pv-browser" \
    about:blank >/dev/null 2>&1 &
  started="chromium"
fi

# ── CDP 브리지 (컨테이너 전용 — 홈이 /home/agent 일 때만) ──
if [ "$HOME" = "/home/agent" ] && ! pgrep -f "cdp-proxy.js" >/dev/null 2>&1; then
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
