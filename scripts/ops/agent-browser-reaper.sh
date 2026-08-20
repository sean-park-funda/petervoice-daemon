#!/bin/bash
# agent-browser 임시 프로필 크롬 리퍼
#
# agent-browser 는 세션마다 /tmp 에 agent-browser-chrome-<uuid> 프로필로 크롬을 띄운다.
# 에이전트가 작업 후 `agent-browser close` 를 부르지 않으면 그대로 남고, 며칠씩 누적되면
# 렌더러들이 CPU 를 계속 먹는다 — 2026-08-20 실사고: 14개 인스턴스 121프로세스 CPU 575%,
# 로드 애버리지 158(10코어). 가장 오래된 것은 13일째였다.
#
# 정책: MAX_AGE_HOURS 를 넘긴 인스턴스만 종료한다. 에이전트 브라우저 작업은 길어야 수십 분이라
# 하루를 넘겼다면 방치된 것으로 본다.
#
# 건드리지 않는 것:
#   - 공유 브라우저(~/.pv-browser, CDP 9222) — 로그인 세션이 살아 있는 상용 자원
#   - 사용자 개인 크롬(기본 프로필)
#   임시 프로필 경로 패턴에 걸리는 것만 대상이므로 둘 다 구조적으로 제외된다.

set -uo pipefail
MAX_AGE_HOURS="${AGENT_BROWSER_MAX_AGE_HOURS:-24}"
PATTERN="agent-browser-chrome-"

killed=0

# "13-08:18:05" / "1-01:25:55" / "05:40:17" / "12:34" → 시간(정수)
# (macOS ps 에는 etimes 가 없어 etime 문자열을 직접 해석한다. lstart 는 공백이 섞여 있어
#  read 로 안전하게 못 읽는다 — 실제로 그렇게 짰다가 파싱이 조용히 실패했다.)
etime_hours() {
  local e="$1" days=0 rest="$e"
  case "$e" in *-*) days="${e%%-*}"; rest="${e#*-}";; esac
  local h=0 m=0
  local c=$(echo "$rest" | awk -F: '{print NF}')
  if [ "$c" -eq 3 ]; then h=$(echo "$rest" | cut -d: -f1)
  else h=0; fi
  echo $(( 10#$days * 24 + 10#${h#0} ))
}

profiles=$(ps -Ao command | grep "Google Chrome" | grep -o "${PATTERN}[a-f0-9-]\{36\}" | sort -u)

for prof in $profiles; do
  # 이 프로필에서 가장 오래된(=가장 먼저 뜬) 프로세스의 나이를 인스턴스 나이로 본다
  oldest=0
  for e in $(ps -Ao etime,command | grep "$prof" | grep "Google Chrome" | grep -v grep | awk '{print $1}'); do
    h=$(etime_hours "$e")
    [ "$h" -gt "$oldest" ] && oldest=$h
  done

  if [ "$oldest" -ge "$MAX_AGE_HOURS" ]; then
    n=$(pgrep -f "$prof" | wc -l | tr -d ' ')
    pkill -f "$prof" && {
      echo "$(date '+%Y-%m-%d %H:%M:%S') reaped ${prof#$PATTERN} (age=${oldest}h, procs=${n})"
      killed=$((killed + 1))
    }
  fi
done

[ "$killed" -gt 0 ] && echo "$(date '+%Y-%m-%d %H:%M:%S') total reaped: $killed instance(s)"
exit 0
