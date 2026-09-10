"""Approved file transactions with hash checks, recovery and conservative undo.

Atomicity is per file, not across the project. The advisory project lock excludes
other Memory Harness transactions, not editors or arbitrary external processes.
After a process crash, a rerun accepts only recorded before/after images, restores
the before images, and retries. Unknown edits always require human resolution.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
import os
from typing import Iterator

from .common import (
    HarnessError, atomic_write, canonical_hash, hash_file, read_json,
    safe_project_path, sha256_bytes, utc_now, write_json,
)


def _path(root: Path, relative: str) -> Path:
    """Reject Windows escapes even while running on a POSIX host."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise HarnessError("Invalid transaction path")
    win = PureWindowsPath(relative)
    if (win.drive or win.root or "\\" in relative or ":" in relative
            or any(part in ("..", ".") for part in relative.split("/"))
            or relative.startswith("/") or "//" in relative):
        raise HarnessError(f"Unsafe transaction path: {relative!r}")
    return safe_project_path(root, relative)


def _run_path(run_dir: Path, relative: str) -> Path:
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise HarnessError("Run directory must be an existing ordinary directory")
    return _path(run_dir, relative)


@contextmanager
def _project_lock(project: Path) -> Iterator[None]:
    """An OS advisory lock; the lock file is intentionally never unlinked."""
    lock_path = _path(project, ".memory-harness/transaction.lock")
    lock_path.parent.mkdir(exist_ok=True)
    lock_path = _path(project, ".memory-harness/transaction.lock")
    handle = open(lock_path, "a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise HarnessError("Another transaction holds the project lock") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise HarnessError("Another transaction holds the project lock") from exc
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _load_approval(run_dir: Path) -> tuple[dict, dict, Path]:
    approval = read_json(_run_path(run_dir, "approval.json"))
    plan_path = _run_path(run_dir, "plan.json")
    plan = read_json(plan_path)
    if not isinstance(approval, dict) or not isinstance(plan, dict):
        raise HarnessError("Approval and plan must be JSON objects")
    if approval.get("schema_version") != 1 or plan.get("schema_version") != 1:
        raise HarnessError("Unsupported approval or plan schema")
    unsigned = {key: value for key, value in approval.items() if key != "approval_hash"}
    if approval.get("approval_hash") != canonical_hash(unsigned):
        raise HarnessError("Approval checksum does not match; approve the plan again")
    if approval.get("plan_sha256") != hash_file(plan_path):
        raise HarnessError("Plan changed after approval; approve the new plan")
    if approval.get("project") != plan.get("project"):
        raise HarnessError("Approval project differs from plan project")
    raw_project = approval.get("project")
    if not isinstance(raw_project, str):
        raise HarnessError("Approval project is missing")
    project = Path(raw_project)
    if not project.is_absolute() or project.is_symlink() or not project.is_dir():
        raise HarnessError("Approved project must be an existing absolute directory")
    if project != project.resolve(strict=True):
        raise HarnessError("Approved project must use its canonical path without parent links")
    ids = approval.get("candidate_ids")
    if (not isinstance(ids, list) or not ids
            or not all(isinstance(item, str) for item in ids) or len(ids) != len(set(ids))):
        raise HarnessError("Approval requires explicit, unique candidate IDs")
    items = {item.get("candidate_id"): item for item in plan.get("items", [])}
    if any(item_id not in items for item_id in ids):
        raise HarnessError("Approval contains candidates absent from the plan")
    if any(items[item_id].get("blocked_reasons") for item_id in ids):
        raise HarnessError("A blocked candidate cannot be applied")
    # Rebuild bytes from the approved plan, so recomputing the approval checksum
    # cannot substitute arbitrary paths or content into the operation list.
    from .plan import assemble_operations
    expected = assemble_operations(plan, ids)
    operations = approval.get("operations")
    if not isinstance(operations, list) or not operations or operations != expected:
        raise HarnessError("Approved operations do not match the selected plan items")
    seen: set[str] = set()
    for operation in operations:
        relative = operation.get("relative_path")
        _path(project, relative)
        key = relative.casefold() if os.name == "nt" else relative
        if key in seen:
            raise HarnessError("Approval contains duplicate target paths")
        seen.add(key)
        content = operation.get("after_content")
        if not isinstance(content, str):
            raise HarnessError("Approved content must be UTF-8 text")
        if operation.get("after_sha256") != sha256_bytes(content.encode("utf-8")):
            raise HarnessError("Approved content hash is invalid")
        before = operation.get("before_sha256")
        if before is not None and (not isinstance(before, str) or len(before) != 64):
            raise HarnessError("Invalid before-image hash")
    return approval, plan, project


def _save(run_dir: Path, journal: dict) -> None:
    journal["updated_at"] = utc_now()
    write_json(_run_path(run_dir, "transaction.json"), journal)


def _validate_journal(run_dir: Path, journal: dict, approval: dict) -> None:
    if (journal.get("schema_version") != 1
            or journal.get("project") != approval["project"]
            or journal.get("approval_hash") != approval["approval_hash"]
            or journal.get("plan_sha256") != approval["plan_sha256"]):
        raise HarnessError("Transaction journal does not belong to this approval")
    entries = journal.get("operations")
    if not isinstance(entries, list) or len(entries) != len(approval["operations"]):
        raise HarnessError("Invalid transaction journal operations")
    for index, (entry, operation) in enumerate(zip(entries, approval["operations"])):
        for field in ("relative_path", "before_sha256", "after_sha256", "candidate_ids"):
            if entry.get(field) != operation.get(field):
                raise HarnessError("Transaction journal operation was modified")
        expected_backup = f"backups/{index:04d}.before" if entry["before_sha256"] else None
        if entry.get("backup") != expected_backup:
            raise HarnessError("Invalid backup path in transaction journal")
        if expected_backup is not None:
            backup = _run_path(run_dir, expected_backup)
            if hash_file(backup) != entry["before_sha256"]:
                raise HarnessError("Transaction backup is missing or modified")
    directories = journal.get("created_dirs")
    if not isinstance(directories, list):
        raise HarnessError("Invalid directory list in transaction journal")
    allowed = set()
    for operation in approval["operations"]:
        parent = Path(operation["relative_path"]).parent
        while str(parent) != ".":
            allowed.add(parent.as_posix())
            parent = parent.parent
    if not all(isinstance(item, str) and item in allowed for item in directories):
        raise HarnessError("Journal directory is not a parent of an approved file")


def _prepare(run_dir: Path, project: Path, approval: dict) -> dict:
    # Complete preflight precedes any backup, journal, or target-file writes.
    before_images: list[bytes | None] = []
    missing_dirs: set[str] = set()
    for operation in approval["operations"]:
        target = _path(project, operation["relative_path"])
        if target.exists() and not target.is_file():
            raise HarnessError(f"Target is not an ordinary file: {operation['relative_path']}")
        data = target.read_bytes() if target.exists() else None
        actual = sha256_bytes(data) if data is not None else None
        if actual != operation["before_sha256"]:
            raise HarnessError(f"Stale base: {operation['relative_path']}; generate a new plan")
        before_images.append(data)
        parent = target.parent
        while parent != project:
            if not parent.exists():
                missing_dirs.add(parent.relative_to(project).as_posix())
            parent = parent.parent
    backup_dir = _run_path(run_dir, "backups")
    backup_dir.mkdir(exist_ok=True)
    operations = []
    for index, (operation, data) in enumerate(zip(approval["operations"], before_images)):
        entry = {key: operation[key] for key in
                 ("relative_path", "before_sha256", "after_sha256", "candidate_ids")}
        entry["backup"] = f"backups/{index:04d}.before" if data is not None else None
        entry["state"] = "pending"
        if data is not None:
            atomic_write(_run_path(run_dir, entry["backup"]), data)
        operations.append(entry)
    journal = {
        "schema_version": 1, "project": str(project),
        "approval_hash": approval["approval_hash"], "plan_sha256": approval["plan_sha256"],
        "created_at": utc_now(), "state": "prepared", "operations": operations,
        "created_dirs": sorted(missing_dirs, key=lambda item: (item.count("/"), item)),
    }
    _save(run_dir, journal)
    return journal


def _restore(run_dir: Path, project: Path, journal: dict, *, reason: str) -> dict:
    """Recover either an applied transaction or a partially written one."""
    conflicts = []
    for entry in journal["operations"]:
        target = _path(project, entry["relative_path"])
        actual = hash_file(target)
        if actual not in (entry["before_sha256"], entry["after_sha256"]):
            conflicts.append(entry["relative_path"])
        backup = entry["backup"]
        if backup is not None and hash_file(_run_path(run_dir, backup)) != entry["before_sha256"]:
            raise HarnessError(f"Backup is missing or changed: {entry['relative_path']}")
    if conflicts:
        journal["state"] = "rollback_conflict"
        journal["conflicts"] = conflicts
        _save(run_dir, journal)
        raise HarnessError("Rollback preserved later edits; conflicts: " + ", ".join(conflicts))
    journal["state"] = "rolling_back"
    journal["rollback_reason"] = reason
    journal.pop("conflicts", None)
    _save(run_dir, journal)
    for entry in reversed(journal["operations"]):
        target = _path(project, entry["relative_path"])
        current = hash_file(target)
        if current == entry["before_sha256"]:
            entry["state"] = "restored"
            _save(run_dir, journal)
            continue
        if current != entry["after_sha256"]:
            # Recheck immediately before mutation; an editor is outside our lock.
            raise HarnessError(f"File changed during rollback: {entry['relative_path']}")
        if entry["before_sha256"] is None:
            target.unlink()
        else:
            atomic_write(target, _run_path(run_dir, entry["backup"]).read_bytes())
        entry["state"] = "restored"
        _save(run_dir, journal)
    for relative in sorted(journal["created_dirs"], key=lambda item: item.count("/"), reverse=True):
        directory = _path(project, relative)
        if directory.exists():
            try:
                directory.rmdir()
            except OSError:
                # Never remove a directory containing files added after apply.
                pass
    journal["state"] = "rolled_back"
    _save(run_dir, journal)
    return journal


def _check_evaluation(run_dir: Path, approval: dict, override: bool) -> dict:
    """Refuse to apply a comparison that came out worse than the baseline.

    Evaluation stays optional: with no evaluation.json this returns "not_run"
    and apply proceeds. Once a comparison exists it has to belong to this
    approval and has to be favourable, otherwise measuring is decorative.
    """
    path = _run_path(run_dir, "evaluation.json")
    if not path.exists():
        return {"state": "not_run"}
    report = read_json(path)
    if not isinstance(report, dict):
        raise HarnessError("evaluation.json はJSONオブジェクトである必要があります")
    status = {"state": "checked", "kind": report.get("kind"),
              "summary": report.get("summary"), "complete": report.get("complete")}
    if report.get("approval_hash") != approval["approval_hash"]:
        status["state"] = "stale"
        if not override:
            raise HarnessError(
                "評価は別の承認に対するものです。evaluateをやり直すか、"
                "--ignore-evaluation と理由を指定してください")
        return status
    summary = report.get("summary") or {}
    regressions = summary.get("regressions") or []
    mean_delta = summary.get("mean_delta")
    failed = []
    if regressions:
        failed.append(f"検査の退行: {', '.join(map(str, regressions))}")
    if isinstance(mean_delta, (int, float)) and not isinstance(mean_delta, bool) and mean_delta < 0:
        failed.append(f"スコアが低下: 平均delta={mean_delta}")
    if not report.get("complete"):
        failed.append("評価が完了していません")
    if failed:
        status["state"] = "override" if override else "blocked"
        status["reasons"] = failed
        if not override:
            raise HarnessError(
                "評価結果が候補の適用を支持していません（" + " / ".join(failed) + "）。"
                "候補を選び直すか、--ignore-evaluation と理由を指定してください")
        return status
    status["state"] = "passed"
    return status


def apply_approval(run_dir: Path, ignore_evaluation: bool = False,
                   override_notes: str = "") -> dict:
    """Apply exactly the selected plan; recover interrupted attempts if safe."""
    run_dir = Path(run_dir).absolute()
    approval, _plan, project = _load_approval(run_dir)
    if ignore_evaluation and not override_notes.strip():
        raise HarnessError("--ignore-evaluation には理由（--notes）が必要です")
    with _project_lock(project):
        # Approval files may have changed while this invocation waited for a lock.
        checked, _plan, checked_project = _load_approval(run_dir)
        if checked_project != project or checked["approval_hash"] != approval["approval_hash"]:
            raise HarnessError("Approval changed while acquiring the project lock")
        approval = checked
        journal_path = _run_path(run_dir, "transaction.json")
        if journal_path.exists():
            journal = read_json(journal_path)
            _validate_journal(run_dir, journal, approval)
            if journal["state"] == "applied":
                for entry in journal["operations"]:
                    if hash_file(_path(project, entry["relative_path"])) != entry["after_sha256"]:
                        raise HarnessError("Applied files changed; refusing to overwrite later edits")
                return journal
            if journal["state"] == "rolled_back" and journal.get("rollback_reason") == "requested":
                raise HarnessError("This transaction was rolled back; create and approve a new run")
            if journal["state"] not in {
                "prepared", "applying", "failed", "rolling_back", "rollback_conflict", "rolled_back"
            }:
                raise HarnessError("Unknown transaction state; refusing mutation")
            _restore(run_dir, project, journal, reason="recovery")
        from .plan import verify_plan_inputs
        verify_plan_inputs(run_dir, _plan)
        # Gate on the comparison before touching the project, so a candidate
        # that measured worse cannot be applied by simply ignoring the report.
        evaluation_status = _check_evaluation(run_dir, approval, ignore_evaluation)
        if ignore_evaluation:
            evaluation_status["override_notes"] = override_notes.strip()
        journal = _prepare(run_dir, project, approval)
        journal["evaluation"] = evaluation_status
        try:
            journal["state"] = "applying"
            _save(run_dir, journal)
            for relative in journal["created_dirs"]:
                _path(project, relative).mkdir()
            for entry, operation in zip(journal["operations"], approval["operations"]):
                target = _path(project, entry["relative_path"])
                if hash_file(target) != entry["before_sha256"]:
                    raise HarnessError(f"File changed during apply: {entry['relative_path']}")
                entry["state"] = "writing"
                _save(run_dir, journal)
                atomic_write(target, operation["after_content"].encode("utf-8"))
                entry["state"] = "applied"
                _save(run_dir, journal)
            journal["state"] = "applied"
            _save(run_dir, journal)
            return journal
        except Exception as exc:
            journal["state"] = "failed"
            journal["error"] = str(exc)
            try:
                _save(run_dir, journal)
                _restore(run_dir, project, journal, reason="apply_failure")
            except Exception as recovery_error:
                raise HarnessError(
                    f"Apply failed: {exc}. Recovery incomplete: {recovery_error}. "
                    "Keep the run directory and retry rollback after resolving conflicts."
                ) from exc
            raise HarnessError(f"Apply failed; original files restored: {exc}") from exc


def rollback(run_dir: Path) -> dict:
    """Undo approved writes, refusing all writes when any preflight conflicts."""
    run_dir = Path(run_dir).absolute()
    approval, _plan, project = _load_approval(run_dir)
    with _project_lock(project):
        checked, _plan, checked_project = _load_approval(run_dir)
        if checked_project != project or checked["approval_hash"] != approval["approval_hash"]:
            raise HarnessError("Approval changed while acquiring the project lock")
        approval = checked
        journal_path = _run_path(run_dir, "transaction.json")
        if not journal_path.exists():
            raise HarnessError("No transaction exists for this run")
        journal = read_json(journal_path)
        _validate_journal(run_dir, journal, approval)
        if journal.get("state") == "rolled_back":
            return journal
        return _restore(run_dir, project, journal, reason="requested")
