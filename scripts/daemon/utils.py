"""Utility functions: JSON I/O, ANSI strip, text split, file download."""

import re
import json
import time
import urllib.request
from pathlib import Path

from daemon.globals import DOWNLOADS_DIR, logger


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
