#!/bin/bash
# pv-restart-when-idle.sh — 데몬을 「도는 턴이 0개인 순간」에 다시 올린다.
# config.json 의 값 대부분(칸 수 · 턴 시간 제한 등)은 데몬이 시작할 때만 읽는다. 그냥 재시작하면 도는 턴이
# 드레인 상한(shutdown_drain_sec) 뒤에 끊기므로, 턴이 하나도 없는 순간을 기다렸다가 재시작한다.
#
#   pv-restart-when-idle.sh --label <launchd 라벨> [--tag 이름] [--maxwait 초=7200]
#     라벨: 이 맥(Sean) com.petervoice.claude-daemon · 설치본(기기 · 고객 맥) com.petervoice.daemon
#
# 결과: ~/.claude-daemon/restart-when-idle/<tag>.result 한 줄 (OK … | TIMEOUT … | FAIL …), 과정은 <tag>.log
# 종료코드: 0 재시작함 · 1 기다리다 시간 초과(재시작 안 함) · 2 재시작했는데 데몬이 안 올라옴
#
# 데몬의 자식으로 띄우지 말 것 — 데몬을 멈추는 순간 같이 죽을 수 있다. ssh 로 띄우거나 launchd 1회 작업으로 돌린다.
LABEL=""; TAG="restart"; MAXWAIT=7200
while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --maxwait) MAXWAIT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
[ -n "$LABEL" ] || { echo "--label required" >&2; exit 64; }

D="$HOME/.claude-daemon"; OUT="$D/restart-when-idle"; mkdir -p "$OUT"
R="$OUT/$TAG.result"; L="$OUT/$TAG.log"
exec >>"$L" 2>&1
rm -f "$R"
say(){ echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
daemon_pid(){ cat "$D/daemon.pid" 2>/dev/null; }
# 도는 턴 = 데몬의 직계 자식 중 엔진 프로세스(claude -p · codex exec · gemini). 데몬이 없으면 -1.
running(){
  local dp; dp=$(daemon_pid)
  { [ -n "$dp" ] && kill -0 "$dp" 2>/dev/null; } || { echo -1; return; }
  pgrep -P "$dp" -f 'claude -p|codex exec|gemini' 2>/dev/null | wc -l | tr -d ' '
}
# 시작할 때만 읽는 값들 — 결과 줄에 남겨 「무엇이 적용됐는지」를 나중에 읽을 수 있게 한다.
cfg(){ /usr/bin/python3 - "$D/config.json" <<'PY' 2>/dev/null
import json,sys
c=json.load(open(sys.argv[1]))
print("hard=%s tools=%s lanes=%s" % (c.get("claude_hard_timeout_sec","기본 900"), c.get("claude_hard_timeout_with_tools_sec","기본 1800"), c.get("max_concurrent","기본 3")))
PY
}

say "start: label $LABEL, max wait ${MAXWAIT}s, running $(running), config: $(cfg)"
END=$(( $(date +%s) + MAXWAIT )); zeros=0; last_logged=""
while :; do
  n=$(running)
  if [ "$n" = "0" ]; then zeros=$((zeros+1)); else zeros=0; fi
  [ "$zeros" -ge 2 ] && break
  if [ "$(date +%s)" -ge "$END" ]; then
    say "timeout, running=$n"; echo "TIMEOUT running=$n — not restarted (waited ${MAXWAIT}s)" > "$R"; exit 1
  fi
  # 10분에 한 번만 진행을 남긴다
  stamp=$(date '+%H:%M' | cut -c1-4); [ "$stamp" != "$last_logged" ] && { say "waiting, running=$n"; last_logged="$stamp"; }
  sleep 10
done

OLD=$(daemon_pid); T0=$(date '+%Y-%m-%d %H:%M:%S')
say "idle (2 checks 10s apart) → stop pid $OLD"
launchctl stop "$LABEL"
for i in $(seq 1 150); do kill -0 "$OLD" 2>/dev/null || break; sleep 1; done
kill -0 "$OLD" 2>/dev/null && say "old pid still alive after 150s (launchd kills it at ExitTimeOut)"

# KeepAlive=true 인 맥은 launchd 가 스스로 올린다. 설치본(SuccessfulExit=false)은 정상 종료 뒤 안 올라오므로 직접 올린다.
up(){ local p; p=$(daemon_pid); [ -n "$p" ] && [ "$p" != "$OLD" ] && kill -0 "$p" 2>/dev/null; }
for i in $(seq 1 20); do up && break; sleep 1; done
if ! up; then say "not up by itself → kickstart"; launchctl kickstart "gui/$(id -u)/$LABEL" || launchctl start "$LABEL"; fi
for i in $(seq 1 120); do up && break; sleep 1; done
if ! up; then
  say "still not up → start again"; launchctl start "$LABEL"; sleep 30
fi
if ! up; then
  say "FAIL: daemon not up (pid file: $(daemon_pid))"
  echo "FAIL daemon not up after restart at $T0 (old pid $OLD, pid file $(daemon_pid))" > "$R"; exit 2
fi
# 올라온 뒤 15초 버티는지 본다(설정 오류로 바로 죽는 경우)
NEW=$(daemon_pid); sleep 15
if ! kill -0 "$NEW" 2>/dev/null; then
  say "FAIL: new pid $NEW died within 15s"
  echo "FAIL daemon pid $NEW died within 15s of restart at $T0" > "$R"; exit 2
fi
loaded=$(grep -E 'Config loaded:' "$D/daemon.log" 2>/dev/null | tail -1 | cut -c1-19)
[ -n "$loaded" ] && [ "$loaded" \> "$T0" ] || loaded="(로그 줄 못 찾음)"
say "ok: pid $OLD → $NEW, config loaded $loaded, $(cfg)"
echo "OK restarted $T0 · pid $OLD → $NEW · config loaded $loaded · $(cfg)" > "$R"
exit 0
