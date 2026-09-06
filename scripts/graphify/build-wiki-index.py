#!/usr/bin/env python3
"""Build wiki/index.md from graphify-out/ data for each project.

Runs after graphify-sync.sh. No LLM calls — pure data extraction.
Generates a ~500-1000 token knowledge index that gets injected into agent prompts.
"""

from __future__ import annotations  # 3.9 호환 (launchd가 /usr/bin/python3 3.9 사용)

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path(os.path.expanduser("~/Projects"))
GLOBAL_GRAPH_DIR = Path(os.path.expanduser("~/.claude-daemon/global-graph"))


def load_graph(graph_path: Path) -> dict | None:
    if not graph_path.exists():
        return None
    try:
        return json.load(open(graph_path))
    except (json.JSONDecodeError, IOError):
        return None


RETIRED_STATUSES = {"deprecated", "superseded", "archived", "retired", "폐기", "폐기됨", "대체됨"}
# lifecycle: 은 열거형 전용 필드다 (status: 는 자유 서술 진행상황 트래킹 — 건드리지 않음).
# 열거형 밖 값은 무시하되 경고를 남긴다 — 규약은 강제되지 않으면 자유 서술로 흘러간다
# (2026-08-12 evolution 실측: status 값 15건 중 열거형 1건, 게이트 매칭 0건).
VALID_LIFECYCLE = RETIRED_STATUSES | {"active", "draft", "현역", "작성중"}

# 폐기 표기는 그래프가 아니라 "원본 파일"에서 직접 읽는다.
# 이유(2026-08-11 실측): graphify 는 문서 frontmatter 를 노드 속성으로 옮기는 경로가 없고
# (node["status"] 는 7,736개 전부 null — status: 를 선언한 파일의 노드조차 null),
# cache.py 가 .md 해시에서 frontmatter 를 의도적으로 제외해 메타데이터만 고쳐선
# 재추출조차 일어나지 않는다. 따라서 노드 속성 기반 판정은 구조적으로 항상 False 다.
_retired_cache: dict = {}

_MARKER_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(lifecycle|status)(?:\*\*)?\s*:\s*(.+)$", re.I)


def _marker_value(raw: str) -> str:
    """'deprecated (2026-08-01, X로 대체)' → 'deprecated'. 자유 서술이면 매칭 안 됨."""
    token = re.split(r"[\s,(\[#—|/]", raw.strip(), maxsplit=1)[0]
    return token.strip("*`\"'“”").lower()


def file_is_retired(path) -> bool:
    """문서 머리 20줄에서 lifecycle:/status: 표기를 찾아 폐기 계열인지 판정.
    lifecycle: 이 있으면 그것만 신뢰한다 (status: 는 기존 관행상 진행상황 자유서술)."""
    key = str(path)
    if key in _retired_cache:
        return _retired_cache[key]
    verdict = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            status_val = None
            for i, line in enumerate(f):
                if i >= 20:
                    break
                m = _MARKER_RE.match(line)
                if not m:
                    continue
                field, val = m.group(1).lower(), _marker_value(m.group(2))
                if field == "lifecycle":
                    if val not in VALID_LIFECYCLE:
                        print(f"  ⚠️ lifecycle 열거형 위반: {path} → {m.group(2).strip()[:50]!r} (무시됨)")
                    status_val = val
                    break  # lifecycle 우선 — 찾으면 즉시 확정
                if status_val is None:
                    status_val = val
            verdict = status_val in RETIRED_STATUSES
    except OSError:
        verdict = False
    _retired_cache[key] = verdict
    return verdict


def is_retired(node: dict, roots) -> bool:
    """폐기 문서에서 나온 노드는 인덱스에서 제외한다.
    roots: 노드의 source_file(프로젝트 상대경로)을 붙여볼 루트 목록.
    여러 루트에 같은 경로가 있으면 '전부 폐기'일 때만 폐기로 본다."""
    src = node.get("source_file", "")
    if not src:
        return False
    found = [Path(r) / src for r in roots if (Path(r) / src).exists()]
    return bool(found) and all(file_is_retired(p) for p in found)


def extract_knowledge_index(graph: dict, project_name: str, roots=()) -> str:
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    hyperedges = graph.get("hyperedges", [])
    node_map = {n["id"]: n for n in nodes}

    degree = Counter()
    for l in links:
        degree[l.get("source", "")] += 1
        degree[l.get("target", "")] += 1

    doc_nodes = []
    seen_sources = set()
    for nid, deg in degree.most_common(200):
        n = node_map.get(nid, {})
        if n.get("file_type") not in ("document",):
            continue
        if is_retired(n, roots):
            continue
        src = n.get("source_file", "")
        if not src.startswith("docs/"):
            continue
        label = n.get("label", "?")
        if len(label) > 80:
            continue
        if src in seen_sources and deg < 8:
            continue
        seen_sources.add(src)
        doc_nodes.append((n, deg))
        if len(doc_nodes) >= 15:
            break

    communities = Counter(n.get("community", -1) for n in nodes)
    doc_communities: dict[int, list] = {}
    for n in nodes:
        if n.get("file_type") != "document":
            continue
        if is_retired(n, roots):
            continue
        src = n.get("source_file", "")
        if not src.startswith("docs/"):
            continue
        c = n.get("community", -1)
        if c not in doc_communities:
            doc_communities[c] = []
        if len(doc_communities[c]) < 4:
            label = n.get("label", "?")
            if len(label) <= 60:
                doc_communities[c].append(label)

    lines = [
        f"# {project_name} 지식 인덱스",
        f"_자동 생성: {datetime.now().strftime('%Y-%m-%d %H:%M')} | "
        f"{len(nodes)} 노드, {len(links)} 연결_",
        "",
    ]

    if doc_nodes:
        lines.append("## 핵심 개념 (연결 많은 순)")
        for n, deg in doc_nodes:
            label = n.get("label", "?")
            src = n.get("source_file", "")
            lines.append(f"- **{label}** ({deg}) → `{src}`")
        lines.append("")

    if hyperedges:
        lines.append("## 핵심 패턴")
        for he in hyperedges[:10]:
            label = he.get("label", "?")
            lines.append(f"- {label}")
        lines.append("")

    if doc_communities:
        lines.append("## 지식 영역")
        sorted_comms = sorted(doc_communities.items(), key=lambda x: len(x[1]), reverse=True)
        for comm_id, labels in sorted_comms[:8]:
            if not labels:
                continue
            lines.append(f"- {', '.join(labels[:3])}")
        lines.append("")

    lines.append("_관련 개념이 보이면 해당 파일을 Read. 더 깊이: `graphify query \"keywords\" --budget 800`_")
    lines.append("")

    return "\n".join(lines)


def extract_global_index(graph: dict, roots=()) -> str:
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    node_map = {n["id"]: n for n in nodes}

    degree = Counter()
    for l in links:
        degree[l.get("source", "")] += 1
        degree[l.get("target", "")] += 1

    project_nodes: dict[str, list] = {}
    for nid, deg in degree.most_common(200):
        n = node_map.get(nid, {})
        if n.get("file_type") == "code":
            continue
        if is_retired(n, roots):
            continue
        # 병합기(build-global-graph.py)가 노드에 project 를 붙인다. 그게 정답이다.
        # 없을 때만 경로 첫 세그먼트로 추정한다 (구 그래프 호환).
        project = n.get("project")
        if not project:
            src = n.get("source_file", "")
            for part in src.split("/"):
                if part and part not in ("Users", "sean", "Projects", ".claude-daemon", "projects", "docs"):
                    project = part
                    break
            else:
                project = "unknown"
        if project not in project_nodes:
            project_nodes[project] = []
        if len(project_nodes[project]) < 3:
            project_nodes[project].append((n.get("label", "?"), deg))

    lines = [
        "# 글로벌 지식 인덱스 (전 프로젝트)",
        f"_자동 생성: {datetime.now().strftime('%Y-%m-%d %H:%M')} | "
        f"{len(nodes)} 노드, {len(links)} 연결_",
        "",
        "## 프로젝트별 핵심 지식",
    ]

    for proj, items in sorted(project_nodes.items()):
        if not items:
            continue
        concepts = ", ".join(f"{label}({deg})" for label, deg in items)
        lines.append(f"- **{proj}**: {concepts}")

    lines.append("")
    lines.append("## 사용법")
    lines.append("다른 프로젝트 지식이 필요하면 글로벌 그래프 쿼리:")
    lines.append("`graphify query \"keywords\" --budget 800 --graph ~/.claude-daemon/global-graph/graph.json`")
    lines.append("")

    return "\n".join(lines)


def build_project_index(project_dir: Path) -> bool:
    graph_path = project_dir / "graphify-out" / "graph.json"
    graph = load_graph(graph_path)
    if not graph:
        return False

    wiki_dir = project_dir / "wiki"
    wiki_dir.mkdir(exist_ok=True)

    index_path = wiki_dir / "index.md"
    project_name = project_dir.name

    content = extract_knowledge_index(graph, project_name, roots=[project_dir])

    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    if existing.split("\n", 2)[2:] == content.split("\n", 2)[2:]:
        return False

    index_path.write_text(content, encoding="utf-8")
    return True


def build_global_index() -> bool:
    graph_path = GLOBAL_GRAPH_DIR / "graph.json"
    graph = load_graph(graph_path)
    if not graph:
        return False

    wiki_dir = GLOBAL_GRAPH_DIR / "wiki"
    wiki_dir.mkdir(exist_ok=True)

    # 글로벌 그래프의 source_file 은 프로젝트 상대경로라 소속 프로젝트가 없다 →
    # 모든 프로젝트 루트를 붙여보고, 존재하는 사본이 전부 폐기일 때만 제외한다.
    roots = []
    for base in (PROJECTS_DIR, Path(os.path.expanduser("~/.claude-daemon/projects"))):
        if base.exists():
            roots += [p for p in base.iterdir() if p.is_dir()]
    content = extract_global_index(graph, roots=roots)
    index_path = wiki_dir / "index.md"

    # 본문이 같으면 쓰지 않는다 (build_project_index 와 동일한 가드).
    # 2행의 "_자동 생성: ..." 타임스탬프만 바뀌어도 파일이 바뀌면,
    # 이 파일은 매 턴 시스템 프롬프트에 주입되므로 프롬프트 캐시가 통째로 깨진다.
    # 실측(2026-07-30): 프롬프트 무변경 턴 cache_creation 443토큰 / $0.026,
    #                   타임스탬프만 바뀐 턴 cache_creation 54.5K토큰 / $0.334.
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    if existing.split("\n", 2)[2:] == content.split("\n", 2)[2:]:
        return False

    index_path.write_text(content, encoding="utf-8")
    return True


def main():
    updated = 0

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        if not (project_dir / "graphify-out" / "graph.json").exists():
            continue
        if build_project_index(project_dir):
            print(f"  updated: {project_dir.name}/wiki/index.md")
            updated += 1

    if build_global_index():
        print("  updated: global-graph/wiki/index.md")
        updated += 1

    print(f"wiki index: {updated} updated")


if __name__ == "__main__":
    main()
