#!/bin/bash
# agent-browser 임시 프로필 크롬 + 고아 세션 리퍼
#
# agent-browser 는 세션마다 /tmp 에 agent-browser-chrome-<uuid> 프로필로 크롬을 띄우고,
# 세션당 서버 프로세스 하나(~/.agent-browser/<세션>.pid)를 유지한다. 에이전트가 작업 후
# `agent-browser close` 를 부르지 않으면 둘 다 남는다 — 2026-08-20 실사고: 방치된 크롬
# 14개(121 프로세스)가 CPU 575%, 로드 애버리지 158(10코어). 가장 오래된 것은 13일째였다.
#
# 3단계로 정리한다:
#   ① 오래된 크롬 인스턴스
#   ② 크롬이 없는 채로 오래 떠 있는 고아 서버
#   ③ 죽은 세션의 런타임 파일
#
# **서버는 나이만으로 판단하지 않는다.** --session 은 재사용이 목적이라 서버가 며칠씩
# 사는 게 정상이다. 실제로 10일 된 v2 서버가 6분 전 띄운 크롬을 물고 활발히 쓰이고 있었다.
# 크롬 자식이 하나라도 있으면 사용 중으로 보고 건드리지 않는다.
#
# 건드리지 않는 것:
#   - 공유 브라우저(~/.pv-browser, CDP 9222) — 로그인 세션이 살아 있는 상용 자원
#   - 사용자 개인 크롬(기본 프로필)
#   - `~/.agent-browser/sessions/*.json` — --restore 용 쿠키. 세션을 지워도 이건 남긴다
#   앞의 둘은 임시 프로필 경로 패턴에 안 걸리므로 구조적으로 제외된다.
#
# DRY_RUN=1 로 실행하면 무엇을 지울지만 출력한다.

set -uo pipefail
MAX_AGE_HOURS="${AGENT_BROWSER_MAX_AGE_HOURS:-24}"
DRY_RUN="${DRY_RUN:-0}"
PATTERN="agent-browser-chrome-"
AB_DIR="$HOME/.agent-browser"
CLAUDE_PROJECTS="$HOME/.claude/projects"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
act() {  # DRY_RUN 이면 실행하지 않고 표시만
  if [ "$DRY_RUN" = "1" ]; then echo "         [dry-run] $*"; else "$@" >/dev/null 2>&1; fi
}

# "13-08:18:05" / "1-01:25:55" / "05:40:17" / "12:34" → 시간(정수)
# (macOS ps 에는 etimes 가 없어 etime 문자열을 직접 해석한다. lstart 는 공백이 섞여 있어
#  read 로 안전하게 못 읽는다 — 실제로 그렇게 짰다가 파싱이 조용히 실패했다.)
etime_hours() {
  local e="$1" days=0 rest="$1" h=0
  case "$e" in *-*) days="${e%%-*}"; rest="${e#*-}";; esac
  if [ "$(echo "$rest" | awk -F: '{print NF}')" -eq 3 ]; then h=$(echo "$rest" | cut -d: -f1); fi
  echo $(( 10#$days * 24 + 10#${h#0} ))
}

# 세션을 만든 프로젝트를 찾는다 — 정리 로그에 남겨야 반복되는 누수의 주인을 추적할 수 있다.
# (1.2GB / 3600여 개 jsonl 을 훑으므로 실제로 뭔가 지울 때만 부른다)
owner_of() {
  local name="$1" hit
  # 여러 프로젝트가 같은 세션명을 언급할 수 있다(트러블슈팅 중인 대화도 포함된다 —
  # 실제로 이 리퍼를 만들며 세션명을 조회한 대화가 매칭됐다). **가장 먼저 언급한 파일**
  # = 만든 쪽으로 본다. 그래서 mtime 오름차순 첫 번째를 고른다.
  hit=$(grep -rl --include='*.jsonl' -m1 -- "--session $name" "$CLAUDE_PROJECTS" 2>/dev/null \
        | xargs -I{} stat -f '%m {}' {} 2>/dev/null | sort -n | head -1 | cut -d' ' -f2-)
  if [ -n "$hit" ]; then
    basename "$(dirname "$hit")" | sed -E 's/^-Users-sean-(-claude-daemon-)?[Pp]rojects-+//'
  else
    echo "unknown"
  fi
}

killed_chrome=0; killed_srv=0; cleaned=0

# ── ① 오래된 크롬 인스턴스 ────────────────────────────────────
for prof in $(ps -Ao command | grep "Google Chrome" | grep -o "${PATTERN}[a-f0-9-]\{36\}" | sort -u); do
  oldest=0
  for e in $(ps -Ao etime,command | grep "$prof" | grep "Google Chrome" | grep -v grep | awk '{print $1}'); do
    h=$(etime_hours "$e"); [ "$h" -gt "$oldest" ] && oldest=$h
  done
  if [ "$oldest" -ge "$MAX_AGE_HOURS" ]; then
    n=$(pgrep -f "$prof" | wc -l | tr -d ' ')
    log "reap chrome ${prof#$PATTERN} (age=${oldest}h, procs=${n})"
    act pkill -f "$prof"
    killed_chrome=$((killed_chrome + 1))
  fi
done

# ── ② 크롬 없는 고아 서버 ─────────────────────────────────────
for pidfile in "$AB_DIR"/*.pid; do
  [ -e "$pidfile" ] || continue
  name=$(basename "$pidfile" .pid)
  pid=$(cat "$pidfile" 2>/dev/null)
  [ -n "${pid:-}" ] || continue
  et=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')
  [ -n "$et" ] || continue                       # 이미 죽음 → ③에서 파일 정리
  # 크롬 자식이 있으면 사용 중 — 나이와 무관하게 살려둔다
  if pgrep -P "$pid" 2>/dev/null | xargs -I{} ps -o command= -p {} 2>/dev/null | grep -q "Google Chrome"; then
    continue
  fi
  # 최근에 restore 쿠키가 저장된 세션은 사용 중으로 본다. agent-browser 는 세션이 살아 있는
  # 동안 쿠키를 주기적으로 파일에 쓰므로, 이 파일의 mtime 이 곧 마지막 활동 시각이다.
  # 크롬이 잠깐 닫힌 순간에 서버가 정리되는 것을 막는다 — 로그인 세션을 든 채로
  # 다단계 폼을 작성하는 작업(pv4-inicis 등)이 여기 걸린다.
  if [ -n "$(find "$AB_DIR/sessions" -name "$name-*.json" -newermt "-${MAX_AGE_HOURS} hours" 2>/dev/null)" ]; then
    continue
  fi

  age=$(etime_hours "$et")
  if [ "$age" -ge "$MAX_AGE_HOURS" ]; then
    log "reap server '$name' (age=${age}h, no chrome, owner=$(owner_of "$name"))"
    act kill "$pid"
    killed_srv=$((killed_srv + 1))
  fi
done

# ── ③ 죽은 세션의 런타임 파일 ─────────────────────────────────
# sessions/*.json(--restore 쿠키)은 건드리지 않는다.
for cfg in "$AB_DIR"/*.config; do
  [ -e "$cfg" ] || continue
  name=$(basename "$cfg" .config)
  pid=$(cat "$AB_DIR/$name.pid" 2>/dev/null)
  if [ -n "${pid:-}" ] && ps -p "$pid" >/dev/null 2>&1; then continue; fi   # 살아 있음
  for ext in config engine pid sock stream version; do
    [ -e "$AB_DIR/$name.$ext" ] && act rm -f "$AB_DIR/$name.$ext"
  done
  cleaned=$((cleaned + 1))
done
[ "$cleaned" -gt 0 ] && log "cleaned runtime files for $cleaned dead session(s)"

if [ $((killed_chrome + killed_srv + cleaned)) -gt 0 ]; then
  log "summary: chrome=$killed_chrome server=$killed_srv files=$cleaned"
fi
exit 0
