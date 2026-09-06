#!/usr/bin/env python3
"""프로젝트 그래프들의 문서(.md) 노드를 글로벌 그래프로 병합.

배경 (2026-09-06): 글로벌 그래프 graph.json 의 노드가 2026-04-21 이후 한 건도
갱신되지 않고 있었다. 30분 sync 는 cluster-only(재클러스터)만 돌려서 파일 mtime 만
새로 찍혔고, 노드를 실제로 합치는 장치는 어디에도 없었다. graphify CLI 에도
merge/global 명령이 없다. 그래서 이 스크립트가 그 자리를 맡는다.

- 대상: ~/Projects/*/graphify-out/graph.json + ~/.claude-daemon/projects/*/graphify-out/graph.json
- 문서(.md) 노드만 병합한다. 코드 AST 노드는 프로젝트 로컬에서만 의미가 있고 양이 압도적이다.
- 노드 id 는 "<project>::<원래id>" 로 유일화한다 (문서 노드 id 는 라벨 슬러그라 프로젝트 간 충돌한다).
- 커뮤니티는 프로젝트별로 오프셋을 줘 섞이지 않게 한다 (cluster-only 재실행 시 덮어써짐).
"""
import json, os, sys, time
from pathlib import Path

ROOTS = [Path.home() / "Projects", Path.home() / ".claude-daemon" / "projects"]
OUT = Path.home() / ".claude-daemon" / "global-graph" / "graph.json"


# ─────────────────────────────────────────────────────────────────────────────
# 레슨·공유지 위키 기계 인덱서 (LLM 없음, 비용 0)
#
# 30분 sync 의 `graphify update` 는 AST 전용이라 .md 노드를 만들지 않는다. 문서 노드는
# 유료 /graphify 스킬로만 생기고, 그걸 도는 evolution 하트비트의 스코프는 ~/Projects 뿐이다.
# 그래서 외부연동 노하우가 실제로 쌓이는 곳(infohub·login-manager·axl-* 의 docs/lessons)이
# 그래프에 한 건도 없었다 (2026-09-06 조사).
#
# 레슨과 위키는 형식이 고정이라 LLM 없이 노드화할 수 있다:
#   레슨 1편 = 노드 1개 + 소제목 노드 N개, 서비스 허브 노드로 프로젝트를 가로질러 연결
# 서비스 허브가 핵심이다 — "naver" 를 타고 infohub·ax-agent-platform·login-manager 의
# 네이버 경험으로 한 번에 넘어갈 수 있다.
# ─────────────────────────────────────────────────────────────────────────────

import re

WIKI_DIR = Path.home() / "Projects" / "peter-voice-web" / "content" / "commons"

# 파일명 첫 토큰이 서비스명인 경우가 대부분이다 (naver-blog-…, coupang-partners-…).
# 서비스가 아닌 첫 토큰은 여기서 걸러 허브를 만들지 않는다.
_NOT_A_SERVICE = {"browser", "web", "any", "shadow", "realestate", "sites", "temp", "route"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:80]


def _parse_md(path: Path):
    """제목 / 소제목 / frontmatter services 를 뽑는다."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    services = []
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            fm, body = text[3:end], text[end + 4:]
            m = re.search(r"^services:\s*(.+)$", fm, re.MULTILINE)
            if m:
                services = [s.strip() for s in m.group(1).split(",") if s.strip()]

    title = None
    headings = []
    for line in body.splitlines():
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
        elif line.startswith("## ") or line.startswith("### "):
            h = line.lstrip("#").strip()
            if h and len(h) < 120:
                headings.append(h)
    return title or path.stem, headings[:12], services


def _services_for(path: Path, declared) -> list:
    if declared:
        return declared
    first = path.stem.split("-")[0].lower()
    return [] if (first in _NOT_A_SERVICE or len(first) < 3) else [first]


def index_docs(nodes, links, seen, existing):
    """docs/lessons/*.md 와 공유지 위키를 노드로 만든다. 이미 추출된 파일은 건너뛴다."""
    hubs = {}
    added = 0

    targets = []
    for root in ROOTS:
        if not root.is_dir():
            continue
        for proj in sorted(root.iterdir()):
            d = proj / "docs" / "lessons"
            if d.is_dir():
                for f in sorted(d.glob("*.md")):
                    targets.append((proj.name, f"docs/lessons/{f.name}", f, "lesson"))
    if WIKI_DIR.is_dir():
        for f in sorted(WIKI_DIR.rglob("*.md")):
            if f.name.startswith("_"):
                continue
            targets.append(("commons", str(f.relative_to(WIKI_DIR)), f, "commons"))

    for project, rel, path, kind in targets:
        if (project, rel) in existing:
            continue  # 이미 문서 추출로 들어간 파일
        parsed = _parse_md(path)
        if not parsed:
            continue
        title, headings, declared = parsed

        nid = f"{project}::{kind}::{_slug(path.stem)}"
        if nid in seen:
            continue
        seen.add(nid)
        nodes.append({
            "id": nid, "label": title, "file_type": "document",
            "source_file": rel, "project": project,
            "norm_label": title, "indexed_by": "mechanical",
        })
        added += 1

        for i, h in enumerate(headings):
            hid = f"{nid}::h{i}"
            if hid in seen:
                continue
            seen.add(hid)
            nodes.append({
                "id": hid, "label": h, "file_type": "document",
                "source_file": rel, "project": project,
                "norm_label": h, "indexed_by": "mechanical",
            })
            links.append({"source": nid, "target": hid, "relation": "contains",
                          "confidence": "EXTRACTED", "weight": 1.0, "project": project})

        for svc in _services_for(path, declared):
            hub = f"service::{_slug(svc)}"
            if hub not in hubs:
                hubs[hub] = svc
                nodes.append({
                    "id": hub, "label": f"외부 서비스: {svc}", "file_type": "document",
                    "source_file": "(서비스 허브)", "project": "commons",
                    "norm_label": svc, "indexed_by": "mechanical",
                })
                seen.add(hub)
            links.append({"source": hub, "target": nid, "relation": "experience_with",
                          "confidence": "EXTRACTED", "weight": 1.0, "project": project})

    return added, len(hubs)


def project_graphs():
    for root in ROOTS:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            g = d / "graphify-out" / "graph.json"
            if g.is_file():
                yield d.name, g


def main():
    nodes, links = [], []
    seen = set()
    stats = []
    community_offset = 0

    for project, path in project_graphs():
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"  SKIP {project}: {e}", file=sys.stderr)
            continue

        keep = {}
        maxc = 0
        for n in data.get("nodes", []):
            src = str(n.get("source_file") or "")
            if not src.endswith(".md"):
                continue
            old = n.get("id")
            if old is None:
                continue
            new = f"{project}::{old}"
            if new in seen:
                continue
            seen.add(new)
            keep[old] = new
            m = dict(n)
            m["id"] = new
            m["project"] = project
            # 절대경로는 프로젝트 루트 기준 상대경로로 (출처가 한눈에 보이게)
            for r in ROOTS:
                p = str(r / project) + "/"
                if src.startswith(p):
                    m["source_file"] = src[len(p):]
                    break
            c = n.get("community")
            if isinstance(c, int):
                m["community"] = c + community_offset
                maxc = max(maxc, c)
            nodes.append(m)

        nl = 0
        for e in data.get("links", []):
            s, t = e.get("source"), e.get("target")
            if s in keep and t in keep:
                m = dict(e)
                m["source"], m["target"] = keep[s], keep[t]
                if "_src" in m: m["_src"] = keep.get(m["_src"], m["_src"])
                if "_tgt" in m: m["_tgt"] = keep.get(m["_tgt"], m["_tgt"])
                m["project"] = project
                links.append(m)
                nl += 1

        if keep:
            stats.append((project, len(keep), nl))
            community_offset += maxc + 1

    # 이미 문서 추출로 그래프에 들어간 파일은 기계 인덱서가 중복 생성하지 않도록 집합으로 넘긴다
    existing = {(n.get("project"), n.get("source_file")) for n in nodes}
    n_docs, n_hubs = index_docs(nodes, links, seen, existing)
    print(f"  기계 인덱싱: 문서 {n_docs}건, 서비스 허브 {n_hubs}개")

    out = {
        "directed": False,
        "multigraph": False,
        "graph": {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "builder": "build-global-graph.py"},
        "nodes": nodes,
        "links": links,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    json.dump(out, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, OUT)

    print(f"global graph: {len(nodes)} nodes / {len(links)} links / {len(stats)} projects")
    for p, n, l in sorted(stats, key=lambda x: -x[1]):
        print(f"  {n:6} {l:6}  {p}")


if __name__ == "__main__":
    main()
