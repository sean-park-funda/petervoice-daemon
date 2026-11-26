#!/usr/bin/env python3
"""프로젝트를 같은 계정의 다른 맥(기기)으로 옮기고, 되돌리고, 거울을 둔다 (2026-11-26, 한 계정 · 여러 기기 단계 D).

본기기(유저 열쇠의 데몬이 도는 맥)에서 실행한다. 접속은 본기기 → 기기 한 방향(ssh)뿐이다.

    pv-project-move.py plan   <프로젝트> --to <기기>     # 읽기만: 무엇을 어디로, 걸리는 것
    pv-project-move.py precopy <프로젝트> --to <기기>    # 미리 복사만(배치 그대로) — 큰 프로젝트는 먼저
    pv-project-move.py move   <프로젝트> --to <기기>     # 본기기 → 기기
    pv-project-move.py back   <프로젝트>                 # 기기 → 본기기 (기기가 죽었으면 --device-dead)
    pv-project-move.py mirror <프로젝트> --install       # 기기 → 본기기 읽기 전용 거울(launchd, 기본 5분)
    pv-project-move.py status <프로젝트>

move 의 순서 (서버가 걸음 규칙을 강제한다 — POST /api/devices/placement):
  1. 점검        기기 켜짐 · 디스크 · 대상 폴더 · 이 맥에서 그 폴더를 쓰는 상주 작업
  2. 미리 복사   프로젝트가 본기기에서 도는 동안 rsync (멈춤 시간을 줄인다)
  3. 이동 중     배치 = "__moving__" — 아무도 새 메시지를 받지 않는다. 대기 시간 + 돌던 턴 · 이 맥 대기 줄이 빌 때까지
  4. 마지막 복사 rsync --delete, 양쪽 목록(이름 · 크기 · 수정 시각) 대조
  5. 세션        대화 기록 파일을 기기로 옮기고 기기 데몬의 세션 장부에 적는다(없으면 서버의 인계 요약으로 새 세션)
  6. 배치        배치 = 기기, 폴더 칸을 기기 경로로 바꿔 쓴다
  7. 확인        (--verify) 실제 턴 한 번으로 호스트 · 폴더를 답하게 한다
실패하면 배치를 원래 자리로 되돌린다(그쪽 폴더는 손대지 않았으므로 그대로 이어 간다).

열쇠는 인자로 받지 않는다: 기본은 <config-dir>/config.json 의 api_key, 다른 계정은 --api-key-file(권한 600 파일).
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import os
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

MOVING = "__moving__"
DAEMON_LABEL = "com.petervoice.daemon"
MIRROR_LABEL_PREFIX = "com.petervoice.mirror."
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class MoveError(Exception):
    pass


# ─── 경로 · 세션 이름 ────────────────────────────────────────────

def claude_slug(path: str) -> str:
    """Claude Code 가 대화 기록 폴더 이름을 만드는 방식: 영문 · 숫자가 아닌 글자를 전부 '-' 로."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def map_dir(local_dir: str, local_home: str, device_home: str) -> str:
    """본기기 폴더 → 기기 폴더. 홈 아래면 기기 홈 아래 같은 자리, 아니면 같은 절대 경로."""
    local_dir = local_dir.rstrip("/")
    local_home = local_home.rstrip("/")
    if local_dir == local_home or local_dir.startswith(local_home + "/"):
        return device_home.rstrip("/") + local_dir[len(local_home):]
    return local_dir


def session_keys_for(sessions: dict, project: str, branch_ids: list[int]) -> list[str]:
    """세션 장부에서 이 프로젝트 몫의 키: '<프로젝트>:…' 와 그 브랜치의 'branch:<번호>:…'."""
    heads = [f"{project}:"] + [f"branch:{b}:" for b in branch_ids]
    return sorted(k for k in sessions if any(k.startswith(h) for h in heads))


# ─── 폴더 목록 대조 (양쪽에서 같은 코드를 돌린다) ────────────────

MANIFEST_SRC = r'''
import hashlib, json, os, sys
root = sys.argv[1]
skip = set(sys.argv[2:])
h = hashlib.sha256()
files = links = dirs = size = 0
def rec(*parts):
    h.update("\0".join(parts).encode("utf-8", "surrogateescape") + b"\n")
for dp, dns, fns in os.walk(root):
    dns[:] = sorted(d for d in dns if d not in skip)
    rel = os.path.relpath(dp, root)
    keep = []
    for d in dns:
        p = os.path.join(dp, d)
        if os.path.islink(p):
            links += 1
            rec("L", os.path.join(rel, d), os.readlink(p))
        else:
            keep.append(d)
            dirs += 1
            rec("D", os.path.join(rel, d))
    dns[:] = keep
    for f in sorted(fns):
        if f in skip:
            continue
        p = os.path.join(dp, f)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if os.path.islink(p):
            links += 1
            rec("L", os.path.join(rel, f), os.readlink(p))
        elif os.path.isfile(p):
            files += 1
            size += st.st_size
            rec("F", os.path.join(rel, f), str(st.st_size), str(int(st.st_mtime)))
print(json.dumps({"files": files, "links": links, "dirs": dirs, "bytes": size, "sha": h.hexdigest()}))
'''


def manifest_local(root: str, excludes: list[str]) -> dict:
    out = subprocess.run([sys.executable, "-c", MANIFEST_SRC, root, *excludes], capture_output=True, text=True)
    if out.returncode != 0:
        raise MoveError(f"목록을 만들지 못했습니다({root}): {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


# ─── 기기(ssh) ───────────────────────────────────────────────────

class Remote:
    def __init__(self, host: str):
        self.host = host

    def run(self, command: str, stdin: str | None = None, timeout: int = 120, check: bool = True) -> str:
        proc = subprocess.run(["ssh", *SSH_OPTS, self.host, command], input=stdin, capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise MoveError(f"기기 명령 실패({proc.returncode}): {command[:80]} — {proc.stderr.strip()[:300]}")
        return proc.stdout

    def python(self, source: str, *args: str, timeout: int = 1800) -> str:
        quoted = " ".join(shlex.quote(a) for a in args)
        return self.run(f"/usr/bin/env python3 - {quoted}", stdin=source, timeout=timeout)

    def manifest(self, root: str, excludes: list[str]) -> dict:
        return json.loads(self.python(MANIFEST_SRC, root, *excludes))

    def home(self) -> str:
        return self.run('printf %s "$HOME"').strip()


# 볼륨 뿌리에 macOS 가 두는 것 — 프로젝트 안에 따로 붙인 디스크가 있으면 딸려 온다. 못 읽고(권한) 옮길 것도 아니다.
VOLUME_JUNK = [".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems", ".DocumentRevisions-V100"]


def with_junk(excludes: list[str] | None) -> list[str]:
    return list(dict.fromkeys(list(excludes or []) + VOLUME_JUNK))


def rsync(src: str, dst: str, excludes: list[str], delete: bool, timeout: int = 6 * 3600) -> None:
    cmd = ["rsync", "-a", "-e", "ssh " + " ".join(SSH_OPTS)]
    if delete:
        cmd.append("--delete")
    for name in excludes:
        cmd.append(f"--exclude={name}")
    cmd += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    # 24 = 복사 중 사라진 파일(도는 프로젝트를 미리 복사할 때 흔하다) — 마지막 복사와 대조가 잡는다
    if proc.returncode not in (0, 24):
        raise MoveError(f"rsync 실패({proc.returncode}): {proc.stderr.strip()[-400:]}")
    log(f"  rsync {'(삭제 포함) ' if delete else ''}{time.time() - started:.0f}초")


# ─── 서버 API ────────────────────────────────────────────────────

class Api:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.key = key

    def call(self, method: str, path: str, body: dict | None = None, bearer: bool = False, timeout: int = 30) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "User-Agent": "pv-project-move"}
        headers["Authorization" if bearer else "X-Api-Key"] = (f"Bearer {self.key}" if bearer else self.key)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.status, json.loads(res.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8") or "{}")
            except Exception:
                return e.code, {}

    def ok(self, method: str, path: str, body: dict | None = None, **kw) -> dict:
        status, data = self.call(method, path, body, **kw)
        if not 200 <= status < 300:       # 릴레이는 201 을 준다
            raise MoveError(f"{method} {path} → {status} {data.get('error') or ''} {data.get('reason') or ''}".strip())
        return data

    def project(self, project: str) -> dict:
        for p in self.ok("GET", "/api/projects").get("projects") or []:
            if p.get("id") == project and not p.get("team_id") and not p.get("is_team"):
                return p
        raise MoveError(f"이 계정에 「{project}」 프로젝트가 없습니다(팀 프로젝트는 옮기지 않습니다)")

    def branch_ids(self, project: str) -> list[int]:
        rows = self.ok("GET", f"/api/branches?project_id={urllib.parse.quote(project)}").get("branches") or []
        return sorted(int(b["id"]) for b in rows if b.get("id"))

    def device(self, name: str) -> dict:
        for d in self.ok("GET", "/api/devices").get("devices") or []:
            if d.get("name") == name:
                return d
        raise MoveError(f"이 계정에 「{name}」 기기가 없습니다 — 먼저 등록하세요(POST /api/devices)")

    def placement(self, project: str) -> dict:
        return self.ok("GET", f"/api/devices/placement?project={urllib.parse.quote(project)}")

    def place(self, project: str, to: str | None, directory: dict | None = None) -> tuple[int, dict]:
        body: dict = {"project": project, "to": to}
        if directory:
            body["directory"] = directory
        return self.call("POST", "/api/devices/placement", body)


# ─── 상태 파일 ───────────────────────────────────────────────────

class State:
    def __init__(self, config_dir: Path, project: str):
        self.dir = config_dir / "moves"
        self.path = self.dir / f"{project}.json"
        self.data: dict = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, **updates) -> None:
        self.data.update(updates)
        self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


# ─── 공통 조각 ───────────────────────────────────────────────────

def local_queue_keys(config_dir: Path, keys: set[str]) -> list[str]:
    """이 맥 데몬이 이미 받아 두고 아직 시작하지 않은 메시지의 프로젝트 키(이동 뒤에 옛 폴더에서 돌면 안 된다)."""
    try:
        queue = json.loads((config_dir / "queue.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted({m.get("project") for m in queue if isinstance(m, dict) and m.get("project") in keys and not m.get("processed")})


def wait_until_quiet(api: Api, project: str, config_dir: Path | None, keys: set[str], timeout_min: float) -> None:
    """대기 시간이 지나고, 서버가 보는 도는 턴이 없고, 이 맥 대기 줄에도 없을 때까지 기다린다."""
    deadline = time.time() + timeout_min * 60
    last = ""
    while True:
        state = api.placement(project)
        active = state.get("active") or []
        queued = local_queue_keys(config_dir, keys) if config_dir else []
        left = int(state.get("settle_left_ms") or 0)
        if left == 0 and not active and not queued:
            return
        note = f"대기 {left // 1000}초 · 도는 턴 {active or '없음'} · 이 맥 대기 줄 {queued or '없음'}"
        if note != last:
            log("  " + note)
            last = note
        if time.time() > deadline:
            raise MoveError(f"{timeout_min:g}분 안에 조용해지지 않았습니다 — {note}")
        time.sleep(min(15, max(2, left / 1000)))


def place_final(api: Api, project: str, to: str | None, directory: dict | None) -> dict:
    """마지막 걸음. 서버가 「대기 중 · 도는 턴 있음」이라고 하면 잠깐 더 기다렸다 다시 한다.
    떠나온 자리로 되돌리는 걸음은 서버가 도는 턴을 따지지 않는다(그 턴은 그 자리에서 돌고 있다)."""
    for _ in range(40):
        status, data = api.place(project, to, directory)
        if status == 200:
            return data
        if status == 409 and data.get("reason") in ("settling", "busy"):
            time.sleep(min(15, max(2, int(data.get("retry_after_ms") or 5000) / 1000)))
            continue
        raise MoveError(f"배치를 바꾸지 못했습니다: {status} {data.get('error')} ({data.get('reason')})")
    raise MoveError("배치를 바꾸지 못했습니다: 서버가 계속 「대기 중 · 도는 턴 있음」이라고 답합니다")


def compare(local_dir: str, remote: Remote, device_dir: str, excludes: list[str]) -> dict:
    here = manifest_local(local_dir, excludes)
    there = remote.manifest(device_dir, excludes)
    if here != there:
        raise MoveError(f"양쪽 폴더가 다릅니다 — 이 맥 {here} / 기기 {there}")
    return here


def plists_using(local_dir: str) -> list[str]:
    hits = []
    for path in sorted(glob.glob(os.path.expanduser("~/Library/LaunchAgents/*.plist"))):
        try:
            if local_dir in Path(path).read_text(encoding="utf-8", errors="replace"):
                hits.append(os.path.basename(path)[: -len(".plist")])
        except OSError:
            pass
    return [h for h in hits if not h.startswith(MIRROR_LABEL_PREFIX)]


def du_kb(path: str) -> int:
    # 못 읽는 파일이 하나라도 있으면 du 는 1 로 끝나지만 합계는 찍는다 — 종료 코드로 버리지 않는다
    out = subprocess.run(["du", "-sk", path], capture_output=True, text=True)
    first = (out.stdout.split() or ["0"])[0]
    return int(first) if first.isdigit() else 0


# ─── 세션 옮기기 ─────────────────────────────────────────────────

MERGE_SESSIONS_SRC = r'''
import json, os, sys
path, incoming_path = (os.path.expanduser(a) for a in sys.argv[1:3])
incoming = json.load(open(incoming_path, encoding="utf-8"))
try:
    current = json.load(open(path, encoding="utf-8"))
    if not isinstance(current, dict):
        current = {}
except (OSError, ValueError):
    current = {}
current.update(incoming)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(current, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
os.remove(incoming_path)
print(len(incoming))
'''

READ_SESSIONS_SRC = r'''
import json, os, sys
try:
    print(json.dumps(json.load(open(os.path.expanduser(sys.argv[1]), encoding="utf-8"))))
except (OSError, ValueError):
    print("{}")
'''


def transcripts_of(session_id: str, claude_dir: Path) -> tuple[str, list[str]] | None:
    """세션의 대화 기록: (폴더 이름, [기록 파일, 딸린 폴더…]). 없으면 None."""
    for p in (claude_dir / "projects").glob(f"*/{session_id}.jsonl"):
        items = [str(p)]
        side = p.with_suffix("")
        if side.is_dir():
            items.append(str(side))
        return p.parent.name, items
    return None


def mounts_of(values: list[str] | None) -> list[str]:
    return [os.path.abspath(os.path.expanduser(m)) for m in (values or [])]


def require_mounted(mounts: list[str] | None) -> None:
    """이 맥에서 따로 붙이는 디스크(예: branches/)가 프로젝트 폴더 안에 있을 때 — 안 붙은 채로 복사하면
    보내는 쪽에서는 빈 폴더가 --delete 로 기기 사본을 비우고, 받는 쪽에서는 내장 디스크를 채운다."""
    for mount in mounts or []:
        if not os.path.ismount(mount):
            raise MoveError(f"{mount} 이 붙어 있지 않습니다 — 디스크를 붙인 뒤 다시 하세요")
        if not os.listdir(mount):
            raise MoveError(f"{mount} 이 비어 있습니다 — 디스크가 제대로 붙었는지 확인하세요")


def require_device_filled(remote: "Remote", local_dir: str, device_dir: str, mounts: list[str] | None) -> None:
    """기기 → 이 맥(--delete) 전에: 이 맥의 따로 붙인 디스크에 해당하는 기기 쪽 폴더가 비어 있으면 그 디스크를 비우게 된다 — 막는다."""
    for mount in mounts or []:
        rel = os.path.relpath(mount, local_dir)
        if rel.startswith(".."):
            continue
        count = remote.run(f"ls -A {shlex.quote(device_dir.rstrip('/') + '/' + rel)} 2>/dev/null | wc -l", check=False).strip()
        if not count.isdigit() or int(count) == 0:
            raise MoveError(f"기기의 {rel}/ 이 비어 있거나 읽히지 않습니다({count!r}) — 이 맥의 {mount} 을 덮지 않습니다")


def sibling_tails(local_dir: str) -> list[str]:
    """이름이 「<이 프로젝트>-…」로 시작하는 이웃 폴더의 꼬리(기억 폴더 이름에서 이 프로젝트의 하위 폴더와 구분이 안 된다 — 남의 것으로 보고 건드리지 않는다)."""
    parent, name = os.path.dirname(local_dir.rstrip("/")), os.path.basename(local_dir.rstrip("/"))
    head = claude_slug(local_dir)
    tails = []
    try:
        for other in os.listdir(parent):
            slug = claude_slug(os.path.join(parent, other))
            if other != name and slug.startswith(head + "-"):
                tails.append(slug[len(head):])
    except OSError:
        pass
    return tails


def carry_memory(remote: Remote, claude_dir: Path, local_dir: str, device_dir: str, to_device: bool) -> int:
    """담당들의 기억 폴더(~/.claude/projects/<폴더 이름>/memory)를 옮긴다. 대화 기록과 달리 세션 번호와 무관하게 폴더째 간다
    (새 세션으로 시작하는 담당도 기억은 이어진다). 지우지 않는 복사 — 받는 쪽에만 있는 파일은 남는다."""
    here, there = claude_slug(local_dir), claude_slug(device_dir)
    ssh = "ssh " + " ".join(SSH_OPTS)
    moved = 0

    others = sibling_tails(local_dir)

    def mine(name: str, head: str) -> bool:
        if name == head:
            return True
        tail = name[len(head):]
        return name.startswith(head + "-") and not any(tail == o or tail.startswith(o + "-") for o in others)

    if to_device:
        for folder in sorted((claude_dir / "projects").glob(here + "*")):
            memory = folder / "memory"
            if not mine(folder.name, here) or not memory.is_dir() or not any(memory.iterdir()):
                continue
            target = f"~/.claude/projects/{there}{folder.name[len(here):]}/memory"
            remote.run(f"mkdir -p {target}")
            proc = subprocess.run(["rsync", "-a", "-e", ssh, str(memory) + "/", f"{remote.host}:{target}/"], capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                raise MoveError(f"기억 폴더 복사 실패({folder.name}): {proc.stderr.strip()[-200:]}")
            moved += 1
        return moved
    listed = remote.run(f"cd ~/.claude/projects 2>/dev/null && ls -d {there}*/memory 2>/dev/null; echo __listed__", check=False).split()
    if "__listed__" not in listed:
        raise MoveError("기기의 기억 폴더 목록을 읽지 못했습니다(ssh 실패)")
    for rel in listed:
        if rel == "__listed__":
            continue
        name = rel.split("/")[0]
        if not mine(name, there):
            continue
        local = claude_dir / "projects" / (here + name[len(there):]) / "memory"
        local.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["rsync", "-a", "-e", ssh, f"{remote.host}:~/.claude/projects/{rel}/", str(local) + "/"], capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise MoveError(f"기억 폴더 가져오기 실패({name}): {proc.stderr.strip()[-200:]}")
        moved += 1
    return moved


def device_daemon(remote: Remote, action: str) -> None:
    uid = remote.run("id -u").strip()
    if action == "stop":
        remote.run(f"launchctl bootout gui/{uid}/{DAEMON_LABEL} 2>/dev/null; "
                   f"for i in $(seq 1 60); do launchctl print gui/{uid}/{DAEMON_LABEL} >/dev/null 2>&1 || exit 0; sleep 2; done; exit 1",
                   timeout=180)
    else:
        remote.run(f"launchctl bootstrap gui/{uid} ~/Library/LaunchAgents/{DAEMON_LABEL}.plist", timeout=60)


def carry_sessions_to_device(api: Api, remote: Remote, device: str, sessions_path: Path, claude_dir: Path, project: str,
                             branch_ids: list[int], local_dir: str, device_dir: str, idle_wait_min: float) -> dict:
    """이 프로젝트 몫 세션의 대화 기록을 기기로 보내고 기기 데몬의 세션 장부에 적는다.
    기기 데몬은 장부를 시작할 때만 읽으므로 잠깐 내렸다 올린다 — 기기가 다른 턴을 돌리는 중이면 끝나기를 기다린다."""
    try:
        sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sessions = {}
    keys = session_keys_for(sessions, project, branch_ids)
    src_slug, dst_slug = claude_slug(local_dir), claude_slug(device_dir)
    carried: dict = {}
    skipped: list[str] = []
    for key in keys:
        entry = sessions[key]
        sid = entry.get("session_id")
        found = transcripts_of(sid, claude_dir) if sid else None
        if not found or not found[0].startswith(src_slug):
            skipped.append(key)      # 기록 파일이 없거나 이 프로젝트 폴더 밖에서 돈 세션 — 기기에서 새 세션으로 시작한다
            continue
        folder, items = found
        target = f"~/.claude/projects/{dst_slug}{folder[len(src_slug):]}"
        remote.run(f"mkdir -p {target}")
        for item in items:
            dest = f"{remote.host}:{target}/" + (os.path.basename(item) + "/" if os.path.isdir(item) else "")
            src = item + ("/" if os.path.isdir(item) else "")
            proc = subprocess.run(["rsync", "-a", "-e", "ssh " + " ".join(SSH_OPTS), src, dest], capture_output=True, text=True, timeout=1800)
            if proc.returncode != 0:
                raise MoveError(f"대화 기록 복사 실패({key}): {proc.stderr.strip()[-200:]}")
        carried[key] = {k: v for k, v in entry.items() if k != "account"}
    if not carried:
        return {"carried": [], "skipped": skipped}

    deadline = time.time() + idle_wait_min * 60
    while api.device(device).get("is_working"):
        if time.time() > deadline:
            log(f"  기기가 {idle_wait_min:g}분 안에 쉬지 않아 세션 장부를 적지 못했습니다 — 서버의 인계 요약으로 새 세션을 엽니다")
            return {"carried": [], "skipped": keys}
        time.sleep(10)
    # 장부에 적을 것을 먼저 기기에 파일로 올려 두고(표준 입력은 스크립트가 쓴다), 데몬을 내린 사이에 합친다
    remote.run("umask 077; cat > ~/.claude-daemon/sessions.incoming.json", stdin=json.dumps(carried, ensure_ascii=False))
    device_daemon(remote, "stop")
    try:
        remote.python(MERGE_SESSIONS_SRC, "~/.claude-daemon/sessions.json", "~/.claude-daemon/sessions.incoming.json")
    finally:
        device_daemon(remote, "start")
    return {"carried": sorted(carried), "skipped": skipped}


def carry_sessions_back(remote: Remote, sessions_path: Path, claude_dir: Path, project: str, branch_ids: list[int],
                        local_dir: str, device_dir: str) -> dict:
    """기기의 대화 기록을 이 맥으로 가져온다. 이 맥 데몬의 세션 장부는 건드리지 않는다(도는 데몬의 기억을 밖에서 못 바꾼다):
    세션 번호가 그대로인 세션은 가져온 기록으로 이어지고, 기기에서 새로 생긴 세션은 서버의 인계 요약으로 새로 시작한다."""
    theirs = json.loads(remote.python(READ_SESSIONS_SRC, "~/.claude-daemon/sessions.json") or "{}")
    try:
        ours = json.loads(sessions_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ours = {}
    keys = session_keys_for(theirs, project, branch_ids)
    src_slug, dst_slug = claude_slug(device_dir), claude_slug(local_dir)
    same, fresh = [], []
    for key in keys:
        sid = theirs[key].get("session_id")
        if not sid:
            continue
        listing = remote.run(f"ls -d ~/.claude/projects/*/{shlex.quote(sid)}.jsonl 2>/dev/null | head -1", check=False).strip()
        if not listing:
            continue
        folder = os.path.basename(os.path.dirname(listing))
        if not folder.startswith(src_slug):
            continue
        target = claude_dir / "projects" / (dst_slug + folder[len(src_slug):])
        target.mkdir(parents=True, exist_ok=True)
        for suffix in (".jsonl", "/"):
            src = f"{remote.host}:{os.path.dirname(listing)}/{sid}{suffix}"
            dest = str(target) + "/" + (sid + "/" if suffix == "/" else "")
            subprocess.run(["rsync", "-a", "-e", "ssh " + " ".join(SSH_OPTS), src, dest], capture_output=True, text=True, timeout=1800)
        (same if ours.get(key, {}).get("session_id") == sid else fresh).append(key)
    return {"continued": same, "fresh_on_primary": fresh}


# ─── 확인 턴 ─────────────────────────────────────────────────────

def verify_turn(api: Api, project: str, timeout_min: float = 6) -> str:
    marker = f"이동 확인 {datetime.now().strftime('%H%M%S')}"
    text = (f"[{marker}] 프로젝트 배치를 바꾼 뒤의 확인입니다. 다른 작업은 하지 말고, 셸에서 `hostname` 과 `pwd` 를 실행해 "
            "그 두 값만 한 줄로 답하세요. — 회신 불요")
    api.ok("POST", "/api/relay/message", {"from_project": project, "to_project": project, "text": text})
    sent = time.time()
    deadline = sent + timeout_min * 60
    seen_marker = False
    while time.time() < deadline:
        time.sleep(8)
        status, data = api.call("GET", f"/api/bot/conversation?project={urllib.parse.quote(project)}&limit=6", bearer=True)
        if status != 200:
            continue
        msgs = list(reversed(data.get("messages") or []))     # 응답은 최신순 → 시간순으로
        for i, m in enumerate(msgs):
            if marker in (m.get("text") or ""):
                seen_marker = True
                for later in msgs[i + 1:]:
                    answer = (later.get("text") or "").strip()
                    if later.get("type") == "bot" and answer and not answer.startswith("🔧"):
                        return answer
    raise MoveError(f"확인 턴의 답이 {timeout_min:g}분 안에 오지 않았습니다" + ("" if seen_marker else " (보낸 글도 대화에 안 보임)"))


# ─── 거울 (기기 → 본기기, 읽기 전용) ─────────────────────────────

def mirror_label(project: str) -> str:
    return MIRROR_LABEL_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "-", project)


def mirror_plist(project: str, config_dir: Path, interval: int, key_file: str | None) -> dict:
    args = ["/usr/bin/python3", os.path.abspath(__file__), "mirror", project, "--once", "--config-dir", str(config_dir)]
    if key_file:
        args += ["--api-key-file", key_file]
    log_path = str(config_dir / "moves" / f"{project}.mirror.log")
    return {
        "Label": mirror_label(project),
        "ProgramArguments": args,
        "StartInterval": int(interval),
        "RunAtLoad": False,
        # 등급을 Background · LowPriorityIO 로 두면 한 회차가 11배 느려진다(실측 33GB · 18만 파일: 24초 → 270초) — 기본 등급으로 둔다
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home())},
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


def mirror_once(api: Api, state: State, project: str) -> int:
    d = state.data
    if d.get("phase") != "placed" or not d.get("device"):
        log("거울: 이 프로젝트는 기기에 놓인 상태가 아닙니다 — 아무것도 하지 않습니다")
        return 0
    # 서버가 「지금도 그 기기에 있다」고 할 때만 당겨 온다. 본기기로 돌아온 폴더에 거울을 덮으면 작업이 지워진다.
    placement = api.placement(project)
    if placement.get("device") != d["device"]:
        log(f"거울: 서버의 배치가 {placement.get('device')!r} 입니다(기대 {d['device']!r}) — 건너뜁니다")
        return 0
    # 이 맥에서 따로 붙이는 디스크가 프로젝트 폴더 안에 있으면(예: branches/), 안 붙은 채로 당겨 와 내장 디스크를 채우지 않는다
    for mount in d.get("mirror_require_mounts") or []:
        if not os.path.ismount(mount) or not os.listdir(mount):
            log(f"거울: {mount} 이 붙어 있지 않습니다 — 건너뜁니다")
            (state.dir / f"{project}.mirror.json").write_text(json.dumps(
                {"at": datetime.now().isoformat(timespec="seconds"), "ok": False, "error": f"not mounted: {mount}"}, ensure_ascii=False), encoding="utf-8")
            return 1
    lock = open(state.dir / f"{project}.mirror.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("거울: 앞 회차가 아직 도는 중 — 건너뜁니다")
        return 0
    started = time.time()
    note = {"at": datetime.now().isoformat(timespec="seconds")}

    def stop(_signum, _frame):
        # back 이 거울을 내리면 launchd 가 SIGTERM 을 보낸다. 그대로 죽으면 rsync 가 고아로 남아
        # 본기기로 돌아온 폴더를 지울 수 있다 — SystemExit 로 바꿔 subprocess.run 이 자식을 죽이게 한다.
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, stop)
    try:
        os.makedirs(d["local_dir"], exist_ok=True)
        # 기기 폴더가 통째로 비었으면(디스크가 안 붙었거나 지워짐) --delete 가 이 맥 사본을 전부 지운다 — 그때는 당기지 않는다
        entries = Remote(d["ssh"]).run(f"ls -A {shlex.quote(d['device_dir'])} | wc -l", timeout=60).strip()
        if not entries.isdigit() or int(entries) == 0:
            raise MoveError(f"기기 폴더가 비어 있거나 읽히지 않습니다({entries!r}) — 거울을 덮지 않습니다")
        require_device_filled(Remote(d["ssh"]), d["local_dir"], d["device_dir"], d.get("mirror_require_mounts"))
        rsync(f"{d['ssh']}:{d['device_dir']}", d["local_dir"], d.get("excludes") or [], delete=True, timeout=3 * 3600)
        note.update(ok=True, seconds=round(time.time() - started, 1))
    except (MoveError, subprocess.TimeoutExpired) as e:
        note.update(ok=False, seconds=round(time.time() - started, 1), error=str(e)[:300])
        log(f"거울 실패: {e}")
    (state.dir / f"{project}.mirror.json").write_text(json.dumps(note, ensure_ascii=False), encoding="utf-8")
    return 0 if note.get("ok") else 1


def mirror_install(project: str, config_dir: Path, interval: int, key_file: str | None) -> None:
    label = mirror_label(project)
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    with open(path, "wb") as f:
        plistlib.dump(mirror_plist(project, config_dir, interval, key_file), f)
    proc = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise MoveError(f"거울 상주 작업을 올리지 못했습니다: {proc.stderr.strip()}")
    log(f"거울 상주 작업 {label} — {interval}초마다")


def mirror_uninstall(project: str) -> None:
    label = mirror_label(project)
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    if path.exists():
        path.unlink()
        log(f"거울 상주 작업 {label} 내림")


def mirror_wait_gone(state: State, project: str, timeout_s: int = 120) -> None:
    """내린 거울의 도는 회차가 완전히 끝났는지 잠금으로 확인한다(끝나기 전에 본기기로 돌리면 그 회차가 새 작업을 지울 수 있다)."""
    path = state.dir / f"{project}.mirror.lock"
    if not path.exists():
        return
    deadline = time.time() + timeout_s
    with open(path, "w") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
                return
            except OSError:
                if time.time() > deadline:
                    raise MoveError(f"거울 회차가 {timeout_s}초 안에 끝나지 않았습니다 — 잠시 뒤 다시 실행하세요")
                time.sleep(2)


# ─── 명령 ────────────────────────────────────────────────────────

class Ctx:
    def __init__(self, args):
        self.args = args
        self.project: str = args.project
        self.config_dir = Path(args.config_dir).expanduser()
        self.claude_dir = Path(args.claude_dir).expanduser()
        config = {}
        try:
            config = json.loads((self.config_dir / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        key = config.get("api_key", "")
        self.own_account = True
        if args.api_key_file:
            key_path = Path(args.api_key_file).expanduser()
            if key_path.stat().st_mode & 0o077:
                raise MoveError(f"{key_path} 의 권한이 넓습니다 — chmod 600")
            key = key_path.read_text(encoding="utf-8").strip()
            self.own_account = False       # 다른 계정: 이 맥 데몬의 대기 줄 · 세션 장부와 상관없다
        if not key:
            raise MoveError("api_key 를 찾지 못했습니다(config.json 또는 --api-key-file)")
        # 세션 장부: 자기 계정이면 이 맥 데몬의 것, 다른 계정이면 --sessions-file 을 줄 때만 옮긴다
        explicit = getattr(args, "sessions_file", None)
        self.sessions_path = Path(explicit).expanduser() if explicit else (self.config_dir / "sessions.json" if self.own_account else None)
        self.api = Api(args.api_url or config.get("api_url") or "https://www.peter-voice.site", key)
        self.state = State(self.config_dir, self.project)


def gather(ctx: Ctx, device_name: str, ssh_host: str | None, local_dir_arg: str | None) -> dict:
    project = ctx.api.project(ctx.project)
    device = ctx.api.device(device_name)
    remote = Remote(ssh_host or device_name)
    local_dir = (local_dir_arg or project.get("directory") or "").rstrip("/")
    if not local_dir or not os.path.isdir(local_dir):
        raise MoveError(f"이 맥에 프로젝트 폴더가 없습니다: {local_dir or '(폴더 칸이 비어 있음)'}")
    device_home = remote.home()
    device_dir = map_dir(local_dir, str(Path.home()), device_home)
    return {"project": project, "device": device, "remote": remote, "local_dir": local_dir, "device_dir": device_dir,
            "device_home": device_home, "branch_ids": ctx.api.branch_ids(ctx.project)}


PROMPT_PATH = re.compile(r"(?<![\w/.-])((?:~|\$HOME|/Users/[A-Za-z0-9_-]+|/opt/homebrew)/[A-Za-z0-9_./@+-]+)")


def prompt_paths(texts: list[str], project_dir: str, home: str) -> list[str]:
    """프롬프트가 가리키는 경로 중 이 맥에 실제로 있는 것(이 프로젝트 폴더 안은 뺀다 — 폴더째 옮겨 간다).
    담당은 Bash 말고도 Read · 스킬로 이 경로들을 연다 — 명령 기록만 재면 빠진다(11/26 game-develop: hand.py · 모델 기준 문서)."""
    root = project_dir.rstrip("/")
    found = set()
    for text in texts:
        for m in PROMPT_PATH.finditer(text):
            token = m.group(1).rstrip(".,)")
            if token.endswith("-"):
                continue                                   # 한글 앞에서 잘린 조각
            full = token.replace("$HOME", home, 1) if token.startswith("$HOME") else (home + token[1:] if token.startswith("~") else token)
            full = os.path.normpath(full)
            if full == root or full.startswith(root + "/") or full == home:
                continue
            if os.path.exists(full):
                found.add(full)
    return sorted(found)


def prompts_of(config_dir: Path, project: str, branch_ids: list[int]) -> list[str]:
    """이 맥 데몬이 만들어 둔 합친 프롬프트(공통 + 프로젝트 + 담당). 한 번도 안 돈 담당의 것은 없다."""
    names = [f"_combined_{project}_default.md", f"{project}.md"] + [f"_combined_branch_{b}_default.md" for b in branch_ids]
    texts = []
    for name in names:
        try:
            texts.append((config_dir / "prompts" / name).read_text(encoding="utf-8"))
        except OSError:
            pass
    return texts


def cmd_plan(ctx: Ctx) -> int:
    a = ctx.args
    g = gather(ctx, a.to, a.ssh, a.local_dir)
    remote: Remote = g["remote"]
    problems: list[str] = []
    notes: list[str] = []

    placement = ctx.api.placement(ctx.project)
    if placement.get("device"):
        problems.append(f"지금 배치가 본기기가 아닙니다: {placement['device']}")
    if not g["device"].get("online"):
        problems.append(f"기기 「{a.to}」가 꺼져 있습니다(마지막 연락 {g['device'].get('last_heartbeat')})")
    if g["project"].get("directory") and g["project"]["directory"].rstrip("/") != g["local_dir"]:
        notes.append(f"서버의 폴더 칸({g['project']['directory']})과 --local-dir 이 다릅니다 — 폴더 칸은 서버 값 기준으로 바꿔 씁니다")

    size_kb = du_kb(g["local_dir"])
    free_kb = int(remote.run(f"df -k {shlex.quote(os.path.dirname(g['device_dir']) or '/')} 2>/dev/null | tail -1 | awk '{{print $4}}' || echo 0; true",
                             check=False).strip() or 0)
    if free_kb == 0:
        free_kb = int(remote.run("df -k \"$HOME\" | tail -1 | awk '{print $4}'").strip() or 0)
    if free_kb < size_kb * 1.2:
        problems.append(f"기기 디스크가 모자랍니다: 필요 {size_kb / 1048576:.1f}GB × 1.2, 남은 {free_kb / 1048576:.1f}GB")
    existing = remote.run(f"ls -A {shlex.quote(g['device_dir'])} 2>/dev/null | head -3", check=False).strip()
    mine = ctx.state.data.get("device_dir") == g["device_dir"] and ctx.state.data.get("phase") in ("precopy", "precopied", "moving", "aborted")
    if existing and not mine and not a.adopt_existing:
        problems.append(f"기기에 대상 폴더가 이미 있고 비어 있지 않습니다: {g['device_dir']} (이어 쓰려면 --adopt-existing)")
    jobs = plists_using(g["local_dir"])
    if jobs:
        notes.append("이 맥에서 그 폴더를 쓰는 상주 작업(이동 뒤에는 거울 사본을 읽게 됩니다 — 쓰는 작업이면 같이 옮기거나 꺼야 합니다): " + ", ".join(jobs))
    local_user_home = str(Path.home())
    if local_user_home != g["device_home"] and remote.run(f"test -e {shlex.quote(local_user_home)} && echo y || echo n").strip() != "y":
        notes.append(f"기기에 {local_user_home} 경로가 없습니다 — 문서 · 스크립트에 박힌 절대 경로가 깨집니다. "
                     f"기기에서: sudo ln -s {g['device_home']} {local_user_home}")
    pointed = prompt_paths(prompts_of(ctx.config_dir, ctx.project, g["branch_ids"]), g["local_dir"], local_user_home)
    if pointed:
        there = [g["device_home"] + x[len(local_user_home):] if x.startswith(local_user_home + "/") else x for x in pointed]
        listing = " ".join(shlex.quote(x) for x in there)
        gone = set(remote.run(f"for p in {listing}; do [ -e \"$p\" ] || echo \"$p\"; done; true", check=False).split("\n"))
        missing = [x for x, y in zip(pointed, there) if y in gone]
        if missing:
            notes.append(f"프롬프트가 가리키는데 기기에 없는 경로 {len(missing)}개 — 읽기 전용 사본으로 보내거나 대역을 두고, 일부러 안 두는 것은 프롬프트에 적습니다: "
                         + ", ".join(missing[:25]) + (" …" if len(missing) > 25 else ""))
    sessions = {}
    if ctx.sessions_path:
        try:
            sessions = json.loads(ctx.sessions_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    keys = session_keys_for(sessions, ctx.project, g["branch_ids"])

    print(json.dumps({
        "project": ctx.project, "to": a.to, "ssh": remote.host,
        "local_dir": g["local_dir"], "device_dir": g["device_dir"],
        "size_gb": round(size_kb / 1048576, 2), "device_free_gb": round(free_kb / 1048576, 1),
        "branches": len(g["branch_ids"]), "sessions_to_carry": len(keys),
        "device_version": g["device"].get("version"), "problems": problems, "notes": notes,
    }, ensure_ascii=False, indent=2))
    return 1 if problems else 0


def cmd_move(ctx: Ctx) -> int:
    a = ctx.args
    if cmd_plan(ctx) != 0:
        raise MoveError("점검에서 걸린 것이 있어 옮기지 않습니다")
    if not a.yes:
        raise MoveError("실제로 옮기려면 --yes 를 붙이세요")
    g = gather(ctx, a.to, a.ssh, a.local_dir)
    remote: Remote = g["remote"]
    local_dir, device_dir, excludes = g["local_dir"], g["device_dir"], with_junk(a.exclude)
    server_dir = (g["project"].get("directory") or local_dir).rstrip("/")
    keys = {ctx.project} | {f"branch:{b}" for b in g["branch_ids"]}
    queue_dir = ctx.config_dir if ctx.own_account else None
    mounts = mounts_of(a.require_mount)
    require_mounted(mounts)
    before = ctx.state.data.get("excludes")
    if ctx.state.data.get("phase") == "precopied" and before is not None and sorted(before) != sorted(excludes):
        log(f"  ⚠️ 미리 복사 때와 제외 목록이 다릅니다({before} → {excludes}) — 그때 제외했던 것은 지금 복사되고, 지금만 제외한 것은 기기에 남습니다")

    ctx.state.save(project=ctx.project, device=a.to, ssh=remote.host, local_dir=local_dir, device_dir=device_dir,
                   server_dir=server_dir, excludes=excludes, phase="precopy", api_key_file=a.api_key_file, mirror_require_mounts=mounts)

    log(f"2/7 미리 복사 {local_dir} → {remote.host}:{device_dir}")
    remote.run(f"mkdir -p {shlex.quote(device_dir)}")
    rsync(local_dir, f"{remote.host}:{device_dir}", excludes, delete=False)
    ctx.state.save(phase="precopied")

    log("3/7 이동 중으로 표시 — 새 메시지는 기다립니다")
    status, data = ctx.api.place(ctx.project, MOVING)
    if status != 200:
        raise MoveError(f"이동 중으로 두지 못했습니다: {status} {data.get('error')} ({data.get('reason')})")
    ctx.state.save(phase="moving")
    try:
        wait_until_quiet(ctx.api, ctx.project, queue_dir, keys, a.idle_timeout)

        log("4/7 마지막 복사와 대조")
        require_mounted(mounts)
        rsync(local_dir, f"{remote.host}:{device_dir}", excludes, delete=True)
        try:
            counts = compare(local_dir, remote, device_dir, excludes)
        except MoveError as e:
            # 담당의 턴은 다 끝났어도 이 맥의 다른 작업(색인 갱신 등)이 그 사이 파일을 건드릴 수 있다 — 한 번만 다시
            log(f"  대조가 어긋남 — 한 번 더 복사하고 대조합니다 ({e})")
            rsync(local_dir, f"{remote.host}:{device_dir}", excludes, delete=True)
            counts = compare(local_dir, remote, device_dir, excludes)
        require_mounted(mounts)          # 복사 도중 디스크가 빠졌으면 「둘 다 비어서 같음」일 수 있다
        log(f"  같음 — 파일 {counts['files']} · 링크 {counts['links']} · 폴더 {counts['dirs']} · {counts['bytes'] / 1048576:.1f}MB")

        sessions = {"carried": [], "skipped": []}
        if ctx.sessions_path and not a.no_sessions:
            log("5/7 세션(대화 기록) 옮기기")
            sessions = carry_sessions_to_device(ctx.api, remote, a.to, ctx.sessions_path, ctx.claude_dir, ctx.project, g["branch_ids"],
                                                local_dir, device_dir, a.device_idle_wait)
            log(f"  이어지는 세션 {len(sessions['carried'])} · 새로 시작 {len(sessions['skipped'])}")
        else:
            log("5/7 세션 옮기기 건너뜀 — 기기에서는 서버의 인계 요약으로 새 세션을 엽니다")
        if not a.no_sessions:
            log(f"  기억 폴더 {carry_memory(remote, ctx.claude_dir, local_dir, device_dir, to_device=True)}개 옮김")

        log(f"6/7 배치 = {a.to}, 폴더 칸 {server_dir} → {device_dir}")
        placed = place_final(ctx.api, ctx.project, a.to, {"from": server_dir, "to": device_dir})
    except BaseException as e:
        log(f"실패 — 배치를 본기기로 되돌립니다: {e}")
        try:
            place_final(ctx.api, ctx.project, None, None)
            ctx.state.save(phase="aborted", error=str(e)[:300])
            log("본기기로 되돌렸습니다(이 맥 폴더는 손대지 않았습니다)")
        except MoveError as e2:
            log(f"🔴 되돌리기도 실패 — 배치가 「이동 중」에 남아 있을 수 있습니다: {e2}")
        raise
    ctx.state.save(phase="placed", moved_at=datetime.now().isoformat(timespec="seconds"), files=counts["files"],
                   sessions_carried=sessions["carried"], sessions_fresh=sessions["skipped"],
                   directories_rewritten=placed.get("directories_rewritten"))
    log(f"  폴더 칸 {placed.get('directories_rewritten')}개 바꿈")

    verified = True
    if a.verify:
        log("7/7 확인 턴")
        try:
            log("  답: " + verify_turn(ctx.api, ctx.project).replace("\n", " ")[:300])
        except MoveError as e:
            verified = False
            log(f"  ⚠️ 확인 턴 실패 — 배치는 이미 {a.to} 입니다. 대화창에서 직접 확인하고, 이상하면 back 으로 되돌리세요: {e}")
    else:
        log("7/7 확인 턴 건너뜀(--verify 없음)")
    if a.mirror:
        ctx.state.save(mirror_interval=a.mirror_interval)
        mirror_install(ctx.project, ctx.config_dir, a.mirror_interval, a.api_key_file)
    log("끝 — 되돌리려면: pv-project-move.py back " + ctx.project)
    return 0 if verified else 3


def cmd_precopy(ctx: Ctx) -> int:
    """미리 복사만 한다(배치는 그대로 — 프로젝트는 본기기에서 계속 돈다). 큰 프로젝트는 이것을 먼저 돌려 두면 move 의 멈춤 시간이 짧다."""
    a = ctx.args
    if cmd_plan(ctx) != 0:
        raise MoveError("점검에서 걸린 것이 있어 복사하지 않습니다")
    g = gather(ctx, a.to, a.ssh, a.local_dir)
    remote: Remote = g["remote"]
    local_dir, device_dir, excludes = g["local_dir"], g["device_dir"], with_junk(a.exclude)
    server_dir = (g["project"].get("directory") or local_dir).rstrip("/")
    mounts = mounts_of(a.require_mount)
    require_mounted(mounts)
    ctx.state.save(project=ctx.project, device=a.to, ssh=remote.host, local_dir=local_dir, device_dir=device_dir,
                   server_dir=server_dir, excludes=excludes, phase="precopy", api_key_file=a.api_key_file, mirror_require_mounts=mounts)
    log(f"미리 복사 {local_dir} → {remote.host}:{device_dir}")
    remote.run(f"mkdir -p {shlex.quote(device_dir)}")
    rsync(local_dir, f"{remote.host}:{device_dir}", excludes, delete=False)
    ctx.state.save(phase="precopied", precopied_at=datetime.now().isoformat(timespec="seconds"))
    log("미리 복사 끝 — 이어서 move (같은 --exclude 로)")
    return 0


def cmd_back(ctx: Ctx) -> int:
    a = ctx.args
    d = ctx.state.data
    if not d.get("device") or d.get("phase") not in ("placed", "returning"):
        raise MoveError("이 도구로 옮긴 기록이 없습니다(상태 파일 phase 가 placed 가 아님)")
    if not a.yes:
        raise MoveError("실제로 되돌리려면 --yes 를 붙이세요")
    remote = Remote(d["ssh"])
    local_dir, device_dir, excludes = d["local_dir"], d["device_dir"], d.get("excludes") or []
    server_dir = d.get("server_dir") or local_dir
    branch_ids = ctx.api.branch_ids(ctx.project)

    mirror_uninstall(ctx.project)
    mirror_wait_gone(ctx.state, ctx.project)
    log("1/5 이동 중으로 표시")
    status, data = ctx.api.place(ctx.project, MOVING)
    if status != 200:
        raise MoveError(f"이동 중으로 두지 못했습니다: {status} {data.get('error')} ({data.get('reason')})")
    ctx.state.save(phase="returning")
    sessions = {"continued": [], "fresh_on_primary": []}
    try:
        # 기기가 죽었으면 서버는 그 기기의 「작업 중」을 연락이 끊긴 지 150초 뒤부터 세지 않는다 — 그때까지 여기서 기다린다
        wait_until_quiet(ctx.api, ctx.project, None, set(), a.idle_timeout)
        if a.device_dead:
            log("2/5 기기가 죽었다고 보고 복사를 건너뜁니다 — 이 맥의 마지막 거울 상태로 이어 갑니다")
        else:
            log(f"2/5 가져오기 {remote.host}:{device_dir} → {local_dir}")
            os.makedirs(local_dir, exist_ok=True)
            require_mounted(d.get("mirror_require_mounts"))
            require_device_filled(remote, local_dir, device_dir, d.get("mirror_require_mounts"))
            rsync(f"{remote.host}:{device_dir}", local_dir, excludes, delete=True)
            counts = compare(local_dir, remote, device_dir, excludes)
            log(f"  같음 — 파일 {counts['files']} · {counts['bytes'] / 1048576:.1f}MB")
            if ctx.sessions_path and not a.no_sessions:
                log("3/5 세션(대화 기록) 가져오기")
                sessions = carry_sessions_back(remote, ctx.sessions_path, ctx.claude_dir, ctx.project, branch_ids, local_dir, device_dir)
                log(f"  이어지는 세션 {len(sessions['continued'])} · 이 맥에서 새로 시작 {len(sessions['fresh_on_primary'])}")
            if not a.no_sessions:
                try:
                    log(f"  기억 폴더 {carry_memory(remote, ctx.claude_dir, local_dir, device_dir, to_device=False)}개 가져옴")
                except MoveError as e:
                    log(f"  ⚠️ 기억 폴더를 가져오지 못했습니다(기기에는 남아 있습니다 — 손으로 가져오세요): {e}")

        log(f"4/5 배치 = 본기기, 폴더 칸 {device_dir} → {server_dir}")
        placed = place_final(ctx.api, ctx.project, None, {"from": device_dir, "to": server_dir})
    except BaseException as e:
        log(f"실패 — 배치를 기기({d['device']})로 되돌립니다: {e}")
        try:
            place_final(ctx.api, ctx.project, d["device"], None)
            ctx.state.save(phase="placed", error=str(e)[:300])
            log("기기로 되돌렸습니다(기기 폴더는 손대지 않았습니다)")
            if d.get("mirror_interval"):
                mirror_install(ctx.project, ctx.config_dir, int(d["mirror_interval"]), d.get("api_key_file"))
        except MoveError as e2:
            log(f"🔴 되돌리기도 실패 — 배치가 「이동 중」에 남아 있을 수 있습니다: {e2}")
        raise
    ctx.state.save(phase="returned", returned_at=datetime.now().isoformat(timespec="seconds"),
                   directories_rewritten=placed.get("directories_rewritten"), sessions_back=sessions, mirror_interval=None)
    log(f"  폴더 칸 {placed.get('directories_rewritten')}개 바꿈")
    verified = True
    if a.verify:
        log("5/5 확인 턴")
        try:
            log("  답: " + verify_turn(ctx.api, ctx.project).replace("\n", " ")[:300])
        except MoveError as e:
            verified = False
            log(f"  ⚠️ 확인 턴 실패 — 배치는 이미 본기기입니다. 대화창에서 직접 확인하세요: {e}")
    else:
        log("5/5 확인 턴 건너뜀(--verify 없음)")
    log(f"끝 — 기기의 사본({remote.host}:{device_dir})은 지우지 않았습니다")
    return 0 if verified else 3


def cmd_mirror(ctx: Ctx) -> int:
    a = ctx.args
    if a.install:
        if ctx.state.data.get("phase") != "placed":
            raise MoveError("기기에 놓인 프로젝트가 아닙니다(상태 파일 phase 가 placed 가 아님)")
        if a.require_mount is not None:
            ctx.state.save(mirror_require_mounts=[os.path.abspath(os.path.expanduser(m)) for m in a.require_mount])
        mirror_install(ctx.project, ctx.config_dir, a.mirror_interval, a.api_key_file or ctx.state.data.get("api_key_file"))
        ctx.state.save(mirror_interval=a.mirror_interval)
        return 0
    if a.uninstall:
        mirror_uninstall(ctx.project)
        ctx.state.save(mirror_interval=None)
        return 0
    return mirror_once(ctx.api, ctx.state, ctx.project)


def cmd_status(ctx: Ctx) -> int:
    out = {"placement": ctx.api.placement(ctx.project), "state": ctx.state.data}
    note = ctx.state.dir / f"{ctx.project}.mirror.json"
    if note.exists():
        out["mirror_last"] = json.loads(note.read_text(encoding="utf-8"))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("project")
    common.add_argument("--config-dir", default="~/.claude-daemon")
    common.add_argument("--claude-dir", default="~/.claude")
    common.add_argument("--api-url", default=None)
    common.add_argument("--api-key-file", default=None, help="다른 계정의 유저 열쇠가 든 파일(권한 600)")
    common.add_argument("--sessions-file", default=None, help="세션 장부(기본: 자기 계정이면 <config-dir>/sessions.json, 다른 계정이면 옮기지 않음)")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("plan", "precopy", "move"):
        p = sub.add_parser(name, parents=[common])
        p.add_argument("--to", required=True, help="기기 이름")
        p.add_argument("--ssh", default=None, help="기기의 ssh 이름(기본: 기기 이름)")
        p.add_argument("--local-dir", default=None, help="이 맥의 프로젝트 폴더(기본: 서버의 폴더 칸)")
        p.add_argument("--adopt-existing", action="store_true")
        if name == "precopy":
            p.add_argument("--exclude", action="append", help="복사하지 않을 파일 · 폴더 이름(여러 번)")
            p.add_argument("--require-mount", action="append", help="이 맥에 붙어 있어야 하는 마운트 지점(여러 번)")
        if name == "move":
            p.add_argument("--yes", action="store_true")
            p.add_argument("--exclude", action="append", help="복사하지 않을 파일 · 폴더 이름(여러 번)")
            p.add_argument("--require-mount", action="append", help="복사 · 거울 · 되돌리기 전에 이 맥에 붙어 있어야 하는 마운트 지점(여러 번)")
            p.add_argument("--no-sessions", action="store_true")
            p.add_argument("--idle-timeout", type=float, default=45, help="돌던 턴이 끝나기를 기다릴 분")
            p.add_argument("--device-idle-wait", type=float, default=10, help="세션 장부를 적으려고 기기가 쉬기를 기다릴 분")
            p.add_argument("--verify", action="store_true")
            p.add_argument("--mirror", action="store_true")
            p.add_argument("--mirror-interval", type=int, default=300)
    p = sub.add_parser("back", parents=[common])
    p.add_argument("--yes", action="store_true")
    p.add_argument("--device-dead", action="store_true")
    p.add_argument("--no-sessions", action="store_true")
    p.add_argument("--idle-timeout", type=float, default=45)
    p.add_argument("--verify", action="store_true")
    p = sub.add_parser("mirror", parents=[common])
    p.add_argument("--once", action="store_true")
    p.add_argument("--install", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--mirror-interval", type=int, default=300)
    p.add_argument("--require-mount", action="append", help="거울을 받기 전에 붙어 있어야 하는 마운트 지점(여러 번)")
    sub.add_parser("status", parents=[common])

    args = parser.parse_args(argv)
    try:
        ctx = Ctx(args)
        return {"plan": cmd_plan, "precopy": cmd_precopy, "move": cmd_move, "back": cmd_back, "mirror": cmd_mirror,
                "status": cmd_status}[args.command](ctx)
    except MoveError as e:
        log(f"중단: {e}")
        return 2
    except subprocess.TimeoutExpired as e:
        log(f"중단: 시간 초과 — {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
