#!/usr/bin/env python3
"""피터보이스 사용 검진(닥터) 1단계 — 자기 계정 데이터만으로 4축 초안을 만든다.

축: 1 못 푼 과제 · 2 기능 미사용 · 4 단일 vs 멀티 · 6 설정   (3 조사 방법론 · 5 반복 수작업 · 7 기억 은 2단계)
읽는 것: /api/projects, /api/branches, /api/bot/conversation(항목당 최근 50건), /api/tasks, /api/bp/status, /api/bp/list,
        /api/prompts?project=_common, /api/help/search — 전부 이 계정 범위. 대화 원문은 120자 발췌만 보고서에 적는다.
출력: 마크다운 초안(--out) + 요약 JSON 한 줄(stdout 마지막 줄).
"""
import argparse, json, os, re, sys, urllib.parse, urllib.request, datetime as dt
from pathlib import Path

# 결론부(메시지 끝 300자)에서만 본다 — 본문 중간의 설명("…안 됩니다 라고 답하기 전에")은 결론이 아니다
NEG = re.compile(r"(불가능합니다|할 수 없습니다|할 수 없었습니다|할 수가 없|못 찾았습니다|찾지 못했습니다|찾을 수 없었습니다|지원하지 않습니다|지원되지 않습니다|안 됩니다|되지 않습니다|직접 해 ?주(세요|셔야)|직접 (진행|하셔야|해 주셔야)|수동으로 (해|진행)|제가 할 수 없|처리할 수 없)")
OLD_MODEL = re.compile(r"(^claude-sonnet-5$|claude-sonnet-5-20\d{6}|haiku|^claude-opus-5$|claude-(opus|sonnet)-4)")
KEY_SKILLS = ["gmail", "google-calendar", "google-drive", "google-sheets", "google-docs", "slack", "notion-api", "agent-browser",
              "browser-handoff", "commons-lookup", "pv-api", "file-share", "local-publish", "graphify"]
SERVICE_SIGNALS = {  # 연결 신호(환경변수) → 사용 신호(도구 로그에 나타나는 문자열)
    "google": (["GOOGLE_ACCOUNTS", "GOOGLE_REFRESH_TOKEN"], ["gmail", "google-calendar", "calendar.py", "gdrive", "google-drive", "sheets", "google-docs"]),
    "slack": (["SLACK_BOT_TOKEN", "SLACK_WORKSPACES", "SLACK_ACCESS_TOKEN"], ["slack"]),
    "notion": (["NOTION_API_TOKEN", "NOTION_ACCESS_TOKEN"], ["notion"]),
}
ERRORS = []

def creds():
    cfg = Path.home() / ".claude-daemon" / "config.json"
    if cfg.exists():
        c = json.loads(cfg.read_text())
        return c.get("api_url", "https://www.peter-voice.site").rstrip("/"), c["api_key"]
    return os.environ.get("PV_API_URL", "https://www.peter-voice.site").rstrip("/"), os.environ["PV_API_KEY"]

def api(path, bearer=False):
    url = API_URL + path
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"} if bearer else {"X-Api-Key": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # 조회 실패는 0건이 아니다 — 따로 센다
        ERRORS.append(f"{path.split('?')[0]}: {e}")
        return None

def excerpt(t, n=120):
    t = re.sub(r"\s+", " ", t or "").strip()
    return (t[:n] + "…") if len(t) > n else t

def link(pid, bno=None):
    return f"/?project={pid}&branch={bno}" if bno else f"/?project={pid}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default=None, help="마크다운 초안 경로 (기본 docs/checkup/<오늘>.md)")
    ap.add_argument("--max-items", type=int, default=600)
    a = ap.parse_args()
    global API_URL, API_KEY
    API_URL, API_KEY = creds()
    today = dt.date.today().isoformat()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)
    out = Path(a.out or f"docs/checkup/{today}.md"); out.parent.mkdir(parents=True, exist_ok=True)

    pj = api("/api/projects?all=1") or {}
    projects = [p for p in pj.get("projects", []) if not p.get("hidden")]
    pmap = {p["id"]: p for p in projects}
    branches = (api("/api/branches?all_active=1") or {}).get("branches", [])
    branches = [b for b in branches if b.get("project_id") in pmap]
    tasks = (api("/api/tasks") or {}).get("tasks", [])
    active_tasks = [t for t in tasks if t.get("status") == "active"]
    bp_status = {s["bp_id"]: s for s in (api("/api/bp/status") or {}).get("statuses", [])}
    bpl = api("/api/bp/list") or {}
    bp_cards = bpl.get("cards") or bpl.get("items") or bpl.get("bps") or (bpl if isinstance(bpl, list) else [])
    common_len = len(((api("/api/prompts?project=_common") or {}).get("content") or ""))
    skills_dir = Path.home() / ".claude" / "skills"
    installed = sorted(d.name for d in skills_dir.iterdir() if d.is_dir() and not d.name.startswith(".")) if skills_dir.exists() else []

    # ── 대화 훑기 (프로젝트 + 활성 브랜치, 항목당 최근 50건) ──
    items = [("p", p["id"], p["id"], None, p.get("name") or p["id"]) for p in projects]
    items += [("b", f"branch:{b['id']}", b["project_id"], b.get("branch_number"), b.get("title") or f"#{b.get('branch_number')}") for b in branches]
    items = items[: a.max_items]
    stats = {}          # key → 집계
    neg_hits = []       # 축 1 후보
    aborts = []         # 중단·한도로 끝난 턴 (부정 결론이 아니라 시스템 종료 — 따로 센다)
    tool_text = []      # 도구 로그 전체 (축 2 사용 신호)
    user_msgs = []      # (when, pid, bno, title, text) — BP 규칙 (전달 요청·경로 질문·현황 질문)
    bot_msgs = []       # (when, pid, bno, title, text) — BP 규칙 (기억 없음 문구)
    msgs_total = 0
    for kind, key, pid, bno, title in items:
        data = api(f"/api/bot/conversation?project={urllib.parse.quote(key)}&limit=50", bearer=True)
        if data is None:
            continue
        msgs = list(reversed(data.get("messages", [])))  # 시간순
        msgs = [m for m in msgs if m.get("created_at") and dt.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")) >= since]
        if not msgs:
            continue
        st = {"kind": kind, "pid": pid, "bno": bno, "title": title, "user": 0, "bot": 0, "tool": 0, "skill": 0, "agent": 0, "relay_in": 0, "heartbeat": 0, "neg": 0, "aborted": 0, "capped": len(data.get("messages", [])) >= 50}
        last_user = None
        for m in msgs:
            t = m.get("text") or ""
            if m.get("type") == "user":
                st["user"] += 1
                if t.startswith("[relay from:"): st["relay_in"] += 1
                if t.startswith("[heartbeat]"): st["heartbeat"] += 1
                elif not t.startswith("[relay from:") and not t.startswith("[카드 #"): user_msgs.append((m["created_at"][:16].replace("T", " "), pid, bno, title, t[:400]))
                last_user = t
            elif m.get("type") == "bot":
                if t.startswith("🔧"):
                    st["tool"] += 1; tool_text.append(t[:300])
                    if t.startswith("🔧 Skill"): st["skill"] += 1
                    if t.startswith("🔧 Agent") or t.startswith("🔧 Task"): st["agent"] += 1
                    continue
                st["bot"] += 1
                bot_msgs.append((m["created_at"][:16].replace("T", " "), pid, bno, title, t[:600]))
                if "⏹ 작업이 중단되었습니다" in t or t.startswith("⏸") or t.startswith("(응답 없음)"):
                    st["aborted"] += 1
                    aborts.append({"when": m["created_at"][:16].replace("T", " "), "pid": pid, "bno": bno, "title": title, "kind": ("한도" if t.startswith("⏸") else "중단")})
                    continue
                last_para = t.strip().split("\n\n")[-1]
                caveat = ("확인하지 못한 점" in last_para) or last_para.lstrip().startswith("- **확인")
                if NEG.search(last_para[-300:]) and not caveat and last_user and not last_user.startswith("[heartbeat]") and not last_user.startswith("[relay from:"):
                    st["neg"] += 1
                    neg_hits.append({"when": m["created_at"][:16].replace("T", " "), "pid": pid, "bno": bno, "title": title,
                                     "ask": excerpt(last_user, 70), "said": excerpt(t[-300:], 110)})
        msgs_total += len(msgs)
        stats[key] = st

    # ── 축 1: 부정 결론 → 공통 검색 대조 (최근 12건) ──
    neg_hits.sort(key=lambda h: h["when"], reverse=True)
    for h in neg_hits[:12]:
        q = re.sub(r"[^\w가-힣 ]", " ", h["ask"])[:60].strip()
        res = api(f"/api/help/search?q={urllib.parse.quote(q)}&limit=3") or {}
        # 토큰 하나만 걸린 페이지는 후보로 치지 않는다 — 2개 이상 일치 또는 점수 6 이상
        strong = [r for r in (res.get("results") or []) if not r.get("weak") and (len(r.get("matched") or []) >= 2 or (r.get("score") or 0) >= 6)]
        h["answer"] = ({"id": strong[0]["id"], "title": strong[0]["title"][:60]} if strong else None)
        h["lookup_failed"] = bool(res.get("sources_unavailable"))
    answered = [h for h in neg_hits[:12] if h.get("answer")]

    # ── 축 2: 연결됐는데 안 쓴 서비스 / 설치됐는데 안 쓴 스킬 / 기능 ──
    tool_blob = "\n".join(tool_text).lower()
    svc_rows = []
    for svc, (envs, sigs) in SERVICE_SIGNALS.items():
        connected = any(os.environ.get(e) for e in envs) or any(k.startswith(e) for e in envs for k in os.environ)
        used = sum(tool_blob.count(s) for s in sigs)
        svc_rows.append((svc, connected, used))
    unused_skills = [s for s in installed if s.lower() not in tool_blob and f"skill: {s.lower()}" not in tool_blob]
    unused_key = [s for s in KEY_SKILLS if s in installed and s in unused_skills]
    docs_counts = {}
    for p in projects:
        d = Path(p["directory"]).expanduser() if p.get("directory") else Path.home() / ".claude-daemon" / "projects" / p["id"]
        dd = d / "docs"
        docs_counts[p["id"]] = sum(1 for f in dd.rglob("*.md") if f.is_file()) if dd.exists() else 0
    relay_total = sum(s["relay_in"] for s in stats.values())
    agent_total = sum(s["agent"] for s in stats.values())
    kanban_on = sum(1 for p in projects if p.get("kanban_enabled"))

    # ── 축 4: 단일 vs 멀티 ──
    root_msgs = {}
    for s in stats.values():
        r = root_msgs.setdefault(s["pid"], {"msgs": 0, "branches": 0, "agent": 0, "relay": 0, "capped": False})
        r["msgs"] += s["user"] + s["bot"]; r["agent"] += s["agent"]; r["relay"] += s["relay_in"]; r["capped"] |= s["capped"]
        if s["kind"] == "b": r["branches"] += 1
    solo = [(pid, r) for pid, r in root_msgs.items() if r["msgs"] >= 60 and r["branches"] == 0 and r["agent"] == 0 and r["relay"] == 0]
    solo.sort(key=lambda x: -x[1]["msgs"])

    # ── 축 6: 설정 ──
    model_flags = [(p["id"], p.get("model") or "", p.get("effort") or "") for p in projects if p.get("model") and OLD_MODEL.search(p["model"])]
    active_pids = sorted(root_msgs, key=lambda k: -root_msgs[k]["msgs"])[:5]
    no_effort = [pid for pid in active_pids if pid in pmap and not pmap[pid].get("effort")]
    applied = {k for k, v in bp_status.items() if v.get("status") == "applied"}
    bp_not = [c for c in bp_cards if isinstance(c, dict) and c.get("id") and c["id"] not in applied]

    # ── BP 처방: 카탈로그(detect)가 가리키는 신호를 수집 데이터에서 찾는다 (2026-10-07 Sean 결정 — BP = 검진 규칙) ──
    DEFAULT_DETECT = {  # 라이브 manifest 에 detect 가 없을 때(구버전) 폴백 — 내용은 manifest 와 같게 유지
        "agent-relay": ("forward_without_relay", "담당자 간 릴레이로 한 번에 넘기게 한다", "이 결과를 마케팅 담당자에게 전달해줘"),
        "past-conversation-recall": ("no_memory_phrases", "세션 리셋 시 이전 대화를 조회해 복구하는 규칙 한 줄", "아까 얘기한 거 이어서 해줘"),
        "delegate-to-experts": ("solo_projects", "주제별 담당자(브랜치)로 나누고 릴레이로 잇는다", "이 프로젝트를 영업 담당자와 회계 담당자로 나눠줘"),
        "vp-task-management": ("many_active_no_coordinator", "COO(부사장) 담당자를 만들어 전 프로젝트 현황을 맡긴다", "부사장 담당자를 만들어서 전 프로젝트 현황을 관리하게 해줘"),
        "temp-share-hub": ("many_published_pages", "고정 temp 허브의 하위 경로로 모은다", "공유 페이지는 temp 허브 아래에 모아줘"),
        "browser-handoff": ("login_wall_giveup", "원격 로그인 인계로 유저가 한 번 로그인하면 에이전트가 이어받는다", "로그인은 내가 할 테니 화면 넘겨줘"),
        "route-evidence-based": ("route_questions_without_tool", "route-search 스킬로 실시간 경로 데이터로 답한다", "양양 가는 길에 점심 먹을 데 찾아줘"),
        "memory-graphify": (None, "지식 그래프로 과거 대화·문서를 검색하게 한다", "지난번에 정한 가격 기준 다시 찾아줘"),
        "research-cross-validation": (None, "교차검증 규칙 한 줄 + 같은 주제 재조사로 전후 비교", "이 업체를 실사용 후기 기준으로 다시 검증해줘"),
    }
    cat = {}
    for c in bp_cards:
        if not isinstance(c, dict) or not c.get("id"): continue
        d = c.get("detect") or {}
        auto, presc, ask = DEFAULT_DETECT.get(c["id"], (None, "", ""))
        cat[c["id"]] = {"title": c.get("title", c["id"]), "auto": d.get("auto", auto), "prescription": d.get("prescription") or presc, "ask": d.get("ask") or ask, "signal": d.get("signal", "")}
    active_roots = [pid for pid, r in root_msgs.items() if r["msgs"] >= 6 and pid in pmap]
    coord = re.compile(r"(coo|vp|부사장|비서|secretary|manager|총괄)", re.I)
    def rule(key):
        """→ (count, [발췌…]) — 발췌는 근거 요건(최대 2개)"""
        if key == "solo_projects":
            return len(solo), [f"[{pid}]({link(pid)}) 30일 {r['msgs']}{'+' if r['capped'] else ''}턴 · 브랜치 0" for pid, r in solo[:2]]
        if key == "many_active_no_coordinator":
            has = any(coord.search(pid) or coord.search(pmap[pid].get("name") or "") for pid in active_roots)
            return (len(active_roots) if (len(active_roots) >= 6 and not has) else 0), [f"활성 담당자 {len(active_roots)}개, 조정 역할 프로젝트 없음"]
        if key == "login_wall_giveup":
            rx = re.compile(r"로그인.{0,24}(필요|직접|해 ?주|하셔야|할 수 없|진행할 수 없|막혀|인증)")
            hits = [h for h in neg_hits if rx.search(h["said"])]
            return len(hits), [f"{h['when']} [{h['title'][:18]}]({link(h['pid'], h['bno'])}) {h['said'][:90]}" for h in hits[:2]]
        if key == "no_memory_phrases":
            rx = re.compile(r"(이전 대화를 확인할 수 없|이전 대화 기록이 없|기억이 없|대화 기록이 없어|세션이 새로 시작|이전 세션의 내용은)")
            hits = [b for b in bot_msgs if rx.search(b[4])]
            return len(hits), [f"{b[0]} [{b[3][:18]}]({link(b[1], b[2])}) {excerpt(b[4], 90)}" for b in hits[:2]]
        if key == "forward_without_relay":
            rx = re.compile(r"(전달해 ?줘|전해 ?줘|공유해 ?줘|넘겨 ?줘)")
            hits = [u for u in user_msgs if rx.search(u[4])]
            return (len(hits) if relay_total == 0 and len(hits) >= 2 else 0), [f"{u[0]} [{u[3][:18]}]({link(u[1], u[2])}) {excerpt(u[4], 90)}" for u in hits[:2]]
        if key == "many_published_pages":
            n = sum(1 for t in tool_text if re.search(r"(files/upload|local-publish|file-share|publish\.py)", t))
            return (n if n >= 5 else 0), [f"업로드·퍼블리싱 도구 호출 {n}회 (30일)"]
        if key == "route_questions_without_tool":
            rx = re.compile(r"(맛집|휴게소|주유소|가는 길에|경로로|몇 시쯤 어디)")
            hits = [u for u in user_msgs if rx.search(u[4])]
            used = any("route" in t.lower() for t in tool_text)
            return (len(hits) if hits and not used else 0), [f"{u[0]} [{u[3][:18]}]({link(u[1], u[2])}) {excerpt(u[4], 90)}" for u in hits[:2]]
        return 0, []
    bp_findings = []
    for bid, c in cat.items():
        if not c["auto"]: continue
        n, ev = rule(c["auto"])
        if n > 0:
            bp_findings.append({"id": bid, "title": c["title"], "count": n, "evidence": ev, "prescription": c["prescription"], "ask": c["ask"], "applied": bid in applied})
    bp_findings.sort(key=lambda f: -f["count"])
    judged = [bid for bid, c in cat.items() if not c["auto"]]

    # ── 보고서 초안 ──
    L = [f"# 사용 검진 보고서 — {today} (최근 {a.days}일)", "",
         "_검진 스킬 1단계 자동 초안. 담당자가 판단을 덧붙인 뒤 유저에게는 요약 5줄과 결정 1~3개만 보고합니다. 대화 원문은 120자 발췌만 담았습니다._", "",
         "## 요약 (담당자가 채움)", "- (결정 후보 1~3개 — 각 항목의 [적용] 을 유저가 고르면 그때 실행)", ""]
    L += ["## 1. 못 푼 과제 — 답이 이미 있었을 수 있는 것",
          f"최근 {a.days}일 부정 결론(불가·못 찾음·지원 안 됨·직접 해주세요) {len(neg_hits)}건 중 최신 {min(12, len(neg_hits))}건을 공통 검색과 대조 → **답 후보 있음 {len(answered)}건**.", ""]
    if neg_hits[:12]:
        L += ["| 언제 | 어디서 | 요청(발췌) | 결론(발췌) | 공통 검색 후보 |", "|---|---|---|---|---|"]
        for h in neg_hits[:12]:
            ans = ("🔴 조회 실패" if h.get("lookup_failed") else (f"**{h['answer']['id']}** — {h['answer']['title']}" if h.get("answer") else "없음"))
            L.append(f"| {h['when']} | [{h['title'][:24]}]({link(h['pid'], h['bno'])}) | {h['ask']} | {h['said']} | {ans} |")
        L += ["", "처방: 답 후보가 있는 건은 그 페이지(`GET /api/commons/doc?id=` 또는 href)를 읽고 **같은 과제를 다시** 해 본다. 전후 결과를 이 보고서에 나란히 적는다(비교 적용). 담당자 판단: 후보가 정말 그 과제의 답인지 먼저 가른다 — 아니면 「해당 없음」으로 지운다.", ""]
    else:
        L += ["부정 결론이 없다. (대화가 적거나, 요청 자체가 보수적일 수 있다 — 2축을 본다)", ""]
    if aborts:
        aborts.sort(key=lambda x: x["when"], reverse=True)
        n_q = sum(1 for x in aborts if x["kind"] == "한도")
        L += [f"**중단·한도로 끝난 턴 {len(aborts)}건** (중단 {len(aborts) - n_q} · 한도 {n_q} — 부정 결론이 아니라 시스템 종료. 유저가 다시 보냈는지, 반복되는 프로젝트가 있는지 본다): " + ", ".join(f"[{x['title'][:18]}]({link(x['pid'], x['bno'])}) {x['when'][5:]} {x['kind']}" for x in aborts[:8]) + (" …" if len(aborts) > 8 else ""), ""]
    L += ["## 2. 기능 미사용", "", "### 연결됐는데 안 쓴 서비스", "| 서비스 | 연결 | 도구 로그 사용 횟수 |", "|---|---|---|"]
    for svc, conn, used in svc_rows:
        L.append(f"| {svc} | {'O' if conn else '-'} | {used}{'  ← **연결만 하고 0회**' if conn and used == 0 else ''} |")
    L += ["", f"### 설치됐는데 {a.days}일간 한 번도 안 부른 스킬 — {len(unused_skills)}/{len(installed)}개",
          ("핵심 스킬 중 미사용: " + ", ".join(f"`{s}`" for s in unused_key)) if unused_key else "핵심 스킬은 다 쓰고 있다.",
          "(전체 목록: " + ", ".join(f"`{s}`" for s in unused_skills[:40]) + (" …" if len(unused_skills) > 40 else "") + ")", "",
          "### 기능 사용량", f"- 문서탭(docs/) 문서(.md): " + ", ".join(f"{pid} {n}" for pid, n in sorted(docs_counts.items(), key=lambda x: -x[1])[:8]) + (f" … (0개 프로젝트 {sum(1 for n in docs_counts.values() if n == 0)}개)"),
          f"- 활성 하트비트 {len(active_tasks)}개 · 활성 브랜치 {len(branches)}개 · 릴레이 수신 {relay_total}건 · 서브에이전트 호출 {agent_total}건 · 칸반 켠 프로젝트 {kanban_on}개", "",
          "처방: 연결만 된 서비스는 유저의 실제 과제에 맞춘 예시 1개(예: \"어제 온 메일 중 답장 안 한 것 정리해줘\")를 보고서에 적고, 유저가 고르면 그 자리에서 해 본다. 미사용 스킬은 설명만 나열하지 말고 **이 유저 대화에서 쓸 자리가 있었던 것만** 고른다.", ""]
    L += ["## 4. 단일 vs 멀티 — 한 담당자가 다 한 묶음", ""]
    if solo:
        L += ["| 프로젝트 | 메시지(30일) | 브랜치 | 서브에이전트 | 릴레이 |", "|---|---|---|---|---|"]
        for pid, r in solo[:8]:
            L.append(f"| [{pid}]({link(pid)}) | {r['msgs']}{'+' if r['capped'] else ''} | 0 | 0 | 0 |")
        L += ["", "처방: 주제가 둘 이상 섞인 프로젝트는 **브랜치(담당자)로 나누고**, 긴 조사·제작은 서브에이전트 병렬로. BP `delegate-to-experts`(여러 담당자에게 나눠 시키기) · `vp-task-management`(부사장으로 작업 관리). 담당자 판단: 메시지가 많아도 주제가 하나면 해당 없음.", ""]
    else:
        L += ["해당 없음 — 대화량이 많은 프로젝트는 브랜치·서브에이전트·릴레이 중 하나 이상을 쓰고 있다.", ""]
    L += ["## 6. 설정", ""]
    L += [f"- 공통 프롬프트(`_common`) {common_len:,}자" + (" — **2,000자 미만: 반복해서 설명하는 규칙·배경을 여기에 쌓으면 매 턴 자동으로 들어간다** (BP `memory-graphify`·`past-conversation-recall` 참고)" if common_len < 2000 else " — 규칙이 쌓여 있다")]
    if model_flags:
        L.append("- 구형·소형 모델로 고정된 프로젝트: " + ", ".join(f"`{pid}`={m}{' (추론 강도 없음)' if not e else ''}" for pid, m, e in model_flags) + " → 주력 프로젝트는 **Opus 5.5 + 추론 강도 high** 권장 (프로젝트 설정 > 엔진 & 모델)")
    else:
        L.append("- 구형·소형 모델로 고정된 프로젝트 없음")
    if no_effort:
        L.append("- 대화량 상위 프로젝트 중 추론 강도 미지정: " + ", ".join(f"`{p}`" for p in no_effort))
    L.append(f"- 베스트 프랙티스 미적용 {len(bp_not)}/{len(bp_cards)}장: " + ", ".join(f"`{c['id']}`" for c in bp_not[:10]) + (" …" if len(bp_not) > 10 else "") + " (설정 > Best Practice, 또는 `GET /api/bp/doc?id=`)")
    L += ["## BP 처방 — 신호가 보인 베스트 프랙티스 (상한 3)", ""]
    if bp_findings:
        for i, f in enumerate(bp_findings[:3], 1):
            L += [f"**처방 {i}. {f['title']}**" + (" _(카드는 적용됨인데 신호가 남아 있음 — 쓰이지 않는다)_" if f["applied"] else ""),
                  f"- 신호 {f['count']}건: " + " · ".join(f["evidence"]),
                  f"- 이렇게: {f['prescription']}",
                  f"- 유저 예시 요청: 「{f['ask']}」 · 적용하려면 「처방 {i} 적용해줘」 (카드 Setup: `GET /api/bp/doc?id={f['id']}`)", ""]
        if len(bp_findings) > 3:
            L += [f"_상한 밖 후보 {len(bp_findings) - 3}개: " + ", ".join(f"{f['title']}({f['count']})" for f in bp_findings[3:]) + " — 다음 달에 다시 본다._", ""]
    else:
        L += ["자동 탐지 신호 없음.", ""]
    L += ["담당자 판단 규칙(자동 탐지 없음): " + ", ".join(cat[b]["title"] for b in judged if b in cat) + " — 1축 발췌와 조사형 대화를 열어 근거 2개 이상일 때만 처방에 더한다.", ""]
    L += ["", "## 부록 — 훑은 범위",
          f"- 프로젝트 {len(projects)}개 + 활성 브랜치 {len(branches)}개 중 {a.days}일 내 대화가 있는 {len(stats)}곳, 메시지 {msgs_total:,}건(항목당 최근 50건까지 — `+` 표시는 그 이상)",
          f"- 조회 실패 {len(ERRORS)}건" + (": " + "; ".join(ERRORS[:5]) if ERRORS else "") + " — 실패는 0건이 아니다",
          "- 읽은 것: 이 계정의 대화·프로젝트·브랜치·하트비트·BP 상태·공통 프롬프트 길이·설치 스킬·연결 서비스(환경변수 유무). 밖으로 보낸 것 없음.", ""]
    out.write_text("\n".join(L), encoding="utf-8")
    summary = {"out": str(out), "items_scanned": len(stats), "messages": msgs_total, "neg_hits": len(neg_hits), "neg_answered": len(answered), "aborted_turns": len(aborts),
               "unused_key_skills": unused_key, "services_connected_unused": [s for s, c, u in svc_rows if c and u == 0],
               "solo_projects": [p for p, _ in solo[:8]], "model_flags": model_flags, "common_prompt_len": common_len,
               "bp_not_applied": [c["id"] for c in bp_not], "bp_findings": [(f["id"], f["count"]) for f in bp_findings], "errors": len(ERRORS)}
    print(json.dumps(summary, ensure_ascii=False))

if __name__ == "__main__":
    main()
