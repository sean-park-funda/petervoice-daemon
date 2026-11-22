#!/bin/bash
# 헤드리스 chromium 을 CDP(PV_CDP_PORT, 기본 9222)와 함께 기동한다 (내 것이 이미 떠 있으면 no-op).
# 에이전트(브라우저 자동화 시작 시)와 데몬(인계 중 컨테이너 재생성 후)이 같은 스크립트를 쓴다.
# 종료 코드: 0 = 떠 있음/띄웠음 · 1 = 못 띄움 · 2 = 그 포트가 다른 OS 계정의 브라우저(공유 맥) — 붙지 말 것.
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

# ── 포트 소유권 판정 (2026-11-22, KI-122) ──
# 맥의 127.0.0.1 TCP 포트는 OS 계정 사이에 공유된다. 공유 맥(테넌트 계정 여럿)에서 PV_CDP_PORT 가 없으면
# 전원이 9222 로 수렴하고, 먼저 뜬 다른 계정의 크롬이 응답하면 "already running" 으로 끝나 그 뒤의
# agent-browser --cdp 가 전부 남의 브라우저에 들어갔다(11/22 실사고 — 남의 쿠키·탭을 읽고 썼다).
# 그래서 "응답이 있나" 가 아니라 "리스너의 uid 가 내 uid 인가" 로 판정한다. 비루트는 자기 프로세스의
# 소켓만 볼 수 있으므로, 응답은 오는데 내 리스너가 안 보이면 남의 것이다.
cdp_port_is_mine() {
  local port="$1"
  # 컨테이너는 계정이 하나다(클라우드) — 판정 생략
  if [ -f /run/.containerenv ] || [ "$HOME" = "/home/agent" ]; then return 0; fi
  if [ "$(uname)" = "Darwin" ]; then
    [ -n "$(/usr/sbin/lsof -nP -a -u "$(id -u)" -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)" ]
    return
  fi
  if command -v ss >/dev/null 2>&1; then
    # 비루트 ss -p 는 자기 프로세스에만 users:(…) 를 붙인다
    ss -Hltnp "sport = :$port" 2>/dev/null | grep -q 'users:(('
    return
  fi
  # 최후: /proc/net/tcp 의 LISTEN(0A) 행 중 로컬 포트(hex)가 같은 것의 uid 열
  local hex; hex="$(printf '%04X' "$port")"
  awk -v hex=":$hex" -v uid="$(id -u)" '$2 ~ hex"$" && $4=="0A" && $8==uid {found=1} END{exit !found}' \
    /proc/net/tcp /proc/net/tcp6 2>/dev/null
}

if curl -sf -m 2 "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1 && ! cdp_port_is_mine "$CDP_PORT"; then
  echo "CDP port $CDP_PORT answers but its listener is not owned by uid $(id -u) — another OS account's browser (shared Mac)." >&2
  echo "Refusing to treat it as mine. Use this account's own port: PV_CDP_PORT (daemon-injected; shared-Mac tenant = 19000+uid)." >&2
  exit 2
fi

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
  # 남의 호스트명으로 걸린 프로필 잠금 제거 — 컨테이너 재생성·호스트→컨테이너 이전 뒤엔 옛 호스트명의
  # SingletonLock 이 남아 크롬이 "다른 컴퓨터에서 사용 중" 으로 기동을 거부한다 (2026-09-17 sean3 실측).
  # 여기는 CDP 가 응답하지 않는 분기라 이 프로필을 쓰는 크롬이 없다.
  lock="$HOME/.pv-browser/SingletonLock"
  if [ -L "$lock" ]; then
    owner="$(readlink "$lock")"
    if [ "${owner%-*}" != "$(hostname)" ] || ! kill -0 "${owner##*-}" 2>/dev/null; then
      rm -f "$HOME/.pv-browser/SingletonLock" "$HOME/.pv-browser/SingletonCookie" "$HOME/.pv-browser/SingletonSocket"
    fi
  fi
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

# ── CDP 브리지 (컨테이너 전용) ──
# 컨테이너 판정: 홈이 /home/agent 이거나 podman 이 만드는 /run/.containerenv 가 있을 때.
# 공용 호스트 컨테이너는 세션 보존을 위해 홈을 호스트와 같은 경로(/srv/pv/users/<id>)로 쓴다 (2026-09-17).
if { [ "$HOME" = "/home/agent" ] || [ -f /run/.containerenv ]; } && ! pgrep -f "node .*cdp-proxy.js" >/dev/null 2>&1; then
  nohup node "$SCRIPT_DIR/cdp-proxy.js" >/dev/null 2>&1 &
  started="$started cdp-proxy"
fi

if [ -z "$started" ]; then
  echo "already running (cdp $CDP_PORT, uid $(id -u))"
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
