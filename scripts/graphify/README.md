# graphify 지식그래프 동기화 (Sean 맥미니 전용)

launchd `com.petervoice.graphify-sync` 가 30분마다 `graphify-sync.sh` 를 돌린다.
**고객 데몬은 이 스크립트를 실행하지 않는다** — 경로가 Sean 맥미니 기준으로 하드코딩돼 있고,
데몬 코드 어디에서도 참조하지 않는다. 여기 둔 이유는 버전관리·복구 경로 확보다.

## 배선
`~/.claude-daemon/scripts/` 의 세 파일은 이 디렉토리로 향하는 **심링크**다.
즉 레포에서 고치면 다음 실행부터 바로 반영된다.

```
~/.claude-daemon/scripts/graphify-sync.sh      -> <repo>/scripts/graphify/graphify-sync.sh
~/.claude-daemon/scripts/build-global-graph.py -> <repo>/scripts/graphify/build-global-graph.py
~/.claude-daemon/scripts/build-wiki-index.py   -> <repo>/scripts/graphify/build-wiki-index.py
```

새 머신에서 복원하려면 위 심링크를 만들고 launchd plist 를 등록하면 된다.

## 흐름
1. `graphify-sync.sh` — 두 루트(`~/Projects`, `~/.claude-daemon/projects`)의 프로젝트마다
   `.md` 가 graph.json 보다 새로우면 `graphify update`(AST 전용, 무료) 실행
2. `build-global-graph.py` — 프로젝트 그래프의 **문서 노드**를 글로벌 그래프로 병합 +
   `docs/lessons`·공유지 위키를 LLM 없이 기계 인덱싱(서비스 허브 노드로 프로젝트 간 연결)
3. `graphify cluster-only` — 재클러스터
4. `build-wiki-index.py` — 프로젝트별/글로벌 `wiki/index.md` 재생성 (매 턴 프롬프트에 주입되는 파일)

## 이 스크립트에서 나왔던 버그 — 전부 "조용히 일부만 하고 성공한 척"
전수 조사 기록: `peter-voice/docs/reports/2026-09-06-external-integration-knowledge-audit.md`

- **글로벌 그래프가 2026-04-21 이후 정지**: 노드를 합치는 장치가 아예 없었고(graphify CLI 에
  merge 명령이 없다) `cluster-only` 만 돌아 파일 mtime 만 새로 찍혔다 → `build-global-graph.py` 신설
- **`for x in "${ARR[@]}"/*/`**: 접미 글로브가 배열 **마지막 원소에만** 붙어 첫 루트가 통째로 누락
- **`find ... | head -1`**: find 에 SIGPIPE → `pipefail` 이 141 을 물려받아 `set -e` 가 스크립트 중단
- **`set -e` + `graphify update`**: 5,000노드 초과 프로젝트는 HTML viz 에서 반드시 non-zero 를 내는데,
  `set -e` 가 즉시 끝내버려 다음 줄 `rc=$?` 가 실행조차 안 됐다 → `peter-voice` 차례마다 전체 중단.
  로그에 "sync started" 는 있고 "sync done" 이 없는 구간이 그 흔적이다.

수정 시 주의: **성공 판정을 종료코드로 하지 말 것.** graph.json 갱신 여부/유효성으로 판정한다.
