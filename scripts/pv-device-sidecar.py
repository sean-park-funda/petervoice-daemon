#!/usr/bin/env python3
"""기기 곁다리 동기화 — 프로젝트를 다른 맥(기기)으로 옮긴 뒤, 그 프로젝트가 본기기의 다른 폴더에 기대던 것을 이어 준다 (2026-11-26, 한 계정 · 여러 기기 단계 E).

본기기에서 launchd 가 짧은 간격(기본 15초)으로 한 회차씩 돌린다. 접속은 본기기 → 기기 한 방향(ssh)뿐이다.

  push  본기기의 폴더를 기기의 같은 자리(홈 기준 같은 경로)에 읽기 전용 사본으로 둔다 (--delete — 기기에서 고친 것은 되돌아간다)
  pull  기기의 폴더를 본기기의 같은 자리로 가져온다 (지우지 않는다 · 본기기 쪽이 더 새 파일이면 덮지 않는다)
        가져온 것이 있으면 on_change 명령을 본기기에서 돌린다 (예: 사이트 재빌드) — 따로 떼어 돌리므로 그동안에도 가져오기는 계속된다.
        도는 중에 또 가져오면 끝난 뒤 한 번 더 돈다(겹쳐 돌지 않는다)
  exec  기기가 맡긴 명령을 본기기에서 대신 돌린다 — 본기기에만 있는 열쇠 · 상태 파일을 쓰는 스크립트용(열쇠를 기기에 두지 않는다).
        기기의 <spool>/req/<id>/{args.json, files/, ready} 를 가져와, 첫 인자가 allow 에 있는 것만 command 뒤에 붙여 돌리고
        결과를 <spool>/res/<id>.json 으로 돌려준다. 인자 「@@file:N」은 files/N-… 으로 바뀐다. 같은 id 는 한 번만 돈다

설정: <config-dir>/sidecar/<기기>.json
  { "ssh": "gamemac", "device_home": "/Users/vibegame", "interval": 15,
    "push": [ {"path": "~/.claude-daemon/projects/ox-plan/ops", "every": 20, "filter": ["+ *.py", "- *"]} ],
    "pull": [ {"path": "~/Projects/_general/temp-hub/public", "every": 1, "on_change": ["python3", "…", "rebuild", "temp"]} ],
    "exec": [ {"spool": "~/.claude-daemon/spool/games-ops", "command": ["python3", "…/games-ops.py"], "allow": ["progress", "status"]} ] }
  every = 몇 회차마다(1 = 매 회차). filter = rsync 필터 규칙(순서대로).
  background = true 면 그 항목은 따로 떼어 돌린다(큰 폴더가 빠른 항목을 막지 않게 — launchd 는 같은 작업을 겹쳐 띄우지 않는다).
  가져오기(pull)가 보내기(push)보다 먼저 돈다.

    pv-device-sidecar.py run <기기>            # 한 회차 (launchd 가 부른다)
    pv-device-sidecar.py run <기기> --all      # every 와 상관없이 전부 한 번 (처음 깔 때)
    pv-device-sidecar.py install <기기>        # launchd 상주 작업 올리기
    pv-device-sidecar.py uninstall <기기>
    pv-device-sidecar.py status <기기>
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LABEL_PREFIX = "com.petervoice.sidecar."


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def ssh_opts(control_dir: Path) -> list[str]:
    # 회차마다 손잡기를 다시 하지 않게 연결을 붙여 둔다
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15", "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control_dir}/cm-%C", "-o", "ControlPersist=120"]


def device_path(path: str, device_home: str) -> str:
    """본기기의 홈 기준 경로 → 기기의 같은 자리."""
    home = str(Path.home())
    full = os.path.abspath(os.path.expanduser(path))
    if not full.startswith(home + "/"):
        raise ValueError(f"홈 아래의 경로만 다룹니다(홈 자체 · 홈 밖은 안 됨): {path}")
    return device_home.rstrip("/") + full[len(home):]


def due(entry: dict, tick: int, run_all: bool) -> bool:
    every = max(1, int(entry.get("every", 1)))
    return run_all or tick % every == 0


def rsync(args: list[str], timeout: int) -> tuple[int, list[str], str]:
    proc = subprocess.run(["rsync", "-a", "--itemize-changes", "--timeout=120"] + args, capture_output=True, text=True, timeout=timeout)
    changed = [line for line in proc.stdout.splitlines() if line and not line.startswith("cd") and not line.startswith(".d")]
    return proc.returncode, changed, proc.stderr.strip()[-300:]


def push_one(entry: dict, ssh: str, device_home: str, opts: list[str]) -> tuple[int, int, str]:
    """보내기 한 항목. (rsync 종료 코드, 바뀐 개수, 오류 글) — 없는 경로는 (0, 0, "")."""
    src = os.path.abspath(os.path.expanduser(entry["path"]))
    if not os.path.exists(src):
        return 0, 0, ""
    dst = device_path(entry["path"], device_home)
    is_dir = os.path.isdir(src)
    if is_dir and not os.listdir(src):
        return 0, 0, ""          # 빈 원본(디스크가 안 붙었을 수 있다)으로 기기 사본을 비우지 않는다
    subprocess.run(["ssh"] + opts + [ssh, "mkdir", "-p", dst if is_dir else os.path.dirname(dst)], capture_output=True, timeout=30)
    args = ["-e", "ssh " + " ".join(opts)]
    if is_dir:
        args.append("--delete")
    for rule in entry.get("filter") or []:
        args.append(f"--filter={rule}")
    args += [src + ("/" if is_dir else ""), f"{ssh}:{dst}" + ("/" if is_dir else "")]
    rc, changed, err = rsync(args, int(entry.get("timeout", 3600)))
    return rc, len(changed), err


def run_background(conf: dict, state_dir: Path, name: str, index: int) -> int:
    """background 항목 하나를 자기 잠금 아래에서 돌린다(앞 회차가 아직 돌면 건너뛴다)."""
    entry = (conf.get("push") or [])[index]
    lock = open(state_dir / f"{name}.bg{index}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0

    def stop(_signum, _frame):
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, stop)
    rc, changed, err = push_one(entry, conf["ssh"], conf["device_home"], ssh_opts(state_dir))
    if rc not in (0, 24):
        log(f"오류 push {entry['path']}: {rc} {err}")
        return 1
    if changed:
        log(f"보냄 {entry['path']} — {changed}개")
    return 0


def spawn(state_dir: Path, name: str, flag: str, index: int) -> None:
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", name, "--config-dir", str(state_dir.parent), flag, str(index)],
                     stdin=subprocess.DEVNULL, stdout=open(state_dir / f"{name}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)


def run_hook(conf: dict, state_dir: Path, name: str, index: int) -> int:
    """가져오기 뒤처리(on_change) — 표시(dirty)가 남아 있는 동안 되풀이한다. 이미 도는 중이면 그쪽이 표시를 보고 한 번 더 돈다."""
    entry = (conf.get("pull") or [])[index]
    command = entry.get("on_change")
    dirty = state_dir / f"{name}.pull{index}.dirty"
    lock = open(state_dir / f"{name}.hook{index}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    if not command:
        dirty.unlink(missing_ok=True)
        return 0
    code, failures = 0, 0
    while dirty.exists():
        dirty.unlink()
        started = time.time()
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=int(entry.get("on_change_timeout", 900)))
            ok, why = proc.returncode == 0, f"{proc.returncode} {(proc.stderr or proc.stdout).strip()[-200:]}"
        except subprocess.TimeoutExpired:
            ok, why = False, "시간 초과"
        log(f"  뒤처리 {os.path.basename(command[0])} … {'됨' if ok else '실패'} {time.time() - started:.0f}초")
        if not ok:
            failures += 1
            code = 1
            log(f"오류 on_change {entry['path']}: {why}")
            if failures >= 3:
                break            # 세 번 실패하면 놓는다 — 다음 가져오기가 다시 건다
            dirty.write_text(str(time.time()))
            time.sleep(30)
    return code


SPOOL_ID = re.compile(r"[A-Za-z0-9_-]{6,80}")


def spool_execute(entry: dict, req: Path) -> dict:
    try:
        args = json.loads((req / "args.json").read_text(encoding="utf-8"))["args"]
        assert isinstance(args, list) and args and all(isinstance(x, str) for x in args)
    except (OSError, ValueError, KeyError, AssertionError):
        return {"rc": 2, "stdout": "", "stderr": "요청을 읽지 못했습니다"}
    if args[0] not in (entry.get("allow") or []):
        return {"rc": 2, "stdout": "", "stderr": f"이 통로로는 돌릴 수 없는 명령입니다: {args[0]}"}
    resolved = []
    for x in args:
        m = re.fullmatch(r"@@file:(\d+)", x)
        if m:
            found = sorted((req / "files").glob(m.group(1) + "-*"))
            if not found:
                return {"rc": 2, "stdout": "", "stderr": f"첨부 파일이 오지 않았습니다: {x}"}
            x = str(found[0])
        resolved.append(x)
    try:
        proc = subprocess.run(list(entry["command"]) + resolved, capture_output=True, text=True, timeout=int(entry.get("timeout", 300)), cwd=str(req))
        return {"rc": proc.returncode, "stdout": proc.stdout[-200000:], "stderr": proc.stderr[-20000:]}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "stdout": "", "stderr": "본기기에서 시간 초과"}
    except OSError as e:
        return {"rc": 127, "stdout": "", "stderr": f"본기기에서 실행하지 못했습니다: {e}"}


def run_spool(entry: dict, ssh: str, device_home: str, opts: list[str]) -> None:
    rsh = "ssh " + " ".join(opts)
    local = Path(os.path.abspath(os.path.expanduser(entry["spool"])))
    remote = device_path(entry["spool"], device_home)
    for sub in ("req", "res", "done"):
        (local / sub).mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh"] + opts + [ssh, "mkdir", "-p", remote + "/req", remote + "/res"], capture_output=True, timeout=30)
    rc, _changed, err = rsync(["-e", rsh, f"{ssh}:{remote}/req/", str(local / "req") + "/"], 600)
    if rc not in (0, 24):
        raise RuntimeError(f"요청 가져오기 {rc} {err}")
    now = time.time()
    for req in sorted((local / "req").iterdir()):
        if not req.is_dir():
            continue
        rid = req.name
        if not SPOOL_ID.fullmatch(rid) or (local / "done" / rid).exists():
            shutil.rmtree(req, ignore_errors=True)          # 이미 돌린 요청이 다시 딸려 온 것 — 두 번 돌리지 않는다
            continue
        if not (req / "ready").exists():
            if now - req.stat().st_mtime > 3600:
                shutil.rmtree(req, ignore_errors=True)      # 쓰다 만 요청
            continue
        started = time.time()
        (local / "done" / rid).write_text(str(started))     # 먼저 표시 — 돌리다 죽어도 같은 요청을 다시 돌리지 않는다
        result = spool_execute(entry, req)
        tmp = local / "res" / f"{rid}.tmp"
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        tmp.rename(local / "res" / f"{rid}.json")
        shutil.rmtree(req, ignore_errors=True)
        log(f"대신 돌림 {os.path.basename(entry['command'][-1])} {rid} … rc={result['rc']} {time.time() - started:.0f}초")
    if any((local / "res").glob("*.json")):
        rc, _changed, err = rsync(["-e", rsh, "--remove-source-files", "--exclude=*.tmp", str(local / "res") + "/", f"{ssh}:{remote}/res/"], 600)
        if rc not in (0, 24):
            raise RuntimeError(f"결과 보내기 {rc} {err}")
    for mark in (local / "done").iterdir():
        if now - mark.stat().st_mtime > 2 * 86400:
            mark.unlink(missing_ok=True)


def run_once(conf: dict, state_dir: Path, name: str, run_all: bool) -> int:
    ssh, device_home = conf["ssh"], conf["device_home"]
    opts = ssh_opts(state_dir)
    rsh = "ssh " + " ".join(opts)
    tick_path = state_dir / f"{name}.tick"
    try:
        tick = int(tick_path.read_text().strip()) + 1
    except (OSError, ValueError):
        tick = 0
    tick_path.write_text(str(tick))

    alive = subprocess.run(["ssh"] + opts + [ssh, "true"], capture_output=True, timeout=30).returncode == 0
    note = {"at": datetime.now().isoformat(timespec="seconds"), "tick": tick, "alive": alive, "errors": []}
    if not alive:
        (state_dir / f"{name}.last.json").write_text(json.dumps(note, ensure_ascii=False))
        return 0          # 기기가 꺼져 있으면 조용히 넘어간다 — 켜지면 다음 회차가 잇는다

    for entry in conf.get("exec") or []:
        if not due(entry, tick, run_all):
            continue
        try:
            run_spool(entry, ssh, device_home, opts)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
            note["errors"].append(f"exec {entry.get('spool')}: {e}")

    for index, entry in enumerate(conf.get("pull") or []):
        dirty = state_dir / f"{name}.pull{index}.dirty"
        if dirty.exists():
            spawn(state_dir, name, "--hook", index)          # 앞 회차가 남긴 뒤처리(도는 중이면 저쪽이 알아서 건너뛴다)
        if not due(entry, tick, run_all):
            continue
        dst = os.path.abspath(os.path.expanduser(entry["path"]))
        try:
            src = device_path(entry["path"], device_home)
        except ValueError as e:
            note["errors"].append(str(e))
            continue
        if subprocess.run(["ssh"] + opts + [ssh, "test", "-d", src], capture_output=True, timeout=30).returncode != 0:
            continue
        os.makedirs(dst, exist_ok=True)
        args = ["-e", rsh, "--update"]            # 지우지 않는다 · 본기기 쪽이 더 새 파일이면 덮지 않는다
        for rule in entry.get("filter") or []:
            args.append(f"--filter={rule}")
        args += [f"{ssh}:{src}/", dst + "/"]
        rc, changed, err = rsync(args, int(entry.get("timeout", 3600)))
        if rc not in (0, 24):
            note["errors"].append(f"pull {entry['path']}: {rc} {err}")
            continue
        if changed:
            log(f"가져옴 {entry['path']} — {len(changed)}개")
            if entry.get("on_change"):
                dirty.write_text(str(time.time()))
                spawn(state_dir, name, "--hook", index)

    for index, entry in enumerate(conf.get("push") or []):
        if not due(entry, tick, run_all):
            continue
        if entry.get("background"):
            spawn(state_dir, name, "--only", index)
            continue
        try:
            rc, changed, err = push_one(entry, ssh, device_home, opts)
        except (ValueError, subprocess.TimeoutExpired) as e:
            note["errors"].append(f"push {entry['path']}: {e}")
            continue
        if rc not in (0, 24):
            note["errors"].append(f"push {entry['path']}: {rc} {err}")
        elif changed:
            log(f"보냄 {entry['path']} — {changed}개")

    for e in note["errors"]:
        log("오류 " + e)
    (state_dir / f"{name}.last.json").write_text(json.dumps(note, ensure_ascii=False))
    return 1 if note["errors"] else 0


def label_of(name: str) -> str:
    return LABEL_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "-", name)


def install(name: str, config_dir: Path, interval: int) -> None:
    label = label_of(name)
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    logf = str(config_dir / "sidecar" / f"{name}.log")
    plist = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/python3", os.path.abspath(__file__), "run", name, "--config-dir", str(config_dir)],
        "StartInterval": int(interval), "RunAtLoad": True, "ProcessType": "Background", "LowPriorityIO": True,
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home())},
        "StandardOutPath": logf, "StandardErrorPath": logf,
    }
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    proc = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"상주 작업을 올리지 못했습니다: {proc.stderr.strip()}")
    log(f"상주 작업 {label} — {interval}초마다")


def uninstall(name: str) -> None:
    label = label_of(name)
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    if path.exists():
        path.unlink()
    log(f"상주 작업 {label} 내림")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "install", "uninstall", "status"])
    parser.add_argument("device")
    parser.add_argument("--config-dir", default="~/.claude-daemon")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--only", type=int, default=None, help="background 항목 하나만(내부용)")
    parser.add_argument("--hook", type=int, default=None, help="가져오기 뒤처리 하나만(내부용)")
    args = parser.parse_args(argv)
    config_dir = Path(args.config_dir).expanduser()
    state_dir = config_dir / "sidecar"
    state_dir.mkdir(parents=True, exist_ok=True)
    conf_path = state_dir / f"{args.device}.json"

    if args.command == "uninstall":
        uninstall(args.device)
        return 0
    try:
        conf = json.loads(conf_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log(f"설정을 읽지 못했습니다({conf_path}): {e}")
        return 2
    if args.command == "install":
        install(args.device, config_dir, int(conf.get("interval", 15)))
        return 0
    if args.command == "status":
        try:
            print((state_dir / f"{args.device}.last.json").read_text())
        except OSError:
            print("{}")
        return 0

    if args.only is not None:
        return run_background(conf, state_dir, args.device, args.only)
    if args.hook is not None:
        return run_hook(conf, state_dir, args.device, args.hook)

    lock = open(state_dir / f"{args.device}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0          # 앞 회차가 아직 도는 중(큰 폴더를 처음 보낼 때) — 건너뛴다

    def stop(_signum, _frame):
        raise SystemExit(143)      # 내려질 때 도는 rsync 를 같이 끝낸다
    signal.signal(signal.SIGTERM, stop)
    try:
        return run_once(conf, state_dir, args.device, args.all)
    except subprocess.TimeoutExpired as e:
        log(f"시간 초과: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
