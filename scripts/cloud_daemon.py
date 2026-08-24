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
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import cloud_container as ctr

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

def user_allowed(user_id: int) -> bool:
    """이 호스트가 담당하는 유저인지. 호스트를 2대 이상 띄울 때 이중응답 방지용.

    로스터(/api/cloud/roster)는 daemon_target=cloud 전 유저를 돌려주므로,
    같은 host_key 로 호스트를 하나 더 띄우면 두 호스트가 같은 메시지를 처리한다.
    전용 호스트(예: 고객 전용기)는 users_allow 로, 공용 호스트는 users_deny 로
    담당 범위를 나눈다. 양쪽 config 를 반드시 같이 갱신할 것.
    """
    allow = config.get("users_allow")
    if allow is not None and user_id not in allow:
        return False
    return user_id not in (config.get("users_deny") or [])


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
                self._users = {u["userId"]: u for u in result["users"]
                               if user_allowed(u["userId"])}
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

    def peek(self, user_id: int) -> dict | None:
        """미스여도 동기화하지 않는 조회 — 핫패스(정렬 키·리퍼)용.
        get() 을 핫패스에 쓰면 타 호스트 유저 메시지마다 강제 sync HTTP 가 나간다."""
        with self._lock:
            return self._users.get(user_id)


roster = Roster()


# ── 티어 한도 (로스터 전달값) ────────────────────────────────────
# 수치의 단일 원천은 웹 lib/tiers.ts — 데몬은 로스터가 내려준 limits 를 그대로 쓴다.
# 전용 호스트 유저(dedicated)와 구 웹(limits 미제공)은 None → 기존 호스트 config 일괄값.

def user_limits(user_id: int, peek: bool = False) -> dict | None:
    u = roster.peek(user_id) if peek else roster.get(user_id)
    if not u or u.get("dedicated"):
        return None
    return u.get("limits") or None


# ── 월 턴 카운팅 ─────────────────────────────────────────────────
# 카운트의 단일 진실은 웹 DB(turn_usage) — 데몬은 턴 종료마다 POST 하고,
# 응답/로스터(turnsUsed)로 로컬 캐시를 갱신해 사전 차단에 쓴다.

_turn_usage: dict[int, tuple[str, int]] = {}   # uid -> (month "YYYY-MM" UTC, turns)
_turn_usage_lock = threading.Lock()
_quota_warned: set[tuple[int, str]] = set()     # (uid, month) — 90% 경고 1회


def _usage_month() -> str:
    return time.strftime("%Y-%m", time.gmtime())  # 웹 to_char(now(),'YYYY-MM')와 동일(UTC)


def turns_used(user_id: int) -> int:
    """이번 달 사용 턴. 로스터(60초 주기)와 로컬 카운트 중 큰 값 — 폴링 지연으로 과소평가 방지."""
    u = roster.get(user_id) or {}
    from_roster = int(u.get("turnsUsed") or 0)
    month = _usage_month()
    with _turn_usage_lock:
        cached = _turn_usage.get(user_id)
    local = cached[1] if cached and cached[0] == month else 0
    return max(from_roster, local)


def record_turn(user_id: int):
    """턴 1개 소비를 웹에 기록 (백그라운드, 실패해도 턴을 막지 않는다)."""
    def _post():
        result = host_api("POST", "/api/cloud/turns", {"user_id": user_id})
        if result and "turns" in result:
            with _turn_usage_lock:
                _turn_usage[user_id] = (result.get("month") or _usage_month(),
                                        int(result["turns"]))
        else:  # 웹 미배포/일시 실패 — 로컬만이라도 증가시켜 사전 차단이 뚫리지 않게
            month = _usage_month()
            base = turns_used(user_id)  # 락 밖에서 (turns_used 도 같은 락을 잡는다)
            with _turn_usage_lock:
                cur = _turn_usage.get(user_id)
                n = cur[1] + 1 if cur and cur[0] == month else base + 1
                _turn_usage[user_id] = (month, n)
    threading.Thread(target=_post, daemon=True).start()


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
    if ctr.enabled_for(user_id):
        # 컨테이너 모드: 홈 준비는 ctr.ensure_home 담당(agent uid 소유 + claude 700).
        # 여기서 chown/skills 심링크를 걸면 ubuntu 가 접근 못 하는 700 폴더를 파이썬으로
        # 건드려 턴 전체가 실패한다. 스킬은 컨테이너에 shared_skills 로 마운트됨.
        return
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
            # 번들 스킬: 유저 소유 skills/ 폴더 안에 번들 스킬을 개별 심링크
            # → 유저(claude)가 자기 스킬을 옆에 설치할 수 있음 (통짜 심링크는 읽기전용이라 불가했음)
            self_provision_skills(user_id)
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


_disk_cache: dict[int, tuple[float, bool]] = {}
_disk_lock = threading.Lock()
DISK_CHECK_TTL_SEC = 600


def disk_quota_gb(user_id: int) -> int:
    """이 유저의 디스크 한도(GB). 티어 한도 우선, 없으면 호스트 config."""
    lim = user_limits(user_id)
    if lim and lim.get("diskGb"):
        return int(lim["diskGb"])
    return config.get("limits", {}).get("disk_quota_gb", 0)


def over_disk_quota(user_id: int) -> bool:
    """유저 사용량이 한도 초과인지 (10분 캐시). du 는 무거우니 드물게.

    apt 설치물은 홈이 아니라 컨테이너 레이어에 쌓이므로 레이어 크기를 합산한다
    (프로젝트 프롬프트의 '알려진 함정' — du 만으로는 안 잡힌다)."""
    gb = disk_quota_gb(user_id)
    if not gb:
        return False
    now = time.time()
    with _disk_lock:
        c = _disk_cache.get(user_id)
        if c and now - c[0] < DISK_CHECK_TTL_SEC:
            return c[1]
    try:
        # root du — 모드(systemd/container)별 소유권 차이와 무관하게 측정
        r = subprocess.run(["sudo", "-n", "du", "-sb", str(user_workspace(user_id))],
                           capture_output=True, text=True, timeout=30)
        used = int((r.stdout or "0").split()[0]) if r.stdout.strip() else 0
        if ctr.enabled_for(user_id):
            used += ctr.layer_size_bytes(user_id)
        over = used > gb * 1024 ** 3
    except Exception:
        over = False
    with _disk_lock:
        _disk_cache[user_id] = (now, over)
    return over


def self_provision_skills(user_id: int):
    """유저 skills/ 폴더(유저 소유)에 번들 스킬을 개별 심링크로 채운다.
    유저가 자기 스킬을 같은 폴더에 추가 설치할 수 있게 하려는 목적.
    번들 스킬이 늘면 새 것만 추가된다 (기존 유지)."""
    shared = Path(config.get("shared_skills_dir", "/srv/pv/shared/skills"))
    if not shared.exists():
        return
    name = unix_user(user_id)
    skills_dir = user_claude_dir(user_id) / "skills"
    try:
        is_link = skills_dir.is_symlink()
    except OSError as e:  # 홈이 데몬(ubuntu)에게 닫혀 있는 경우 — 스킬만 포기, 턴은 진행
        logger.warning(f"skills provision skipped user={user_id}: {e}")
        return
    if is_link:  # 과거 통짜 심링크 → 실제 폴더로 전환
        subprocess.run(["sudo", "-n", "-u", name, "env", "rm", "-f", str(skills_dir)],
                       capture_output=True)
    subprocess.run(["sudo", "-n", "-u", name, "env", "mkdir", "-p", str(skills_dir)],
                   capture_output=True)
    for skill in shared.iterdir():
        if not skill.is_dir():
            continue
        link = skills_dir / skill.name
        if not link.exists():
            subprocess.run(
                ["sudo", "-n", "-u", name, "env", "ln", "-sfn", str(skill), str(link)],
                capture_output=True)


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
    if ctr.enabled_for(user_id):
        return ctr.has_credentials(user_id)
    cred = user_claude_dir(user_id) / ".credentials.json"
    if not isolation_enabled():
        return cred.exists()
    # sudoers 는 pvusers 강등 실행으로 env/claude 만 허용 → env 로 test 를 exec
    r = subprocess.run(
        ["sudo", "-n", "-u", unix_user(user_id), "env", "test", "-f", str(cred)],
        capture_output=True)
    return r.returncode == 0


PV_FEATURES_PROMPT = """
# 피터보이스 기능 (프로젝트 관리·이동)

유저는 피터보이스 웹 채팅 UI로 대화합니다. 왼쪽 사이드바에 **프로젝트** 목록이 있고,
프로젝트마다 독립된 대화 세션과 작업 폴더를 가집니다. 유저가 프로젝트 생성/전환을 요청하면
"직접 하셔야 한다"고 하지 말고 아래 방법으로 처리하세요 (인증: `X-Api-Key: $API_KEY` 헤더).

- 프로젝트 목록: `GET $API_URL/api/projects`
- 프로젝트 생성: `POST $API_URL/api/projects` — body `{"id": "영문소문자-숫자-하이픈", "name": "표시 이름"}`
  생성하면 사이드바에 바로 나타납니다. **워크스페이스에 폴더만 만드는 것은 프로젝트 생성이 아닙니다.**
- 프로젝트 역할(프롬프트) 설정: `PUT $API_URL/api/prompts` — body `{"project": "프로젝트ID", "content": "역할/지시 전문"}`
  특정 역할(예: 부사장, 마케터)로 동작할 프로젝트는 생성 직후 이걸로 역할을 부여하세요.

## 프로젝트 이동/전환
- 응답에 `[표시이름](/?project=프로젝트ID)` 마크다운 링크를 넣으면 유저가 클릭해 그 프로젝트 대화로 이동합니다.
- 유저가 "그 프로젝트로 바꿔줘/넘겨줘"라고 하면: 짧은 확인 멘트와 함께 응답 **마지막 줄**에
  `[[voice/handoff:프로젝트ID:한줄맥락]]` 마커를 붙이세요. 마커는 유저 화면·음성에서 숨겨지고,
  음성 모드에선 자동으로 대화가 전환되며 채팅 모드에선 이동 버튼으로 표시됩니다.
  - 한줄맥락: 유저가 실제 말한 용건만 한 줄로 (콜론 없이). 용건을 모르면 "유저 요청으로 전환"이라고만.
  - 전환 의도가 명확할 때만 마커 사용. 단순 언급·보고에는 위의 링크 형식을 쓰세요.
- 존재하지 않는 프로젝트 ID를 링크/마커에 쓰지 마세요 (목록 조회로 확인).

## 응답 규칙 (중요)
도구 실행 결과(명령 출력, 파일 내용)는 유저에게 보이지 않습니다. 전달할 내용은 반드시
응답 텍스트에 직접 포함하세요.
"""


def _container_system_prompt(user_id: int | None = None) -> str:
    """전용 컨테이너용 환경 안내. 실제 적용값을 그대로 알려준다
    (하드코딩하면 호스트·티어별로 사양을 잘못 안내하게 된다).
    user_id 가 있고 티어 한도가 오면 그 유저의 티어값으로 안내한다."""
    c = config.get("container", {}) or {}
    lim = user_limits(user_id) if user_id is not None else None
    if lim:
        mem_mb = min(int(lim.get("turnMemoryMb") or 3072), int(c.get("max_memory_mb", 6144)))
        mem = f"{mem_mb / 1024:g}GB"
        mins = int(lim.get("turnTimeoutMin") or 30)
        disk = int(lim.get("diskGb") or 0)
        hb_min = int(lim.get("automationMinIntervalMin")
                     or config.get("heartbeat_min_interval_min", 30))
    else:
        mem = str(c.get("memory", "3g")).upper().replace("G", "GB")
        mins = int(c.get("turn_timeout_sec", config.get("limits", {}).get(
            "turn_timeout_sec", TURN_TIMEOUT_SEC)) / 60)
        disk = config.get("limits", {}).get("disk_quota_gb", 0)
        hb_min = config.get("heartbeat_min_interval_min", 30)
    disk_txt = f"디스크 {disk}GB" if disk else "디스크 한도 없음"
    cpus = c.get("cpus", 1.5)
    dedicated = bool(config.get("dedicated"))
    intro = ("당신은 이 고객 **전용 서버** 위의 전용 리눅스 컨테이너에서 실행됩니다. "
             "다른 고객과 자원을 나눠 쓰지 않습니다."
             if dedicated else
             "당신은 이 사용자 전용 리눅스 컨테이너에서 실행됩니다.")
    return f"""# 실행 환경: 피터보이스 클라우드 (전용 컨테이너)

{intro} 이 안에서는 자유롭게 작업하세요.

## 가능한 것
- 패키지 설치 자유: `sudo apt-get install -y <패키지>`(비번 없이 됨), `pip install`, `npm install -g` 모두 가능.
  설치물은 유지됩니다.
- 서버/포트도 컨테이너 내부라 자유롭게 사용 가능 (다른 사용자와 격리됨).

## 이 환경의 실제 사양 (추측하지 말고 이 값을 그대로 안내할 것)
- 메모리 {mem} / CPU {cpus}코어 / 한 턴 최대 {mins}분 / {disk_txt}

## 함께 쓰는 사람들
이 프로젝트는 여러 사람이 함께 쓸 수 있습니다. 메시지가
`[팀원 OOO 님이 보낸 메시지]` 로 시작하면 **프로젝트 소유자가 아닌 팀원**이 보낸 것입니다.
- 그 표시가 없으면 소유자 본인입니다
- 이전 대화의 "내가"가 지금 말하는 사람과 다를 수 있으니, 사람이 바뀌면 그 사람 기준으로 답하세요
- 답변에는 이 표시를 따라 쓰지 마세요 (사람에게 보이는 건 대화 화면의 이름표입니다)

## 제약
- 위 한도를 넘으면 강제 종료됩니다. 대규모 학습·장시간 렌더링은 피하세요.
- **반복/예약 작업**: crontab 대신 피터보이스 HeartBeat 를 쓰세요
  (`docs/HEARTBEAT.md` + `POST $API_URL/api/tasks`, interval_min>={hb_min}).

무거운 작업(딥러닝 학습, GPU, 대량 처리)이 필요하면 응답에 `[HEAVY_TASK]` 를 포함하고,
사용자에게 "내 컴퓨터에 설치하면 제한 없이 가능하다"고 안내하세요.
가벼운·중간 작업은 자유롭게 하세요.
"""


def _shared_system_prompt(user_id: int | None = None) -> str:
    lim = config.get("limits", {})
    tl = user_limits(user_id) if user_id is not None else None
    if tl:
        mem = f"{int(tl.get('turnMemoryMb') or 3072) / 1024:g}GB"
        mins = int(tl.get("turnTimeoutMin") or 30)
        hb_min = int(tl.get("automationMinIntervalMin")
                     or config.get("heartbeat_min_interval_min", 30))
    else:
        mem = str(lim.get("memory_max", "3G")).upper().replace("G", "GB")
        mins = int(lim.get("turn_timeout_sec", TURN_TIMEOUT_SEC) / 60)
        hb_min = config.get("heartbeat_min_interval_min", 30)
    return CLOUD_SYSTEM_PROMPT_TMPL.format(mem=mem, mins=mins, hb_min=hb_min)


CLOUD_SYSTEM_PROMPT_TMPL = """# 실행 환경: 피터보이스 클라우드 (공유 서버)

당신은 여러 사용자가 공유하는 리눅스 서버에서, 이 사용자 전용 계정으로 격리 실행됩니다.
자원이 제한된 공유 환경이므로 아래를 지켜주세요.

## 환경 제약
- 한 번의 작업(턴)은 메모리 {mem}, CPU, {mins}분 시간 제한이 걸려 있습니다. 초과하면 강제 종료됩니다.
- 시스템 전역 설치(apt, sudo, npm -g)는 불가합니다. 대신:
  - 파이썬 패키지: `pip install --user` 또는 가상환경(venv)을 홈 아래에 만들어 사용
  - node 패키지: 프로젝트 폴더에 로컬 설치(`npm install`), node 버전 변경은 홈에 nvm 설치
- 서버/데몬을 상시 띄우지 마세요. 확인용으로 잠깐 띄웠다면 작업 끝에 반드시 종료하세요.
- 대용량 데이터, 파일은 작업 폴더에만. 디스크도 사용자별 한도가 있습니다.
- **반복/예약 작업(cron)**: crontab은 사용할 수 없습니다. 대신 피터보이스 **HeartBeat**를 쓰세요.
  사용자가 "매일 아침 뉴스 정리해줘", "1시간마다 확인해줘" 같은 반복 작업을 원하면
  `docs/HEARTBEAT.md`에 체크리스트를 쓰고 `POST $API_URL/api/tasks`로 등록하세요
  (interval_min 최소 {hb_min}, max_runs 필수). 이렇게 하면 정해진 주기마다 저를 깨워 처리하며,
  리소스 한도 안에서 안전하게 동작합니다.

## 함께 쓰는 사람들
이 프로젝트는 여러 사람이 함께 쓸 수 있습니다. 메시지가
`[팀원 OOO 님이 보낸 메시지]` 로 시작하면 **프로젝트 소유자가 아닌 팀원**이 보낸 것입니다.
표시가 없으면 소유자 본인입니다. 사람이 바뀌면 그 사람 기준으로 답하고, 답변에 이 표시를 따라 쓰지 마세요.

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


def _dedicated_host_prompt() -> str:
    """전용 호스트 + 컨테이너 없는 모드용 환경 안내.

    공유 서버 문구를 그대로 쓰면 apt·sudo 불가, 공유 환경 등 사실과 다른 안내가 나간다
    (2026-07-29 뉴넥스 내부 서버에서 실제 발생). 전용 서버는 하드웨어 전부가 이 고객 것이다."""
    lim = config.get("limits", {})
    mem = str(lim.get("memory_max", "6G")).upper().replace("G", "GB")
    mins = int(lim.get("turn_timeout_sec", TURN_TIMEOUT_SEC) / 60)
    disk = lim.get("disk_quota_gb", 0)
    hb_min = config.get("heartbeat_min_interval_min", 30)
    return f"""# 실행 환경: 피터보이스 (고객 전용 서버)

당신은 이 고객사 **전용 서버**에서 실행됩니다. 다른 사용자와 자원을 나눠 쓰지 않습니다.

## 가능한 것
- 패키지 설치 자유: `sudo apt-get install -y <패키지>`, `pip install`, `npm install -g` 모두 가능. 설치물은 유지됩니다
- 서버/포트도 내부에서 자유롭게 사용 가능

## 이 서버의 실제 사양 (추측하지 말고 이 값을 그대로 안내할 것)
- 메모리 {mem} / 한 턴 최대 {mins}분 / 디스크 {disk}GB

## 제약
- **인터넷 아웃바운드는 443·80·53 포트만 열려 있습니다.** 그 외 포트(DB 직결, SSH 등)는 차단됩니다
- **반복/예약 작업**: crontab 대신 피터보이스 HeartBeat 를 쓰세요
  (`docs/HEARTBEAT.md` + `POST $API_URL/api/tasks`, interval_min>={hb_min})

## 함께 쓰는 사람들
메시지가 `[팀원 OOO 님이 보낸 메시지]` 로 시작하면 프로젝트 소유자가 아닌 팀원이 보낸 것입니다.
표시가 없으면 소유자 본인입니다. 사람이 바뀌면 그 사람 기준으로 답하고, 답변에 이 표시를 따라 쓰지 마세요.

GPU 가 필요한 작업만 불가합니다. 그 외 작업은 자유롭게 하세요.
"""


def _ensure_cloud_system_prompt(user_id: int) -> str | None:
    """클라우드 환경 가이드 시스템 프롬프트 파일 (유저 워크스페이스에 1회 생성, ACL로 pv유저 읽기 가능)."""
    try:
        p = user_workspace(user_id) / ".cloud-system-prompt.md"
        text = _dedicated_host_prompt() if config.get("dedicated") else _shared_system_prompt(user_id)
        if not p.exists() or p.read_text() != text:
            p.write_text(text)
            os.chmod(p, 0o644)
        return str(p)
    except OSError:
        return None


# ── 프롬프트 주입 (셀프호스팅 데몬과 동일한 두뇌 세팅) ────────────
# 시스템 가이드(_petervoice_system) + 실행환경 + 공통(_common) + 프로젝트/브랜치 프롬프트를
# 웹 API 에서 가져와 턴마다 합성 주입한다. 웹 장애 시엔 환경 프롬프트만으로 동작 (턴을 막지 않는다).

_PROMPT_TTL_SEC = 60
_prompt_cache: dict[tuple[int, str], tuple[float, str]] = {}
_prompt_cache_lock = threading.Lock()


def _fetch_web_prompt(user_id: int, api_key: str, project: str,
                      context: str | None = None) -> str:
    key = (user_id, project)
    now = time.time()
    with _prompt_cache_lock:
        hit = _prompt_cache.get(key)
    if hit and now - hit[0] < _PROMPT_TTL_SEC:
        return hit[1]
    path = f"/api/prompts?project={urllib.parse.quote(project, safe='')}"
    if context:
        path += f"&context={urllib.parse.quote(context, safe='')}"
    result = user_api(api_key, "GET", path)
    if result is None:
        # 조회 실패 — 이전 캐시가 있으면 유지 (일시 장애로 두뇌가 빠지지 않게)
        return hit[1] if hit else ""
    content = (result.get("content") or "").strip()
    with _prompt_cache_lock:
        _prompt_cache[key] = (now, content)
    return content


def _env_system_prompt(user_id: int) -> str:
    if ctr.enabled_for(user_id):
        return _container_system_prompt(user_id)
    if config.get("dedicated"):
        return _dedicated_host_prompt()
    return _shared_system_prompt(user_id)


def compose_system_prompt(user_id: int, project: str) -> str:
    """턴 주입용 시스템 프롬프트 합성. 순서: 시스템 가이드 → 실행환경 → 공통 → 프로젝트/브랜치."""
    user = roster.get(user_id)
    api_key = (user or {}).get("apiKey") or ""
    sys_p = common = proj_p = ""
    if api_key:
        sys_p = _fetch_web_prompt(user_id, api_key, "_petervoice_system")
        ctx = None if project.startswith("branch:") else project
        common = _fetch_web_prompt(user_id, api_key, "_common", context=ctx)
        proj_p = _fetch_web_prompt(user_id, api_key, project)
    parts = []
    if sys_p:
        parts.append(sys_p)
    # 시스템 가이드를 못 가져왔을 때만 응급 기능 안내(PV_FEATURES_PROMPT)로 보강 (중복 방지)
    parts.append(_env_system_prompt(user_id) + ("" if sys_p else PV_FEATURES_PROMPT))
    if common:
        parts.append(f"# 공통 지침 (유저 설정)\n\n{common}")
    if proj_p:
        label = "브랜치 프롬프트" if project.startswith("branch:") else "프로젝트 프롬프트"
        parts.append(f"# {label}\n\n{proj_p}")
    return "\n\n".join(parts)


def _sysprompt_filename(project: str) -> str:
    return f".pv-sysprompt-{re.sub(r'[^A-Za-z0-9_-]', '-', project)}.md"


def _write_turn_sysprompt(user_id: int, project: str) -> str | None:
    """비컨테이너 턴용: 합성 프롬프트를 프로젝트별 파일로 기록 (동시 턴 간 파일 경합 방지)."""
    try:
        p = user_workspace(user_id) / _sysprompt_filename(project)
        text = compose_system_prompt(user_id, project)
        if not p.exists() or p.read_text() != text:
            p.write_text(text)
            os.chmod(p, 0o644)
        return str(p)
    except OSError:
        return None


# ── 모델/에포트 (웹 프로젝트 설정 반영) ───────────────────────────
# 웹 UI 에서 고른 프로젝트·브랜치 모델을 실제 턴에 반영한다.
# 우선순위는 셀프호스팅 데몬(daemon/claude_runner.py)과 동일: 브랜치 > 프로젝트 > 호스트 config.
# 어디에도 지정이 없으면 --model 을 붙이지 않는다 (기존 동작 = CLI 기본 모델 유지).

_MODEL_TTL_SEC = 120
_proj_settings_cache: dict[int, tuple[float, dict]] = {}
_branch_settings_cache: dict[int, tuple[float, dict]] = {}
_model_cache_lock = threading.Lock()
_BRANCH_RE = re.compile(r"^branch:(\d+)$")


def _fetch_project_settings(user_id: int, api_key: str) -> dict:
    """유저의 프로젝트 설정 {project_id: row}. 조회 실패 시 이전 캐시 유지."""
    now = time.time()
    with _model_cache_lock:
        hit = _proj_settings_cache.get(user_id)
    if hit and now - hit[0] < _MODEL_TTL_SEC:
        return hit[1]
    result = user_api(api_key, "GET", "/api/projects")
    if result is None:
        return hit[1] if hit else {}
    rows = {str(p.get("id")): p for p in (result.get("projects") or []) if p.get("id")}
    with _model_cache_lock:
        _proj_settings_cache[user_id] = (now, rows)
    return rows


def _fetch_branch_settings(user_id: int, api_key: str) -> dict:
    """유저의 active 브랜치 설정 {branch_id: row}. 브랜치 턴에서만 호출한다(응답이 큼)."""
    now = time.time()
    with _model_cache_lock:
        hit = _branch_settings_cache.get(user_id)
    if hit and now - hit[0] < _MODEL_TTL_SEC:
        return hit[1]
    result = user_api(api_key, "GET", "/api/branches?all_active=1")
    if result is None:
        return hit[1] if hit else {}
    rows = {}
    for b in (result.get("branches") or []):
        try:
            rows[int(b.get("id"))] = b
        except (TypeError, ValueError):
            continue
    with _model_cache_lock:
        _branch_settings_cache[user_id] = (now, rows)
    return rows


def resolve_model_effort(user_id: int, project: str) -> tuple[str | None, str | None]:
    """이 턴에 쓸 (model, effort). 없으면 (None, None) → CLI 기본값."""
    user = roster.get(user_id)
    api_key = (user or {}).get("apiKey") or ""
    model = effort = None
    if api_key:
        try:
            projects = _fetch_project_settings(user_id, api_key)
            m = _BRANCH_RE.match(project or "")
            if m:
                branch = _fetch_branch_settings(user_id, api_key).get(int(m.group(1))) or {}
                model, effort = branch.get("model"), branch.get("effort")
                parent = projects.get(str(branch.get("project_id") or "")) or {}
                model = model or parent.get("model")
                effort = effort or parent.get("effort")
            else:
                row = projects.get(project or "") or {}
                model, effort = row.get("model"), row.get("effort")
        except Exception as e:  # 설정 조회 실패가 턴을 막지 않게
            logger.warning(f"[model] resolve failed user={user_id} project={project}: {e}")
    model = model or config.get("claude_model")
    effort = effort or config.get("claude_effort")
    # engine=codex 로 쓰던 프로젝트에 gpt-* 코드가 남아 있으면 claude CLI 가 거부한다 → 무시
    if model and str(model).startswith("gpt-"):
        model = config.get("claude_model")
        if model and str(model).startswith("gpt-"):
            model = None
    return (str(model) if model else None, str(effort) if effort else None)


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


def turn_timeout_sec(user_id: int) -> int:
    """이 유저의 턴 시간 한도(초). 티어 한도 우선, 없으면 호스트 config."""
    tl = user_limits(user_id)
    if tl and tl.get("turnTimeoutMin"):
        return int(tl["turnTimeoutMin"]) * 60
    return int(config.get("limits", {}).get("turn_timeout_sec", TURN_TIMEOUT_SEC))


def build_systemd_run(user_id: int, cmd: list[str], env: dict, cwd: str, unit: str,
                      env_file: str) -> list[str]:
    lim = config.get("limits", {})
    tl = user_limits(user_id)
    if tl and tl.get("turnMemoryMb"):
        # 티어 값에도 호스트 물리 보호 캡 적용 (컨테이너 경로와 동일).
        # 캡 없이는 MAX 8192M 이 7.6GB 공용 호스트에서 그대로 허용돼 프리즈 원인이 됐다 (2026-08-06).
        host_cap = int(config.get("container", {}).get("max_memory_mb", 6144))
        mem = f"{min(int(tl['turnMemoryMb']), host_cap)}M"
    else:
        mem = lim.get("memory_max", "3G")
    cpu = lim.get("cpu_quota", "150%")
    tasks = str(lim.get("tasks_max", 256))
    runtime_max = str(turn_timeout_sec(user_id))
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


def kill_systemd_turns(user_id: int) -> None:
    """이 데몬 프로세스가 띄운 해당 유저의 턴 유닛을 정지한다 (systemd 격리 모드)."""
    prefix = f"pvturn-{user_id}-{os.getpid()}-"
    r = subprocess.run(["systemctl", "list-units", "--no-legend", "--plain",
                        f"{prefix}*.service"], capture_output=True, text=True, timeout=10)
    for line in (r.stdout or "").splitlines():
        unit = line.split()[0] if line.split() else ""
        if unit.startswith(prefix):
            subprocess.run(["sudo", "-n", "systemctl", "stop", unit],
                           capture_output=True, timeout=15)


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


_SENDER_RE = re.compile(r"[\x00-\x1f\[\]\n]")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def fetch_attachments(user_id: int, project: str, files: list) -> list[str]:
    """첨부 파일을 워크스페이스 `docs/uploaded/` 에 내려받아, **claude 가 보는 경로**로 돌려준다.

    맥 데몬(daemon/worker.py)의 규약을 클라우드로 이식한 것 — 이게 없어서 클라우드 유저의
    이미지 첨부가 통째로 무시됐다(2026-07-29 뉴넥스에서 발견). 파일별 실패는 건너뛴다."""
    if not files:
        return []
    pdir = project_dir(user_id, project)
    updir = pdir / "docs" / "uploaded"
    try:
        updir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"attachment dir failed user={user_id}: {e}")
        return []
    out: list[str] = []
    for f in files:
        url = (f.get("url") or "").strip()
        name = re.sub(r"[^\w.\-가-힣 ]", "_", f.get("name") or "file")[:80]
        if not url.startswith(("http://", "https://")):
            continue
        dest = updir / f"{int(time.time() * 1000)}_{name}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pv-cloud"})
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as w:
                shutil.copyfileobj(r, w, 1 << 16)
            os.chmod(dest, 0o644)  # 격리 모드에서 pv<id>/agent 가 읽을 수 있게
        except Exception as e:
            logger.error(f"attachment download failed {name}: {e}")
            continue
        if ctr.enabled_for(user_id):
            out.append(f"/home/agent/workspace/{pdir.name}/docs/uploaded/{dest.name}")
        else:
            out.append(str(dest))
    return out


def with_attachments(text: str, paths: list[str]) -> str:
    if not paths:
        return text
    lines = "\n".join(f"- {p}" for p in paths)
    exts = {os.path.splitext(p)[1].lower() for p in paths}
    has_img, has_doc = bool(exts & IMAGE_EXTS), bool(exts - IMAGE_EXTS)
    if has_img and has_doc:
        hint = "이미지는 Read 도구로, 문서(xlsx/docx/pdf 등)는 Bash에서 python으로 읽으세요"
    elif has_doc:
        hint = "Bash에서 python으로 파일 내용을 읽으세요 (예: openpyxl, python-docx, PyPDF2 등)"
    else:
        hint = "Read 도구로 확인하세요"
    return f"{text}\n\n[첨부 파일 ({hint})]\n{lines}"


def with_sender(msg: dict, text: str) -> str:
    """팀 프로젝트에서 온 메시지에 발신자를 표시한다.

    메시지는 프로젝트 **소유자**의 user_id 로 저장되므로(팀원이 보내도 마찬가지),
    이걸 붙이지 않으면 여러 사람이 한 세션을 써도 비서가 전부 같은 사람으로 인식한다.
    소유자 본인이 보낸 메시지에는 아무것도 붙이지 않는다(기존 동작 유지)."""
    name = (msg.get("from_username") or "").strip()
    if not name or msg.get("from_user_id") in (None, msg.get("user_id")):
        return text
    return f"[팀원 {_SENDER_RE.sub('', name)[:40]} 님이 보낸 메시지]\n{text}"


def run_claude_turn(user_id: int, project: str, prompt: str) -> tuple[str, str]:
    """한 턴 실행. returns (응답 텍스트, 상태).
    상태: "ok" | "auth" (재로그인 필요) | "heavy_mem" (메모리 초과) | "heavy_time" (시간 초과)."""
    # T3: 컨테이너 모드 유저는 podman exec 경로
    if ctr.enabled_for(user_id):
        user = roster.get(user_id)
        secrets = fetch_user_secrets(user_id, user["apiKey"]) if user else {}
        # 에이전트가 피터보이스 API(HeartBeat 등록, 브라우저 인계 등)를 부를 수 있게
        # API_URL/API_KEY 를 env 로 제공 (시스템 프롬프트가 $API_URL 을 참조한다).
        # 유저가 SecretsPanel 에 같은 키를 직접 넣었으면 그 값을 존중.
        secrets = dict(secrets)
        secrets.setdefault("API_URL", config["api_url"])
        if user:
            secrets.setdefault("API_KEY", user["apiKey"])
        # 이 턴의 대화 라우팅 값 (브랜치면 branch:N) — 스킬이 API 회신 project 로 사용
        secrets["PV_PROJECT"] = project
        # 컨테이너 안 CDP 는 항상 9222 (호스트로는 19000+uid 로 퍼블리시됨)
        secrets["PV_CDP_PORT"] = "9222"
        sp = ctr.sysprompt_path_in_home(user_id, compose_system_prompt(user_id, project),
                                        name=_sysprompt_filename(project))
        sid = load_session(user_id, project)
        model, effort = resolve_model_effort(user_id, project)
        rc, out, err = ctr.exec_claude_turn(user_id, project, prompt, sid, secrets, sp,
                                            limits=user_limits(user_id),
                                            model=model, effort=effort)
        if rc == 137:
            return ("", "heavy_mem")
        if rc == 124:
            return ("", "heavy_time")
        return _parse_turn_result(rc, out, err, user_id, project, prompt, sid)

    claude_cmd = config.get("claude_cmd", "claude")
    cmd = [
        claude_cmd, "-p",
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    # 웹에서 고른 프로젝트/브랜치 모델·에포트 반영 (지정 없으면 CLI 기본값)
    model, effort = resolve_model_effort(user_id, project)
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])
    logger.info(f"[turn] user={user_id} project={project} model={model or '(default)'} "
                f"effort={effort or '(default)'}")
    sp = _write_turn_sysprompt(user_id, project) or _ensure_cloud_system_prompt(user_id)
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
    secrets = dict(secrets)
    secrets.setdefault("API_URL", config["api_url"])
    if user:
        secrets.setdefault("API_KEY", user["apiKey"])
    # 이 턴의 대화 라우팅 값 (브랜치면 branch:N) — 스킬이 API 회신 project 로 사용
    secrets["PV_PROJECT"] = project
    # 비컨테이너 실행: chromium 이 호스트에서 직접 돌므로 포탈이 유도하는 유저별 포트를 써야 한다
    secrets["PV_CDP_PORT"] = str(ctr.CDP_PORT_BASE + user_id)
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
                                timeout=turn_timeout_sec(user_id) + 30)
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
        if ctr.enabled_for(self.user_id):
            pid, fd, url = ctr.login_start(self.user_id)
            if url:
                self._pid, self._fd, self.url = pid, fd, url
                self.state = "waiting_code"
                return url
            self.state = "failed"
            return None
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


# ── Claude 사용 한도(/usage) 수집 ─────────────────────────────────
# 맥미니(self) 데몬의 daemon/heartbeat.py fetch_usage_limits 와 동일한 파싱 로직을
# 유저별 환경(HOME/CLAUDE_CONFIG_DIR 또는 컨테이너)에서 실행하는 멀티테넌트 버전.
# 결과는 각 유저의 api_key 로 PATCH /api/bot/status → 웹 사용량 배너가 표시된다.

USAGE_INTERVAL_SEC = 15 * 60
USAGE_REFRESH_POLL_SEC = 30
USAGE_PROBE_TIMEOUT_SEC = 60


def _parse_usage_output(out: str) -> dict | None:
    """`claude -p /usage` 출력에서 세션/주간 리밋 % + 리셋 시각 파싱."""
    if not out or not out.strip():
        return None

    def _pct(pattern: str):
        m = re.search(pattern, out)
        if not m:
            return None, None
        pct = int(m.group(1))
        reset = (m.group(2).strip() if m.lastindex and m.lastindex >= 2 else "")
        reset = re.sub(r"\s*\(.*\)$", "", reset).strip()  # "(Asia/Seoul)" 제거
        return pct, reset

    session_pct, session_reset = _pct(r"Current session:\s*(\d+)% used(?:\s*·\s*resets\s*(.+))?")
    week_pct, week_reset = _pct(r"Current week \(all models\):\s*(\d+)% used(?:\s*·\s*resets\s*(.+))?")
    fable_pct, _ = _pct(r"Current week \(Fable\):\s*(\d+)% used")
    if session_pct is None and week_pct is None:
        return None
    return {
        "session_pct": session_pct,
        "week_pct": week_pct,
        "week_fable_pct": fable_pct,
        "session_reset": session_reset,
        "week_reset": week_reset,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def probe_usage(user_id: int, allow_wake: bool = False) -> dict | None:
    """해당 유저 환경에서 /usage 실행 → 파싱 결과. 실패/미로그인이면 None.
    allow_wake=False 면 꺼진 컨테이너는 건너뛴다(유휴 정지 유지)."""
    if ctr.enabled_for(user_id):
        if allow_wake and ctr.container_state(user_id) != "running":
            # 아직 한 번도 값을 못 받은 유저 — 배너가 아예 안 뜨므로 1회는 깨워서 수집.
            # 단 **로그인한 유저만** 깨운다. 미로그인 유저는 /usage 가 영영 실패해
            # _usage_ok 에 들어가지 못하고 15분마다 빈 컨테이너를 띄우는 루프가 된다.
            if not ctr.has_credentials(user_id):
                return None
            ctr.ensure_running(user_id)
        rc, out, err = ctr.exec_claude_cmd(user_id, ["-p", "/usage"],
                                           timeout=USAGE_PROBE_TIMEOUT_SEC)
        if rc != 0 and not (out or "").strip():
            return None
        return _parse_usage_output((out or "") + "\n" + (err or ""))

    if not has_credentials(user_id):
        return None  # 클로드 미로그인 → 배너 숨김이 정상

    cmd = [config.get("claude_cmd", "claude"), "-p", "/usage"]
    cwd = str(user_workspace(user_id))
    try:
        if isolation_enabled():
            wrapped = wrap_isolated(user_id, cmd, isolated_env_overrides(user_id), cwd)
            r = subprocess.run(wrapped, capture_output=True, text=True,
                               timeout=USAGE_PROBE_TIMEOUT_SEC)
        else:
            r = subprocess.run(cmd, cwd=cwd, env=claude_env(user_id),
                               capture_output=True, text=True,
                               timeout=USAGE_PROBE_TIMEOUT_SEC)
    except Exception as e:
        logger.warning(f"[usage] probe failed user={user_id}: {e}")
        return None
    return _parse_usage_output((r.stdout or "") + "\n" + (r.stderr or ""))


def _usage_ok_path() -> Path:
    return _state_dir() / "usage_ok.json"


def _load_usage_ok() -> set[int]:
    """한 번이라도 값 수집에 성공한 유저 — 재시작 후 컨테이너를 다시 깨우지 않도록 파일로 유지."""
    try:
        return set(json.loads(_usage_ok_path().read_text()))
    except Exception:
        return set()


_usage_ok: set[int] = set()


def _claude_account_email(user_id: int) -> str | None:
    """유저 Claude 계정 이메일 (CLAUDE_CONFIG_DIR/.claude.json 의 oauthAccount).
    맥 데몬의 배너 계정 표시(daemon/heartbeat.py)와 동일 기능 — 이 필드가 없으면
    웹 UsageBanner 에 "어느 계정으로 로그인돼 있는지" 버튼 자체가 안 뜬다.
    홈이 다른 uid 소유(700)라 직접 읽기 실패 시 sudo cat 으로 폴백."""
    path = user_claude_dir(user_id) / ".claude.json"
    text = None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        try:
            r = subprocess.run(["sudo", "-n", "cat", str(path)],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                text = r.stdout
        except Exception:
            pass
    if not text:
        return None
    try:
        return (json.loads(text).get("oauthAccount") or {}).get("emailAddress") or None
    except Exception:
        return None


def collect_and_store_usage(user_id: int, api_key: str, allow_wake: bool = False) -> bool:
    limits = probe_usage(user_id, allow_wake=allow_wake)
    if not limits:
        return False
    limits["account"] = _claude_account_email(user_id)
    if user_id not in _usage_ok:
        _usage_ok.add(user_id)
        try:
            _usage_ok_path().write_text(json.dumps(sorted(_usage_ok)))
        except OSError:
            pass
    user_api(api_key, "PATCH", "/api/bot/status", {"usage_limits": limits})
    logger.info(f"[usage] user={user_id} session={limits.get('session_pct')}% "
                f"week={limits.get('week_pct')}%")
    return True


_usage_last: dict[int, float] = {}
_usage_last_lock = threading.Lock()

# 폴링 응답(/api/cloud/poll)이 실어다 주는 "배너 새로고침 요청" 유저 목록.
# 메인 루프가 3초마다 갱신하고 사용량 스레드가 읽는다 — 유저별 status 조회를 없애기 위함.
_usage_refresh: set[int] = set()
_usage_refresh_lock = threading.Lock()
# 새 새로고침 요청이 폴링에 실려 오면 UsageThread 를 즉시 깨운다. 이게 없으면 버튼을 눌러도
# 30초 루프가 돌아올 때까지 기다려야 해서 웹 배너 스피너(25초 안전장치)가 먼저 포기하고
# 옛 값을 보여준 뒤 몇 초 뒤에야 값이 바뀌는 것처럼 보였다 (2026-08-24 뉴넥스 재로그인 실측).
_usage_wake = threading.Event()


def _set_usage_refresh(user_ids) -> None:
    wanted = {int(u) for u in (user_ids or [])}
    with _usage_refresh_lock:
        new = wanted - _usage_refresh
        _usage_refresh.clear()
        _usage_refresh.update(wanted)
    if new:
        _usage_wake.set()


def _usage_refresh_wanted(user_id: int) -> bool:
    with _usage_refresh_lock:
        return user_id in _usage_refresh


def collect_usage_after_turn(user_id: int, api_key: str):
    """턴 직후 수집 — 컨테이너 유저는 대화 중에만 컨테이너가 떠 있으므로
    주기 스레드만으로는 값이 안 잡힐 수 있다. 5분 쿨다운."""
    with _usage_last_lock:
        if time.time() - _usage_last.get(user_id, 0) < 300:
            return
        _usage_last[user_id] = time.time()
    threading.Thread(target=collect_and_store_usage, args=(user_id, api_key),
                     daemon=True, name=f"usage-{user_id}").start()


class UsageThread(threading.Thread):
    """유저별 사용 한도 수집 — 15분 주기 + 배너 새로고침 버튼(usage_refresh) 대응."""

    def __init__(self):
        super().__init__(daemon=True, name="usage")
        self._last = _usage_last

    def _users(self) -> list[dict]:
        with roster._lock:
            return list(roster._users.values())

    REFRESH_MAX_TRIES = 5  # 새로고침 재시도 상한 (30초 주기 → 최대 ~2.5분)

    def run(self):
        _usage_ok.update(_load_usage_ok())
        logger.info("[usage] thread started")
        shutdown_event.wait(30)  # 기동 직후 로스터 동기화 대기
        retry_counts: dict[int, int] = {}  # uid → 새로고침 연속 실패 횟수
        while not shutdown_event.is_set():
            try:
                now = time.time()
                for u in self._users():
                    if shutdown_event.is_set():
                        break
                    uid, api_key = u.get("userId"), u.get("apiKey")
                    if uid is None or not api_key:
                        continue
                    due = now - self._last.get(uid, 0) > USAGE_INTERVAL_SEC
                    # 배너 새로고침 버튼 요청 — 폴링 응답이 실어다 준 목록으로 판단한다.
                    # 유저마다 GET /api/bot/status 를 때리면 안 쓰는 유저까지
                    # 하루 2,880회가 붙는다(가입자 수에 선형 비례).
                    refresh = _usage_refresh_wanted(uid)
                    if not due and not refresh:
                        continue
                    self._last[uid] = time.time()
                    # 직렬 실행 (부하 제한). 값이 한 번도 없는 유저와 명시적 새로고침은
                    # 컨테이너를 깨워서라도 수집 (버튼을 누른 유저는 지금 값을 원한다)
                    ok = collect_and_store_usage(
                        uid, api_key, allow_wake=(refresh or uid not in _usage_ok))
                    if not refresh:
                        continue
                    # 새로고침 플래그는 **성공했을 때만** 내린다. 실패를 조용히 삼키면
                    # 버튼이 "눌렀는데 안 바뀜"이 된다 (2026-08-20 뉴넥스 재로그인 직후 실측).
                    # /usage 가 계속 실패하는 환경(미로그인 등)에서 무한 재시도가 되지 않게
                    # 상한 후에는 포기 로그를 남기고 내린다.
                    if ok or retry_counts.get(uid, 0) + 1 >= self.REFRESH_MAX_TRIES:
                        if not ok:
                            logger.warning(f"[usage] on-demand refresh gave up user={uid} "
                                           f"({self.REFRESH_MAX_TRIES} tries)")
                        else:
                            logger.info(f"[usage] on-demand refresh done user={uid}")
                        user_api(api_key, "PATCH", "/api/bot/status",
                                 {"usage_refresh": False})
                        retry_counts.pop(uid, None)
                    else:
                        retry_counts[uid] = retry_counts.get(uid, 0) + 1
                        logger.info(f"[usage] on-demand refresh failed user={uid} — "
                                    f"retry {retry_counts[uid]}/{self.REFRESH_MAX_TRIES}")
            except Exception as e:
                logger.error(f"[usage] loop error: {e}", exc_info=True)
            # 30초 주기 대기 — 단, 새 새로고침 요청이 오면 즉시 깨어난다
            _usage_wake.wait(USAGE_REFRESH_POLL_SEC)
            _usage_wake.clear()
        logger.info("[usage] thread stopped")


class HandoffThread(threading.Thread):
    """브라우저 세션 인계 폴러 (10초 주기).

    활성 인계 동안 ① 컨테이너 유휴정지 억제(keep_alive) ② CDP 포트 매핑 보장(재생성 포함)
    ③ 헤드리스 chromium 기동을 책임진다. 유저가 인계 페이지를 열었을 때 브라우저가
    죽어 있으면 안 되므로 데몬이 생존을 보장한다.
    설계: peter-voice docs/plans/2026-07-30-browser-session-handoff.md"""

    KEEPALIVE_GRACE_SEC = 120

    def __init__(self):
        super().__init__(daemon=True, name="handoff")

    @staticmethod
    def _parse_iso(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def run(self):
        logger.info("[handoff] thread started")
        shutdown_event.wait(15)
        while not shutdown_event.is_set():
            try:
                self.tick()
            except Exception as e:
                logger.error(f"[handoff] loop error: {e}")
            shutdown_event.wait(10)
        logger.info("[handoff] thread stopped")

    def tick(self):
        if not config.get("container", {}).get("enabled"):
            return
        res = host_api("GET", "/api/cloud/handoffs")
        if not res:
            return
        for h in res.get("handoffs", []):
            uid = h.get("user_id")
            if uid is None or not user_allowed(uid):
                continue
            uid = int(uid)
            until = self._parse_iso(h.get("expires_at")) or (time.time() + 900)
            ctr.keep_alive(uid, until + self.KEEPALIVE_GRACE_SEC)
            ctr.ensure_browser(uid)


# ── 메시지 처리 ──────────────────────────────────────────────────

class CloudWorker:
    def __init__(self):
        self._executor = ThreadPoolExecutor(
            max_workers=config.get("max_concurrent", 5), thread_name_prefix="turn")
        self._locks: dict[tuple, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._spawned: set = set()
        # 실제로 처리에 들어간 메시지 (processed 마킹까지 끝난 것) — 종료 시 되돌리기용
        self._inflight: dict = {}
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

        # ── 티어 월 턴 한도 초과 → 차단 안내 (턴 미소비) ──
        tl = user_limits(user_id)
        if tl and tl.get("turnsPerMonth"):
            used = turns_used(user_id)
            if used >= int(tl["turnsPerMonth"]):
                self.reply(api_key,
                           (f"⚠️ 이번 달 턴 한도({tl['turnsPerMonth']}턴)를 모두 사용했어요.\n\n"
                            "다음 달 1일에 초기화돼요. 더 많은 턴이 필요하면 "
                            "**요금제 업그레이드**를 문의해주세요 (설정 > 문의하기)."),
                           [msg_id], project)
                return

        # ── 디스크 쿼터 초과 → 전환 안내 ──
        if over_disk_quota(user_id):
            gb = disk_quota_gb(user_id) or 10
            user_api(api_key, "POST", "/api/bot/reply", {
                "text": (f"⚠️ 저장 공간 한도({gb}GB)를 초과했어요.\n\n"
                         "클라우드는 저장 공간이 제한돼 있어요. 큰 파일을 다루거나 더 많은 공간이 "
                         "필요하면 **내 컴퓨터(맥/PC)에 피터를 설치**하면 제한 없이 쓸 수 있어요. "
                         "설정에서 '내 AI 비서 설치'를 신청해주세요.\n\n"
                         "(문서탭에서 필요 없는 파일을 지우면 공간을 확보할 수 있어요.)"),
                "reply_to": [msg_id], "project": project, "is_final": True,
                "subtype": "provision_suggest",
            })
            user_api(api_key, "POST", "/api/cloud/heavy-event",
                     {"kind": "heavy_disk", "project": project, "context": "disk quota exceeded"})
            return

        # ── 일반 턴 ──
        attachments = fetch_attachments(user_id, project, msg.get("files") or [])
        if not text and not attachments:
            return  # 빈 메시지 — 빈 프롬프트로 claude 를 부르면 exit 1 로 죽는다
        prompt = with_attachments(with_sender(msg, text), attachments)
        # 진행 표시("생각 중...") — 웹은 is_working + active_project 로 판단한다.
        # 이걸 안 보내면 응답이 나올 때까지 화면이 조용해서 멈춘 것처럼 보인다.
        self.set_working(api_key, True, project, text[:80] or "첨부 파일 확인", msg_id)
        try:
            response, status = run_claude_turn(user_id, project, prompt)
        finally:
            self.set_working(api_key, False, project)
        collect_usage_after_turn(user_id, api_key)  # 컨테이너가 살아있는 동안 사용량 갱신
        # 턴 소비 기록 — 실제로 실행된 경우만 (auth 실패는 미소비)
        if status in ("ok", "heavy_mem", "heavy_time"):
            record_turn(user_id)
            self.maybe_warn_quota(api_key, user_id, project, msg_id)
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

    def maybe_warn_quota(self, api_key, user_id, project, msg_id):
        """턴 한도 90% 도달 시 월 1회 경고 (한도 초과 전에 미리 알린다)."""
        tl = user_limits(user_id)
        if not tl or not tl.get("turnsPerMonth"):
            return
        limit = int(tl["turnsPerMonth"])
        used = turns_used(user_id)
        if used < limit * 0.9:
            return
        key = (user_id, _usage_month())
        if key in _quota_warned:
            return
        _quota_warned.add(key)
        self.reply(api_key,
                   (f"ℹ️ 이번 달 턴을 {used}/{limit} 사용했어요 (90% 이상). "
                    "한도를 넘으면 다음 달 1일까지 새 작업을 시작할 수 없어요. "
                    "더 필요하면 요금제 업그레이드를 문의해주세요."),
                   [msg_id], project)

    def report_heavy(self, api_key, user_id, project, msg_id, status, user_text):
        """리소스 한도 초과 → 이유 + 전환 안내(CTA). heavy 이벤트 기록도 함께."""
        lim = config.get("limits", {})
        tl = user_limits(user_id)
        if status == "heavy_mem":
            if tl and tl.get("turnMemoryMb"):
                mb = int(tl["turnMemoryMb"])
                if ctr.enabled_for(user_id):  # 공용 호스트 물리 캡 반영 (MAX 임시 6GB)
                    mb = min(mb, int(config.get("container", {}).get("max_memory_mb", 6144)))
                mem_txt = f"{mb / 1024:g}GB"
            else:
                mem_txt = lim.get("memory_max", "3G")
            reason = f"메모리 한도({mem_txt})를 넘어 작업이 중단됐어요."
        else:
            mins = turn_timeout_sec(user_id) // 60
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
        lock = self._key_lock(*key)
        # 같은 (유저, 프로젝트) 턴이 진행 중이면 **워커를 붙들지 않고** 물러난다.
        # 블로킹 대기는 긴 턴 하나 + 같은 키 후속 메시지들이 워커 풀(5개)을 전부
        # 잠식해, 다른 유저 메시지까지 픽업이 서는 wedge 를 만든다 (2026-08-19 뉴넥스 장애).
        # 메시지는 아직 processed 전이므로 _spawned 에서 빼두면 다음 폴링에 다시 온다.
        if not lock.acquire(blocking=False):
            with self._spawned_guard:
                self._spawned.discard(msg_id)
            return
        try:
            try:
                if shutdown_event.is_set():
                    return  # 아직 시작 전 — processed 마킹도 안 됐으니 그대로 재전달된다
                with self._spawned_guard:
                    self._inflight[msg_id] = msg
                self.process(msg)
            finally:
                lock.release()
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
                self._inflight.pop(msg_id, None)

    # ── 종료 처리 ────────────────────────────────────────────────
    # systemd 모드의 턴은 `systemd-run --unit=pvturn-<uid>-<pid>-<n>` 전용 유닛이라
    # 데몬과 다른 cgroup 에 있다 → 데몬이 죽어도 계속 돈다. 되돌릴 때 같이 정리한다.
    # 클라우드는 턴 시작 **전에** processed 를 마킹한다(2026-07-06 폴링 핫루프 사고 대응).
    # 그래서 턴 도중에 데몬이 죽으면 그 메시지는 다시 폴링되지 않고 **영구 유실**된다.
    # 종료 시 (1) 진행 중인 턴을 잠시 기다리고 (2) 그래도 안 끝난 것은 되돌려 재전달시킨다.

    def handle_force_restart(self, user_ids) -> bool:
        """웹 재시작 버튼(user_status.force_restart) 처리. True = 데몬 자체 재시작.

        멀티테넌트라 재시작은 호스트 전체에 걸린다 — 진행 중 턴은 drain 후 requeue 로
        재전달되므로 유실은 없다. **플래그 clear 가 성공했을 때만 재시작한다**
        (clear 없이 재시작하면 기동 직후 플래그를 또 읽어 재시작 루프가 된다)."""
        for uid in (user_ids or []):
            if not user_allowed(uid):
                continue
            user = roster.get(uid)
            if not user:
                continue
            ok = user_api(user["apiKey"], "PATCH", "/api/bot/status",
                          {"force_restart": False})
            if ok is None:
                logger.error(f"force_restart clear failed user={uid} — 재시작 보류")
                continue
            logger.warning(f"force_restart requested by user={uid} — restarting daemon "
                           "(systemd Restart=always 가 재기동)")
            return True
        return False

    def drain(self) -> int:
        """진행 중인 턴이 끝날 때까지 대기. 남은 개수를 반환(0이면 깨끗이 종료)."""
        sec = int(config.get("shutdown_drain_sec", 90))
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # py<3.9
            self._executor.shutdown(wait=False)
        with self._spawned_guard:
            n = len(self._inflight)
        if n:
            logger.info(f"draining {n} in-flight turn(s), up to {sec}s")
        deadline = time.time() + sec
        while time.time() < deadline:
            with self._spawned_guard:
                if not self._inflight:
                    logger.info("drained cleanly")
                    return 0
            time.sleep(0.5)
        return self.requeue_inflight()

    def requeue_inflight(self) -> int:
        """드레인 시한을 넘긴 턴을 되돌린다 — processed 해제 + 고아 프로세스 정리."""
        with self._spawned_guard:
            items = list(self._inflight.items())
        for msg_id, msg in items:
            uid = msg.get("user_id")
            project = msg.get("project", "general") or "general"
            user = roster.get(uid)
            if not user:
                logger.error(f"requeue skipped msg #{msg_id}: unknown user {uid}")
                continue
            api_key = user["apiKey"]
            # 컨테이너 안 claude 는 exec 클라이언트를 죽여도 살아남는다(실측 확인).
            # 정리하지 않으면 재전달된 메시지가 같은 세션에서 두 번 돌게 된다.
            try:
                if ctr.enabled_for(uid):
                    ctr.kill_turns(uid)
                else:
                    kill_systemd_turns(uid)
            except Exception as e:
                logger.warning(f"kill_turns failed user={uid}: {e}")
            ok = user_api(api_key, "PATCH", "/api/bot/message",
                          {"id": msg_id, "updates": {"processed": False, "fetched_at": None}})
            if ok is None:
                logger.error(f"requeue FAILED msg #{msg_id} — 유실 위험")
                continue
            logger.info(f"requeued msg #{msg_id} user={uid} project={project}")
            try:
                self.set_working(api_key, False, project)
                self.reply(api_key,
                           "(서버 업데이트로 작업이 중단됐어요. 잠시 후 자동으로 다시 처리할게요.)",
                           [msg_id], project)
            except Exception:
                pass
        return len(items)

    def set_working(self, api_key: str, working: bool, project: str,
                    task: str | None = None, msg_id=None):
        """턴 진행 표시. 배치 heartbeat 는 last_heartbeat 만 갱신하므로 이 값을 덮지 않는다."""
        body = {"is_working": working, "cloud": True, "project": project}
        if working:
            body["current_task"] = task
            body["message_ids"] = [msg_id] if msg_id else None
        try:
            user_api(api_key, "POST", "/api/bot/heartbeat", body)
        except Exception as e:  # 표시 실패로 턴이 죽으면 안 된다
            logger.warning(f"set_working({working}) failed: {e}")

    def clear_stale_working(self):
        """기동 시 남아 있는 '작업 중' 표시를 정리한다.
        턴 도중에 데몬이 죽으면 is_working=true 가 그대로 남는데, 배치 heartbeat 가
        last_heartbeat 를 계속 갱신하므로 웹의 stale 판정(60초)에도 걸리지 않아
        '생각 중...' 이 영원히 도는 상태가 된다."""
        with roster._lock:
            users = list(roster._users.values())
        for u in users:
            try:
                user_api(u["apiKey"], "POST", "/api/bot/heartbeat",
                         {"is_working": False, "cloud": True})
            except Exception:
                pass

    def heartbeats(self):
        """전 유저 온라인 표시.

        호스트 단위 배치 1회로 처리한다. 유저별로 보내면 안 쓰는 유저까지
        하루 2,880회씩 붙어 가입자 수에 선형 비례한다.
        구 웹(배치 엔드포인트 없음)에서는 예전 방식으로 되돌아간다."""
        if host_api("POST", "/api/cloud/heartbeat", {}) is not None:
            return
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
        self.clear_stale_working()
        UsageThread().start()
        HandoffThread().start()
        last_heartbeat = 0.0
        last_reap = 0.0
        poll_interval = config.get("poll_interval_sec", 3)
        errors = 0
        while not shutdown_event.is_set():
            try:
                now = time.time()
                roster.sync()
                if now - last_heartbeat > 30:
                    threading.Thread(target=self.heartbeats, daemon=True).start()
                    last_heartbeat = now
                # 유휴 컨테이너 정지 (5분마다) — MAX(상시 유지) 유저는 건너뜀
                if config.get("container", {}).get("enabled") and now - last_reap > 300:
                    threading.Thread(
                        target=ctr.reap_idle, daemon=True,
                        kwargs={"skip": lambda uid: bool(
                            (user_limits(uid, peek=True) or {}).get("containerAlwaysOn"))},
                    ).start()
                    last_reap = now

                result = host_api("GET", "/api/cloud/poll")
                if result is None:
                    errors += 1
                    shutdown_event.wait(min(30, 2 ** errors))
                    continue
                errors = 0
                _set_usage_refresh(result.get("usage_refresh"))
                if self.handle_force_restart(result.get("force_restart")):
                    break  # → drain() → 프로세스 종료 → systemd 재기동

                spawned_any = False
                # 우선 큐: MAX 티어 메시지를 먼저 워커에 배정 (정렬은 안정적 — 같은 티어끼리는 도착순)
                pending = sorted(
                    result.get("pending", []),
                    key=lambda m: 0 if (user_limits(m.get("user_id"), peek=True) or {}).get("priorityQueue") else 1)
                for msg in pending:
                    if not user_allowed(msg.get("user_id")):
                        continue  # 다른 호스트 담당 유저
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
        self.drain()
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
    ctr.init(config, logger)

    def handle_sig(_sig, _frm):
        shutdown_event.set()
    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    CloudWorker().run()


if __name__ == "__main__":
    main()
