#!/bin/bash
# 셀프호스트 인스턴스 비밀번호 로컬 복구
#
# 이메일 복구 대신 "머신 접근 = 소유권 증명" 원칙으로 로컬에서 재설정한다.
# 로컬 supabase-db 컨테이너의 users 테이블을 직접 갱신한다.
#
# Usage:
#   bash reset_password.sh [--dry-run] [username]
#     --dry-run  : DB를 변경하지 않고 해시/SQL만 출력
#     username   : 생략 시 users 테이블의 유일한 유저 자동 선택
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH

WEB_DIR="${SELFHOST_WEB_DIR:-$HOME/selfhost/web}"
DRY_RUN=false
USERNAME="${2:-}"
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=true; USERNAME="${2:-}"; elif [ -n "${1:-}" ]; then USERNAME="$1"; fi

command -v docker >/dev/null || { echo "❌ docker가 없습니다 (colima start 필요)"; exit 1; }
docker ps --format '{{.Names}}' | grep -q '^supabase-db$' || { echo "❌ supabase-db 컨테이너가 실행 중이 아닙니다"; exit 1; }
[ -d "$WEB_DIR/node_modules/bcryptjs" ] || { echo "❌ bcryptjs를 찾을 수 없습니다: $WEB_DIR (SELFHOST_WEB_DIR로 지정 가능)"; exit 1; }

# 주의: -i 금지 — stdin(비밀번호 입력)을 삼켜버림
psql_cmd() { docker exec supabase-db psql -U postgres -d postgres -t -A -c "$1" </dev/null; }

# 대상 유저 결정: 미지정 시 유일 유저 자동 선택
if [ -z "$USERNAME" ]; then
  COUNT=$(psql_cmd "SELECT count(*) FROM users;")
  if [ "$COUNT" = "1" ]; then
    USERNAME=$(psql_cmd "SELECT username FROM users LIMIT 1;")
  else
    echo "유저가 ${COUNT}명입니다. username을 지정하세요: reset_password.sh <username>"
    psql_cmd "SELECT username FROM users ORDER BY id;"
    exit 1
  fi
fi
EXISTS=$(psql_cmd "SELECT count(*) FROM users WHERE username='$USERNAME';")
[ "$EXISTS" = "1" ] || { echo "❌ 유저 없음: $USERNAME"; exit 1; }

echo "대상 유저: $USERNAME"
read -r -s -p "새 비밀번호 (8자 이상): " PW1; echo
read -r -s -p "새 비밀번호 확인: " PW2; echo
[ "$PW1" = "$PW2" ] || { echo "❌ 비밀번호가 일치하지 않습니다"; exit 1; }
[ "${#PW1}" -ge 8 ] || { echo "❌ 8자 이상이어야 합니다"; exit 1; }

HASH=$(PW="$PW1" node -e '
const bcrypt = require(process.argv[1] + "/node_modules/bcryptjs");
console.log(bcrypt.hashSync(process.env.PW, 10));
' "$WEB_DIR")

if $DRY_RUN; then
  echo "[dry-run] 해시 생성 OK: ${HASH:0:12}..."
  echo "[dry-run] 실행될 SQL: UPDATE users SET password='<hash>' WHERE username='$USERNAME';"
  exit 0
fi

psql_cmd "UPDATE users SET password='$HASH', reset_token=NULL, reset_token_expires=NULL WHERE username='$USERNAME';" >/dev/null
echo "✅ 비밀번호가 변경되었습니다. 웹에서 새 비밀번호로 로그인하세요."
echo "   (기존 로그인 세션은 만료 전까지 유효할 수 있습니다)"
