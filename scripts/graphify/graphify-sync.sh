#!/bin/bash
# graphify-sync.sh — 30분마다 전체 프로젝트 그래프 + 글로벌 그래프 갱신
# 비용: 0원 (AST 파싱만, LLM 호출 없음)

set -euo pipefail

GRAPHIFY="/Users/sean/.local/bin/graphify"
# 프로젝트는 두 곳에 있다 — ~/Projects(개발 레포)와 ~/.claude-daemon/projects(에이전트 워크스페이스).
# 2026-09-06 까지 앞쪽만 훑어서, 외부연동 경험이 쌓이는 infohub·login-manager·axl-* 등
# 25개 워크스페이스가 그래프 갱신에서 통째로 빠져 있었다.
PROJECT_ROOTS=("/Users/sean/Projects" "/Users/sean/.claude-daemon/projects")
GLOBAL_GRAPH_DIR="/Users/sean/.claude-daemon/global-graph"
LOG="/Users/sean/.claude-daemon/graphify-sync.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

log "=== sync started ==="

updated=0

# 1. docs/가 있는 프로젝트마다 graphify update 실행
# 주의: for x in "${ARR[@]}"/*/ 는 접미 글로브가 배열의 마지막 원소에만 붙는다.
# (첫 루트가 통째로 누락된다 — 2026-09-06 이 패치를 처음 넣을 때 실제로 걸렸다)
ALL_PROJECT_DIRS=()
for root in "${PROJECT_ROOTS[@]}"; do
    for d in "$root"/*/; do
        [ -d "$d" ] && ALL_PROJECT_DIRS+=("$d")
    done
done

for project_dir in "${ALL_PROJECT_DIRS[@]}"; do
    [ -d "$project_dir/docs" ] || continue

    project_name=$(basename "$project_dir")

    # graphify-out/graph.json이 없으면 아직 초기 빌드 안 된 프로젝트 → 스킵
    if [ ! -f "$project_dir/graphify-out/graph.json" ]; then
        continue
    fi

    # graph.json보다 새로운 .md 가 있는지 확인.
    # docs/ 만 보면 conversation-logs/ 나 중첩 레포(peter-voice-daemon 등) 변경을 놓친다
    # (2026-08-08 evolution 지적). 프로젝트 루트를 훑되 잡음 디렉토리는 제외.
    graph_json="$project_dir/graphify-out/graph.json"
    newer_files=$(find "$project_dir" \
        \( -name node_modules -o -name .git -o -name graphify-out -o -name .next -o -name dist \) -prune -o \
        -name "*.md" -newer "$graph_json" -print -quit 2>/dev/null)
    # -print -quit 로 첫 매치에서 멈춘다. 예전 `| head -1` 은 find 에 SIGPIPE 를 보내고
    # pipefail 이 141 을 물려받아 set -e 가 스크립트를 통째로 중단시켰다 (2026-09-06).

    if [ -n "$newer_files" ]; then
        log "updating: $project_name"
        cd "$project_dir"
        # 성공 판정은 종료코드가 아니라 graph.json 갱신 여부로 한다.
        # 5,000노드 초과 프로젝트는 HTML 뷰 생성 단계에서 exit 1 이 나지만 graph.json 은
        # 정상 갱신된다. 종료코드만 보면 updated=0 이 되어 아래 글로벌 그래프 재빌드가
        # 통째로 건너뛰어진다 (2026-08-08 발견: 글로벌 그래프가 하루 넘게 정지).
        before=$(stat -f %m "$graph_json" 2>/dev/null || echo 0)
        # set -e 아래에서는 실패한 명령이 곧바로 스크립트를 끝낸다 — `rc=$?` 는 실행조차 안 된다.
        # 5,000노드 초과 프로젝트는 HTML viz 단계에서 반드시 non-zero 를 내므로, 그 프로젝트
        # 하나 때문에 이후 전 프로젝트와 글로벌 재빌드가 통째로 건너뛰어지고 있었다
        # (2026-09-06 발견: peter-voice 에서 매번 중단 → "sync done" 로그가 아예 안 찍힘).
        "$GRAPHIFY" update . >> "$LOG" 2>&1 && rc=0 || rc=$?
        after=$(stat -f %m "$graph_json" 2>/dev/null || echo 0)
        if [ "$after" -gt "$before" ]; then
            [ $rc -ne 0 ] && log "  done (viz skipped, rc=$rc): $project_name" || log "  done: $project_name"
            updated=$((updated + 1))
        else
            log "  FAILED (rc=$rc, graph.json unchanged): $project_name"
        fi
    fi
done

# 2. 프로젝트 그래프가 하나라도 갱신되었으면 글로벌 그래프도 재빌드
if [ $updated -gt 0 ]; then
    log "rebuilding global graph ($updated projects updated)"

    # 2-1. 프로젝트 문서 노드를 글로벌 그래프로 병합.
    # graphify CLI 에는 merge/global 명령이 없어서, 여기까지 cluster-only(재클러스터)만
    # 돌고 있었다 — 파일 mtime 만 새로 찍히고 노드는 2026-04-21 이후 한 건도 안 들어왔다.
    if python3 /Users/sean/.claude-daemon/scripts/build-global-graph.py >> "$LOG" 2>&1; then
        log "global graph merged"
    else
        log "global merge FAILED"
    fi

    # 2-2. 글로벌 그래프 리클러스터 (graphify-out/ 심볼릭 링크 경유)
    # 종료코드가 아니라 graph.json 갱신 여부로 판정한다 (5,000노드 초과 시 HTML viz 단계에서
    # ValueError 가 나지만 graph.json 은 정상 갱신된다 — 프로젝트 그래프와 같은 사정).
    if [ -f "$GLOBAL_GRAPH_DIR/graph.json" ]; then
        cd "$GLOBAL_GRAPH_DIR"
        gbefore=$(stat -f %m graph.json 2>/dev/null || echo 0)
        "$GRAPHIFY" cluster-only . >> "$LOG" 2>&1 || true
        gafter=$(stat -f %m graph.json 2>/dev/null || echo 0)
        if [ "$gafter" -ge "$gbefore" ] && python3 -c "import json,sys; json.load(open('graph.json'))" 2>/dev/null; then
            # cluster-only 출력이 graphify-out/에 떨어지므로 루트로 복사
            cp -f graphify-out/GRAPH_REPORT.md . 2>/dev/null || true
            cp -f graphify-out/graph.html . 2>/dev/null || true
            log "global graph report updated"
        else
            log "global cluster FAILED"
        fi
    fi
fi

# 3. wiki/index.md 재생성 (graphify 데이터 → 지식 인덱스)
python3 /Users/sean/.claude-daemon/scripts/build-wiki-index.py >> "$LOG" 2>&1

log "=== sync done (updated: $updated) ==="
