#!/bin/bash
# pv-sync-scripts.sh — 공유 체크아웃의 프로비저닝 스크립트를 root 전용 경로로 동기화.
# sudoers NOPASSWD 대상: 에이전트가 스크립트 수정 후 스스로 root 복사본을 갱신할 수 있게 한다.
# (이 스크립트를 NOPASSWD 로 허용하는 것은 사실상 관리 에이전트에게 root 위임 — 운영자 결정 사항)
set -euo pipefail
export PATH=/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin

SRC="/Users/Shared/petervoice/peter-voice-daemon/scripts"
DST="/usr/local/lib/petervoice"
mkdir -p "$DST"
for f in provision-macuser.sh pv-mount-homes.sh pv-sync-scripts.sh; do
  [ -f "$SRC/$f" ] && install -m 755 -o root -g wheel "$SRC/$f" "$DST/$f"
done
# 마이그레이션 등 일회성 헬퍼 (있을 때만)
for f in /Users/Shared/petervoice/*.sh; do
  [ -f "$f" ] && install -m 755 -o root -g wheel "$f" "$DST/$(basename "$f")"
done
echo "synced: $(ls "$DST")"
