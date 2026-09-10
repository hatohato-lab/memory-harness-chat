from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath


class HarnessError(Exception):
    """An actionable, user-facing workflow failure."""


RUN_DIR_NAME = ".memory-harness"


def is_link(info: os.stat_result) -> bool:
    """Symlink, or on Windows any reparse point such as a junction.

    Python 3.11 has no Path.is_junction(); the reparse attribute from lstat()
    is what keeps junctions from being followed there.
    """
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(obj) -> str:
    return sha256_bytes(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False).encode("utf-8"))


def hash_file(path: Path) -> str | None:
    path = Path(path)
    if path.is_symlink():
        raise HarnessError(f"シンボリックリンクは変更できません: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise HarnessError(f"通常ファイルではありません: {path}")
    return sha256_bytes(path.read_bytes())


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError, UnicodeError) as exc:
        raise HarnessError(f"JSONを読めません: {path}: {exc}") from exc


def atomic_write(path: Path, data: bytes) -> None:
    path = Path(path)
    if path.is_symlink():
        raise HarnessError(f"シンボリックリンクへの書込を拒否: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path: Path, obj) -> None:
    atomic_write(Path(path), (json.dumps(obj, ensure_ascii=False, indent=2,
                                       allow_nan=False) + "\n").encode("utf-8"))


def safe_project_path(project: Path, relative: str) -> Path:
    """Portable lexical and symlink checks; never accept Windows drive paths."""
    project = Path(project).resolve(strict=True)
    if not project.is_dir():
        raise HarnessError(f"projectはディレクトリを指定してください: {project}")
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise HarnessError(f"不正な相対パス: {relative!r}")
    raw_parts = relative.split("/")
    if any(p in ("", ".", "..") for p in raw_parts):
        raise HarnessError(f"不正な相対パス: {relative!r}")
    if PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise HarnessError(f"絶対パスは指定できません: {relative!r}")
    reserved = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
    for part in raw_parts:
        if ":" in part or part.endswith((" ", ".")) or reserved.match(part):
            raise HarnessError(f"OS依存の危険なパス名: {relative!r}")
    result = project
    for part in raw_parts:
        result = result / part
        try:
            info = result.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise HarnessError(f"パスを検査できません: {result}: {exc}") from exc
        if is_link(info):
            raise HarnessError(f"リンクを含むパスは使用できません: {result}")
    try:
        result.resolve().relative_to(project)
    except ValueError as exc:
        raise HarnessError(f"project外のパス: {relative!r}") from exc
    return result
