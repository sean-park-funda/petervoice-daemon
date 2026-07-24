#!/usr/bin/env python3
"""PeterVoice Cloud Daemon — 멀티테넌트 단일 프로세스.

공용 클라우드 호스트에서 daemon_target=cloud 유저 전원을 서비스한다.
고객 맥미니의 claude_daemon.py 와는 완전히 별개의 진입점 (기존 코드 무변경 원칙).

구조 (계획: peter-voice/docs/plans/2026-07-23-cloud-first-signup.md):
- 통합 폴링: GET /api/cloud/poll (host_key) — 전 유저 pending 을 요청 1개로
- 로스터: GET /api/cloud/roster — user_id ↔ api_key 매핑 (60초 주기)
- 유저 격리: Claude CLI 서브프로세스에 CLAUDE_CONFIG_DIR/cwd 주입
  · /srv/pv/users/<id>/claude    — 유저 본인의 클로드 OAuth 토큰 (격리)
  · /srv/pv/users/<id>/workspace — 파일 작업 디렉토리
- 미로그인 유저: "재로그인" 트리거 → claude setup-token 기반 로그인 플로우
  (기존 relogin 원칙 준수: 자격증명을 직접 읽거나 고치지 않고 정식 CLI 플로우만 실행,
   코드는 로그에 남기지 않음)
- 세션: (user_id, project) 단위 --resume

설정: /etc/pv-cloud/config.json
  {"api_url": "https://www.peter-voice.site", "host_key": "...",
   "users_root": "/srv/pv/users", "poll_interval_sec": 3,
   "max_concurrent": 5, "claude_cmd": "claude", "isolate_users": true}
"""

import json
import logging
import os
import pty
import re
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("PV_CLOUD_CONFIG", "/etc/pv-cloud/config.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("pv-cloud")

config: dict = {}
shutdown_event = threading.Event()

TURN_TIMEOUT_SEC = 30 * 60
LOGIN_URL_TIMEOUT_SEC = 60
LOGIN_CODE_TTL_SEC = 600
CODE_RE = re.compile(r"[A-Za-z0-9_\-]{20,}#[A-Za-z0-9_\-]{8,}")
_URL_RE = re.compile(r"https?://[^\s\x00-\x1f\"']+")

NEED_LOGIN_MESSAGE = (
    "아직 클로드(Claude) 계정이 연결되지 않아 AI 비서가 잠들어 있어요. 🌙\n\n"
    "**연결 방법** (2분이면 돼요)\n"
    "1. 여기에 `재로그인` 이라고 입력해주세요.\n"
    "2. 제가 보내드리는 링크를 열어 클로드 계정으로 로그인해주세요.\n"
    "3. 화면에 나오는 코드를 복사해서 이 채팅에 붙여넣어 주세요.\n\n"
    "클로드 구독 계정이 없다면, 다른 사용자의 프로젝트에 초대받아 대화하는 것은 지금도 가능해요."
)


# ── HTTP helpers ─────────────────────────────────────────────────

def http_json(method: str, path: str, *, headers: dict, body: dict | None = None,
              timeout: int = 30) -> dict | None:
    url = config["api_url"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP {e.code} {method} {path}: {e.read()[:200]}")
        return None
    except Exception as e:
        logger.warning(f"HTTP error {method} {path}: {e}")
        return None


def host_api(method: str, path: str, body: dict | None = None) -> dict | None:
    return http_json(method, path, headers={"X-Host-Key": config["host_key"]}, body=body)


def user_api(api_key: str, method: str, path: str, body: dict | None = None) -> dict | None:
    # 기존 맥 데몬과 동일하게 두 헤더 모두 전송 — 엔드포인트마다 요구 헤더가 다름
    # (heartbeat=Bearer, 다수 라우트=X-Api-Key)
    return http_json(method, path, headers={
        "Authorization": f"Bearer {api_key}",
        "X-Api-Key": api_key,
    }, body=body)


# ── Per-user secrets (외부 연동 토큰 주입) ───────────────────────
# 유저별 SecretsPanel 시크릿 + OAuth 토큰(구글/슬랙/노션)을 해당 유저의 api_key 로
# 조회해 claude 서브프로세스 env 에만 주입한다.
# 보안 원칙:
# - 값은 절대 argv 에 싣지 않는다 (다른 pv 유저가 /proc cmdline 으로 볼 수 있음)
#   → sudo --preserve-env=<이름목록> + Popen(env=...) 조합: 이름만 argv, 값은 env 로 전달
# - 유저 A 의 시크릿은 유저 A 의 턴에만 주입 (전역 os.environ 오염 금지)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_secrets_cache: dict[int, tuple[float, dict]] = {}
_secrets_lock = threading.Lock()
SECRETS_TTL_SEC = 60


def fetch_user_secrets(user_id: int, api_key: str) -> dict:
    now = time.time()
    with _secrets_lock:
        cached = _secrets_cache.get(user_id)
        if cached and now - cached[0] < SECRETS_TTL_SEC:
            return cached[1]
    result = user_api(api_key, "GET", "/api/secrets?raw=true")
    secrets: dict[str, str] = {}
    if result:
        for s in result.get("secrets", []):
            k = (s.get("key") or "").strip()
            v = (s.get("value") or "").strip()
            if k and v and _ENV_NAME_RE.match(k):
                secrets[k] = v
    with _secrets_lock:
        # 조회 실패(None) 시 이전 캐시 유지 — 일시 장애로 연동이 끊기지 않게
        if result is None and cached:
            return cached[1]
        _secrets_cache[user_id] = (now, secrets)
    return secrets


# ── Roster ───────────────────────────────────────────────────────

class Roster:
    """user_id → {apiKey, username, botName} 매핑. 60초 주기 + 미스 시 즉시 갱신."""

    def __init__(self):
        self._users: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._last_sync = 0.0

    def sync(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_sync < 60:
            return
        result = host_api("GET", "/api/cloud/roster")
        if result and "users" in result:
            with self._lock:
                self._users = {u["userId"]: u for u in result["users"]}
                self._last_sync = now
            logger.info(f"roster synced: {len(self._users)} cloud users")

    def get(self, user_id: int) -> dict | None:
        with self._lock:
            user = self._users.get(user_id)
        if user is None:
            self.sync(force=True)  # 신규 가입 직후 로스터 미스 → 즉시 재동기화
            with self._lock:
                user = self._users.get(user_id)
        return user


roster = Roster()


# ── Per-user dirs / sessions ─────────────────────────────────────

def user_root(user_id: int) -> Path:
    return Path(config.get("users_root", "/srv/pv/users")) / str(user_id)


# ── OS 유저 격리 ─────────────────────────────────────────────────
# 각 클라우드 유저를 별도 unix 계정(pv<id>)으로 강등 실행해 파일시스템을 격리한다.
# 데몬은 ubuntu 로 돌고, 제한적 sudo(useradd/chown/mkdir + pvusers 그룹 강등 실행)만 사용.
# config.isolate_users=false 면 (예: 로컬 개발) 강등 없이 현재 유저로 실행.

_provisioned_uids: set[int] = set()
_provision_lock = threading.Lock()


def unix_user(user_id: int) -> str:
    return f"pv{user_id}"


def isolation_enabled() -> bool:
    return bool(config.get("isolate_users", True))


def provision_unix_user(user_id: int):
    """유저별 unix 계정 + 소유권 격리 (idempotent). 첫 등장 시 1회."""
    if not isolation_enabled():
        ensure_user_dirs(user_id)
        return
    with _provision_lock:
        if user_id in _provisioned_uids:
            return
        name = unix_user(user_id)
        root = user_root(user_id)
        try:
            exists = subprocess.run(["id", name], capture_output=True).returncode == 0
            if not exists:
                subprocess.run(
                    ["sudo", "-n", "useradd", "-M", "-d", str(root),
                     "-s", "/usr/sbin/nologin", "-g", "pvusers", name],
                    check=True, capture_output=True)
            for sub in ("claude", "workspace", "workspace/general", "workspace/general/docs"):
                subprocess.run(["sudo", "-n", "mkdir", "-p", str(root / sub)],
                               check=True, capture_output=True)
            subprocess.run(["sudo", "-n", "chown", "-R", f"{name}:pvusers", str(root)],
                           check=True, capture_output=True)
            # 700: 소유자(pv<id>)만 접근 — 교차 유저 읽기 차단
            subprocess.run(["sudo", "-n", "install", "-d", "-m", "700",
                            "-o", name, "-g", "pvusers", str(root)],
                           check=True, capture_output=True)
            # 루트(700)에 ubuntu traverse(x)만 허용 — 목록/읽기는 불가, 하위 진입만 가능
            subprocess.run(["sudo", "-n", "setfacl", "-m", "u:ubuntu:--x", str(root)],
                           check=True, capture_output=True)
            # 번들 스킬 공유: 유저 claude/skills → 공용 스킬 디렉토리 심링크
            shared_skills = config.get("shared_skills_dir", "/srv/pv/shared/skills")
            skills_link = root / "claude" / "skills"
            if Path(shared_skills).exists() and not skills_link.exists():
                subprocess.run(
                    ["sudo", "-n", "-u", name, "env", "ln", "-s", shared_skills, str(skills_link)],
                    capture_output=True)
            # workspace 에 ACL: 포탈/데몬(ubuntu)과 claude(pv<id>) 모두 접근,
            # 다른 pv 유저는 여전히 차단 (root 700 이 1차 방어)
            subprocess.run(["sudo", "-n", "setfacl", "-R",
                            "-m", "u:ubuntu:rwx", "-m", f"u:{name}:rwx",
                            "-m", "d:u:ubuntu:rwx", "-m", f"d:u:{name}:rwx",
                            str(root / "workspace")],
                           check=True, capture_output=True)
            _provisioned_uids.add(user_id)
            logger.info(f"provisioned unix user {name}")
        except subprocess.CalledProcessError as e:
            logger.error(f"provision {name} failed: {e.stderr.decode()[:200] if e.stderr else e}")
            raise


def wrap_isolated(user_id: int, cmd: list[str], env_overrides: dict, cwd: str,
                  preserve_env_names: list[str] | None = None) -> list[str]:
    """cmd 를 해당 유저로 강등 실행하는 명령으로 감싼다.
    env_overrides 는 sudo 경계를 넘기기 위해 `env K=V` 로, cwd 는 sudo -D 로 전달.
    preserve_env_names: 값 노출 없이(argv 에는 이름만) 부모 env 에서 전달할 변수들
    (시크릿 주입용 — sudoers SETENV 태그 필요)."""
    if not isolation_enabled():
        return cmd
    name = unix_user(user_id)
    args = ["sudo", "-n", "-u", name, "-H", "-D", cwd]
    if preserve_env_names:
        args.append("--preserve-env=" + ",".join(preserve_env_names))
    env_args = [f"{k}={v}" for k, v in env_overrides.items()]
    return [*args, "env", *env_args, *cmd]


def user_claude_dir(user_id: int) -> Path:
    return user_root(user_id) / "claude"


def user_workspace(user_id: int) -> Path:
    return user_root(user_id) / "workspace"


def project_dir(user_id: int, project: str) -> Path:
    """프로젝트별 작업 폴더 (맥 데몬의 프로젝트 디렉토리와 동일 개념).
    branch:N / kanban:N 은 부모 프로젝트 구분이 어려우므로 v1은 general 로 통합."""
    name = project if re.fullmatch(r"[a-z0-9_\-]{1,60}", project or "") else "general"
    return user_workspace(user_id) / name


def ensure_user_dirs(user_id: int):
    user_claude_dir(user_id).mkdir(parents=True, exist_ok=True)
    ws = user_workspace(user_id)
    (ws / "docs").mkdir(parents=True, exist_ok=True)


def _state_dir() -> Path:
    # 데몬(ubuntu) 소유 메타 영역 — 유저의 700 격리 폴더 밖에 둔다
    d = Path(config.get("users_root", "/srv/pv/users")).parent / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sessions_path(user_id: int) -> Path:
    return _state_dir() / f"{user_id}.json"


def load_session(user_id: int, project: str) -> str | None:
    try:
        data = json.loads(_sessions_path(user_id).read_text())
        return data.get(project)
    except Exception:
        return None


def save_session(user_id: int, project: str, session_id: str | None):
    path = _sessions_path(user_id)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        data = {}
    if session_id:
        data[project] = session_id
    else:
        data.pop(project, None)
    path.write_text(json.dumps(data))


def isolated_env_overrides(user_id: int) -> dict:
    """강등 실행 시 sudo 경계를 넘겨야 하는 환경변수."""
    return {
        "CLAUDE_CONFIG_DIR": str(user_claude_dir(user_id)),
        "HOME": str(user_root(user_id)),
    }


def claude_env(user_id: int) -> dict:
    """비격리 모드(로컬)용 전체 env."""
    env = os.environ.copy()
    env.update(isolated_env_overrides(user_id))
    return env


def has_credentials(user_id: int) -> bool:
    """자격증명 파일 존재 여부만 확인 (내용은 절대 읽지 않음).
    격리 모드에서는 파일이 pv<id> 소유라 sudo -u 로 확인."""
    cred = user_claude_dir(user_id) / ".credentials.json"
    if not isolation_enabled():
        return cred.exists()
    # sudoers 는 pvusers 강등 실행으로 env/claude 만 허용 → env 로 test 를 exec
    r = subprocess.run(
        ["sudo", "-n", "-u", unix_user(user_id), "env", "test", "-f", str(cred)],
        capture_output=True)
    return r.returncode == 0


CLOUD_SYSTEM_PROMPT = """# 실행 환경: 피터보이스 클라우드 (공유 서버)

당신은 여러 사용자가 공유하는 리눅스 서버에서, 이 사용자 전용 계정으로 격리 실행됩니다.
자원이 제한된 공유 환경이므로 아래를 지켜주세요.

## 환경 제약
- 한 번의 작업(턴)은 메모리 3GB, CPU, 30분 시간 제한이 걸려 있습니다. 초과하면 강제 종료됩니다.
- 시스템 전역 설치(apt, sudo, npm -g)는 불가합니다. 대신:
  - 파이썬 패키지: `pip install --user` 또는 가상환경(venv)을 홈 아래에 만들어 사용
  - node 패키지: 프로젝트 폴더에 로컬 설치(`npm install`), node 버전 변경은 홈에 nvm 설치
- 서버/데몬을 상시 띄우지 마세요. 확인용으로 잠깐 띄웠다면 작업 끝에 반드시 종료하세요.
- 대용량 데이터, 파일은 작업 폴더에만. 디스크도 사용자별 한도가 있습니다.

## 무거운 작업 안내 (중요)
아래 유형은 **착수하지 말고**, 먼저 사용자에게 안내하세요:
"이 작업은 클라우드 환경의 한도를 넘어요. 내 컴퓨터(맥/PC)에 피터를 직접 설치하면 제한 없이 할 수 있어요. 설정에서 '내 AI 비서 설치'를 신청해보세요."
- 머신러닝/딥러닝 모델 학습·추론, GPU 필요 작업
- 대량 크롤링/스크래핑, 대용량(수 GB) 데이터 처리
- 영상 렌더링·인코딩 등 장시간 CPU 작업
- 상시 실행 서버/봇 운영, 시스템 패키지 설치가 꼭 필요한 작업
이런 요청에는 응답 어딘가에 `[HEAVY_TASK]` 를 포함하세요 (사용자에겐 보이지 않게 처리됩니다).

가벼운 작업(리서치, 문서 작성, 데이터 분석, 소규모 코드/스크립트, 웹 API 호출)은 자유롭게 하세요.
"""


def _ensure_cloud_system_prompt(user_id: int) -> str | None:
    """클라우드 환경 가이드 시스템 프롬프트 파일 (유저 워크스페이스에 1회 생성, ACL로 pv유저 읽기 가능)."""
    try:
        p = user_workspace(user_id) / ".cloud-system-prompt.md"
        if not p.exists() or p.read_text() != CLOUD_SYSTEM_PROMPT:
            p.write_text(CLOUD_SYSTEM_PROMPT)
            os.chmod(p, 0o644)
        return str(p)
    except OSError:
        return None


# ── Claude 실행 (리소스 상한: systemd-run cgroup) ────────────────
# 각 턴을 systemd 일회성 유닛으로 격리 실행 → 메모리/CPU/태스크/시간 상한을 강제한다.
# 폭주해도 그 턴 유닛만 죽고 호스트/데몬/타 유저는 무사. PrivateTmp 로 /tmp 도 사설.
# 시크릿은 EnvironmentFile(600, ubuntu 소유, 턴마다 삭제)로 전달 — argv 비노출.

_turn_counter = [0]
_turn_counter_lock = threading.Lock()


def _turnenv_dir() -> Path:
    d = _state_dir() / "turnenv"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _next_unit_id(user_id: int) -> str:
    with _turn_counter_lock:
        _turn_counter[0] += 1
        n = _turn_counter[0]
    return f"pvturn-{user_id}-{os.getpid()}-{n}"


def _escape_envfile_value(v: str) -> str:
    # systemd EnvironmentFile: 한 줄 KEY=value, 값의 개행 제거 (다중 라인 미지원)
    return v.replace("\n", " ").replace("\r", " ")


def build_systemd_run(user_id: int, cmd: list[str], env: dict, cwd: str, unit: str,
                      env_file: str) -> list[str]:
    lim = config.get("limits", {})
    mem = lim.get("memory_max", "3G")
    cpu = lim.get("cpu_quota", "150%")
    tasks = str(lim.get("tasks_max", 256))
    runtime_max = str(lim.get("turn_timeout_sec", TURN_TIMEOUT_SEC))
    return [
        "sudo", "-n", "systemd-run",
        f"--uid={unix_user(user_id)}", "--gid=pvusers",
        f"--unit={unit}", "--wait", "--pipe", "--quiet",
        f"--property=EnvironmentFile={env_file}",
        f"--property=WorkingDirectory={cwd}",
        f"--property=MemoryMax={mem}", "--property=MemorySwapMax=0",
        f"--property=CPUQuota={cpu}", f"--property=TasksMax={tasks}",
        f"--property=RuntimeMaxSec={runtime_max}",
        "--property=PrivateTmp=yes", "--property=NoNewPrivileges=yes",
        *cmd,
    ]


def _unit_result(unit: str) -> str:
    """systemd 유닛 종료 사유: success | oom-kill | timeout | ... """
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "show", unit,
                            "-p", "Result", "--value"],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _reset_unit(unit: str):
    subprocess.run(["sudo", "-n", "systemctl", "reset-failed", unit],
                   capture_output=True)


def run_claude_turn(user_id: int, project: str, prompt: str) -> tuple[str, str]:
    """한 턴 실행. returns (응답 텍스트, 상태).
    상태: "ok" | "auth" (재로그인 필요) | "heavy_mem" (메모리 초과) | "heavy_time" (시간 초과)."""
    claude_cmd = config.get("claude_cmd", "claude")
    cmd = [
        claude_cmd, "-p",
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    sp = _ensure_cloud_system_prompt(user_id)
    if sp:
        cmd.extend(["--append-system-prompt-file", sp])
    sid = load_session(user_id, project)
    if sid:
        cmd.extend(["--resume", sid])
    cmd.append("--")
    cmd.append(prompt)

    pdir = project_dir(user_id, project)
    try:
        (pdir / "docs").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    ws = str(pdir)

    user = roster.get(user_id)
    secrets = fetch_user_secrets(user_id, user["apiKey"]) if user else {}
    env = {**isolated_env_overrides(user_id), **secrets}

    if not isolation_enabled():
        # 로컬 개발: 상한 없이 직접 실행
        try:
            result = subprocess.run(cmd, cwd=ws, env={**claude_env(user_id), **secrets},
                                    capture_output=True, text=True, timeout=TURN_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return ("작업이 너무 오래 걸려 중단됐어요.", "heavy_time")
        return _parse_turn_result(result.returncode, result.stdout, result.stderr,
                                  user_id, project, prompt, sid)

    # 격리 모드: systemd-run cgroup 상한
    unit = _next_unit_id(user_id)
    env_file = _turnenv_dir() / f"{unit}.env"
    try:
        lines = [f"{k}={_escape_envfile_value(v)}" for k, v in env.items()]
        env_file.write_text("\n".join(lines) + "\n")
        os.chmod(env_file, 0o600)
    except OSError as e:
        logger.error(f"env file write failed: {e}")
        return ("(일시적 오류로 실행에 실패했어요.)", "ok")

    wrapped = build_systemd_run(user_id, cmd, env, ws, unit, str(env_file))
    try:
        result = subprocess.run(wrapped, capture_output=True, text=True,
                                timeout=config.get("limits", {}).get("turn_timeout_sec", TURN_TIMEOUT_SEC) + 30)
        rc, out, err = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "timeout"
    finally:
        try:
            env_file.unlink()
        except OSError:
            pass

    # 비정상 종료 → cgroup 사유 판별
    if rc != 0:
        res = _unit_result(unit)
        _reset_unit(unit)
        if res == "oom-kill":
            logger.info(f"turn OOM-killed user={user_id}")
            return ("", "heavy_mem")
        if res in ("timeout", "runtime-max"):
            return ("", "heavy_time")
    else:
        _reset_unit(unit)

    return _parse_turn_result(rc, out, err, user_id, project, prompt, sid)


def _parse_turn_result(rc, out, err, user_id, project, prompt, sid) -> tuple[str, str]:
    out = (out or "").strip()
    err = (err or "").strip()
    auth_markers = ("Invalid API key", "not logged in", "Please run /login",
                    "OAuth token has expired", "authentication_error")
    if rc != 0:
        combined = f"{out}\n{err}"
        if any(m.lower() in combined.lower() for m in auth_markers):
            return ("", "auth")
        if sid and ("No conversation found" in combined or "session" in combined.lower()):
            save_session(user_id, project, None)
            return run_claude_turn(user_id, project, prompt)
        logger.error(f"claude exit {rc} user={user_id}: {combined[:300]}")
        return ("(처리 중 오류가 발생했어요. 다시 시도해주세요.)", "ok")
    try:
        payload = json.loads(out)
        text = payload.get("result", "")
        new_sid = payload.get("session_id")
        if new_sid:
            save_session(user_id, project, new_sid)
        if payload.get("is_error"):
            if any(m.lower() in (text or err).lower() for m in auth_markers):
                return ("", "auth")
        return (text or "(응답이 비어 있어요)", "ok")
    except json.JSONDecodeError:
        return (out[:4000] or "(응답이 비어 있어요)", "ok")


# ── 로그인 플로우 (claude setup-token, pty) ──────────────────────

class LoginSession:
    """유저별 setup-token 로그인 세션. 코드/토큰은 로그에 절대 남기지 않는다."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.state = "starting"      # starting → waiting_code → done/failed
        self.url: str | None = None
        self.created = time.time()
        self._pid: int | None = None
        self._fd: int | None = None
        self._lock = threading.Lock()

    def start(self) -> str | None:
        """`claude auth login --claudeai` 를 띄우고 완전한 인증 URL을 캡처해 반환.
        맥 데몬(relogin.py)과 동일한 명령/방식 — setup-token 은 redirect_uri 없는
        불완전 URL을 내므로 쓰지 않는다."""
        claude_cmd = config.get("claude_cmd", "claude")
        ws = str(user_workspace(self.user_id))
        base_cmd = [claude_cmd, "auth", "login", "--claudeai"]
        wrapped = wrap_isolated(self.user_id, base_cmd,
                                isolated_env_overrides(self.user_id), ws)
        env = os.environ.copy() if isolation_enabled() else claude_env(self.user_id)
        env = {k: v for k, v in env.items() if k != "CLAUDECODE"}
        env["LANG"] = "en_US.UTF-8"
        pid, fd = pty.fork()
        if pid == 0:  # child
            if not isolation_enabled():
                os.chdir(ws)
            os.execvpe(wrapped[0], wrapped, env)
        self._pid, self._fd = pid, fd

        buf = ""
        enter_sent = 0
        deadline = time.time() + LOGIN_URL_TIMEOUT_SEC
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", chunk.decode(errors="replace"))
            url = self._extract_complete_url(buf)
            if url:
                self.url = url
                self.state = "waiting_code"
                return url
            # 온보딩 화면(테마 선택 등)이면 Enter 로 진행 (최대 2회)
            low = buf.lower()
            if enter_sent < 2 and any(mk in low for mk in
                                      ("choose", "theme", "select", "press enter", "dark mode")):
                try:
                    os.write(fd, b"\r")
                except OSError:
                    pass
                enter_sent += 1
                time.sleep(0.5)
        self.terminate()
        self.state = "failed"
        return None

    @staticmethod
    def _extract_complete_url(buf: str) -> str | None:
        """OAuth URL을 **완전히** 출력된 경우에만 반환.
        pty 출력이 버퍼 경계에 걸쳐 잘리는 것을 막기 위해, URL 뒤에 공백/개행이
        온(=출력이 끝난) 경우 + redirect_uri 포함을 조건으로 한다."""
        for m in _URL_RE.finditer(buf):
            cand = m.group(0).rstrip(").,")
            low = cand.lower()
            if not ("oauth" in low or "authorize" in low or "claude.ai" in low or "claude.com" in low):
                continue
            terminated = m.end() < len(buf)  # 매치 뒤에 문자가 더 있음 = URL 종료 확인
            if terminated and "redirect_uri=" in low:
                return cand
        return None

    def submit_code(self, code: str) -> bool:
        with self._lock:
            if self.state != "waiting_code" or self._fd is None:
                return False
            try:
                os.write(self._fd, (code.strip() + "\n").encode())
            except OSError:
                self.state = "failed"
                return False
            # 성공/실패 판정: 프로세스 종료 대기 (최대 60초)
            deadline = time.time() + 60
            while time.time() < deadline:
                pid_done, status = os.waitpid(self._pid, os.WNOHANG)
                if pid_done:
                    ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
                    self.state = "done" if ok else "failed"
                    self._cleanup()
                    return ok
                # 남은 출력 소진 (블록 방지)
                r, _, _ = select.select([self._fd], [], [], 0.5)
                if r:
                    try:
                        os.read(self._fd, 4096)
                    except OSError:
                        pass
            self.state = "failed"
            self.terminate()
            return False

    def expired(self) -> bool:
        return time.time() - self.created > LOGIN_CODE_TTL_SEC

    def terminate(self):
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self._cleanup()

    def _cleanup(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


login_sessions: dict[int, LoginSession] = {}
login_lock = threading.Lock()


# ── 메시지 처리 ──────────────────────────────────────────────────

class CloudWorker:
    def __init__(self):
        self._executor = ThreadPoolExecutor(
            max_workers=config.get("max_concurrent", 5), thread_name_prefix="turn")
        self._locks: dict[tuple, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._spawned: set = set()
        self._spawned_guard = threading.Lock()
        self._processed: set = set()

    def _key_lock(self, user_id: int, project: str) -> threading.Lock:
        key = (user_id, project)
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def reply(self, api_key: str, text: str, reply_to, project: str):
        user_api(api_key, "POST", "/api/bot/reply", {
            "text": text, "reply_to": reply_to, "project": project, "is_final": True,
        })

    def mark_processed(self, api_key: str, msg_id):
        # 마킹 유실 = pending 영구 잔류 = 폴링 핫루프 (2026-07-06 사고) → 백오프 재시도
        for attempt in range(3):
            result = user_api(api_key, "PATCH", "/api/bot/message",
                              {"id": msg_id, "updates": {"processed": True}})
            if result is not None:
                return
            time.sleep(1 + attempt * 2)
        logger.error(f"mark_processed FAILED after retries: msg #{msg_id}")

    def process(self, msg: dict):
        msg_id = msg.get("id")
        user_id = msg.get("user_id")
        project = msg.get("project", "general") or "general"
        text = (msg.get("text") or "").strip()

        user = roster.get(user_id)
        if not user:
            logger.warning(f"msg #{msg_id}: unknown cloud user {user_id}, skipping")
            return
        api_key = user["apiKey"]

        if msg_id in self._processed:
            self.mark_processed(api_key, msg_id)
            return
        self._processed.add(msg_id)
        if len(self._processed) > 2000:
            self._processed = {msg_id}

        self.mark_processed(api_key, msg_id)
        try:
            provision_unix_user(user_id)
        except Exception as e:
            logger.error(f"provision failed user={user_id}: {e}")
            self.reply(api_key, "(일시적 오류로 준비에 실패했어요. 잠시 후 다시 시도해주세요.)", [msg_id], project)
            return
        logger.info(f"msg #{msg_id} user={user_id} project={project}: {text[:60]}")

        # ── 로그인 플로우 ──
        with login_lock:
            session = login_sessions.get(user_id)
            if session and session.expired():
                session.terminate()
                del login_sessions[user_id]
                session = None

        code_match = CODE_RE.search(text.split("https://")[0]) if text else None
        if session and session.state == "waiting_code" and code_match:
            self.reply(api_key, "코드를 확인하고 있어요...", [msg_id], project)
            ok = session.submit_code(code_match.group(0))
            with login_lock:
                login_sessions.pop(user_id, None)
            if ok:
                self.reply(api_key,
                           "✅ 클로드 계정이 연결됐어요! 이제 뭐든 시켜보세요.",
                           [msg_id], project)
            else:
                self.reply(api_key,
                           "연결에 실패했어요. `재로그인` 이라고 입력해 다시 시도해주세요.",
                           [msg_id], project)
            return

        if text in ("재로그인", "/relogin", "리로그인", "로그인", "클로드 연결", "연결"):
            self.reply(api_key, "클로드 로그인을 시작할게요. 잠시만요...", [msg_id], project)
            new_session = LoginSession(user_id)
            with login_lock:
                old = login_sessions.pop(user_id, None)
                if old:
                    old.terminate()
                login_sessions[user_id] = new_session
            url = new_session.start()
            if url:
                self.reply(api_key,
                           f"아래 링크를 열어 클로드 계정으로 로그인한 뒤, 표시되는 코드를 여기에 붙여넣어 주세요:\n\n{url}",
                           [msg_id], project)
            else:
                with login_lock:
                    login_sessions.pop(user_id, None)
                self.reply(api_key,
                           "로그인 준비에 실패했어요. 잠시 후 `재로그인` 으로 다시 시도해주세요.",
                           [msg_id], project)
            return

        # ── 미연결 유저: 안내만 ──
        if not has_credentials(user_id):
            self.reply(api_key, NEED_LOGIN_MESSAGE, [msg_id], project)
            return

        # ── 일반 턴 ──
        response, status = run_claude_turn(user_id, project, text)
        if status == "auth":
            self.reply(api_key,
                       "클로드 로그인이 만료됐어요. `재로그인` 이라고 입력해 다시 연결해주세요.",
                       [msg_id], project)
            return
        if status in ("heavy_mem", "heavy_time"):
            self.report_heavy(api_key, user_id, project, msg_id, status, text)
            return
        # L0 사전 감지: Claude 가 무거운 작업이라 판단해 [HEAVY_TASK] 태그를 남긴 경우
        if "[HEAVY_TASK]" in response:
            response = response.replace("[HEAVY_TASK]", "").strip()
            subtype = "provision_suggest"
            user_api(api_key, "POST", "/api/cloud/heavy-event", {
                "kind": "intent", "project": project, "context": text[:200],
            })
        else:
            subtype = None
        chunks = _split_chunks(response)
        for i, chunk in enumerate(chunks):
            # CTA 카드는 마지막 청크에만 (설치 신청 버튼)
            st = subtype if (subtype and i == len(chunks) - 1) else None
            user_api(api_key, "POST", "/api/bot/reply", {
                "text": chunk, "reply_to": [msg_id], "project": project,
                "is_final": True, **({"subtype": st} if st else {}),
            })

    def report_heavy(self, api_key, user_id, project, msg_id, status, user_text):
        """리소스 한도 초과 → 이유 + 전환 안내(CTA). heavy 이벤트 기록도 함께."""
        lim = config.get("limits", {})
        if status == "heavy_mem":
            reason = f"메모리 한도({lim.get('memory_max', '3G')})를 넘어 작업이 중단됐어요."
        else:
            mins = int(lim.get("turn_timeout_sec", TURN_TIMEOUT_SEC)) // 60
            reason = f"작업이 시간 한도({mins}분)를 넘어 중단됐어요."
        msg = (
            f"⚠️ {reason}\n\n"
            "클라우드 피터보이스는 리서치·문서·가벼운 코드 작업에 맞춰져 있어요. "
            "지금처럼 무거운 작업(대용량 처리·머신러닝·상시 서버 등)은 "
            "**내 컴퓨터(맥/PC)에 피터를 직접 설치하면 제한 없이** 할 수 있어요.\n\n"
            "설정 화면에서 '내 AI 비서 설치'를 신청하시면 안내해드릴게요."
        )
        # subtype 으로 웹이 CTA 카드(설치 신청 버튼)를 렌더 + heavy 이벤트 기록
        user_api(api_key, "POST", "/api/bot/reply", {
            "text": msg, "reply_to": [msg_id], "project": project, "is_final": True,
            "subtype": "provision_suggest",
        })
        user_api(api_key, "POST", "/api/cloud/heavy-event", {
            "kind": status, "project": project, "context": user_text[:200],
        })

    def _process_safe(self, msg: dict):
        key = (msg.get("user_id"), msg.get("project", "general") or "general")
        msg_id = msg.get("id")
        try:
            with self._key_lock(*key):
                if shutdown_event.is_set():
                    return
                self.process(msg)
        except Exception as e:
            logger.error(f"process error msg #{msg_id}: {e}", exc_info=True)
            user = roster.get(msg.get("user_id"))
            if user:
                try:
                    self.reply(user["apiKey"], f"(처리 오류: {e})", [msg_id], key[1])
                except Exception:
                    pass
        finally:
            with self._spawned_guard:
                self._spawned.discard(msg_id)

    def heartbeats(self):
        """전 유저 온라인 표시 — 로스터의 각 유저로 heartbeat 전송."""
        with roster._lock:
            users = list(roster._users.values())
        for u in users:
            user_api(u["apiKey"], "POST", "/api/bot/heartbeat", {
                "is_working": False, "cloud": True,
            })

    def run(self):
        logger.info("cloud daemon started")
        # 재시작 시 지난 턴의 잔여 env 파일 정리 (600, 시크릿 잔존 방지)
        try:
            for f in _turnenv_dir().glob("*.env"):
                f.unlink()
        except Exception:
            pass
        roster.sync(force=True)
        last_heartbeat = 0.0
        poll_interval = config.get("poll_interval_sec", 3)
        errors = 0
        while not shutdown_event.is_set():
            try:
                now = time.time()
                roster.sync()
                if now - last_heartbeat > 30:
                    threading.Thread(target=self.heartbeats, daemon=True).start()
                    last_heartbeat = now

                result = host_api("GET", "/api/cloud/poll")
                if result is None:
                    errors += 1
                    shutdown_event.wait(min(30, 2 ** errors))
                    continue
                errors = 0

                spawned_any = False
                for msg in result.get("pending", []):
                    msg_id = msg.get("id")
                    with self._spawned_guard:
                        if msg_id in self._spawned:
                            continue
                        self._spawned.add(msg_id)
                    spawned_any = True
                    self._executor.submit(self._process_safe, msg)

                if not spawned_any:
                    shutdown_event.wait(poll_interval)
            except Exception as e:
                errors += 1
                logger.error(f"loop error: {e}", exc_info=True)
                shutdown_event.wait(min(30, 2 ** errors))
        logger.info("cloud daemon stopped")


def _split_chunks(text: str, limit: int = 3500) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def main():
    global config
    if not CONFIG_PATH.exists():
        print(f"config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    config = json.loads(CONFIG_PATH.read_text())
    for k in ("api_url", "host_key"):
        if not config.get(k):
            print(f"config missing: {k}", file=sys.stderr)
            sys.exit(1)

    def handle_sig(_sig, _frm):
        shutdown_event.set()
    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    CloudWorker().run()


if __name__ == "__main__":
    main()
