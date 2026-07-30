#!/bin/bash
# 헤드리스 chromium 을 CDP(9222)와 함께 기동한다 (이미 떠 있으면 no-op).
# 에이전트(브라우저 자동화 시작 시)와 데몬(인계 중 컨테이너 재생성 후)이 같은 스크립트를 쓴다.
# 프로필($HOME/.pv-browser)은 유저 홈에 있어 로그인 세션(쿠키)이 컨테이너 재시작에도 유지된다.
set -u

if pgrep -f "remote-debugging-port=9222" >/dev/null 2>&1; then
  echo "already running"
  exit 0
fi

# playwright 가 받아둔 chromium 우선, 없으면 시스템 chromium
BIN="$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux/chrome 2>/dev/null | sort | tail -1)"
if [ -z "$BIN" ] && command -v chromium >/dev/null 2>&1; then BIN=chromium; fi
if [ -z "$BIN" ] && command -v chromium-browser >/dev/null 2>&1; then BIN=chromium-browser; fi
if [ -z "$BIN" ]; then
  echo "chromium not found — run: npx -y playwright install --with-deps chromium" >&2
  exit 1
fi

mkdir -p "$HOME/.pv-browser"
# 0.0.0.0 바인딩: 포트 퍼블리시(호스트 127.0.0.1 전용)가 컨테이너 IP 로 포워딩되기 때문.
# 컨테이너 간 접근은 pv-isolated 네트워크(isolate=true)가 차단한다.
nohup "$BIN" \
  --headless=new \
  --remote-debugging-port=9222 \
  --remote-debugging-address=0.0.0.0 \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --window-size=1280,800 \
  --user-data-dir="$HOME/.pv-browser" \
  about:blank >/dev/null 2>&1 &

for _ in $(seq 1 20); do
  if curl -sf http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    echo "started"
    exit 0
  fi
  sleep 0.5
done
echo "failed to start" >&2
exit 1
