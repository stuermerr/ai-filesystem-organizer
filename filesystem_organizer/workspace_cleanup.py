from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from .linux_filesystem import LinuxFilesystemError, nonblocking_lock
from .run_workspace import DATABASE_NAME, read_only_transaction, table_exists

COMPLETION_RECORD_NAME = "completion.json"
COMPLETION_RECORD_VERSION = 1
RUN_LOCK_NAME = ".filesystem-organizer.lock"


class WorkspaceCleanupError(Exception):
    """A safe refusal to discard non-terminal Run Workspace state."""


def read_completion_record(run_path: Path) -> dict[str, object] | None:
    """Read one small terminal record without following an unsafe entry."""
    record_path = run_path / COMPLETION_RECORD_NAME
    if record_path.is_symlink() or (record_path.exists() and not record_path.is_file()):
        raise WorkspaceCleanupError(
            f"Analysis Run contains an unsafe {COMPLETION_RECORD_NAME}: {run_path}"
        )
    if not record_path.exists():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceCleanupError(
            f"Analysis Run contains an invalid {COMPLETION_RECORD_NAME}: {run_path}"
        ) from error
    if (
        not isinstance(record, dict)
        or record.get("completion_record_version") != COMPLETION_RECORD_VERSION
        or not isinstance(record.get("status"), dict)
    ):
        raise WorkspaceCleanupError(
            f"Analysis Run contains an unsupported {COMPLETION_RECORD_NAME}: {run_path}"
        )
    status = cast(dict[str, object], record["status"])
    required_status_fields = {
        "completed_at",
        "plans",
        "recovery_available",
        "run_id",
        "schema_version",
        "selected_backup_root",
        "snapshot_id",
        "started_at",
        "status",
    }
    if (
        not required_status_fields.issubset(status)
        or not isinstance(status.get("plans"), list)
        or status.get("status") != "complete"
        or status.get("recovery_available") is not False
    ):
        raise WorkspaceCleanupError(
            f"Analysis Run contains inconsistent completion metadata: {run_path}"
        )
    return record


def _preserved(run_path: Path, reason: str) -> dict[str, object]:
    return {
        "analysis_run": str(run_path),
        "cleanup_status": "preserved",
        "reason": reason,
    }


def _safe_run_path(analysis_run: Path) -> Path:
    try:
        run_path = analysis_run.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise WorkspaceCleanupError(
            f"Analysis Run does not exist: {analysis_run}"
        ) from error
    if not run_path.is_dir():
        raise WorkspaceCleanupError(f"Analysis Run is not a directory: {run_path}")
    return run_path


def _eligible_destinations(database_path: Path) -> tuple[list[Path], str | None]:
    with read_only_transaction(database_path) as connection:
        required_tables = {
            "analysis_runs",
            "consolidation_plans",
            "materialization_attempts",
            "execution_manifests",
        }
        if not all(table_exists(connection, table) for table in required_tables):
            return [], "terminal materialization metadata is incomplete"
        run_rows = connection.execute(
            "SELECT status, writer_lease FROM analysis_runs"
        ).fetchall()
        if len(run_rows) != 1 or run_rows[0]["status"] != "complete":
            return [], "Analysis Run is incomplete"
        if run_rows[0]["writer_lease"] is not None:
            return [], "Analysis Run still has an active writer lease"
        plan_ids = {
            str(row[0])
            for row in connection.execute("SELECT plan_id FROM consolidation_plans")
        }
        if not plan_ids:
            return [], "Analysis Run has no completed materialization"
        attempts = connection.execute(
            "SELECT attempts.plan_id, attempts.state, "
            "attempts.failure_classification, manifests.mode, "
            "manifests.canonical_destination, manifests.staging_root_dev, "
            "manifests.staging_root_ino "
            "FROM materialization_attempts AS attempts "
            "LEFT JOIN execution_manifests AS manifests USING (attempt_id)"
        ).fetchall()
        if not attempts:
            return [], "Analysis Run has no completed materialization"
        if any(
            row["state"] != "COMPLETE"
            or row["failure_classification"] is not None
            or row["mode"] != "source-preserving"
            for row in attempts
        ):
            return [], "failed, incomplete, recoverable, or in-place state is preserved"
        completed_plan_ids = {str(row["plan_id"]) for row in attempts}
        if completed_plan_ids != plan_ids:
            return [], "an unmaterialized Consolidation Plan still needs the workspace"
        destinations: list[Path] = []
        for row in attempts:
            if (
                row["canonical_destination"] is None
                or row["staging_root_dev"] is None
                or row["staging_root_ino"] is None
            ):
                return [], "published output identity is incomplete"
            destination = Path(str(row["canonical_destination"]))
            try:
                observed = destination.lstat()
            except OSError:
                return [], "published output identity cannot be verified"
            if (
                not stat.S_ISDIR(observed.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or (observed.st_dev, observed.st_ino)
                != (int(row["staging_root_dev"]), int(row["staging_root_ino"]))
            ):
                return [], "published output identity cannot be verified"
            destinations.append(destination)
        return destinations, None


def _safe_sqlite_artifacts(run_path: Path) -> list[Path]:
    artifacts = [
        run_path / DATABASE_NAME,
        run_path / f"{DATABASE_NAME}-wal",
        run_path / f"{DATABASE_NAME}-shm",
    ]
    for artifact in artifacts:
        if artifact.is_symlink() or (artifact.exists() and not artifact.is_file()):
            raise WorkspaceCleanupError(
                f"Analysis Run contains an unsafe SQLite artifact: {artifact}"
            )
    return artifacts


def _write_completion_record(run_path: Path, status: dict[str, object]) -> None:
    record_path = run_path / COMPLETION_RECORD_NAME
    existing = read_completion_record(run_path)
    if existing is not None:
        return
    payload = {
        "cleaned_at": datetime.now(UTC).isoformat(),
        "completion_record_version": COMPLETION_RECORD_VERSION,
        "status": status,
    }
    temporary = run_path / f".{COMPLETION_RECORD_NAME}.tmp-{uuid4().hex}"
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, record_path)
    _fsync_directory(run_path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cleanup_completed_run_locked(analysis_run: Path) -> dict[str, object]:
    """Discard terminal SQLite state while the persistent run lock is held."""
    run_path = _safe_run_path(analysis_run)
    database_path = run_path / DATABASE_NAME
    record = read_completion_record(run_path)
    artifacts = _safe_sqlite_artifacts(run_path)
    if not database_path.exists():
        if record is None:
            raise WorkspaceCleanupError(
                f"Analysis Run is missing both {DATABASE_NAME} and completion metadata: {run_path}"
            )
        for artifact in artifacts[1:]:
            artifact.unlink(missing_ok=True)
        _fsync_directory(run_path)
        return {
            "analysis_run": str(run_path),
            "cleanup_status": "already-clean",
        }

    _destinations, reason = _eligible_destinations(database_path)
    if reason is not None:
        return _preserved(run_path, reason)

    # Import lazily so the control plane can read completion records without a cycle.
    from .control_plane import inspect_analysis_run

    status = inspect_analysis_run(run_path)
    plans = cast(list[dict[str, object]], status["plans"])
    status["plans"] = [
        {**plan, "recovery": None, "recovery_available": False} for plan in plans
    ]
    _write_completion_record(run_path, status)
    for artifact in artifacts:
        artifact.unlink(missing_ok=True)
    _fsync_directory(run_path)
    return {"analysis_run": str(run_path), "cleanup_status": "cleaned"}


def cleanup_completed_run(analysis_run: Path) -> dict[str, object]:
    """Clean one old terminal run under its persistent advisory lock."""
    run_path = _safe_run_path(analysis_run)
    try:
        with nonblocking_lock(run_path / RUN_LOCK_NAME):
            return cleanup_completed_run_locked(run_path)
    except LinuxFilesystemError as error:
        raise WorkspaceCleanupError(str(error)) from error
