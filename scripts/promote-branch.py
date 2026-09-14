#!/usr/bin/env python3
"""브랜치를 최상위 프로젝트로 승격한다 (DB + 세션 키까지 한 번에).

    python3 scripts/promote-branch.py <브랜치ID> <새_프로젝트ID> [--name "표시 이름"] [--dir /경로] [--dry-run]

왜 스크립트인가 — 승격의 절반은 웹 API 가 할 수 있지만(대화 재키잉·하위 이동·브랜치 삭제),
**세션 키는 데몬 머신의 `~/.claude-daemon/sessions.json` 에 있어 서버가 손댈 수 없다.**
그리고 이 파일은 돌아가는 데몬이 메모리 상태로 통째로 덮어쓰기 때문에, 데몬을 켜둔 채 고치면
조용히 되돌아간다. 그래서 **데몬을 멈춘 뒤에** 바꾸고, 다시 올린 뒤 검증까지 한다.

2026-09-14 티업 승격에서 배운 것:
  - 세션 키 이관을 "나중에" 예약했더니, 그 사이 유저가 새 프로젝트에 말을 걸어 **새 세션이 먼저 생겼다.**
    그래서 여기서는 DB 작업 직후 곧바로, 데몬이 멈춘 상태에서 키를 옮긴다.
  - 대상 키가 이미 있으면 덮어쓰지 않고 멈춘다(새 세션이 이미 일하고 있을 수 있다).
"""
import argparse, json, os, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

DAEMON_DIR = Path(os.path.expanduser("~/.claude-daemon"))
SESSIONS = DAEMON_DIR / "sessions.json"
CONFIG = DAEMON_DIR / "config.json"


def api(method: str, path: str, body: dict | None = None) -> dict:
    cfg = json.loads(CONFIG.read_text())
    url = cfg.get("api_url", "https://www.peter-voice.site") + path
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"X-Api-Key": cfg["api_key"], "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"🔴 API {e.code}: {e.read().decode()[:400]}")


def daemon_label() -> str | None:
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        for label in ("com.petervoice.claude-daemon", "com.petervoice.daemon"):
            if line.endswith(label):
                return label
    return None


def daemon_running(label: str) -> bool:
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.endswith(label):
            return line.split("\t")[0] != "-"
    return False


def move_session_key(src: str, dst: str) -> str:
    """데몬을 멈춘 채 sessions.json 의 키를 옮긴다. 돌아가는 데몬은 파일을 덮어쓴다."""
    label = daemon_label()
    if not label:
        return "⚠️ 데몬 라벨을 못 찾음 — 세션 키는 손대지 않았다"
    subprocess.run(["launchctl", "stop", label], capture_output=True)
    for _ in range(30):
        if not daemon_running(label):
            break
        time.sleep(1)
    data = json.loads(SESSIONS.read_text())
    if src not in data:
        return f"⚠️ 원본 세션 키 없음({src}) — 새 세션으로 시작한다"
    if dst in data:
        return f"⚠️ 대상 키가 이미 있다({dst}) — 새 세션이 이미 일하고 있을 수 있어 덮지 않았다"
    data[dst] = data.pop(src)
    SESSIONS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 데몬은 launchd 가 자동으로 되살린다. 올라온 뒤에도 유지되는지 확인한다.
    for _ in range(40):
        time.sleep(1)
        if daemon_running(label):
            break
    time.sleep(10)
    after = json.loads(SESSIONS.read_text())
    if dst in after and src not in after:
        return f"✅ 세션 이관 확인 ({src} → {dst})"
    return f"🔴 데몬 재기동 후 되돌아갔다 — 수동 확인 필요 ({SESSIONS})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("branch_id", type=int)
    ap.add_argument("project_id")
    ap.add_argument("--name")
    ap.add_argument("--dir", dest="directory")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    br = api("GET", f"/api/branches/{a.branch_id}").get("branch", {})
    if not br:
        raise SystemExit("🔴 브랜치를 찾을 수 없다")
    print(f"승격 대상: #{br.get('branch_number')} {br.get('title')}  "
          f"(프로젝트 {br.get('project_id')} → {a.project_id})")
    if a.dry_run:
        print("--dry-run: 아무것도 바꾸지 않았다"); return

    r = api("POST", f"/api/branches/{a.branch_id}/promote",
            {"project_id": a.project_id, "name": a.name, "directory": a.directory})
    print(f"  대화 {r['moved_messages']}건 이전(복사 아님, 메시지 ID 유지)")
    print(f"  하위 {len(r['moved_descendants'])}개 이동 · 직속 승격 {len(r['detached_children'])}개")
    for w in r.get("warnings", []):
        print(f"  ⚠️ {w}")
    print(" ", move_session_key(r["session_key"]["from"], r["session_key"]["to"]))
    print(f"완료 → /?project={r['project_id']}")


if __name__ == "__main__":
    main()
