#!/usr/bin/env python3
"""PeterVoice Cloud — cron 러너 (배치 실행기).

계약서: peter-voice/docs/ops/cron-runner-contract.md

- **단일 프로세스**(systemd service 1개). 배치마다 timer 를 만들지 않는다 —
  systemd timer 는 유닛끼리 독립이라 전역 동시 실행 상한을 걸 수 없다.
- 선언 파일: `<workspace>/<project>/cron/*.json` (+ 표시용 `<project>/.ax/workflow.json`)
- 실행: 유저 컨테이너 안에서(podman exec). **클로드 턴이 아니다** — 토큰 소비 0.
- 결과 **원본은 워크스페이스**(`<project>/.ax/runs/<날짜>/`), 웹 push 는 캐시.
  push 가 실패해도 원본이 남아야 인계 가능성이 지켜진다.
- `config.cron.enabled` 가 없거나 false 면 아무것도 하지 않는다(공용 호스트 기본값).

설정 (/etc/pv-cloud/config.json):
  "cron": {"enabled": true, "max_concurrent": 1, "scan_interval_sec": 30,
           "default_timeout_sec": 1800, "run_retention_days": 30, "push": true}
"""

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import cloud_container as ctr

CONFIG_PATH = Path(os.environ.get("PV_CLOUD_CONFIG", "/etc/pv-cloud/config.json"))
KST = ZoneInfo("Asia/Seoul")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s")
logger = logging.getLogger("pv-cron")

config: dict = {}
shutdown = threading.Event()

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SUMMARY_RE = re.compile(r"^::summary::\s*(.+?)\s*$", re.M)
RUNDIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

LOG_TAIL_BYTES = 4096
LOG_FILE_MAX = 2 * 1024 * 1024   # 워크스페이스 원본 로그 상한 (디스크 쿼터 보호)
TICK_SEC = 15                    # 분 경계를 놓치지 않을 만큼 촘촘하게


def cron_cfg() -> dict:
    return config.get("cron", {}) or {}


# ── HTTP ─────────────────────────────────────────────────────────

def _http(method: str, path: str, headers: dict, body: dict | None = None,
          timeout: int = 30) -> tuple[int, dict | None]:
    url = config["api_url"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode()
            try:
                return res.status, json.loads(raw)
            except ValueError:
                return res.status, None
    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP {e.code} {method} {path}: {e.read()[:200]}")
        return e.code, None
    except Exception as e:
        logger.warning(f"HTTP error {method} {path}: {e}")
        return 0, None


def host_api(method: str, path: str, body: dict | None = None):
    return _http(method, path, {"X-Host-Key": config["host_key"]}, body)


def user_api(api_key: str, method: str, path: str, body: dict | None = None):
    return _http(method, path, {"Authorization": f"Bearer {api_key}",
                                "X-Api-Key": api_key}, body)


# ── 로스터 / 시크릿 ──────────────────────────────────────────────

class Roster:
    """이 호스트가 담당하는 유저만 돌려준다 (서버가 users.cloud_host 로 걸러 준다)."""

    def __init__(self):
        self._users: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._last = 0.0

    def sync(self, force: bool = False):
        now = time.time()
        if not force and now - self._last < 60:
            return
        _, result = host_api("GET", "/api/cloud/roster")
        if result and "users" in result:
            with self._lock:
                self._users = {u["userId"]: u for u in result["users"]}
                self._last = now

    def users(self) -> list[dict]:
        with self._lock:
            return list(self._users.values())

    def api_key(self, user_id: int) -> str | None:
        with self._lock:
            u = self._users.get(user_id)
        return (u or {}).get("apiKey")


roster = Roster()

_secrets_cache: dict[int, tuple[float, dict]] = {}
_secrets_lock = threading.Lock()
SECRETS_TTL_SEC = 300


def user_secrets(user_id: int) -> dict:
    """배치도 외부 API 키가 필요하다 — 대화 턴과 같은 시크릿을 env 로 넣어 준다."""
    now = time.time()
    with _secrets_lock:
        cached = _secrets_cache.get(user_id)
        if cached and now - cached[0] < SECRETS_TTL_SEC:
            return cached[1]
    key = roster.api_key(user_id)
    if not key:
        return {}
    _, result = user_api(key, "GET", "/api/secrets?raw=true")
    if result is None:
        return cached[1] if cached else {}
    out = {}
    for s in result.get("secrets", []):
        k = (s.get("key") or "").strip()
        v = (s.get("value") or "").strip()
        if k and v and ENV_NAME_RE.match(k):
            out[k] = v
    with _secrets_lock:
        _secrets_cache[user_id] = (now, out)
    return out


# ── crontab 파서 (5필드, TZ=Asia/Seoul 고정) ─────────────────────

_ALIASES = {
    "@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split())}
_DOWS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}


def _num(tok: str, names: dict | None) -> int:
    tok = tok.strip().lower()
    if names and tok in names:
        return names[tok]
    return int(tok)


def _parse_field(spec: str, lo: int, hi: int, names: dict | None = None) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"빈 필드: {spec!r}")
        step = 1
        if "/" in part:
            part, s = part.rsplit("/", 1)
            step = int(s)
            if step <= 0:
                raise ValueError(f"잘못된 step: {spec!r}")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = _num(a_s, names), _num(b_s, names)
        else:
            a = _num(part, names)
            b = hi if step != 1 else a   # crontab 관례: "5/15" = 5,20,35,50
        if a < lo or b > hi or a > b:
            raise ValueError(f"범위 밖: {spec!r} (허용 {lo}-{hi})")
        out.update(range(a, b + 1, step))
    if not out:
        raise ValueError(f"매칭 없는 필드: {spec!r}")
    return out


class CronSpec:
    def __init__(self, expr: str):
        raw = (expr or "").strip()
        raw = _ALIASES.get(raw.lower(), raw)
        f = raw.split()
        if len(f) != 5:
            raise ValueError(f"crontab 5필드가 아님: {expr!r}")
        self.expr = raw
        self.minute = _parse_field(f[0], 0, 59)
        self.hour = _parse_field(f[1], 0, 23)
        self.dom = _parse_field(f[2], 1, 31)
        self.month = _parse_field(f[3], 1, 12, _MONTHS)
        self.dow = {0 if d == 7 else d for d in _parse_field(f[4], 0, 7, _DOWS)}
        self.dom_star = f[2] == "*"
        self.dow_star = f[4] == "*"

    def match(self, dt: datetime) -> bool:
        if dt.minute not in self.minute or dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False
        dom_ok = dt.day in self.dom
        dow_ok = ((dt.weekday() + 1) % 7) in self.dow   # Mon=0 → cron 1, Sun=6 → 0
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok   # 둘 다 지정되면 OR (crontab 규칙)

    def next_after(self, dt: datetime, limit_min: int = 60 * 24 * 400) -> datetime | None:
        cur = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(limit_min):
            if self.match(cur):
                return cur
            cur += timedelta(minutes=1)
        return None


# ── 선언 파일 ────────────────────────────────────────────────────

class Batch:
    __slots__ = ("user_id", "project", "name", "spec", "command", "workdir",
                 "timeout_sec", "enabled", "notify", "slack_channel", "source")

    @property
    def key(self):
        return (self.user_id, self.project, self.name)

    def __repr__(self):
        return f"<Batch {self.user_id}/{self.project}/{self.name} {self.spec.expr}>"


def workspace(user_id: int) -> Path:
    return ctr.home(user_id) / "workspace"


def project_dirs(user_id: int) -> list[Path]:
    ws = workspace(user_id)
    try:
        entries = sorted(p for p in ws.iterdir() if p.is_dir())
    except (FileNotFoundError, PermissionError) as e:
        logger.debug(f"workspace 접근 불가 user={user_id}: {e}")
        return []
    # `_shared`, 숨김 폴더는 프로젝트가 아니다
    return [p for p in entries if PROJECT_RE.match(p.name)]


def _read_json(path: Path) -> dict | None:
    """파싱 실패는 무시하고 경고만 — 한 파일 오류가 다른 배치를 멈추면 안 된다."""
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"선언 파일 무시 {path}: {e}")
        return None


def load_batches(user_id: int, project: Path) -> list[Batch]:
    out = []
    cron_dir = project / "cron"
    if not cron_dir.is_dir():
        return out
    try:
        files = sorted(cron_dir.glob("*.json"))
    except PermissionError as e:
        logger.warning(f"cron 폴더 접근 불가 {cron_dir}: {e}")
        return out
    for f in files:
        d = _read_json(f)
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or f.stem).strip()
        if not BATCH_RE.match(name):
            logger.warning(f"배치명 무시 {f}: {name!r}")
            continue
        command = str(d.get("command") or "").strip()
        if not command:
            logger.warning(f"command 없음, 무시: {f}")
            continue
        try:
            spec = CronSpec(str(d.get("schedule") or ""))
        except ValueError as e:
            logger.warning(f"schedule 오류, 무시 {f}: {e}")
            continue
        b = Batch()
        b.user_id = user_id
        b.project = project.name
        b.name = name
        b.spec = spec
        b.command = command
        b.workdir = str(d.get("workdir") or ".").strip() or "."
        b.timeout_sec = int(d.get("timeout_sec")
                            or cron_cfg().get("default_timeout_sec", 1800))
        b.enabled = d.get("enabled", True) is not False
        onf = d.get("on_failure") or {}
        b.notify = bool(onf.get("notify"))
        b.slack_channel = onf.get("slack_channel")
        b.source = str(f)
        out.append(b)
    return out


def load_workflow_meta(project: Path) -> dict:
    d = _read_json(project / ".ax" / "workflow.json") or {}
    if not isinstance(d, dict):
        d = {}
    out = {
        "title": str(d.get("title") or project.name),
        "description": d.get("description"),
        "owner_team": d.get("owner_team"),
        "owner_label": d.get("owner_label"),
        # 파일이 없으면 active 로 간주 (계약서 §3-2)
        "status": "draft" if d.get("status") == "draft" else "active",
        "paused": d.get("paused") is True,
        "outputs": d.get("outputs") if isinstance(d.get("outputs"), list) else [],
        "branch_id": d.get("branch_id") if isinstance(d.get("branch_id"), int) else None,
        "sort_order": d.get("sort_order") if isinstance(d.get("sort_order"), int) else 0,
    }
    # 흐름도(2026-07-29 웹 계약 추가). 화이트리스트에 없어서 통째로 버려졌고,
    # blob 에도 안 들어가 graph 만 바뀐 편집은 변경 감지조차 안 됐다.
    if isinstance(d.get("graph"), dict):
        out["graph"] = d["graph"]
    return out


# ── 파일 쓰기 (ubuntu → agent 소유 워크스페이스) ─────────────────

def _sudo(*args) -> bool:
    return subprocess.run(["sudo", "-n", *args], capture_output=True).returncode == 0


def _container_host() -> bool:
    """이 호스트가 컨테이너 모드인지. 컨테이너 없는 호스트(뉴넥스 내부 서버 등,
    container.enabled=false)에서는 홈이 데몬 계정 소유라 agent(10000) chown/ubuntu ACL 을
    걸면 오히려 소유권이 깨진다 — 그 경우 아래 소유권 보정들을 전부 건너뛴다."""
    return bool((config.get("container") or {}).get("enabled"))


def write_file(path: Path, content: str):
    """워크스페이스에 원본 기록. ACL 로 직접 쓰고, 안 되면 sudo tee 로 우회한다."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    except OSError:
        _sudo("mkdir", "-p", str(path.parent))
        subprocess.run(["sudo", "-n", "tee", str(path)],
                       input=content, capture_output=True, text=True)
    # 컨테이너 안 agent(10000) 도 읽을 수 있어야 한다 (컨테이너 호스트에서만)
    if _container_host():
        _sudo("chown", f"{ctr.AGENT_UID}:{ctr.AGENT_UID}", str(path))


def read_text(path: Path) -> str:
    """없으면 빈 문자열. **읽기 실패는 예외로 올린다** — 조용히 '' 를 돌려주면
    이어붙이기 대상 파일을 통째로 덮어써서 과거 기록이 날아간다."""
    if not path.exists():
        return ""
    try:
        return path.read_text()
    except OSError:
        tmp = Path("/tmp") / f"pvcron-{os.getpid()}-{path.name}"
        if _sudo("cp", str(path), str(tmp)):
            try:
                return tmp.read_text()
            finally:
                tmp.unlink(missing_ok=True)
        raise


_provisioned: set[tuple] = set()


def ensure_ax_dir(user_id: int, project: str) -> Path:
    d = workspace(user_id) / project / ".ax" / "runs"
    tag = (user_id, project)
    if tag not in _provisioned:
        if not d.is_dir():
            if _container_host():
                _sudo("mkdir", "-p", str(d))
                _sudo("chown", "-R", f"{ctr.AGENT_UID}:{ctr.AGENT_UID}", str(d.parent))
                _sudo("setfacl", "-R", "-m", "u:ubuntu:rwx", "-m", "d:u:ubuntu:rwx",
                      str(d.parent))
            else:
                d.mkdir(parents=True, exist_ok=True)
        _provisioned.add(tag)
    return d


def ensure_shared(user_id: int):
    """`workspace/_shared/` — 프로젝트들이 `../_shared` 로 참조하는 공통 유틸 폴더."""
    p = workspace(user_id) / "_shared"
    if p.is_dir():
        return
    if _container_host():
        _sudo("mkdir", "-p", str(p))
        _sudo("chown", "-R", f"{ctr.AGENT_UID}:{ctr.AGENT_UID}", str(p))
        _sudo("setfacl", "-R", "-m", "u:ubuntu:rwx", "-m", "d:u:ubuntu:rwx", str(p))
    else:
        p.mkdir(parents=True, exist_ok=True)
    logger.info(f"_shared 준비 완료: {p}")


# ── 실행 ─────────────────────────────────────────────────────────

def fmt_dur(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}초"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}분 {s}초" if s else f"{m}분"
    h, m = divmod(m, 60)
    return f"{h}시간 {m}분" if m else f"{h}시간"


def extract_summary(stdout: str) -> str | None:
    hits = SUMMARY_RE.findall(stdout or "")
    return hits[-1].strip()[:500] if hits else None


class Runner:
    def __init__(self):
        self.max_concurrent = max(1, int(cron_cfg().get("max_concurrent", 1)))
        self.pool = ThreadPoolExecutor(max_workers=self.max_concurrent,
                                       thread_name_prefix="batch")
        self.batches: dict[tuple, Batch] = {}
        self._inflight: set[tuple] = set()
        self._inflight_guard = threading.Lock()
        self._fired: dict[tuple, datetime] = {}
        self._fail_streak: dict[tuple, int] = {}
        self._wf_hash: dict[tuple, str] = {}
        self._wf_retry_at: dict[tuple, float] = {}
        self._wf_backoff: dict[tuple, int] = {}

    # ── 선언 스캔 ──
    def scan(self):
        found: dict[tuple, Batch] = {}
        for u in roster.users():
            uid = u.get("userId")
            if uid is None:
                continue
            ensure_shared(uid)
            for pdir in project_dirs(uid):
                batches = load_batches(uid, pdir)
                for b in batches:
                    found[b.key] = b
                if batches or (pdir / ".ax" / "workflow.json").is_file():
                    self.sync_workflow(uid, pdir, batches)
        added = set(found) - set(self.batches)
        removed = set(self.batches) - set(found)
        for k in added:
            logger.info(f"배치 등록: {found[k]!r} (다음 실행 "
                        f"{found[k].spec.next_after(datetime.now(KST))})")
        for k in removed:
            logger.info(f"배치 해제: {k}")
        self.batches = found

    def sync_workflow(self, user_id: int, pdir: Path, batches: list[Batch]):
        """워크플로우 정의를 대시보드로 sync (표시용 캐시). 선언이 바뀔 때만 보낸다.

        실패를 조용히 넘기지 않는다:
        - 4xx = 고쳐 보낼 것이 우리에겐 없다 → 같은 내용 재전송을 막고 사유를 error 로 남긴다.
          선언 파일이 바뀌면 blob 이 달라져 차단이 자연히 풀린다
        - 5xx·네트워크 = 일시 장애 → 해시를 갱신하지 않고 백오프 후 재시도.
          그냥 두면 스캔 주기(30초)마다 영원히 두드린다
        """
        if not cron_cfg().get("push", True):
            return
        meta = load_workflow_meta(pdir)
        payload = dict(meta)
        payload["project_id"] = pdir.name
        payload["batches"] = [
            {"name": b.name, "schedule": b.spec.expr, "enabled": b.enabled}
            for b in sorted(batches, key=lambda x: x.name)
        ]
        # branch_id 는 생략 가능하다 — 웹이 대시보드 채팅용 브랜치를 자동 생성하고
        # 재sync 때 재사용한다. 값이 있을 때만 실어 보낸다(2026-07-28 계약 완화)
        if payload.get("branch_id") is None:
            payload.pop("branch_id", None)

        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        tag = (user_id, pdir.name)
        if self._wf_hash.get(tag) == blob:
            return
        if time.time() < self._wf_retry_at.get(tag, 0):
            return   # 백오프 대기 중
        key = roster.api_key(user_id)
        if not key:
            return

        status, body = user_api(key, "POST", "/api/ax/workflows", payload)
        label = f"{user_id}/{pdir.name}"
        if 200 <= status < 300:
            self._wf_hash[tag] = blob
            self._wf_retry_at.pop(tag, None)
            self._wf_backoff.pop(tag, None)
            logger.info(f"워크플로우 sync: {label} ({meta['status']})")
            return
        reason = (body or {}).get("error") or "(응답 본문 없음)"
        if 400 <= status < 500:
            self._wf_hash[tag] = blob   # 같은 내용 재전송 차단
            self._wf_retry_at.pop(tag, None)
            self._wf_backoff.pop(tag, None)
            logger.error(f"워크플로우 sync 거부됨({status}) {label}: {reason} "
                         f"— 선언 파일을 고쳐야 다시 시도한다")
            return
        wait = min(900, max(60, self._wf_backoff.get(tag, 30) * 2))
        self._wf_backoff[tag] = wait
        self._wf_retry_at[tag] = time.time() + wait
        logger.warning(f"워크플로우 sync 실패({status or '네트워크'}) {label}: {reason} "
                       f"— {wait}초 뒤 재시도")

    # ── 스케줄 틱 ──
    def tick(self, now: datetime):
        minute = now.replace(second=0, microsecond=0)
        for b in list(self.batches.values()):
            if not b.enabled or not b.spec.match(minute):
                continue
            if self._fired.get(b.key) == minute:
                continue
            self._fired[b.key] = minute
            self.fire(b, minute)

    def fire(self, b: Batch, scheduled: datetime):
        with self._inflight_guard:
            if b.key in self._inflight:
                logger.warning(f"이전 실행이 안 끝나 건너뜀: {b.key}")
                self.record_skip(b, scheduled)
                return
            self._inflight.add(b.key)
        self.pool.submit(self._run_safe, b, scheduled)

    def _run_safe(self, b: Batch, scheduled: datetime):
        try:
            self.run(b, scheduled)
        except Exception as e:
            logger.error(f"배치 처리 오류 {b.key}: {e}", exc_info=True)
        finally:
            with self._inflight_guard:
                self._inflight.discard(b.key)

    # ── 실행 본체 ──
    def run(self, b: Batch, scheduled: datetime):
        run_id = uuid.uuid4().hex[:12]
        started = datetime.now(timezone.utc)
        run_key = f"{b.name}-{started.strftime('%Y%m%dT%H%M%SZ')}"
        rec = {
            "run_id": run_id, "run_key": run_key,
            "user_id": b.user_id, "project": b.project, "name": b.name,
            "scheduled_at": scheduled.isoformat(),
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "status": "running", "command": b.command, "workdir": b.workdir,
        }
        self.write_record(b, rec)
        self.push(b, rec, retry_on_fail=False)

        logger.info(f"실행 시작 {b.user_id}/{b.project}/{b.name}: {b.command[:80]}")
        wd = f"/home/agent/workspace/{b.project}"
        if b.workdir not in (".", ""):
            wd = f"{wd}/{b.workdir.strip('/')}"
        env = user_secrets(b.user_id)
        t0 = time.time()
        rc, out, err = ctr.exec_shell(b.user_id, wd, b.command, env, b.timeout_sec)
        dur = time.time() - t0

        if rc == 124:
            status = "timeout"
        elif rc == 0:
            status = "success"
        else:
            status = "failure"

        combined = (out or "") + (("\n[stderr]\n" + err) if err else "")
        summary = extract_summary(out)
        if not summary:
            summary = {
                "success": f"성공 · {fmt_dur(dur)}",
                "failure": f"실패 · 종료코드 {rc}",
                "timeout": f"시간 초과 · {fmt_dur(b.timeout_sec)} 넘김",
            }[status]

        finished = datetime.now(timezone.utc)
        log_path = self.record_path(b, run_id).with_suffix(".log")
        write_file(log_path, combined[-LOG_FILE_MAX:])
        rec.update({
            "finished_at": finished.isoformat().replace("+00:00", "Z"),
            "duration_ms": int(dur * 1000),
            "status": status, "exit_code": rc,
            "summary": summary,
            "log_tail": combined[-LOG_TAIL_BYTES:],
            "log_file": str(log_path),
        })
        self.write_record(b, rec)
        self.push(b, rec)

        if status == "success":
            self._fail_streak.pop(b.key, None)
            logger.info(f"실행 완료 {b.key} · {fmt_dur(dur)} · {summary}")
        else:
            n = self._fail_streak.get(b.key, 0) + 1
            self._fail_streak[b.key] = n
            logger.error(f"실행 {status} {b.key} rc={rc} (연속 {n}회) · {summary}")
            self.notify_failure(b, rec, n)

    def record_skip(self, b: Batch, scheduled: datetime):
        run_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rec = {
            "run_id": run_id, "run_key": f"{b.name}-{scheduled.strftime('%Y%m%dT%H%M%S')}-skip",
            "user_id": b.user_id, "project": b.project, "name": b.name,
            "scheduled_at": scheduled.isoformat(), "started_at": now, "finished_at": now,
            "duration_ms": 0, "status": "skipped", "exit_code": None,
            "summary": "건너뜀 · 이전 실행이 아직 끝나지 않았습니다",
            # 건너뜀은 대시보드 노이즈라 push 하지 않는다 (원본에는 남는다)
            "pushed": None,
        }
        self.write_record(b, rec)

    # ── 원본 기록 ──
    def record_path(self, b: Batch, run_id: str) -> Path:
        day = datetime.now(KST).strftime("%Y-%m-%d")
        return ensure_ax_dir(b.user_id, b.project) / day / f"{b.name}-{run_id}.json"

    def write_record(self, b: Batch, rec: dict):
        rec.setdefault("pushed", False)
        write_file(self.record_path(b, rec["run_id"]),
                   json.dumps(rec, ensure_ascii=False, indent=2))

    # ── 웹 push (캐시) ──
    def push(self, b: Batch, rec: dict, retry_on_fail: bool = True) -> bool:
        """대시보드용 실행결과 push. 실패해도 워크스페이스 원본이 남는다."""
        if not cron_cfg().get("push", True) or rec.get("status") == "skipped":
            return False
        key = roster.api_key(b.user_id)
        if not key:
            logger.warning(f"push 보류 {b.key}: 로스터에 api_key 없음 — 5분 뒤 재시도")
            return False
        status, body = user_api(key, "POST", "/api/ax/runs", {"runs": [run_payload(rec)]})
        ok = 200 <= status < 300
        if ok:
            rec["pushed"] = True
            if retry_on_fail:
                self.write_record(b, rec)
            return True
        reason = (body or {}).get("error") or "(응답 본문 없음)"
        if 400 <= status < 500:
            # 4xx 는 재시도해도 소용없다 — 원본은 남고 대시보드에만 안 뜬다
            rec["pushed"] = None
            logger.error(f"push 거부됨({status}) {b.key}: {reason} — 재시도하지 않음")
            if retry_on_fail:
                self.write_record(b, rec)
        else:
            logger.warning(f"push 실패({status or '네트워크'}) {b.key}: {reason} "
                           f"— 5분 뒤 재시도(원본은 워크스페이스에 있음)")
        return ok

    # ── 실패 알림 (슬랙) ──
    def notify_failure(self, b: Batch, rec: dict, streak: int):
        if not b.notify or not b.slack_channel:
            return
        if not (streak == 1 or streak % 10 == 0):
            return   # 연속 실패 스팸 억제
        sec = user_secrets(b.user_id)
        token = sec.get("SLACK_BOT_TOKEN") or sec.get("SLACK_ACCESS_TOKEN")
        if not token:
            logger.warning(f"슬랙 토큰 없음 — 알림 생략 {b.key}")
            return
        text = (f":rotating_light: 자동화 실패 · *{b.project} / {b.name}*\n"
                f"{rec.get('summary')}\n"
                f"연속 {streak}회 · 로그: `{rec.get('log_file')}`")
        try:
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=json.dumps({"channel": b.slack_channel, "text": text}).encode(),
                method="POST")
            req.add_header("Content-Type", "application/json; charset=utf-8")
            req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=15) as res:
                body = json.loads(res.read().decode())
            if not body.get("ok"):
                logger.warning(f"슬랙 알림 실패 {b.key}: {body.get('error')}")
        except Exception as e:
            logger.warning(f"슬랙 알림 오류 {b.key}: {e}")

    # ── push 재시도 + 보관 정리 ──
    def sweep(self):
        """미전송 실행기록 재시도(24시간 포기) + 보관기간 지난 원본 정리."""
        retention = int(cron_cfg().get("run_retention_days", 30))
        cutoff_day = (datetime.now(KST) - timedelta(days=retention)).strftime("%Y-%m-%d")
        deadline = datetime.now(timezone.utc) - timedelta(hours=24)
        for u in roster.users():
            uid = u.get("userId")
            key = u.get("apiKey")
            if uid is None:
                continue
            for pdir in project_dirs(uid):
                runs = pdir / ".ax" / "runs"
                if not runs.is_dir():
                    continue
                for day in sorted(runs.iterdir()):
                    if not RUNDIR_RE.match(day.name):
                        continue
                    if day.name < cutoff_day:
                        _sudo("rm", "-rf", str(day))
                        logger.info(f"보관기간 초과 정리: {day}")
                        continue
                    for f in day.glob("*.json"):
                        self.retry_push(f, key, deadline)

    def retry_push(self, path: Path, api_key: str | None, deadline: datetime):
        rec = _read_json(path)
        if not rec or rec.get("pushed") is not False or rec.get("status") == "running":
            return
        if not api_key or not cron_cfg().get("push", True):
            return
        try:
            started = datetime.fromisoformat(rec["started_at"].replace("Z", "+00:00"))
        except Exception:
            started = deadline
        if started < deadline:
            rec["pushed"] = None
            rec["push_gave_up"] = True
            write_file(path, json.dumps(rec, ensure_ascii=False, indent=2))
            logger.warning(f"push 24시간 초과 — 포기(원본은 남음): {path}")
            return
        status, body = user_api(api_key, "POST", "/api/ax/runs",
                                {"runs": [run_payload(rec)]})
        if 200 <= status < 300:
            rec["pushed"] = True
            write_file(path, json.dumps(rec, ensure_ascii=False, indent=2))
            logger.info(f"push 재시도 성공: {path.name}")
        elif 400 <= status < 500:
            rec["pushed"] = None
            write_file(path, json.dumps(rec, ensure_ascii=False, indent=2))
            logger.error(f"push 거부됨({status}) {path.name}: "
                         f"{(body or {}).get('error') or '(응답 본문 없음)'} — 재시도 중단")

    # ── 대화 기록 스냅샷 (인계 보존) ──
    def export_chats(self):
        """대시보드 대화는 웹 DB 가 원본이라 인계 시 사라진다 → 워크스페이스에 적재한다.
        (`docs/ops/ax-dashboard-runner-api.md` §3, Sean 결정 2026-07-27)

        워크플로우에 `branch_id` 가 없으면 서버가 404 를 준다 = 연결된 대화가 없다는 뜻이므로
        조용히 넘어간다. 증분(`after`)은 프로젝트별 상태파일에 남겨 중복 적재를 막는다."""
        for u in roster.users():
            uid, key = u.get("userId"), u.get("apiKey")
            if uid is None or not key:
                continue
            for pdir in project_dirs(uid):
                if not (pdir / ".ax" / "workflow.json").is_file():
                    continue
                try:
                    self.export_project_chat(pdir, key)
                except Exception as e:
                    logger.warning(f"대화 기록 적재 실패 {pdir.name}: {e}")

    def export_project_chat(self, pdir: Path, api_key: str):
        state_path = pdir / ".ax" / "chat-export.json"
        state = (_read_json(state_path) if state_path.is_file() else {}) or {}
        q = f"/api/ax/chat/export?project={urllib.parse.quote(pdir.name)}"
        after = state.get("last_created_at")
        if after:
            q += f"&after={urllib.parse.quote(str(after))}"
        status, res = user_api(api_key, "GET", q)
        if status == 404:
            return   # 연결된 대화 없음 (워크플로우에 브랜치가 아직 안 붙음)
        if not (200 <= status < 300):
            reason = (res or {}).get("error") or "(응답 본문 없음)"
            logger.warning(f"대화 기록 조회 실패({status or '네트워크'}) {pdir.name}: {reason}")
            return
        if not res or not res.get("count"):
            return   # 새 메시지 없음
        md = res.get("markdown") or ""
        target = pdir / "docs" / "대화기록" / f"{datetime.now(KST):%Y-%m}.md"
        prev = read_text(target)
        if prev:
            # 매 응답에 붙는 머리말은 첫 적재분에만 남긴다
            head = md.find("\n### ")
            md = md[head + 1:] if head >= 0 else md
            md = prev.rstrip("\n") + "\n\n" + md
        write_file(target, md)
        state["last_created_at"] = res.get("last_created_at")
        write_file(state_path, json.dumps(state, ensure_ascii=False, indent=2))
        logger.info(f"대화 기록 적재 {pdir.name}: {res['count']}건 → {target.name}")

    # ── 재시작 복구 ──
    def reconcile_orphans(self):
        """러너가 죽는 순간 돌던 실행은 `running` 인 채로 남는다.
        그대로 두면 대시보드가 영원히 '실행 중'으로 보이므로 기동 시 한 번 정리한다."""
        days = {(datetime.now(KST) - timedelta(days=d)).strftime("%Y-%m-%d")
                for d in (0, 1)}
        for u in roster.users():
            uid = u.get("userId")
            if uid is None:
                continue
            for pdir in project_dirs(uid):
                for day in days:
                    d = pdir / ".ax" / "runs" / day
                    if not d.is_dir():
                        continue
                    for f in d.glob("*.json"):
                        rec = _read_json(f)
                        if not rec or rec.get("status") != "running":
                            continue
                        rec.update({
                            "status": "failure", "exit_code": None, "pushed": False,
                            "summary": "중단됨 · 러너가 재시작되어 결과를 확인할 수 없습니다",
                            "finished_at": datetime.now(timezone.utc)
                                           .isoformat().replace("+00:00", "Z"),
                        })
                        write_file(f, json.dumps(rec, ensure_ascii=False, indent=2))
                        logger.warning(f"중단된 실행 정리: {f.name}")

    # ── 메인 루프 ──
    def loop(self):
        logger.info(f"cron 러너 시작 · 동시 실행 상한 {self.max_concurrent} · TZ Asia/Seoul")
        roster.sync(force=True)
        try:
            self.reconcile_orphans()
        except Exception as e:
            logger.error(f"중단 실행 정리 오류: {e}", exc_info=True)
        scan_interval = int(cron_cfg().get("scan_interval_sec", 30))
        export_min = int(cron_cfg().get("chat_export_min", 60))   # 0 = 끔
        last_scan = 0.0
        last_sweep = time.time()
        last_export = 0.0
        while not shutdown.is_set():
            try:
                now = time.time()
                roster.sync()
                if now - last_scan >= scan_interval:
                    self.scan()
                    last_scan = now
                    self._last_scan_ts = now
                self.tick(datetime.now(KST))
                if now - last_sweep >= 300:
                    threading.Thread(target=self._sweep_safe, daemon=True,
                                     name="sweep").start()
                    last_sweep = now
                if export_min > 0 and now - last_export >= export_min * 60:
                    threading.Thread(target=self._export_safe, daemon=True,
                                     name="chat-export").start()
                    last_export = now
                if now - self._last_hb >= 60:
                    self._heartbeat()
                    self._last_hb = now
            except Exception as e:
                logger.error(f"루프 오류: {e}", exc_info=True)
            shutdown.wait(TICK_SEC)
        logger.info("cron 러너 정지")

    _last_hb = 0.0
    _hb_404 = False
    _last_scan_ts = 0.0

    def _heartbeat(self):
        """러너 생존 신호 → 대시보드 (60초 주기, 아웃바운드).

        러너가 죽거나 sync 가 막혀도 밖에서 알 길이 없어 대시보드 쪽이 오진했다
        (2026-07-29 graph 미반영 건 — 러너 정지로 의심했으나 실제는 키 필터 문제).
        웹이 아직 이 엔드포인트를 안 만들었으면 404 — 한 번만 로그하고 조용히 넘어간다."""
        body = {
            "batches": len(self.batches),
            "last_scan_at": (datetime.fromtimestamp(self._last_scan_ts, KST).isoformat()
                             if self._last_scan_ts else None),
            "max_concurrent": self.max_concurrent,
        }
        try:
            status, _ = host_api("POST", "/api/ax/runner-heartbeat", body)
        except Exception:
            return
        if status == 404 and not self._hb_404:
            self._hb_404 = True
            logger.info("runner-heartbeat 엔드포인트 미구현(404) — 웹 배포 전까지 무시")

    def _sweep_safe(self):
        try:
            self.sweep()
        except Exception as e:
            logger.error(f"sweep 오류: {e}", exc_info=True)

    def _export_safe(self):
        try:
            self.export_chats()
        except Exception as e:
            logger.error(f"대화 기록 적재 오류: {e}", exc_info=True)


def run_payload(rec: dict) -> dict:
    """워크스페이스 원본 → 대시보드 캐시 스키마(POST /api/ax/runs).

    대시보드 상태값은 success|fail|running 세 가지뿐이라 timeout/failure 를 fail 로 접는다.
    원래 상태와 종료코드는 metrics 에 실어 보존한다.
    """
    st = rec.get("status")
    mapped = {"success": "success", "running": "running"}.get(st, "fail")
    dur = rec.get("duration_ms")
    return {
        "project_id": rec["project"],
        "batch_name": rec["name"],
        "run_key": rec["run_key"],
        "status": mapped,
        "started_at": rec["started_at"],
        "finished_at": rec.get("finished_at"),
        "duration_sec": round(dur / 1000, 1) if isinstance(dur, int) else None,
        "summary": rec.get("summary"),
        "detail": rec.get("log_tail"),
        "metrics": {"outcome": st, "exit_code": rec.get("exit_code"),
                    "run_id": rec.get("run_id")},
    }


# ── 진입점 ───────────────────────────────────────────────────────

def load_config():
    global config
    if not CONFIG_PATH.exists():
        print(f"config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    config = json.loads(CONFIG_PATH.read_text())
    for k in ("api_url", "host_key"):
        if not config.get(k):
            print(f"config missing: {k}", file=sys.stderr)
            sys.exit(1)
    ctr.init(config, logger)


def cmd_list():
    roster.sync(force=True)
    now = datetime.now(KST)
    total = 0
    for u in roster.users():
        uid = u["userId"]
        for pdir in project_dirs(uid):
            for b in load_batches(uid, pdir):
                total += 1
                nxt = b.spec.next_after(now)
                nxt_s = f"{nxt:%Y-%m-%d %H:%M} KST" if nxt else "없음"
                state = "" if b.enabled else "  [비활성]"
                print(f"{uid}/{b.project}/{b.name}{state}\n"
                      f"    schedule : {b.spec.expr}  (다음 {nxt_s})\n"
                      f"    command  : {b.command}\n"
                      f"    workdir  : {b.workdir}   timeout {b.timeout_sec}s")
    print(f"\n총 {total}개 배치 · cron.enabled={cron_cfg().get('enabled', False)}")


def main():
    ap = argparse.ArgumentParser(description="PeterVoice cloud cron runner")
    ap.add_argument("--list", action="store_true", help="선언된 배치와 다음 실행시각 출력")
    args = ap.parse_args()

    load_config()
    if args.list:
        cmd_list()
        return

    if not cron_cfg().get("enabled"):
        logger.info("cron.enabled=false — 러너는 아무것도 하지 않는다 (정상 종료)")
        return

    idle = int((config.get("container") or {}).get("idle_stop_sec", 900))
    if idle > 0:
        logger.warning(f"container.idle_stop_sec={idle} — 배치 도중 컨테이너가 "
                       f"유휴 정지될 수 있다. 전용 호스트는 0(상시 유지) 권장")

    def on_sig(_s, _f):
        shutdown.set()
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)

    Runner().loop()


if __name__ == "__main__":
    main()
