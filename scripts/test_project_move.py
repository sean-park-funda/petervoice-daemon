#!/usr/bin/env python3
"""pv-project-move.py 의 네트워크 없는 부분 시험 (2026-11-26).

실행: python3 scripts/test_project_move.py   (1초 안팎, 임시 폴더만 쓴다)
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pv_project_move", ROOT / "pv-project-move.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

checks = 0


def check(cond, label):
    global checks
    checks += 1
    if not cond:
        raise SystemExit(f"FAIL: {label}")


# ── 경로 · 세션 이름 ──
check(m.claude_slug("/Users/sean/.claude-daemon/projects/game-develop") == "-Users-sean--claude-daemon-projects-game-develop", "slug matches Claude Code's folder naming")
check(m.map_dir("/Users/sean/.claude-daemon/projects/x/", "/Users/sean", "/Users/vibegame") == "/Users/vibegame/.claude-daemon/projects/x", "home-relative folder maps under the device home")
check(m.map_dir("/Users/seanx/p", "/Users/sean", "/Users/vibegame") == "/Users/seanx/p", "a sibling of the home folder is not rewritten")
check(m.map_dir("/Volumes/work/p", "/Users/sean", "/Users/vibegame") == "/Volumes/work/p", "folder outside the home keeps its path")

sessions = {"game:default": {}, "game:member:main": {}, "game-old:default": {}, "branch:12:default": {}, "branch:123:default": {}, "branch:7:default": {}, "other:default": {}}
check(m.session_keys_for(sessions, "game", [12, 7]) == ["branch:12:default", "branch:7:default", "game:default", "game:member:main"], "session keys: this project and its branches only")

# ── 폴더 목록 대조 ──
with tempfile.TemporaryDirectory(prefix="pv-move-test-") as tmp:
    a, b = Path(tmp) / "a", Path(tmp) / "b"
    for root in (a, b):
        (root / "docs" / "한글 폴더").mkdir(parents=True)
        (root / "docs" / "한글 폴더" / "메모.md").write_text("x")
        (root / "node_modules" / "big").mkdir(parents=True)
        (root / "node_modules" / "big" / "f.js").write_text("x" * (1 if root == a else 2))      # 양쪽 크기가 다르다 — 제외하면 같아야 한다
        (root / "empty").mkdir()
        os.symlink("docs", root / "link")
        os.utime(root / "docs" / "한글 폴더" / "메모.md", (1_700_000_000, 1_700_000_000))
    ma, mb = m.manifest_local(str(a), ["node_modules"]), m.manifest_local(str(b), ["node_modules"])
    check(ma == mb and ma["files"] == 1 and ma["links"] == 1 and ma["dirs"] == 3, "identical trees give identical manifests (excluded names ignored)")
    check(m.manifest_local(str(a), []) != m.manifest_local(str(b), []), "without the exclude the differing file shows")
    os.utime(b / "docs" / "한글 폴더" / "메모.md", (1_700_000_500, 1_700_000_500))
    check(m.manifest_local(str(b), ["node_modules"]) != ma, "a changed mtime changes the manifest")
    os.utime(b / "docs" / "한글 폴더" / "메모.md", (1_700_000_000, 1_700_000_000))
    (b / "empty").rmdir()
    check(m.manifest_local(str(b), ["node_modules"]) != ma, "a missing empty folder changes the manifest")
    (b / "empty").mkdir()
    os.remove(b / "link"); os.symlink("empty", b / "link")
    check(m.manifest_local(str(b), ["node_modules"]) != ma, "a retargeted symlink changes the manifest")

    # ── 이 맥 대기 줄 ──
    cfg = Path(tmp) / "cfg"
    cfg.mkdir()
    (cfg / "queue.json").write_text(json.dumps([
        {"project": "game", "processed": False}, {"project": "branch:12", "processed": True},
        {"project": "branch:7"}, {"project": "other"}, "junk",
    ]))
    check(m.local_queue_keys(cfg, {"game", "branch:12", "branch:7"}) == ["branch:7", "game"], "queued (unprocessed) messages of this project are found")
    check(m.local_queue_keys(Path(tmp) / "none", {"game"}) == [], "no queue file → nothing queued")

    # ── 상태 파일 · 거울 ──
    st = m.State(cfg, "game")
    st.save(phase="placed", device="gamemac")
    check(m.State(cfg, "game").data["phase"] == "placed", "state round-trips")
    pl = m.mirror_plist("game/x", cfg, 300, "/tmp/keyfile")
    check(pl["Label"] == "com.petervoice.mirror.game-x" and pl["StartInterval"] == 300 and pl["RunAtLoad"] is False, "mirror job: label, interval, not at load")
    check(pl["ProgramArguments"][:2] == ["/usr/bin/python3", str(ROOT / "pv-project-move.py")] and "--once" in pl["ProgramArguments"]
          and pl["ProgramArguments"][-2:] == ["--api-key-file", "/tmp/keyfile"], "mirror job runs this script once per tick with the key file, never the key")

    # 거울은 「기기에 놓인 상태」가 아니면 아무것도 하지 않는다 (API 를 부르지도 않는다)
    class NoApi:
        def placement(self, project):
            raise AssertionError("must not be called")
    st2 = m.State(cfg, "idle")
    st2.save(phase="returned", device="gamemac")
    check(m.mirror_once(NoApi(), st2, "idle") == 0, "mirror is a no-op unless the project is placed on the device")

    # 서버의 배치가 그 기기가 아니면 당겨 오지 않는다 (본기기로 돌아온 폴더를 덮지 않는다)
    class Api:
        def __init__(self, device):
            self.device = device
        def placement(self, project):
            return {"device": self.device}
    local = Path(tmp) / "live"
    local.mkdir()
    (local / "work.txt").write_text("live work on the primary")
    st3 = m.State(cfg, "back-home")
    st3.save(phase="placed", device="gamemac", ssh="host.invalid", device_dir="/nowhere", local_dir=str(local))
    for device in (None, "__moving__", "othermac"):
        check(m.mirror_once(Api(device), st3, "back-home") == 0 and (local / "work.txt").exists(), f"mirror skips when the server says {device!r}")

    # 마운트 지점이 안 붙어 있으면 당겨 오지 않는다
    st3.save(mirror_require_mounts=[str(local)])
    check(m.mirror_once(Api("gamemac"), st3, "back-home") == 1 and (local / "work.txt").exists(), "mirror refuses when a required mount point is not mounted")

print(f"OK — {checks} checks")
