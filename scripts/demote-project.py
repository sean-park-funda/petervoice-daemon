#!/usr/bin/env python3
"""프로젝트를 다른 프로젝트의 브랜치로 넣는다 (DB + 작업 폴더 + 클로드 대화 기록 + 세션 키까지 한 번에).

    python3 scripts/demote-project.py <프로젝트ID> <부모_프로젝트ID> [--parent-branch <내부ID>] [--title "이름"] [--dry-run] [--force]

`promote-branch.py`(브랜치 → 프로젝트)의 역. 지원#84(2026-10-29, 주리 꿀템파인더 팀 재편)에서 만들었다.

왜 스크립트인가 — DB 쪽(대화 relabel·하위 브랜치 이동·프로젝트 삭제)은 웹 API 가 하지만, 나머지는 데몬 머신에만 있다:
  1. 작업 폴더: 브랜치는 원래 부모 폴더를 공유하지만, 넣어진 프로젝트는 자기 파일이 자기 폴더에 있다.
     그래서 `<부모 폴더>/branches/<프로젝트ID>` 로 **옮기고** 그 경로를 브랜치 작업 폴더로 지정한다.
     제자리에 두면 비게 된 ID 로 나중에 새 프로젝트를 만들 때 데몬이 같은 폴더를 자동 배정한다(Sean 10/29).
  2. 클로드 대화 기록: `~/.claude/projects/<작업 폴더 경로를 - 로 바꾼 이름>/` 에 세션 파일과 자동 메모리가 있다.
     작업 폴더와 같이 이름을 바꿔야 새 위치에서 이어서 쌓인다(10/29 실측: 둘 다 옮기면 resume 이 맥락을 기억함).
  3. 세션 키: `sessions.json` 의 `<프로젝트ID>:default` → `branch:<새id>:default`.
     돌아가는 데몬은 이 파일을 메모리 상태로 덮어쓰므로 **데몬을 내린 채(bootout)** 바꾼다(promote 9/14 교훈).
  4. 파일 속 옛 절대경로(스크립트·문서·프롬프트)를 새 경로로 바꾼다 — `honeypick` 이 `honeypick-lead` 의 앞부분이므로
     경로 뒤가 이름 문자로 이어지는 경우는 건드리지 않는다.

순서: 사전 점검(--dry-run 은 여기까지) → 데몬 bootout → API → 폴더·기록 이동 → 경로 치환 → 세션 키 → HEARTBEAT → bootstrap → 검증.
데몬이 일하는 중(클로드 자식 프로세스 있음)이면 최대 3분 기다리고, 그래도 바쁘면 멈춘다(--force 로 무시).
"""
import argparse, json, os, re, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

HOME = Path.home()
DAEMON_DIR = HOME / ".claude-daemon"
SESSIONS = DAEMON_DIR / "sessions.json"
# 엔진별 세션 파일 — 키는 모두 "<프로젝트>:<task>" 형식
SESSION_FILES = [DAEMON_DIR / n for n in ("sessions.json", "codex_sessions.json", "gemini_sessions.json")]
CONFIG = DAEMON_DIR / "config.json"
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next"}
MAX_TEXT = 5 * 1024 * 1024
LOG_LINES: list[str] = []


def log(msg: str):
    print(msg, flush=True)
    LOG_LINES.append(msg)


def api(method: str, path: str, body: dict | None = None) -> dict:
    cfg = json.loads(CONFIG.read_text())
    url = cfg.get("api_url", "https://www.peter-voice.site") + path
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": cfg["api_key"], "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API {e.code} {path}: {e.read().decode()[:400]}")


def enc(path: str) -> str:
    """클로드 CLI 가 작업 폴더 경로로 만드는 기록 폴더 이름 (영숫자 외 전부 '-')."""
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def claude_project_roots() -> list[Path]:
    roots = [HOME / ".claude" / "projects"]
    roots += sorted(DAEMON_DIR.glob("claude-config*/projects"))
    return [r for r in roots if r.is_dir()]


# ── 데몬 제어 ─────────────────────────────────────────────

def daemon_plist() -> tuple[str, Path] | None:
    la = HOME / "Library" / "LaunchAgents"
    for label in ("com.petervoice.claude-daemon", "com.petervoice.daemon"):
        p = la / f"{label}.plist"
        if p.exists():
            return label, p
    return None


def daemon_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "claude_daemon.py"], capture_output=True, text=True).stdout
    return [int(x) for x in out.split() if x.strip().isdigit() and int(x) != os.getpid()]


def daemon_busy(pids: list[int]) -> bool:
    for pid in pids:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
        for child in out.split():
            cmd = subprocess.run(["ps", "-o", "command=", "-p", child], capture_output=True, text=True).stdout
            if "claude" in cmd and "claude_daemon.py" not in cmd:
                return True
    return False


def bootout(label: str):
    uid = os.getuid()
    r = subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, text=True)
    for _ in range(60):
        if not daemon_pids():
            return
        time.sleep(1)
    raise RuntimeError(f"데몬이 내려가지 않음 (bootout rc={r.returncode} {r.stderr.strip()[:200]})")


def bootstrap(label: str, plist: Path) -> bool:
    uid = os.getuid()
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, text=True)
    for _ in range(40):
        if daemon_pids():
            return True
        time.sleep(1)
    log(f"🔴 데몬 재기동 실패 (rc={r.returncode} {r.stderr.strip()[:200]}) — 수동: launchctl bootstrap gui/{uid} {plist}"
        f"  (SSH 에서 막히면 sudo launchctl bootstrap gui/{uid} {plist})")
    return False


# ── 경로 치환 ─────────────────────────────────────────────

def path_variants(old: str, new: str) -> list[tuple[re.Pattern, str]]:
    pairs = [(old, new)]
    home = str(HOME)
    if old.startswith(home + "/"):
        pairs.append(("~" + old[len(home):], "~" + new[len(home):]))
        pairs.append(("$HOME" + old[len(home):], "$HOME" + new[len(home):]))
    # 뒤가 이름 문자로 이어지면(honeypick → honeypick-lead, honeypick.bak) 다른 경로다 — 건드리지 않는다.
    # 문장 끝 마침표(…/honeypick.)는 경로 끝으로 본다. 앞이 경로 문자면(/mnt/x/Users/…) 다른 경로다.
    pats = [(re.compile(r"(?:(?<=file://)|(?<![\w/.\-$~]))" + re.escape(o) + r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9_\-])"), n)
            for o, n in pairs]
    # 클로드 대화기록 폴더 이름(`~/.claude/projects/-Users-…-honeypick/`)을 적어 둔 프롬프트·문서도 있다 — 같이 옮겼으므로 같이 바꾼다
    pats.append((re.compile(re.escape(enc(old)) + r"(?![A-Za-z0-9\-])"), enc(new)))
    return pats


def scan_text_files(root: Path, pats) -> list[Path]:
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                if p.is_symlink() or p.stat().st_size > MAX_TEXT:
                    continue
                b = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in b[:8192]:
                continue
            try:
                t = b.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if any(pat.search(t) for pat, _ in pats):
                hits.append(p)
    return hits


def rewrite(text: str, pats) -> str:
    for pat, new in pats:
        text = pat.sub(lambda _m, _n=new: _n, text)  # 치환 문자열을 템플릿으로 해석하지 않는다(경로의 \ 등)
    return text


# ── 본체 ─────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("parent_project_id")
    ap.add_argument("--parent-branch", type=int, default=None, help="부모 프로젝트 안의 브랜치 밑으로 넣을 때 그 내부ID")
    ap.add_argument("--title")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="데몬이 일하는 중이어도 진행")
    ap.add_argument("--skip-daemon", action="store_true", help=argparse.SUPPRESS)  # 시험용: 데몬 정지·세션 키 이동 생략 (데몬 안에서 시험할 때)
    a = ap.parse_args()
    pid, parent_id = a.project_id, a.parent_project_id

    # ── 0. 사전 점검 (아무것도 바꾸지 않는다)
    projects = {p["id"]: p for p in api("GET", "/api/projects").get("projects", [])}
    if pid not in projects:
        raise SystemExit(f"🔴 프로젝트 '{pid}' 없음")
    if parent_id not in projects:
        raise SystemExit(f"🔴 부모 프로젝트 '{parent_id}' 없음")
    old_dir = (projects[pid].get("directory") or "").rstrip("/")
    parent_dir = (projects[parent_id].get("directory") or "").rstrip("/")
    if not old_dir or not parent_dir:
        raise SystemExit("🔴 프로젝트·부모 둘 다 작업 폴더(directory)가 있어야 한다")
    new_dir = f"{parent_dir}/branches/{pid}"
    if not Path(old_dir).is_dir():
        raise SystemExit(f"🔴 원본 작업 폴더가 이 맥에 없다: {old_dir} — 프로젝트가 도는 맥에서 실행할 것")
    if Path(new_dir).exists() and any(Path(new_dir).iterdir()):
        raise SystemExit(f"🔴 대상 폴더가 이미 있고 비어 있지 않다: {new_dir}")
    if Path(new_dir).resolve().is_relative_to(Path(old_dir).resolve()):
        raise SystemExit("🔴 부모 폴더가 원본 폴더 안에 있다 — 옮길 수 없다")

    # 같은 디스크여야 rename 이 즉시·원자적으로 된다. 부모 폴더에 쓸 수 있어야 한다
    if os.stat(old_dir).st_dev != os.stat(parent_dir).st_dev:
        raise SystemExit("🔴 원본과 부모 폴더가 다른 디스크다 — 이동이 복사가 된다. 중단")
    if not os.access(parent_dir, os.W_OK):
        raise SystemExit(f"🔴 부모 폴더에 쓸 수 없다: {parent_dir}")
    # 다른 프로젝트·브랜치가 같은 폴더(또는 그 안)를 쓰고 있으면 옮기면 안 된다
    def under(d: str) -> bool:
        d = (d or "").rstrip("/")
        return bool(d) and (d == old_dir or d.startswith(old_dir + "/"))
    sharing = [f"프로젝트 {p['id']}" for p in projects.values() if p["id"] != pid and under(p.get("directory"))]
    sharing += [f"브랜치 {b['id']}" for b in api("GET", "/api/branches?all_active=1").get("branches", [])
                if b.get("project_id") != pid and under(b.get("directory"))]
    if sharing:
        raise SystemExit(f"🔴 이 폴더를 같이 쓰는 담당이 있다: {', '.join(sharing)} — 중단")
    # 상시 실행 서비스·예약 작업이 옛 경로를 쓰면 이동 후 깨진다
    ext_refs = []
    for plist in (HOME / "Library" / "LaunchAgents").glob("*.plist"):
        try:
            if old_dir in plist.read_text(errors="ignore"):
                ext_refs.append(f"LaunchAgent {plist.name}")
        except OSError:
            pass
    cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if old_dir in cron:
        ext_refs.append("crontab")
    if ext_refs and not a.force:
        raise SystemExit(f"🔴 옛 경로를 쓰는 서비스가 있다: {', '.join(ext_refs)} — 먼저 정리하거나 --force")
    # 서버 쪽 검증 (아무것도 안 바꾼다)
    plan = api("POST", f"/api/projects/{pid}/demote",
               {"parent_project_id": parent_id, "parent_branch_id": a.parent_branch, "title": a.title, "dry_run": True})
    if plan["folder_move"]["to"] != new_dir:
        raise SystemExit(f"🔴 서버가 계산한 새 폴더가 다르다: {plan['folder_move']['to']}")

    def session_keys() -> dict[Path, list[str]]:
        out = {}
        for f in SESSION_FILES:
            if f.exists():
                d = json.loads(f.read_text())
                ks = [k for k in d if k == pid or k.startswith(f"{pid}:")]
                if ks:
                    out[f] = ks
        return out
    key_moves = session_keys()
    claude_moves = []
    for root in claude_project_roots():
        src = root / enc(old_dir)
        if src.is_dir():
            dst = root / enc(new_dir)
            if dst.exists() and any(p.name != "memory" or any(p.iterdir()) for p in dst.iterdir()):
                raise SystemExit(f"🔴 대상 대화기록 폴더가 이미 있다: {dst}")
            claude_moves.append((src, dst))
    pats = path_variants(old_dir, new_dir)
    files = scan_text_files(Path(old_dir), pats)
    label_plist = daemon_plist()

    log(f"=== demote {pid} → {parent_id}{f' (브랜치 {a.parent_branch} 밑)' if a.parent_branch else ''}  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"  작업 폴더: {old_dir}\n        →  {new_dir}")
    for s, d in claude_moves:
        log(f"  대화 기록: {s.name} → {d.name}  (세션 파일 {len(list(s.glob('*.jsonl')))}개)")
    log(f"  세션 키: {[k for ks in key_moves.values() for k in ks] or '없음'} → branch:<새id>:…")
    log(f"  서버 검증 통과: 대화 {plan['moved_messages']}건 · 하위 브랜치 {len(plan['moved_branches'])}개")
    for w in plan.get("warnings", []):
        log(f"  ⚠️ {w}")
    log(f"  옛 경로가 적힌 파일 {len(files)}개:")
    for f in files:
        log(f"    - {f.relative_to(old_dir)}")
    hb = Path(old_dir) / "docs" / "HEARTBEAT.md"
    log(f"  HEARTBEAT.md: {'있음 → HEARTBEAT-branch-<새id>.md' if hb.exists() else '없음'}")
    log(f"  데몬: {label_plist[0] if label_plist else '🔴 LaunchAgent plist 못 찾음'}")
    if a.dry_run:
        log("--dry-run: 아무것도 바꾸지 않았다")
        return
    if not label_plist and not a.skip_daemon:
        raise SystemExit("🔴 데몬 LaunchAgent 를 못 찾아 중단 (세션 키를 안전하게 옮길 수 없다)")
    label, plist = label_plist if label_plist else ("", Path())

    # ── 1. 데몬이 한가해질 때까지 기다렸다가 내린다
    if a.skip_daemon:
        log("  (시험 모드: 데몬 정지·세션 키 생략)")
    elif not a.force:
        for i in range(36):
            if not daemon_busy(daemon_pids()):
                break
            if i == 0:
                log("  데몬이 응답 중 — 끝날 때까지 기다린다 (최대 3분)")
            time.sleep(5)
        else:
            raise SystemExit("🔴 데몬이 3분 넘게 일하는 중 — 나중에 다시 (또는 --force)")
    ok = False
    try:
        if not a.skip_daemon:
            log("  데몬 내리는 중…")
            bootout(label)
            log("  ✅ 데몬 정지")
            key_moves = session_keys()  # 기다리는 사이 생긴 키까지 (데몬이 내려간 뒤의 최종본)
        # ── 2. DB (웹 API)
        r = api("POST", f"/api/projects/{pid}/demote",
                {"parent_project_id": parent_id, "parent_branch_id": a.parent_branch, "title": a.title})
        br = r["branch"]
        bkey = f"branch:{br['id']}"
        log(f"  ✅ DB: 브랜치 #{br['branch_number']} (내부ID {br['id']}) 생성 · 대화 {r['moved_messages']}건 이전 · 하위 {len(r['moved_branches'])}개")
        for w in r.get("warnings", []):
            log(f"  ⚠️ {w}")
        if r["folder_move"]["to"] != new_dir:
            log(f"  ⚠️ 서버가 계산한 새 폴더가 다르다: {r['folder_move']['to']} — 서버 값을 따른다")

        # ── 3. 폴더·대화기록 이동 (같은 디스크 안 rename — 즉시)
        target = Path(r["folder_move"]["to"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rmdir()  # 빈 폴더만 (사전 점검에서 확인)
        os.rename(old_dir, target)
        log(f"  ✅ 작업 폴더 이동")
        for s, d in claude_moves:
            d = s.parent / enc(str(target))
            if d.exists():
                for sub in d.iterdir():  # 비어 있는 memory 폴더만 있을 수 있다
                    sub.rmdir()
                d.rmdir()
            os.rename(s, d)
            log(f"  ✅ 대화 기록 이동: {d.name}")

        # ── 4. 파일 속 옛 경로 치환
        pats = path_variants(old_dir, str(target))
        changed = []
        for f in files:
            nf = target / f.relative_to(old_dir)
            try:
                t = nf.read_bytes().decode("utf-8")  # 바이트로 읽고 써서 줄바꿈(CRLF)을 그대로 둔다
                t2 = rewrite(t, pats)
                if t2 != t:
                    nf.write_bytes(t2.encode("utf-8"))
                    changed.append(str(nf.relative_to(target)))
            except OSError as e:
                log(f"  ⚠️ 치환 실패 {nf}: {e}")
        log(f"  ✅ 파일 속 경로 치환 {len(changed)}개")

        # ── 5. 세션 키
        for sf, keys in ({} if a.skip_daemon else key_moves).items():
            data = json.loads(sf.read_text())
            for k in keys:
                nk = bkey + k[len(pid):] if k != pid else bkey
                if nk in data:
                    log(f"  ⚠️ 대상 세션 키가 이미 있다({sf.name} {nk}) — 덮지 않았다")
                    continue
                if k in data:
                    data[nk] = data.pop(k)
                    log(f"  ✅ 세션 키 {sf.name}: {k} → {nk}")
                    if sf.name == "sessions.json" and nk == f"{bkey}:default" and data[nk].get("session_id"):
                        try:  # DB 의 branches.session_id 는 기록용 — sessions.json 과 맞춰 둔다
                            api("PATCH", "/api/bot/branch", {"id": br["id"], "session_id": data[nk]["session_id"]})
                        except Exception as e:
                            log(f"  ⚠️ branches.session_id 기록 실패(동작에는 영향 없음): {e}")
            sf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # ── 6. HEARTBEAT 파일 이름 (브랜치 하트비트는 HEARTBEAT-branch-<id>.md 를 읽는다)
        hb_old = target / r["heartbeat_rename"]["from"]
        hb_new = target / r["heartbeat_rename"]["to"]
        if hb_old.exists() and not hb_new.exists():
            os.rename(hb_old, hb_new)
            log(f"  ✅ {hb_old.name} → {hb_new.name}")

        # ── 7. 프롬프트 속 옛 경로 (이 유저의 프로젝트·브랜치 프롬프트 전부 — 경로 경계 매칭이라 안전)
        n_prompt = 0
        for p in api("GET", "/api/projects").get("projects", []):
            cur = api("GET", f"/api/prompts?project={p['id']}")
            content = cur.get("content") or (cur.get("prompt") or {}).get("content") or ""
            if content and rewrite(content, pats) != content:
                api("PUT", "/api/prompts", {"project": p["id"], "content": rewrite(content, pats),
                                            "changed_by": "demote", "change_reason": f"{pid} 폴더 이동 경로 치환"})
                n_prompt += 1
                log(f"  ✅ 프로젝트 프롬프트 경로 치환: {p['id']}")
        for b in api("GET", "/api/branches?all_active=1").get("branches", []):
            content = b.get("prompt") or ""
            if content and rewrite(content, pats) != content:
                api("PATCH", f"/api/branches/{b['id']}", {"prompt": rewrite(content, pats)})
                n_prompt += 1
                log(f"  ✅ 브랜치 프롬프트 경로 치환: branch:{b['id']}")
        ok = True
    except Exception as e:
        log(f"🔴 중단: {e}")
        log("   → DB·폴더가 중간 상태일 수 있다. 위 로그의 ✅ 까지가 끝난 단계다. 데몬은 다시 올린다.")
    finally:
        up = True if a.skip_daemon else bootstrap(label, plist)
        if up and not a.skip_daemon:
            log("  ✅ 데몬 재기동")

    # ── 8. 검증: 재기동 후에도 세션 키가 유지되는지
    if ok and up and not a.skip_daemon:
        time.sleep(12)
        bad = [f"{sf.name}:{k}" for sf, keys in key_moves.items() for k in keys if k in json.loads(sf.read_text())]
        log("  ✅ 세션 키 유지 확인" if not bad else f"  🔴 데몬 재기동 후 옛 키가 되살아났다: {bad} — 수동 확인")

    logf = DAEMON_DIR / f"demote-{pid}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    logf.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    log(f"기록: {logf}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
