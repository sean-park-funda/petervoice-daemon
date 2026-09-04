#!/bin/bash
# pv-mount-homes.sh — 부팅 시 PV-<name> APFS 볼륨을 /Users/pv-<name> 홈으로 마운트.
# /etc/fstab 대체: 일부 머신에서 보안 에이전트가 fstab 쓰기를 가로채 hang 하므로
# (2026-09-04 Sean 맥 실사고) LaunchDaemon(com.petervoice.mount-homes)이 이 스크립트를 실행한다.
export PATH=/usr/sbin:/sbin:/usr/bin:/bin

for vol in $(diskutil apfs list | grep -o 'PV-[A-Za-z0-9_-]*' | sort -u); do
  name="${vol#PV-}"
  mp="/Users/pv-${name}"
  mount | grep -q " on ${mp} " && continue
  mkdir -p "$mp"
  diskutil mount -mountPoint "$mp" "$vol" || echo "mount failed: $vol"
done
