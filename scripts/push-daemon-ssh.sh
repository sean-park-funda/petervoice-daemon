#!/bin/bash
# Push daemon updates to all customers via SSH (git pull)
# After this, AutoUpdater handles future updates automatically.
#
# Usage: ./push-daemon-ssh.sh [--dry-run]

set -euo pipefail

DRY_RUN="${1:-}"

# ─── Customer list ───────────────────────────────────────────────
# Format: "user@host:os:service_name:repo_path:ssh_opts"
#   os: mac | windows
#   service_name: launchd/nssm service name
#   repo_path: remote path to peter-voice repo root
#   ssh_opts: extra SSH options (use - for none)
# 🔒 고객 목록은 레포에 두지 않는다 — 이 스크립트는 데몬 레포로 **전 고객 맥미니에 배포**되므로
#    여기 적힌 타넷 IP·계정은 다른 고객 전원에게 간다 (2026-09-15 걷어냄).
#    Sean 머신의 ~/.claude-daemon/push-customers.txt (chmod 600, 한 줄에 한 항목, # 주석 허용) 에서 읽는다.
CUSTOMERS_FILE="${PUSH_CUSTOMERS_FILE:-$HOME/.claude-daemon/push-customers.txt}"
if [ ! -f "$CUSTOMERS_FILE" ]; then
    echo "❌ 고객 목록 파일이 없습니다: $CUSTOMERS_FILE" >&2
    echo "   형식: user@host:os:service_name:repo_path:ssh_opts  (ssh_opts 없으면 -)" >&2
    exit 1
fi
CUSTOMERS=()
while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    CUSTOMERS+=("$line")
done < "$CUSTOMERS_FILE"

echo "🚀 Pushing updates to ${#CUSTOMERS[@]} customer(s) via git pull"
echo ""

SUCCESS=0
FAIL=0

for entry in "${CUSTOMERS[@]}"; do
    [[ "$entry" =~ ^# ]] && continue

    IFS=':' read -r ssh_target os service_name repo_path ssh_opts <<< "$entry"
    [ "$ssh_opts" = "-" ] && ssh_opts=""

    echo "━━━ $ssh_target ($os) ━━━"

    if [ "$DRY_RUN" = "--dry-run" ]; then
        echo "   [dry-run] Would git pull at $repo_path and restart $service_name"
        continue
    fi

    # 1. Check connectivity
    if ! ssh -o ConnectTimeout=5 -o BatchMode=yes $ssh_opts "$ssh_target" "echo ok" &>/dev/null; then
        echo "   ❌ Cannot connect — skipping"
        FAIL=$((FAIL + 1))
        continue
    fi

    # 2. Git pull
    echo "   Pulling latest code..."
    PULL_OUT=$(ssh $ssh_opts "$ssh_target" "cd $repo_path && git pull --ff-only origin main 2>&1" || true)
    echo "   $PULL_OUT"

    if echo "$PULL_OUT" | grep -q "Already up to date"; then
        echo "   ✅ Already up to date"
        SUCCESS=$((SUCCESS + 1))
        continue
    fi

    # 3. Install deps if requirements changed
    if echo "$PULL_OUT" | grep -q "requirements.txt"; then
        echo "   Installing updated dependencies..."
        if [ "$os" = "mac" ]; then
            ssh $ssh_opts "$ssh_target" "cd $repo_path && .venv/bin/pip install -r requirements.txt -q 2>/dev/null" || true
        fi
    fi

    # 4. Restart service
    if [ "$os" = "mac" ]; then
        echo "   Restarting $service_name..."
        ssh $ssh_opts "$ssh_target" "launchctl stop $service_name 2>/dev/null || true"
    else
        echo "   Restarting $service_name..."
        ssh $ssh_opts "$ssh_target" "nssm restart $service_name 2>nul" 2>/dev/null || true
    fi

    # 5. Verify
    sleep 3
    if [ "$os" = "mac" ]; then
        LAST_LOG=$(ssh $ssh_opts "$ssh_target" "tail -3 ~/.claude-daemon/daemon.log 2>/dev/null" || echo "")
    else
        LAST_LOG=$(ssh $ssh_opts "$ssh_target" "type %USERPROFILE%\\.claude-daemon\\daemon.log 2>nul" 2>/dev/null | tail -3 || echo "")
    fi

    if echo "$LAST_LOG" | grep -q "updater\|starting\|Config loaded"; then
        echo "   ✅ Running"
        SUCCESS=$((SUCCESS + 1))
    else
        echo "   ⚠️  Started but could not verify"
        SUCCESS=$((SUCCESS + 1))
    fi
    echo ""
done

echo "━━━ Summary ━━━"
echo "   ✅ Success: $SUCCESS"
[ $FAIL -gt 0 ] && echo "   ❌ Failed:  $FAIL"
echo ""
echo "Future updates will auto-deploy via git pull (AutoUpdater)."
