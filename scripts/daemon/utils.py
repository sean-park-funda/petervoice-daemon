"""Utility functions: JSON I/O, ANSI strip, text split, file download."""

import re
import json
import time
import urllib.request
from pathlib import Path

from daemon.globals import DOWNLOADS_DIR, logger


def popen_retry_missing_binary(popen, cmd: list, *, resolve=None, retry_sec: float = 120.0,
                               interval: float = 3.0, log=None, should_stop=None,
                               sleep=time.sleep, clock=time.monotonic):
    """Popen 인데 실행 파일이 잠깐 없을 때(FileNotFoundError) 기다렸다 다시 시도한다.

    배경(2026-11-16): 업데이터가 `npm install -g @anthropic-ai/claude-code@…` 를 돌리는 동안 npm 이 옛 패키지
    디렉터리를 치워 두므로 `/opt/homebrew/bin/claude` 심링크가 수십 초 끊긴다. 그 순간 시작한 턴은
    `[Errno 2] No such file or directory: '/opt/homebrew/bin/claude'` 로 죽어 유저에게 오류 한 줄만 갔다
    (11/10~11/15 Sean 맥 16건 — 설치가 매 5분 실패·롤백을 반복하며 공백을 만들었다).
    popen(cmd) 는 subprocess.Popen 을 감싼 호출자. resolve() 가 있으면 재시도마다 실행 파일 경로를 다시 찾아
    cmd[0] 을 바꾼다(심링크가 다른 곳에 다시 생기는 경우). 기한이 지나면 마지막 예외를 그대로 올린다.
    """
    cmd = list(cmd)
    deadline = clock() + max(0.0, float(retry_sec or 0))
    warned = False
    while True:
        try:
            return popen(cmd)
        except FileNotFoundError as e:
            if clock() >= deadline or (should_stop and should_stop()):
                raise
            if not warned and log:
                log.warning(f"[spawn] {cmd[0]} missing ({e}) — updater reinstalling? retrying up to {retry_sec:.0f}s")
                warned = True
            if resolve:
                try:
                    found = resolve()
                    if found:
                        cmd[0] = found
                except Exception:
                    pass
            sleep(interval)


def download_files(files: list[dict]) -> list[Path]:
    """Download files from Supabase Storage URLs to local directory.
    Returns list of local file paths."""
    if not files:
        return []
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    local_paths = []
    for f in files:
        url = f.get("url", "")
        name = f.get("name", "file")
        file_type = f.get("type", "")
        if not url:
            continue
        MAX_FILE_SIZE = 50 * 1024 * 1024
        file_size = f.get("size", 0)
        if file_size and file_size > MAX_FILE_SIZE:
            logger.info(f"Skipping oversized file: {name} ({file_size} bytes)")
            continue
        ts = int(time.time() * 1000)
        local_name = f"{ts}_{name}"
        local_path = DOWNLOADS_DIR / local_name
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                local_path.write_bytes(resp.read())
            local_paths.append(local_path)
            logger.info(f"Downloaded: {name} → {local_path} ({local_path.stat().st_size} bytes)")
        except Exception as e:
            logger.error(f"Failed to download {name} from {url}: {e}")
    return local_paths


def resolve_files(files: list[dict]) -> tuple[list[Path], list[Path]]:
    """첨부 파일들을 (전체 경로 목록, 정리 대상 임시 경로 목록)으로 변환한다.

    - `local_path`가 있고 실재하면: 맥미니에 이미 있는 파일(채팅→docs/uploaded 직행 업로드).
      그대로 사용하고 **절대 삭제하지 않는다** (유저의 문서 폴더 원본).
    - 그 외(구 메시지/봇 발신/폴백): 기존처럼 URL에서 다운로드 → 처리 후 삭제 대상.
    """
    if not files:
        return [], []
    local_paths: list[Path] = []
    remote_files: list[dict] = []
    for f in files:
        lp = f.get("local_path")
        if lp:
            p = Path(lp)
            if p.exists() and p.is_file():
                local_paths.append(p)
                logger.info(f"Using local upload: {f.get('name', p.name)} → {p}")
                continue
            logger.warning(f"local_path missing on disk, falling back to URL: {lp}")
        remote_files.append(f)
    downloaded = download_files(remote_files)
    return local_paths + downloaded, downloaded


def cleanup_downloads(paths: list[Path]):
    """Clean up downloaded files after processing."""
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def count_dirty_files(project_dir: str | None) -> int | None:
    """중단 보고용: 프로젝트 디렉토리의 커밋 안 된 변경 파일 수. git 레포 아니면/실패하면 None."""
    if not project_dir:
        return None
    import subprocess
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return len([l for l in r.stdout.splitlines() if l.strip()])
    except Exception:
        return None


# ── 강제중단 마크 (D6): 강제 킬로 끝난 프로젝트는 다음 턴에 상태확인 가드를 1회 주입 ──
_INTERRUPTED_PATH = Path.home() / ".claude-daemon" / "interrupted_projects.json"


def mark_interrupted(project: str):
    data = _read_json(_INTERRUPTED_PATH, {})
    data[project] = int(time.time())
    _write_json(_INTERRUPTED_PATH, data)


def pop_interrupted_flag(project: str) -> bool:
    """마크가 있으면 True 반환 후 제거 (1회성)."""
    data = _read_json(_INTERRUPTED_PATH, {})
    if project in data:
        data.pop(project, None)
        _write_json(_INTERRUPTED_PATH, data)
        return True
    return False


def _write_json(path: Path, data):
    """Atomic JSON write: write to tmp, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default=None):
    """Safe JSON read: return default on missing/corrupt file."""
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def _split_text_chunks(text: str, max_len: int = 3500) -> list[str]:
    """Split text into chunks, preferring newline > space > hard break."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        idx = text.rfind('\n', 0, max_len)
        if idx == -1:
            idx = text.rfind(' ', 0, max_len)
        if idx == -1:
            idx = max_len
        chunks.append(text[:idx].rstrip())
        text = text[idx:].lstrip()
    return [c for c in chunks if c]


_SENDER_RE = re.compile(r"[\x00-\x1f\[\]\n]")


def with_sender(msg: dict, text: str) -> str:
    """팀 프로젝트에서 팀원이 보낸 메시지에 발신자를 표시한다 (cloud_daemon.with_sender 와 같은 규칙).

    메시지는 프로젝트 **소유자**의 user_id 로 저장되므로(팀원이 보내도 마찬가지),
    이걸 붙이지 않으면 여러 사람이 한 세션을 써도 비서가 전부 같은 사람으로 인식한다.
    소유자 본인 메시지에는 아무것도 붙이지 않는다(기존 동작 유지).
    외부 메시지(subtype=external)도 from_user_id 를 쓰지만 팀원이 아니고, 이미
    `[외부 relay @보낸사람 · kind]` 머리를 달고 오므로 제외한다."""
    name = (msg.get("from_username") or "").strip()
    if not name or msg.get("subtype") == "external":
        return text
    if msg.get("from_user_id") in (None, msg.get("user_id")):
        return text
    return f"[팀원 {_SENDER_RE.sub('', name)[:40]} 님이 보낸 메시지]\n{text}"
