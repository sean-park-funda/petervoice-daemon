#!/usr/bin/env bash
# 클라우드 호스트 자동배포 — 폴링형(3분 타이머) + 즉시 트리거 겸용
#
# 배경: 2026-07-27 뉴넥스 전 턴 실패의 근본 원인은 "코드는 고쳤는데 호스트에 안 당겼다" 였다.
# 웹 박스의 검증된 배포 구조(폴링 → ff-only → 헬스체크 → 롤백)를 데몬용으로 이식한다.
#
# 실행 위치는 **/usr/local/bin/pv-cloud-deploy.sh** (레포 밖 사본).
#   bash 는 스크립트를 실행하면서 읽으므로, 자기 자신을 pull 로 덮으면 실행이 깨진다.
#   → 사본을 돌리고, 성공한 뒤 마지막에 레포 버전을 사본으로 동기화한다.
#
# 설정: /etc/pv-cloud/deploy.env (선택)
#   PV_DEPLOY_BRANCH=main        # 전용 호스트는 stable 등으로 분리 가능
#   PV_DEPLOY_HEALTH_SEC=90      # 재시작 후 헬스체크 대기
#   PV_DEPLOY_WATCH_SEC=600      # 배포 후 턴 오류 관찰 창
set -uo pipefail

REPO=/home/ubuntu/peter-voice
LOG=/var/log/pv-cloud-deploy.log
LOCK=/run/lock/pv-cloud-deploy.lock   # /run 은 root 소유 — ubuntu 가 쓸 수 있는 곳이어야 한다
FAILED_SHA=/var/lib/pv-cloud/failed-sha
SELF_INSTALLED=/usr/local/bin/pv-cloud-deploy.sh

# deploy.env 는 **ubuntu 소유 600** 이어야 한다 (서비스가 User=ubuntu 로 돈다).
# root 소유면 읽지 못하는데, 조용히 넘어가면 브랜치·알림 설정이 통째로 무시된 채 배포된다.
if [ -f /etc/pv-cloud/deploy.env ]; then
  if [ -r /etc/pv-cloud/deploy.env ]; then
    . /etc/pv-cloud/deploy.env
  else
    log "❌ /etc/pv-cloud/deploy.env 를 읽을 수 없음 (소유권 확인: ubuntu:ubuntu 600)"
    exit 1
  fi
fi
BRANCH="${PV_DEPLOY_BRANCH:-main}"
HEALTH_SEC="${PV_DEPLOY_HEALTH_SEC:-90}"
WATCH_SEC="${PV_DEPLOY_WATCH_SEC:-600}"

log() { echo "$(date '+%F %T') $*" | sudo tee -a "$LOG" >/dev/null; }

# 실패 알림 — 조용한 실패를 만들지 않는다.
# PV_API_KEY 는 /etc/pv-cloud/deploy.env(600, ubuntu 소유)에 둔다. 없으면 로그만 남는다.
notify() {
  local text="[$(hostname -s)] $*"
  log "notify: $text"
  [ -z "${PV_API_KEY:-}" ] && return 0
  local api="${PV_API_URL:-https://www.peter-voice.site}"
  local to="${PV_NOTIFY_PROJECT:-petervoice-cloud}"
  curl -s -o /dev/null --max-time 10 -X POST "$api/api/relay/message" \
    -H "X-Api-Key: $PV_API_KEY" -H "Content-Type: application/json" \
    -d "$(python3 -c "import json,sys;print(json.dumps({'to_project':sys.argv[1],'from_project':sys.argv[1],'text':sys.argv[2]}))" "$to" "$text")" \
    || true
}

# 번들 스킬을 컨테이너가 읽는 공용 폴더로 반영한다.
# 이 단계가 없어서 레포에 스킬을 추가해도 유저 환경에 영원히 안 들어갔다
# (2026-07-28 실측: 공용 8/9개, 전용 **0개** — 전용은 gmail·slack 조차 없었다).
# 데몬은 이 폴더를 읽어 유저 홈에 심링크만 걸고, 비어 있으면 조용히 return 한다.
sync_skills() {
  local src="$REPO/skills"
  local dst="${PV_SHARED_SKILLS:-/srv/pv/shared/skills}"
  [ -d "$src" ] || return 0
  sudo mkdir -p "$dst"
  local n=0
  for s in "$src"/*/; do
    [ -d "$s" ] || continue
    local name; name=$(basename "$s")
    # 번들 스킬 **이름 단위로만** 덮어쓴다. 유저가 같은 폴더에 설치한 다른 스킬은 건드리지 않는다
    if command -v rsync >/dev/null 2>&1; then
      sudo rsync -a --delete "$s" "$dst/$name/" || continue
    else
      sudo rm -rf "$dst/$name" && sudo cp -a "${s%/}" "$dst/$name" || continue
    fi
    n=$((n + 1))
  done
  log "번들 스킬 동기화: ${n}개 → $dst"
}

# 매뉴얼 동기화 — 시스템 가이드가 "매뉴얼을 읽으라"고 하는데 클라우드 유저 홈엔 레포가 없다
# (2026-08-25 jenn: 가이드 경로 ~/peter-voice/docs/manual 이 존재하지 않아 에이전트가 환경을 추측함).
# 유저 계정이 읽을 수 있게 root:root 755/644 로 둔다.
sync_manual() {
  local src="$REPO/docs/manual"
  local dst="${PV_SHARED_MANUAL:-/srv/pv/shared/manual}"
  [ -d "$src" ] || return 0
  sudo mkdir -p "$dst"
  if command -v rsync >/dev/null 2>&1; then
    sudo rsync -a --delete --chmod=D755,F644 "$src/" "$dst/" || return 0
  else
    sudo rm -rf "$dst" && sudo cp -a "$src" "$dst" && sudo chmod -R a+rX "$dst" || return 0
  fi
  log "매뉴얼 동기화: $(ls "$dst" | wc -l)개 → $dst"
}

# 일시정지 — 락도 잡기 전에 조용히 빠진다
[ -f "$REPO/.deploy-pause" ] && exit 0

# 락을 못 잡으면 조용히 넘어가지 말고 남긴다 — 중복 실행은 배포를 반쯤 겹치게 만든다
if ! exec 9>"$LOCK"; then
  log "❌ 락 파일 생성 실패: $LOCK"
  exit 1
fi
flock -n 9 || exit 0   # 이미 돌고 있으면 그냥 종료 (타이머 + 즉시 트리거 중복 방지)

sudo mkdir -p "$(dirname "$FAILED_SHA")"
cd "$REPO" || { log "repo 없음: $REPO"; exit 1; }

# 명시적 refspec — `git fetch origin <브랜치>` 만으로는 FETCH_HEAD 만 갱신되고
# 원격추적 ref(origin/<브랜치>)가 안 생겨서 rev-parse 가 실패한다(main 외 브랜치에서 발생).
git fetch --quiet origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" \
  || { log "❌ fetch 실패 (branch=$BRANCH)"; exit 1; }
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null) \
  || { log "❌ origin/$BRANCH 확인 불가"; exit 1; }
[ "$LOCAL" = "$REMOTE" ] && exit 0   # 새 커밋 없음 — 가장 흔한 경로, 조용히 종료

# 같은 커밋으로 이미 실패했으면 재시도하지 않는다 (3분마다 무한 재시도·알림 스팸 방지).
# 새 커밋을 푸시하면 REMOTE 가 달라져 자동 해제된다.
if [ -f "$FAILED_SHA" ] && [ "$(cat "$FAILED_SHA")" = "$REMOTE" ]; then exit 0; fi

# 호스트에서 직접 고친 흔적이 있으면 덮지 않는다 (사람이 급히 손댄 것을 날리지 않기 위해)
if [ -n "$(git status --porcelain -uno)" ]; then
  log "⚠️ 미커밋 변경 감지 — 배포 중단. 수동 확인 필요: $(git status --porcelain -uno | head -3 | tr '\n' ' ')"
  exit 0
fi

log "배포 시작 ${LOCAL:0:7} → ${REMOTE:0:7} (branch=$BRANCH)"
git pull --ff-only --quiet origin "$BRANCH" || { log "❌ ff-only pull 실패"; exit 1; }

# 의존성 — 지금은 표준 라이브러리만 쓰지만, 생기면 자동 반영되게 둔다
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q '^requirements.txt$'; then
  log "requirements.txt 변경 → pip install"
  python3 -m pip install -q -r requirements.txt || log "⚠️ pip install 실패(계속 진행)"
fi

sync_skills

sync_manual

# 배포 전 기준선 — 헬스체크에서 로스터 수가 줄지 않았는지 비교한다
BASE_ROSTER=$(sudo journalctl -u pv-cloud --no-pager -n 200 \
  | grep -o 'roster synced: [0-9]*' | tail -1 | grep -o '[0-9]*' || echo 0)
RESTART_AT=$(date '+%Y-%m-%d %H:%M:%S')

sudo systemctl restart pv-cloud || { log "❌ restart 실패"; }

# 포탈(home-portal.js) 변경 시 pv-portal 도 재시작 — 2026-07-30 브라우저 인계 배포 때
# pv-cloud 만 재시작해 포탈이 구코드로 남는 갭이 실제로 발생했다
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q '^scripts/home-portal.js$'; then
  log "home-portal.js 변경 → pv-portal 재시작"
  sudo systemctl restart pv-portal || log "⚠️ pv-portal 재시작 실패"
fi

# ── 헬스체크 ──
# 데몬은 HTTP 를 제공하지 않으므로 "살아있다"만으로는 부족하다.
# 2026-07-27 사고 때 데몬은 active 였고 로스터도 정상이었는데 모든 턴이 죽었다.
HEALTHY=false
for _ in $(seq 1 $((HEALTH_SEC / 3))); do
  sleep 3
  systemctl is-active --quiet pv-cloud || continue
  ROSTER=$(sudo journalctl -u pv-cloud --no-pager --since "$RESTART_AT" \
    | grep -o 'roster synced: [0-9]*' | tail -1 | grep -o '[0-9]*' || echo "")
  [ -z "$ROSTER" ] && continue
  # 로스터가 줄었으면 호스트 배정이 깨진 것 — "synced: 0" 도 정상 로그로 찍히므로 수를 비교한다
  if [ "$ROSTER" -lt "$BASE_ROSTER" ]; then
    log "❌ 로스터 축소 $BASE_ROSTER → $ROSTER"
    break
  fi
  HEALTHY=true
  break
done

if ! $HEALTHY; then
  log "❌ 헬스체크 실패 — ${LOCAL:0:7} 로 롤백"
  echo "$REMOTE" | sudo tee "$FAILED_SHA" >/dev/null
  git reset --hard --quiet "$LOCAL"
  sudo systemctl restart pv-cloud
  notify "❌ 배포 실패·롤백: ${REMOTE:0:7} → ${LOCAL:0:7} (헬스체크)"
  exit 1
fi

log "✅ 배포 완료 -> ${REMOTE:0:7}"

# 성공했으니 이 스크립트 사본을 갱신한다 (다음 실행부터 새 버전)
if [ -f "$REPO/scripts/ops/pv-cloud-deploy.sh" ]; then
  sudo install -m 755 "$REPO/scripts/ops/pv-cloud-deploy.sh" "$SELF_INSTALLED"
fi

# ── 관찰 창 ──
# 구조적 헬스체크는 "떠 있지만 턴이 전부 죽는" 상태를 못 잡는다(2026-07-27 실제 사고).
# 배포 후 일정 시간 턴 오류를 세서, 연속 실패가 보이면 알린다. 자동 롤백은 하지 않는다
# — 고객 요청 자체가 원인인 오류를 배포 탓으로 되돌리면 더 나쁘다.
(
  # ⚠️ 락(fd 9)을 물려받으면 관찰 창이 끝날 때까지(기본 10분) 다음 배포가 전부 막힌다.
  # 백그라운드로 떨어지기 전에 반드시 닫는다.
  exec 9>&-
  sleep "$WATCH_SEC"
  ERRS=$(sudo journalctl -u pv-cloud --no-pager --since "$RESTART_AT" \
    | grep -cE 'ERROR (provision failed|claude exit|process error)' || true)
  if [ "${ERRS:-0}" -ge 3 ]; then
    log "⚠️ 배포 후 ${WATCH_SEC}초간 턴 오류 ${ERRS}건 — ${REMOTE:0:7}"
    notify "⚠️ 배포 후 턴 오류 ${ERRS}건 (${REMOTE:0:7}). 확인 필요"
  fi
) >/dev/null 2>&1 &

exit 0
