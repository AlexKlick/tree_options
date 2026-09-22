"""Append-only JSONL history files: torn-tail repair, capped halving
rotation, bounded tail reads.

The book's observation history (per-structure marks, account equity)
accrues in append-only JSONL. A process killed mid-write leaves an
unterminated tail line; appending after it would concatenate two records
and lose both, so writers repair the tail before their first append.
Rotation halves the file when a line cap is exceeded (newest half kept),
via tmp + os.replace so a reader never sees a torn file.

Money values in these files are Decimal-strings (book-lane convention);
the market/discovery quote lane keeps its own float convention and must
never write through this module into book-lane files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def repair_torn_tail(path: Path) -> bool:
    """Truncate an unterminated last line. True when a repair happened."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return False
        f.seek(0)
        data = f.read()
    idx = data.rfind(b"\n")
    with path.open("r+b") as f:
        f.truncate(idx + 1 if idx >= 0 else 0)
    return True


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as f:
        return sum(1 for _ in f)


def append_line(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def rotate_halving(path: Path, cap: int) -> bool:
    """Keep only the newest ``cap // 2`` lines once the count exceeds cap."""
    if not path.exists():
        return False
    with path.open("rb") as f:
        lines = f.readlines()
    if len(lines) <= cap:
        return False
    keep = lines[len(lines) - cap // 2 :]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(b"".join(keep))
    os.replace(tmp, path)
    return True


def read_tail(path: Path, max_bytes: int = 4_000_000) -> list[dict[str, Any]]:
    """Bounded tolerant read: the last ``max_bytes`` bytes, junk lines
    skipped, torn leading fragment ignored. Opens the file BEFORE sizing
    it (fstat on the open descriptor): a rotation that replaces the file
    between a path stat and the open would seek past the new, halved
    file's end and silently return empty history."""
    import os as _os

    try:
        f = path.open("rb")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return []
    with f:
        size = _os.fstat(f.fileno()).st_size
        if size > max_bytes:
            f.seek(size - max_bytes)
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if size > max_bytes and lines:
        lines = lines[1:]  # leading fragment cut by the seek
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out
