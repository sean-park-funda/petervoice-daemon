#!/bin/bash
# pv-mount-homes.sh — 부팅 시 PV-<name> APFS 볼륨을 /Users/pv-<name> 홈으로 마운트.
# /etc/fstab 대체: 일부 머신에서 보안 에이전트가 fstab 쓰기를 가로채 hang 하므로
# (2026-09-04 Sean 맥 실사고) LaunchDaemon(com.petervoice.mount-homes)이 이 스크립트를 실행한다.
#
# 2026-10-04 박태준 맥 재부팅 실사고: macOS 가 볼륨을 먼저 /Volumes/PV-<name> 에 자동 마운트하자
# 제자리 마운트가 실패하고도 조용히 끝났다 → 테넌트 데몬이 빈 홈에서 exit 1 반복, 사이트 502.
# 그래서: 다른 곳에 붙어 있으면 떼어 내고 다시 붙인다 / 볼륨이 늦게 나타날 수 있어 부팅 직후 약 3분간 반복한다 /
# 제자리에 붙였으면 그 테넌트의 데몬·사이트를 재기동한다(빈 홈에서 먼저 떠 있던 프로세스 정리).
export PATH=/usr/sbin:/sbin:/usr/bin:/bin

log() { logger -t pv-mount-homes "$*"; echo "$*"; }

fix_mounts() {
  for vol in $(diskutil apfs list | grep -o 'PV-[A-Za-z0-9_-]*' | sort -u); do
    name="${vol#PV-}"
    mp="/Users/pv-${name}"
    mount | grep -q " on ${mp} " && continue
    if diskutil info "$vol" 2>/dev/null | grep -qE '^ *Mounted: *Yes'; then
      cur=$(diskutil info "$vol" | awk -F': *' '/^ *Mount Point:/{print $2}')
      log "$vol is mounted at $cur — moving to $mp"
      diskutil unmount "$vol" || { log "unmount failed: $vol"; continue; }
    fi
    mkdir -p "$mp"
    if diskutil mount -mountPoint "$mp" "$vol"; then
      log "$vol mounted at $mp"
      for job in $(launchctl list | awk '{print $3}' | grep -E "^com\.petervoice\.(daemon|site)-${name}(-|$)"); do
        launchctl kickstart -k "system/$job" && log "restarted $job"
      done
    else
      log "mount failed: $vol"
    fi
  done
}

for _ in $(seq 1 "${PV_MOUNT_TRIES:-18}"); do
  fix_mounts
  sleep 10
done
