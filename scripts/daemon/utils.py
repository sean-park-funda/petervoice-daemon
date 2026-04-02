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
