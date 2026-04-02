#!/usr/bin/env bash
# diagnose_session.sh — 피터보이스 데몬 프로젝트별 진단/리셋/킬
# Usage:
#   diagnose_session.sh [project]          # 진단 (기본: 전체)
#   diagnose_session.sh <project> reset    # 세션 리셋
#   diagnose_session.sh <project> kill     # 프로세스 강제종료 + 세션 리셋

set -euo pipefail

DAEMON_DIR="$HOME/.claude-daemon"
SESSIONS_FILE="$DAEMON_DIR/sessions.json"
QUEUE_FILE="$DAEMON_DIR/queue.json"
LOG_FILE="$DAEMON_DIR/daemon.log"
PID_FILE="$DAEMON_DIR/daemon.pid"

PROJECT="${1:-}"
ACTION="${2:-diagnose}"

# ─── Helper ──────────────────────────────────────────────────
print_header() { echo -e "\n=== $1 ==="; }

get_session_keys() {
  # Return session keys matching project (e.g. "cocktail:default")
  python3 -c "
import json, sys
sessions = json.load(open('$SESSIONS_FILE'))
project = sys.argv[1]
for k in sessions:
    if k.startswith(project + ':'):
        print(k)
" "$1" 2>/dev/null || true
}

get_session_ids() {
  python3 -c "
import json, sys
sessions = json.load(open('$SESSIONS_FILE'))
project = sys.argv[1]
for k, v in sessions.items():
    if k.startswith(project + ':'):
        print(v['session_id'])
" "$1" 2>/dev/null || true
}

# ─── Diagnose ────────────────────────────────────────────────
do_diagnose() {
  local target="$1"

  print_header "데몬 프로세스"
  if [ -f "$PID_FILE" ]; then
    daemon_pid=$(cat "$PID_FILE")
    if kill -0 "$daemon_pid" 2>/dev/null; then
      echo "데몬 실행 중 (PID $daemon_pid)"
    else
      echo "⚠️  PID 파일 존재하나 프로세스 없음 (PID $daemon_pid)"
    fi
  else
    echo "⚠️  PID 파일 없음 — 데몬 미실행"
  fi

  print_header "활성 Claude 프로세스"
  if [ -n "$target" ]; then
    session_ids=$(get_session_ids "$target")
    if [ -n "$session_ids" ]; then
      found=0
      while IFS= read -r sid; do
        procs=$(pgrep -f "claude.*--resume.*$sid" 2>/dev/null || true)
        if [ -n "$procs" ]; then
          echo "프로젝트 '$target' 세션 $sid:"
          ps -p $procs -o pid,etime,command 2>/dev/null || true
          found=1
        fi
      done <<< "$session_ids"
      if [ "$found" -eq 0 ]; then
        echo "프로젝트 '$target'의 활성 claude 프로세스 없음"
      fi
    else
      echo "프로젝트 '$target'의 세션 없음"
    fi
  else
    # 전체 claude 프로세스
    claude_procs=$(pgrep -f "claude.*-p" 2>/dev/null || true)
    if [ -n "$claude_procs" ]; then
      ps -p $claude_procs -o pid,etime,command 2>/dev/null || true
    else
      echo "활성 claude 프로세스 없음"
    fi
  fi

  print_header "세션 정보"
  if [ -n "$target" ]; then
    python3 -c "
import json, sys
sessions = json.load(open('$SESSIONS_FILE'))
found = False
for k, v in sessions.items():
    if k.startswith(sys.argv[1] + ':'):
        print(f'  {k}:')
        print(f'    session_id: {v[\"session_id\"]}')
        print(f'    messages: {v.get(\"message_count\", \"?\")}')
        print(f'    last_used: {v.get(\"last_used\", \"?\")}')
        found = True
if not found:
    print(f'  프로젝트 \"{sys.argv[1]}\"의 세션 없음')
" "$target"
  else
    python3 -c "
import json
sessions = json.load(open('$SESSIONS_FILE'))
for k, v in sorted(sessions.items()):
    print(f'  {k}: msgs={v.get(\"message_count\",\"?\")}, last={v.get(\"last_used\",\"?\")[:16]}')
"
  fi

  print_header "큐 상태"
  python3 -c "
import json, sys
queue = json.load(open('$QUEUE_FILE')) if __import__('os').path.exists('$QUEUE_FILE') else []
target = sys.argv[1]
if target:
    items = [q for q in queue if q.get('project') == target]
else:
    items = queue
if items:
    for q in items:
        print(f'  프로젝트={q.get(\"project\")}, 시간={q.get(\"created_at\",\"?\")[:16]}, 텍스트={q.get(\"text\",\"\")[:50]}')
else:
    print('  대기 메시지 없음')
" "$target"

  if [ -n "$target" ] && [ -f "$LOG_FILE" ]; then
    print_header "최근 로그 ($target)"
    grep -i "$target" "$LOG_FILE" 2>/dev/null | tail -20 || echo "  관련 로그 없음"
  fi

  # 판정
  print_header "판정"
  if [ -z "$target" ]; then
    echo "특정 프로젝트를 지정하면 상세 판정 가능"
    return
  fi

  session_ids=$(get_session_ids "$target")
  has_process=0
  if [ -n "$session_ids" ]; then
    while IFS= read -r sid; do
      if pgrep -f "claude.*--resume.*$sid" >/dev/null 2>&1; then
        has_process=1
        # 프로세스 실행 시간 확인
        pid=$(pgrep -f "claude.*--resume.*$sid" 2>/dev/null | head -1)
        if [ -n "$pid" ]; then
          etime=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')
          echo "프로세스 실행 중 (PID $pid, 경과: $etime)"
          # 5분 이상이면 경고
          mins=$(echo "$etime" | awk -F: '{if(NF==3) print $1*60+$2; else if(NF==2) print $1; else print 0}')
          if [ "${mins:-0}" -ge 5 ]; then
            echo "⚠️  5분 이상 실행 중 — 멈춤 가능성. 'kill' 모드로 강제종료 고려"
          else
            echo "✅ 정상 처리 중으로 보임"
          fi
        fi
        break
      fi
    done <<< "$session_ids"
  fi

  if [ "$has_process" -eq 0 ]; then
    queue_count=$(python3 -c "
import json, os
queue = json.load(open('$QUEUE_FILE')) if os.path.exists('$QUEUE_FILE') else []
print(len([q for q in queue if q.get('project') == '$target']))
" 2>/dev/null || echo 0)

    if [ "$queue_count" -gt 0 ]; then
      echo "⚠️  큐에 메시지 $queue_count개 있으나 프로세스 없음 — 데몬이 처리 못하는 상태"
      echo "   → 'reset' 후 재시도 또는 데몬 재시작 필요"
    elif [ -n "$session_ids" ]; then
      echo "✅ 세션 존재, 프로세스 없음, 큐 비어있음 — 대기 상태 (정상)"
    else
      echo "ℹ️  세션 없음 — 아직 사용하지 않았거나 이미 리셋됨"
    fi
  fi
}

# ─── Reset ───────────────────────────────────────────────────
do_reset() {
  local target="$1"
  if [ -z "$target" ]; then
    echo "❌ 리셋할 프로젝트를 지정하세요"
    exit 1
  fi

  keys=$(get_session_keys "$target")
  if [ -z "$keys" ]; then
    echo "프로젝트 '$target'의 세션이 없습니다"
    return
  fi

  python3 -c "
import json, sys
sessions = json.load(open('$SESSIONS_FILE'))
project = sys.argv[1]
removed = []
for k in list(sessions.keys()):
    if k.startswith(project + ':'):
        removed.append(k)
        del sessions[k]
with open('$SESSIONS_FILE', 'w') as f:
    json.dump(sessions, f, indent=2)
for k in removed:
    print(f'  삭제: {k}')
print(f'✅ {len(removed)}개 세션 리셋 완료. 다음 메시지 시 새 세션 시작됩니다.')
" "$target"
}

# ─── Kill ────────────────────────────────────────────────────
do_kill() {
  local target="$1"
  if [ -z "$target" ]; then
    echo "❌ 종료할 프로젝트를 지정하세요"
    exit 1
  fi

  session_ids=$(get_session_ids "$target")
  killed=0
  if [ -n "$session_ids" ]; then
    while IFS= read -r sid; do
      pids=$(pgrep -f "claude.*--resume.*$sid" 2>/dev/null || true)
      if [ -n "$pids" ]; then
        echo "세션 $sid의 프로세스 종료: $pids"
        echo "$pids" | xargs kill -9 2>/dev/null || true
        killed=1
      fi
    done <<< "$session_ids"
  fi

  if [ "$killed" -eq 0 ]; then
    echo "종료할 프로세스 없음"
  else
    echo "✅ 프로세스 강제종료 완료"
  fi

  # 세션도 리셋
  do_reset "$target"
}

# ─── Main ────────────────────────────────────────────────────
case "$ACTION" in
  diagnose) do_diagnose "$PROJECT" ;;
  reset)    do_reset "$PROJECT" ;;
  kill)     do_kill "$PROJECT" ;;
  *)        echo "Usage: $0 [project] [diagnose|reset|kill]"; exit 1 ;;
esac
