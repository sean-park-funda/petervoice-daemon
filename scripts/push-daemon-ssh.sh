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
CUSTOMERS=(
    "a111@192.168.100.124:mac:com.petervoice.claude-daemon:/Users/a111/Projects/peter-voice:-o IdentitiesOnly=yes -i ~/.ssh/id_ed25519_migration"
    "willy@100.125.150.24:mac:com.petervoice.daemon:~/peter-voice:-"
    "jennc@100.119.200.43:windows:ClaudeDaemon:C:/PeterVoice/peter-voice:-"
)

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
