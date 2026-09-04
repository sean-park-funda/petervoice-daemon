#!/bin/bash
# provision-macuser.sh — 공유 맥미니에 피터보이스 테넌트 유저 프로비저닝
#
# 사용: sudo ./provision-macuser.sh <username> <api_key> [disk_quota_gb]
#   username : 피터보이스 유저명 (DB users.username, 포탈 서브도메인과 동일)
#   api_key  : 그 유저의 pv_ api key
#   quota    : APFS 볼륨 쿼터 GB (기본 30)
#
# 하는 일:
#   1. macOS 계정 pv-<username> 생성 (로그인 불필요, 랜덤 비밀번호)
#   2. 쿼터 걸린 APFS 볼륨 생성 → /Users/pv-<username> 홈으로 마운트 (디스크 하드 격리)
#   3. 데몬 config.json 생성 (portal_shared 모드 — 포탈/터널은 소유자 것 공유)
#   4. LaunchDaemon 등록 (로그인 없이 부팅 시 기동)
#   5. 포탈 레지스트리(/Users/Shared/petervoice/portal-users.json)에 등록
#   6. 소유자 계정에 docs 읽기 ACL (홈포탈 서빙용)
#
# 남는 수동 1단계: 유저 본인 클로드 구독 로그인
#   sudo -u pv-<username> -H claude  →  /login (또는 웹 재로그인 플로우)

set -euo pipefail
# sudo 는 호출자 PATH 를 물려받는다(macOS 기본 sudoers에 secure_path 없음) —
# 데몬/launchd 환경엔 /usr/sbin 이 빠져 있어 sysadminctl/diskutil 이 127 나므로 고정
export PATH=/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin

USERNAME="${1:?usage: sudo ./provision-macuser.sh <username> <api_key> [quota_gb]}"
API_KEY="${2:?api_key required}"
QUOTA_GB="${3:-30}"

OSUSER="pv-${USERNAME}"
HOME_DIR="/Users/${OSUSER}"
DAEMON_DIR="${HOME_DIR}/.claude-daemon"
SHARED_DIR="/Users/Shared/petervoice"
REGISTRY="${SHARED_DIR}/portal-users.json"
REPO_DIR="${SHARED_DIR}/peter-voice-daemon"
API_URL="${PV_API_URL:-https://www.peter-voice.site}"
OWNER="${SUDO_USER:?run with sudo (need SUDO_USER)}"
LABEL="com.petervoice.daemon-${USERNAME}"

[ "$(uname)" = "Darwin" ] || { echo "macOS only"; exit 1; }
[ "$(id -u)" = "0" ] || { echo "run with sudo"; exit 1; }

echo "== [1/6] macOS 계정 ${OSUSER}"
if id "$OSUSER" >/dev/null 2>&1; then
  echo "   already exists — skip"
else
  PW=$(openssl rand -base64 24)
  sysadminctl -addUser "$OSUSER" -fullName "PV ${USERNAME}" -password "$PW" -home "$HOME_DIR" >/dev/null 2>&1
  # 로그인 화면에서 숨김
  dscl . create "/Users/${OSUSER}" IsHidden 1
fi

echo "== [2/6] APFS 쿼터 볼륨 (${QUOTA_GB}GB) → ${HOME_DIR}"
if mount | grep -q " on ${HOME_DIR} "; then
  echo "   already mounted — 소유권만 보정"
  chown "$OSUSER:staff" "$HOME_DIR"
  chmod 750 "$HOME_DIR"
else
  # 시스템 볼륨이 속한 APFS 컨테이너 자동 감지
  CONTAINER=$(diskutil info / | awk -F': *' '/APFS Container/{print $2}' | tr -d ' ')
  [ -n "$CONTAINER" ] || { echo "APFS container not found"; exit 1; }
  # 기존 홈 내용 보존
  TMP_BAK=""
  if [ -d "$HOME_DIR" ] && [ -n "$(ls -A "$HOME_DIR" 2>/dev/null)" ]; then
    TMP_BAK=$(mktemp -d "/tmp/pv-provision-${OSUSER}.XXXX")
    cp -a "$HOME_DIR/." "$TMP_BAK/"
  fi
  mkdir -p "$HOME_DIR"
  diskutil apfs addVolume "$CONTAINER" APFS "PV-${USERNAME}" -quota "${QUOTA_GB}g" -mountpoint "$HOME_DIR" >/dev/null
  # 재부팅 후에도 같은 위치에 마운트 (synthetic fstab)
  if [ -n "$TMP_BAK" ]; then cp -a "$TMP_BAK/." "$HOME_DIR/"; rm -rf "$TMP_BAK"; fi
  chown -R "$OSUSER:staff" "$HOME_DIR"
  chmod 750 "$HOME_DIR"
fi
# 재부팅 자동 마운트: /etc/fstab 은 일부 머신에서 보안 에이전트가 접근을 가로채
# 무한 대기하므로(2026-09-04 실사고) 사용 금지. 대신 부팅 시 PV-* 볼륨 전체를
# 마운트하는 LaunchDaemon 하나로 처리한다 (전 테넌트 공용, 멱등).
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p /usr/local/lib/petervoice
[ -f /usr/local/lib/petervoice/pv-mount-homes.sh ] || \
  install -m 755 "$SELF_DIR/pv-mount-homes.sh" /usr/local/lib/petervoice/pv-mount-homes.sh
MOUNTD="/Library/LaunchDaemons/com.petervoice.mount-homes.plist"
if [ ! -f "$MOUNTD" ]; then
  cat > "$MOUNTD" << 'MEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.petervoice.mount-homes</string>
    <key>ProgramArguments</key>
    <array><string>/usr/local/lib/petervoice/pv-mount-homes.sh</string></array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>
MEOF
  launchctl bootstrap system "$MOUNTD" 2>/dev/null || true
fi

echo "== [3/6] 데몬 설정"
mkdir -p "$SHARED_DIR"
if [ ! -d "$REPO_DIR" ]; then
  echo "   ⚠ ${REPO_DIR} 없음 — 공유 데몬 체크아웃을 먼저 두세요 (git clone peter-voice-daemon)"
fi
# 소유자 config 에서 공유 터널 ID 추출
OWNER_TUNNEL=$(python3 -c "import json;print(json.load(open('/Users/${OWNER}/.claude-daemon/config.json')).get('cloudflare_tunnel_id',''))")
[ -n "$OWNER_TUNNEL" ] || { echo "소유자 터널 ID 없음 (/Users/${OWNER}/.claude-daemon/config.json)"; exit 1; }
sudo -u "$OSUSER" mkdir -p "$DAEMON_DIR" "$DAEMON_DIR/projects" "$DAEMON_DIR/prompts"
if [ ! -f "$DAEMON_DIR/config.json" ]; then
  cat > "$DAEMON_DIR/config.json" << EOF
{
  "api_url": "${API_URL}",
  "api_key": "${API_KEY}",
  "username": "${USERNAME}",
  "max_concurrent": 3,
  "stream_interval_sec": 2.0,
  "session_ttl_hours": 24,
  "rewriter_enabled": false,
  "portal_shared": true,
  "shared_tunnel_id": "${OWNER_TUNNEL}"
}
EOF
  chown "$OSUSER:staff" "$DAEMON_DIR/config.json"
  chmod 600 "$DAEMON_DIR/config.json"
fi

echo "== [4/6] LaunchDaemon ${LABEL} (로그인 없이 부팅 기동)"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LABEL}</string>
    <key>UserName</key><string>${OSUSER}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3</string>
        <string>${REPO_DIR}/scripts/claude_daemon.py</string>
        <string>--config-dir</string>
        <string>${DAEMON_DIR}</string>
    </array>
    <key>WorkingDirectory</key><string>${HOME_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key><string>${HOME_DIR}</string>
        <key>LANG</key><string>en_US.UTF-8</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>${DAEMON_DIR}/daemon-stdout.log</string>
    <key>StandardErrorPath</key><string>${DAEMON_DIR}/daemon-stderr.log</string>
</dict>
</plist>
EOF
launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"

echo "== [5/6] 포탈 레지스트리 등록"
python3 - << EOF
import json, os
reg = {}
if os.path.exists("${REGISTRY}"):
    reg = json.load(open("${REGISTRY}"))
reg["${USERNAME}"] = {"home": "${HOME_DIR}", "config_dir": "${DAEMON_DIR}"}
json.dump(reg, open("${REGISTRY}", "w"), indent=2, ensure_ascii=False)
print("   registry updated:", list(reg.keys()))
EOF

echo "== [6/6] 소유자(${OWNER}) 읽기 ACL — 홈포탈 docs 서빙용"
# 주의: +a 상속(file_inherit)은 '이후 생성' 파일에만 적용 — 기존 파일엔 -R 로 직접 붙여야 한다
# (2026-09-04 실사고: config.json 에 ACL 이 없어 포탈이 api_key 를 못 읽고 전부 401)
chmod +a "user:${OWNER} allow list,search,read,readattr,readextattr,file_inherit,directory_inherit" "$HOME_DIR" 2>/dev/null || true
chmod -R +a "user:${OWNER} allow list,search,read,readattr,readextattr,file_inherit,directory_inherit" "$DAEMON_DIR" 2>/dev/null || true

# 레지스트리 디렉토리 잠금: /Users/Shared 는 기본이 전원 쓰기 가능 — 그대로 두면
# 아무 로컬 프로세스나 레지스트리를 조작해 타 유저 홈을 자기 이름에 매핑할 수 있다
chown root:wheel "$SHARED_DIR" 2>/dev/null || true
chmod 755 "$SHARED_DIR" 2>/dev/null || true
chown root:wheel "$REGISTRY" 2>/dev/null || true
chmod 644 "$REGISTRY" 2>/dev/null || true

echo ""
echo "✓ ${USERNAME} 프로비저닝 완료"
echo "  - 데몬: launchctl print system/${LABEL} 로 상태 확인"
echo "  - 남은 수동 단계: 유저 클로드 구독 로그인 →  sudo -u ${OSUSER} -H claude  후 /login"
echo "  - 문서탭: https://${USERNAME}.peter-voice.site (데몬이 DNS 라우트 자동 등록)"
