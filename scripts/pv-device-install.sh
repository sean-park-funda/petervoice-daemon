#!/bin/bash
# 피터보이스 「추가 기기」 설치 — 한 계정 · 여러 기기 (2026-11-26)
#
# 이미 피터보이스를 쓰는 계정의 프로젝트 일부를 이 맥에서 돌리게 한다. 이 맥의 데몬은 유저 열쇠가 아니라
# 「기기 열쇠」(pvd_…)로 붙고, 서버는 이 기기에 배치된 프로젝트의 메시지 · 반복 작업 · 허용한 비밀값만 내려준다.
# 계획서: peter-voice docs/plans/2026-11-26-multi-device-project-placement.md
#
# 쓰는 법 (이 맥에서, 기기 열쇠는 표준 입력으로 — 명령줄 인자 · 환경변수로 넘기지 않는다):
#   bash pv-device-install.sh --name gamemac [--branch main] [--pin-codex] [--label "게임 맥"] < 열쇠파일
#   bash pv-device-install.sh --status          # 설치 상태만 본다 (아무것도 바꾸지 않는다)
#   bash pv-device-install.sh --uninstall       # 상주 작업을 내리고 설정을 지운다 (코드 폴더 · 프로젝트 폴더는 남긴다)
#
# 하지 않는 것: Homebrew · node · python · claude 설치(이미 있어야 한다), 클로드 로그인, 프로젝트 배치.
# 안전장치: 이 OS 계정에 이미 다른 열쇠의 데몬 설정이 있으면 멈춘다 — 본기기 데몬을 덮어쓰지 않는다.

set -euo pipefail

REPO_URL="https://github.com/sean-park-funda/petervoice-daemon.git"
API_URL="${PV_API_URL:-https://www.peter-voice.site}"
INSTALL_DIR="$HOME/peter-voice"
DAEMON_DIR="$HOME/.claude-daemon"
VENV_DIR="$INSTALL_DIR/.venv"
PLIST_LABEL="com.petervoice.daemon"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
CONFIG="$DAEMON_DIR/config.json"

NAME=""; BRANCH="main"; PIN_CODEX=0; LABEL=""; MODE="install"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --pin-codex) PIN_CODEX=1; shift ;;
    --status) MODE="status"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    *) echo "모르는 옵션: $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf '실패: %s\n' "$*" >&2; exit 1; }
py() { /usr/bin/env python3 "$@"; }
cfg() { py -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c.get(sys.argv[2], ""))' "$CONFIG" "$1" 2>/dev/null || true; }
loaded() { launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; }

status() {
  say "기기 이름:   $( [[ -f "$CONFIG" ]] && cfg device_name || echo '(설정 없음)')"
  say "코드:        $( [[ -d "$INSTALL_DIR/.git" ]] && git -C "$INSTALL_DIR" log -1 --format='%h %s' | cut -c1-70 || echo '(없음)')"
  say "갱신 브랜치: $( [[ -f "$CONFIG" ]] && cfg update_branch || echo '-')"
  say "상주 작업:   $(loaded && echo '올라와 있음' || echo '없음')"
  say "claude:      $(command -v claude >/dev/null 2>&1 && claude --version 2>/dev/null | head -1 || echo '(없음)')"
  if [[ -f "$DAEMON_DIR/daemon.log" ]]; then
    say "최근 로그:   $(tail -1 "$DAEMON_DIR/daemon.log" | cut -c1-110)"
  fi
}

if [[ "$MODE" == "status" ]]; then status; exit 0; fi

if [[ "$MODE" == "uninstall" ]]; then
  [[ -f "$CONFIG" && -n "$(cfg device_name)" ]] || die "이 계정의 데몬은 기기 설치가 아닙니다(device_name 없음) — 건드리지 않습니다"
  loaded && launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true
  rm -f "$PLIST_PATH" "$CONFIG" "$DAEMON_DIR/.env.secrets"
  say "내렸습니다. 남긴 것: $INSTALL_DIR (코드) · $DAEMON_DIR/projects (프로젝트 폴더) · 로그"
  exit 0
fi

# ── 설치 ─────────────────────────────────────────────────────
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || die "--name 은 영소문자 · 숫자 · 하이픈 1~32자입니다"
[[ "$(uname -s)" == "Darwin" ]] || die "macOS 전용입니다"
[[ -t 0 ]] && die "기기 열쇠를 표준 입력으로 주세요 (예: < 열쇠파일)"
IFS= read -r DEVICE_KEY || true
DEVICE_KEY="$(printf '%s' "${DEVICE_KEY:-}" | tr -d '[:space:]')"
[[ "$DEVICE_KEY" == pvd_* && ${#DEVICE_KEY} -ge 40 ]] || die "기기 열쇠(pvd_…)가 아닙니다 — 유저 열쇠로는 기기를 설치하지 않습니다"

for c in git python3 curl; do command -v "$c" >/dev/null 2>&1 || die "$c 가 없습니다"; done
py -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "python 3.10 이상이 필요합니다"
command -v claude >/dev/null 2>&1 || say "주의: claude 가 PATH 에 없습니다 — 설치 · 로그인 전에는 턴을 돌리지 못합니다"

# 본기기 데몬을 덮어쓰지 않는다
if [[ -f "$CONFIG" ]]; then
  OLD_KEY="$(cfg api_key)"
  if [[ -n "$OLD_KEY" && "$OLD_KEY" != "$DEVICE_KEY" ]]; then
    [[ "$OLD_KEY" == pvd_* && "$(cfg device_name)" == "$NAME" ]] \
      || die "이 OS 계정에는 이미 다른 데몬 설정이 있습니다($CONFIG) — 기기는 전용 OS 계정에 설치하세요"
  fi
fi

# 열쇠가 서버에서 이 이름의 기기로 보이는지 먼저 확인한다 (틀린 열쇠로 상주 작업을 올리지 않는다)
ME="$(printf 'header = "Authorization: Bearer %s"\n' "$DEVICE_KEY" | curl -sS --max-time 15 -K - "$API_URL/api/bot/me" || true)"
SEEN="$(printf '%s' "$ME" | py -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get("device") or "", d.get("username") or "")' )"
[[ "${SEEN%% *}" == "$NAME" ]] || die "서버가 이 열쇠를 기기 \"$NAME\" 로 보지 않습니다(응답의 기기: \"${SEEN%% *}\") — 기기 줄의 이름 · 열쇠를 확인하세요"
ACCOUNT="${SEEN#* }"
say "열쇠 확인: 계정 ${ACCOUNT} 의 기기 ${NAME}"

# 코드
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" fetch -q origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout -q "$BRANCH" 2>/dev/null || git -C "$INSTALL_DIR" checkout -q -b "$BRANCH" "origin/$BRANCH"
  git -C "$INSTALL_DIR" merge -q --ff-only "origin/$BRANCH"
else
  [[ -e "$INSTALL_DIR" ]] && die "$INSTALL_DIR 가 이미 있는데 git 폴더가 아닙니다"
  git clone -q --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
say "코드: $(git -C "$INSTALL_DIR" log -1 --format='%h' ) ($BRANCH)"

[[ -x "$VENV_DIR/bin/python" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"

# 설정 (열쇠는 파일로만 — 권한 600)
mkdir -p "$DAEMON_DIR" && chmod 700 "$DAEMON_DIR"
umask 077
printf '%s' "$DEVICE_KEY" | py -c '
import json, os, sys
path, api_url, name, branch, pin, label, account = sys.argv[1:8]
key = sys.stdin.read().strip()
cfg = {}
if os.path.exists(path):
    try: cfg = json.load(open(path))
    except Exception: cfg = {}
cfg.update({"api_url": api_url, "api_key": key, "device_name": name, "username": account,
            "bot_name": label or f"{account}-{name}", "update_branch": branch})
if pin == "1":
    cfg["codex_cli_update_enabled"] = False   # 이 맥의 codex 버전은 다른 담당이 고정해 관리한다
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
' "$CONFIG" "$API_URL" "$NAME" "$BRANCH" "$PIN_CODEX" "$LABEL" "$ACCOUNT"
unset DEVICE_KEY

# 상주 작업
mkdir -p "$HOME/Library/LaunchAgents"
CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
NPM_BIN="$( (npm config get prefix 2>/dev/null || echo /opt/homebrew) )/bin"
LAUNCHD_PATH="$(printf '%s\n' ${CLAUDE_BIN:+"$(dirname "$CLAUDE_BIN")"} "$NPM_BIN" /opt/homebrew/bin /usr/local/bin /usr/bin /bin /usr/sbin /sbin | awk '!seen[$0]++' | paste -sd: -)"
cat > "$PLIST_PATH" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python</string>
        <string>${INSTALL_DIR}/scripts/claude_daemon.py</string>
    </array>
    <key>WorkingDirectory</key><string>${INSTALL_DIR}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>ExitTimeOut</key><integer>60</integer>
    <key>StandardOutPath</key><string>${DAEMON_DIR}/launchd-stdout.log</string>
    <key>StandardErrorPath</key><string>${DAEMON_DIR}/launchd-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>${LAUNCHD_PATH}</string>
        <key>HOME</key><string>${HOME}</string>
        <key>LANG</key><string>en_US.UTF-8</string>
    </dict>
    <key>ProcessType</key><string>Background</string>
    <key>LowPriorityBackgroundIO</key><true/>
</dict>
</plist>
PLISTEOF

loaded && launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" || die "상주 작업을 올리지 못했습니다 — 이 OS 계정이 화면에 로그인돼 있어야 합니다(자동 로그인 확인)"

# 데몬이 기기 모드로 떴는지 로그로 확인
for _ in $(seq 1 20); do
  sleep 1
  if grep -q "Device mode: \"$NAME\"" "$DAEMON_DIR/daemon.log" 2>/dev/null; then
    say "설치 끝: 데몬이 기기 \"$NAME\" 로 떴습니다."
    status
    exit 0
  fi
done
die "데몬이 기기 모드로 뜬 것을 로그에서 확인하지 못했습니다 — $DAEMON_DIR/daemon.log · launchd-stderr.log 를 보세요"
