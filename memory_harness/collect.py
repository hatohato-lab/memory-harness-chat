"""Inventory explicitly selected inputs without changing the original files.

Chunk offsets are zero-based, half-open Unicode character offsets in the exact
UTF-8 snapshot. Line numbers are one-based and use ``str.splitlines`` semantics.
"""

from __future__ import annotations

import bisect
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .common import RUN_DIR_NAME, HarnessError, atomic_write, is_link, sha256_bytes, utc_now, write_json


_EXTRA_SUFFIXES = {".md", ".txt", ".json", ".jsonl"}
_PRIORITY = {"extra": 0, "history": 1, "memory": 2, "harness": 3}


@dataclass(frozen=True)
class _Source:
    path: Path
    kind: str
    status: str | None = None
    reason: str | None = None


def _absolute(path: Path) -> Path:
    # Keep the lexical path: resolving it first would hide a selected symlink.
    return Path(os.path.abspath(os.fspath(path)))


def _link_ancestor(path: Path) -> Path | None:
    for ancestor in reversed(path.parents):
        try:
            if is_link(ancestor.lstat()):
                return ancestor
        except FileNotFoundError:
            continue
    return None


def _kind_for(path: Path, default: str) -> str:
    if default == "extra" and path.suffix.lower() in {".json", ".jsonl"}:
        return "history"
    return default


def _discover(
    root: Path,
    kind: str,
    *,
    suffixes: set[str],
    filename: str | None = None,
    optional: bool = False,
    require_file: bool = False,
) -> list[_Source]:
    """Enumerate eligible files and explicit records of unreadable scope."""
    result: list[_Source] = []
    try:
        link = _link_ancestor(root)
        if link is not None:
            return [_Source(root, kind, "excluded", f"symlink/reparse ancestor: {link}")]
        root_info = root.lstat()
    except FileNotFoundError:
        return [] if optional else [_Source(root, kind, "error", "source does not exist")]
    except OSError as exc:
        return [_Source(root, kind, "error", f"cannot inspect source: {exc}")]

    pending: list[tuple[Path, os.stat_result]] = [(root, root_info)]
    while pending:
        path, info = pending.pop()
        if is_link(info):
            result.append(_Source(path, kind, "excluded", "symlink/reparse point is not followed"))
        elif stat.S_ISDIR(info.st_mode):
            if require_file:
                result.append(_Source(path, kind, "error", "expected a harness file; found a directory"))
                continue
            try:
                with os.scandir(path) as entries:
                    children = sorted(entries, key=lambda entry: entry.name, reverse=True)
                for entry in children:
                    if entry.name == RUN_DIR_NAME:
                        # Past runs keep snapshots/inventories of earlier inputs here; skipping
                        # silently keeps the analysis complete (an excluded record would not).
                        continue
                    child = Path(entry.path)
                    try:
                        child_info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        result.append(_Source(child, kind, "error", f"cannot inspect source: {exc}"))
                        continue
                    pending.append((child, child_info))
            except OSError as exc:
                result.append(_Source(path, kind, "error", f"cannot enumerate directory: {exc}"))
        elif stat.S_ISREG(info.st_mode):
            selected = path.suffix.lower() in suffixes
            if filename is not None:
                selected = selected and path.name == filename
            if selected:
                result.append(_Source(path, _kind_for(path, kind)))
            elif path == root and not optional:
                result.append(_Source(path, kind, "excluded", "unsupported source extension or filename"))
        else:
            result.append(_Source(path, kind, "excluded", "source is not a regular file or directory"))
    return result


def _harness_sources(project: Path) -> list[_Source]:
    sources: list[_Source] = []
    for name in ("CLAUDE.md", "CLAUDE.local.md"):
        sources.extend(_discover(project / name, "harness", suffixes={".md"}, optional=True, require_file=True))
    claude_dir = project / ".claude"
    try:
        info = claude_dir.lstat()
    except FileNotFoundError:
        return sources
    except OSError as exc:
        sources.append(_Source(claude_dir, "harness", "error", f"cannot inspect harness directory: {exc}"))
        return sources
    if is_link(info):
        sources.append(_Source(claude_dir, "harness", "excluded", "symlink/reparse point is not followed"))
        return sources
    if not stat.S_ISDIR(info.st_mode):
        sources.append(_Source(claude_dir, "harness", "error", "harness directory is not a directory"))
        return sources
    sources.extend(_discover(claude_dir / "CLAUDE.md", "harness", suffixes={".md"}, optional=True, require_file=True))
    sources.extend(_discover(claude_dir / "rules", "harness", suffixes={".md"}, optional=True))
    sources.extend(
        _discover(claude_dir / "skills", "harness", suffixes={".md"}, filename="SKILL.md", optional=True)
    )
    return sources


def _read_bytes(path: Path, max_file_bytes: int) -> tuple[bytes | None, str, str | None, int]:
    """Use bounded binary reads; reject detectable replacement/change races."""
    try:
        link = _link_ancestor(path)
        if link is not None:
            return None, "excluded", f"symlink/reparse ancestor: {link}", 0
        initial = path.lstat()
        if is_link(initial):
            return None, "excluded", "source became a symlink/reparse point", 0
        if not stat.S_ISREG(initial.st_mode):
            return None, "excluded", "source is not a regular file", 0
        if initial.st_size > max_file_bytes:
            return None, "excluded", f"file exceeds max_file_bytes={max_file_bytes}; not truncated", initial.st_size
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (initial.st_dev, initial.st_ino):
                return None, "error", "source changed before reading; retry collection", before.st_size
            if not stat.S_ISREG(before.st_mode):
                return None, "excluded", "opened source is not a regular file", 0
            data = stream.read(max_file_bytes + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        if len(data) > max_file_bytes:
            return None, "excluded", f"file exceeds max_file_bytes={max_file_bytes}; not truncated", max(len(data), after.st_size)
        # st_ctime_ns is excluded on Windows: lstat() and fstat() report it at
        # different resolutions for the same file, so comparing it reports a
        # change race on every read. Identity still rests on dev/ino/size/mtime.
        signature = (
            (lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns))
            if os.name == "nt"
            else (lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns))
        )
        if is_link(current) or signature(before) != signature(after) or signature(after) != signature(current):
            return None, "error", "source changed during reading; retry collection", after.st_size
        if len(data) != after.st_size:
            return None, "error", "source byte count changed or read was incomplete; retry collection", after.st_size
        return data, "ok" if data else "empty", None, len(data)
    except OSError as exc:
        return None, "error", f"cannot read source: {exc}", 0


def _chunk_ranges(text: str, limit: int):
    ends: list[int] = []
    position = 0
    for line in text.splitlines(keepends=True):
        position += len(line)
        ends.append(position)
    start = 0
    while start < len(text):
        target = min(start + limit, len(text))
        boundary = bisect.bisect_right(ends, target) - 1
        end = ends[boundary] if boundary >= 0 and ends[boundary] > start else target
        yield start, end, bisect.bisect_right(ends, start) + 1, bisect.bisect_left(ends, end) + 1
        start = end


def collect(
    project: Path,
    memory_dirs: list[Path],
    extra_sources: list[Path],
    run_dir: Path,
    chunk_chars: int = 8000,
    max_file_bytes: int = 4194304,
) -> dict:
    """Create a new run with an explicit inventory, exact snapshots and chunks.

    A failed or excluded source remains visible in ``sources`` and ``counts``.
    Empty sources get an empty snapshot, but do not require a model call. Files
    outside the selected roots are never discovered; harness files always take
    precedence when an explicitly selected source overlaps the project harness.
    """
    if isinstance(chunk_chars, bool) or not isinstance(chunk_chars, int) or chunk_chars < 1:
        raise HarnessError("chunk_chars must be a positive integer")
    if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 1:
        raise HarnessError("max_file_bytes must be a positive integer")
    project, run_dir = _absolute(project), _absolute(run_dir)
    if not project.is_dir():
        raise HarnessError(f"project directory does not exist: {project}")
    if os.path.lexists(run_dir):
        raise HarnessError(f"run directory already exists; choose a new path: {run_dir}")
    try:
        if is_link(project.lstat()) or _link_ancestor(project) is not None:
            raise HarnessError("project must not be a symlink/reparse point or contain a linked ancestor")
        if _link_ancestor(run_dir) is not None:
            raise HarnessError("run directory must not contain a symlink/reparse ancestor")
    except OSError as exc:
        raise HarnessError(f"cannot inspect project/run path: {exc}") from exc

    # Discover before making the run directory so a run nested under a selected
    # directory cannot recursively ingest its own snapshots and chunk files.
    discovered = _harness_sources(project)
    memory_roots = [_absolute(path) for path in memory_dirs]
    extra_roots = [_absolute(path) for path in extra_sources]
    for root in memory_roots:
        discovered.extend(_discover(root, "memory", suffixes={".md"}))
    for root in extra_roots:
        discovered.extend(_discover(root, "extra", suffixes=_EXTRA_SUFFIXES))
    unique: dict[str, _Source] = {}
    for source in discovered:
        key = os.path.normcase(str(source.path))
        previous = unique.get(key)
        if previous is None or _PRIORITY[source.kind] > _PRIORITY[previous.kind]:
            unique[key] = source

    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise HarnessError(f"cannot create a fresh run directory: {exc}") from exc
    sources: list[dict] = []
    chunks: list[dict] = []
    characters = 0
    try:
        for key in sorted(unique):
            source = unique[key]
            data, status, reason, byte_count = (
                (None, source.status, source.reason, 0)
                if source.status is not None
                else _read_bytes(source.path, max_file_bytes)
            )
            text = None
            if data is not None:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    status, reason = "error", f"source is not valid UTF-8 at byte {exc.start}; not decoded with replacement"
                    data = None
            digest = sha256_bytes(data) if data is not None else None
            source_id = "src-" + sha256_bytes((str(source.path) + "\0" + (digest or "unavailable")).encode("utf-8"))[:24]
            try:
                relative_path = source.path.relative_to(project).as_posix()
            except ValueError:
                relative_path = None
            snapshot_path = f"snapshots/{source_id}.txt" if data is not None else None
            record = {
                "id": source_id,
                "kind": source.kind,
                "origin": str(source.path),
                "relative_path": relative_path,
                "status": status,
                "sha256": digest,
                "bytes": byte_count,
                "line_count": len(text.splitlines()) if text is not None else 0,
                "snapshot": snapshot_path,
                "reason": reason,
            }
            sources.append(record)
            if data is None or text is None:
                continue
            atomic_write(run_dir / snapshot_path, data)
            characters += len(text)
            for number, (start, end, first_line, last_line) in enumerate(_chunk_ranges(text, chunk_chars), 1):
                chunk_id = f"{source_id}-c{number:06d}"
                chunk_path = f"chunks/{chunk_id}.txt"
                atomic_write(run_dir / chunk_path, text[start:end].encode("utf-8"))
                chunks.append({
                    "id": chunk_id,
                    "source_id": source_id,
                    "start_line": first_line,
                    "end_line": last_line,
                    "start_offset": start,
                    "end_offset": end,
                    "path": chunk_path,
                })
        counts = {status: sum(source["status"] == status for source in sources) for status in ("ok", "empty", "error", "excluded")}
        counts.update({
            "sources": len(sources),
            "chunks": len(chunks),
            "bytes": sum(source["bytes"] for source in sources if source["status"] in {"ok", "empty"}),
            "characters": characters,
            "lines": sum(source["line_count"] for source in sources),
        })
        inventory = {
            "schema_version": 1,
            "project": str(project),
            "created_at": utc_now(),
            "scope": {"memory_dirs": [str(path) for path in memory_roots], "extra_sources": [str(path) for path in extra_roots]},
            "chunk_chars": chunk_chars,
            "max_file_bytes": max_file_bytes,
            "offset_unit": "unicode_characters_half_open",
            "line_numbering": "one_based_splitlines",
            "sources": sources,
            "chunks": chunks,
            "counts": counts,
        }
        write_json(run_dir / "inventory.json", inventory)
        return inventory
    except OSError as exc:
        raise HarnessError(f"cannot finish collection; partial run remains at {run_dir}: {exc}") from exc
