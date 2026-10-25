from __future__ import annotations

import errno
import heapq
import json
import os
import shutil
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import NoReturn, cast

from blake3 import blake3

from .consolidation_plan import PLAN_STATUS_FINALIZED
from .content_identity import (
    CONTENT_IDENTITY_ALGORITHM,
    CONTENT_IDENTITY_ALGORITHM_VERSION,
    digest_file,
    stable_read,
)
from .execution_admission import (
    EXECUTION_MODE_IN_PLACE,
    EXECUTION_MODE_SOURCE_PRESERVING,
    EXECUTOR_HARD_LINK_PROTECTION,
    EXECUTOR_RENAME_PROTECTION,
    EXECUTOR_SOURCE_PRESERVING,
    admit_execution,
)
from .execution_admission import (
    RENAME_PROTECTION_VERSION as CURRENT_RENAME_PROTECTION_VERSION,
)
from .linux_filesystem import (
    CloneCapabilities,
    CloneResult,
    FilesystemSync,
    LinuxFilesystemError,
    LockedRenameFallbackError,
    destination_lock_path,
    nonblocking_lock,
    probe_hard_link_support,
    probe_rename_protection_support,
    publish_directory,
    rename_no_replace_raw,
    sync_filesystem,
    try_native_clone,
)
from .progress import MaterializationTelemetry, NullProgressReporter, ProgressReporter
from .rename_protection import OWNER_FILE as RENAME_V1_OWNER_FILE
from .rename_protection import RenameContext
from .rename_protection import staging_name as rename_v1_staging_name
from .rename_protection_v2 import (
    EXECUTOR_VERSION as RENAME_PROTECTION_V2_VERSION,
)
from .rename_protection_v2 import (
    RenameProtectionV2Error,
    RenameProtectionV2NeedsAttention,
)
from .rename_protection_v2 import ensure_schema as ensure_rename_v2_schema
from .rename_protection_v2 import execute as execute_rename_protection_v2
from .rename_protection_v2 import staging_name as rename_v2_staging_name
from .rename_protection_v3 import (
    ActivePivot as RenameV3ActivePivot,
)
from .rename_protection_v3 import (
    AttemptBinding as RenameV3AttemptBinding,
)
from .rename_protection_v3 import (
    Failure as RenameV3Failure,
)
from .rename_protection_v3 import (
    bind_attempt as bind_rename_v3_attempt,
)
from .rename_protection_v3 import (
    checkpoint as checkpoint_rename_v3,
)
from .rename_protection_v3 import (
    ensure_schema as ensure_rename_v3_schema,
)
from .rename_protection_v3 import (
    load_active_pivot as load_rename_v3_active_pivot,
)
from .rename_protection_v3 import (
    load_attempt_progress as load_rename_v3_progress,
)
from .rename_protection_v3 import (
    load_failures as load_rename_v3_failures,
)
from .rename_protection_v3 import (
    load_vacated as load_rename_v3_vacated,
)
from .rename_protection_v3 import (
    record_failure as record_rename_v3_failure,
)
from .rename_protection_v3 import (
    record_vacated as record_rename_v3_vacated,
)
from .rename_protection_v3 import (
    resolve_failure as resolve_rename_v3_failure,
)
from .rename_protection_v3 import (
    set_active_pivot as set_rename_v3_active_pivot,
)
from .run_workspace import (
    ATTEMPT_SCHEMA_VERSION,
    RunWorkspaceError,
    ensure_journal_schema,
    journal_append,
    journal_append_many,
    nearest_existing_directory,
    open_read_only,
    open_read_write,
    require_outside_selected_root,
    resolve_run_directory,
    select_plan_row,
    table_exists,
)
from .structural_relationships import revalidate_structural_snapshot
from .workflow_progress import WorkflowProgress

STAGING_DIRECTORY_NAME = ".materialization-staging"
PARTIAL_OWNER_FILE = ".filesystem-organizer-owner"
EXECUTION_MANIFEST_VERSION = 3
OUTCOME_BATCH_SIZE = 128
STAGING_CHECKPOINT_BYTE_SIZE = 1024 * 1024 * 1024
STAGING_CHECKPOINT_SECONDS = 30.0


@dataclass
class _CheckpointPolicy:
    """Bound redo by files/bytes/time, adapting time to measured barrier cost.

    Limits are evaluated between files; a single slow file can exceed them.
    An EWMA targets <=5% sync overhead, constrained to 1..30 seconds.
    """

    started: float
    interval: float = 5.0
    sync_cost: float | None = None

    def due(self, files: int, byte_count: int) -> bool:
        return (
            files >= OUTCOME_BATCH_SIZE
            or byte_count >= STAGING_CHECKPOINT_BYTE_SIZE
            or time.monotonic() - self.started
            >= min(self.interval, STAGING_CHECKPOINT_SECONDS)
        )

    def confirmed(self, sync_seconds: float) -> None:
        self.sync_cost = (
            sync_seconds
            if self.sync_cost is None
            else 0.75 * self.sync_cost + 0.25 * sync_seconds
        )
        self.interval = min(STAGING_CHECKPOINT_SECONDS, max(1.0, 20 * self.sync_cost))
        self.started = time.monotonic()


EVENT_PLAN_ADMITTED = "PLAN_ADMITTED"
EVENT_STRUCTURAL_SNAPSHOT_VERIFIED = "STRUCTURAL_SNAPSHOT_VERIFIED"
EVENT_FILE_COMPLETED = "FILE_COMPLETED"
EVENT_RECONCILIATION = "RECONCILIATION"
EVENT_MATERIALIZATION_FAILED = "MATERIALIZATION_FAILED"
EVENT_MATERIALIZATION_COMPLETE = "MATERIALIZATION_COMPLETE"
EVENT_DIRECTORY_CREATED = "DIRECTORY_CREATED"
EVENT_IN_PLACE_PROTECTION_COMPLETE = "IN_PLACE_PROTECTION_COMPLETE"
EVENT_IN_PLACE_PUBLICATION_CHECKPOINT = "IN_PLACE_PUBLICATION_CHECKPOINT"
EVENT_IN_PLACE_DUPLICATE_CLEANUP_CHECKPOINT = "IN_PLACE_DUPLICATE_CLEANUP_CHECKPOINT"
EVENT_IN_PLACE_DUPLICATE_CLEANUP_COMPLETE = "IN_PLACE_DUPLICATE_CLEANUP_COMPLETE"
EVENT_IN_PLACE_EXCLUSION_CLEANUP_CHECKPOINT = "IN_PLACE_EXCLUSION_CLEANUP_CHECKPOINT"
EVENT_IN_PLACE_EXCLUSION_CLEANUP_COMPLETE = "IN_PLACE_EXCLUSION_CLEANUP_COMPLETE"
EVENT_IN_PLACE_OUTPUT_DURABLE = "IN_PLACE_OUTPUT_DURABLE"
EVENT_IN_PLACE_STAGING_REMOVED = "IN_PLACE_STAGING_REMOVED"

# Kept as public compatibility names for callers that imported the former
# journal vocabulary. New executions intentionally do not emit these events.
EVENT_COPY_STARTED = "COPY_STARTED"
EVENT_STAGING_WRITTEN = "STAGING_WRITTEN"
EVENT_STAGING_VERIFIED = "STAGING_VERIFIED"
EVENT_FINAL_PUBLISHED = "FINAL_PUBLISHED"

CRASH_POINT_ENVIRONMENT = "FSO_MATERIALIZATION_CRASH_POINT"


class MaterializationError(Exception):
    """A safe, user-facing refusal of a Materialized Consolidation operation."""


class _DestinationConflict(MaterializationError):
    """A no-overwrite publication conflict, recorded as needs-attention."""


def _run_workspace_refusal[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except RunWorkspaceError as error:
            raise MaterializationError(str(error)) from error

    return wrapper


class _CrashSimulation(BaseException):
    """Simulate process termination at a durable materialization boundary."""


@dataclass(frozen=True)
class MaterializationPreflight:
    analysis_run: str
    plan_id: str
    run_id: str
    selected_backup_root: str
    destination: str
    operation_count: int
    structural_union_count: int
    explicit_directory_count: int
    total_bytes: int
    free_bytes: int
    sufficient_free_space: bool
    mode: str = EXECUTION_MODE_SOURCE_PRESERVING
    executor_kind: str = EXECUTOR_SOURCE_PRESERVING
    executor_version: int = 1
    staging_metadata_bytes: int = 0
    recovery_state: str = "not-started"


def _maybe_crash(crash_point: str) -> None:
    if os.environ.get(CRASH_POINT_ENVIRONMENT) == crash_point:
        raise _CrashSimulation(crash_point)


def _plan_entries(connection: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT entry_index AS operation_index, entry_kind, source_relative_path, "
        "output_relative_path, expected_byte_size, algorithm, algorithm_version, digest "
        ", evidence_kind, evidence_mtime_ns "
        "FROM final_plan_entries WHERE plan_id = ? ORDER BY entry_index",
        (plan_id,),
    ).fetchall()


def _excluded_file_entries(
    connection: sqlite3.Connection, plan_id: str
) -> list[sqlite3.Row]:
    """Frozen Custom Layout exclusions, kept separate from executable placements."""
    return connection.execute(
        "SELECT source_relative_path, expected_byte_size, digest "
        "FROM final_plan_exclusions WHERE plan_id = ? ORDER BY source_relative_path",
        (plan_id,),
    ).fetchall()


def _skipped_exclusions(connection: sqlite3.Connection, plan_id: str) -> list[str]:
    return [
        str(row["relative_path"])
        for row in connection.execute(
            "SELECT relative_path FROM final_plan_skipped_actions WHERE plan_id = ? "
            "AND action = 'exclude' ORDER BY relative_path",
            (plan_id,),
        )
    ]


def _skipped_exclusion_entries(
    connection: sqlite3.Connection, plan_id: str
) -> list[sqlite3.Row]:
    """Finalized skipped exclusions with their observed inventory entry type."""
    return connection.execute(
        "SELECT actions.relative_path, inventory.entry_kind "
        "FROM final_plan_skipped_actions AS actions "
        "JOIN consolidation_plans AS plans ON plans.plan_id=actions.plan_id "
        "LEFT JOIN inventory_entries AS inventory ON inventory.run_id=plans.run_id "
        "AND inventory.relative_path=actions.relative_path "
        "WHERE actions.plan_id=? AND actions.action='exclude' "
        "ORDER BY actions.relative_path",
        (plan_id,),
    ).fetchall()


def _require_in_place_skipped_actions(
    connection: sqlite3.Connection, plan_id: str
) -> None:
    expected = {
        str(row["relative_path"])
        for row in connection.execute(
            "SELECT relative_path FROM skipped_entry_findings WHERE run_id = "
            "(SELECT run_id FROM consolidation_plans WHERE plan_id = ?)",
            (plan_id,),
        )
    }
    configured = {
        str(row["relative_path"])
        for row in connection.execute(
            "SELECT relative_path FROM final_plan_skipped_actions WHERE plan_id = ?",
            (plan_id,),
        )
    }
    if missing := expected - configured:
        raise MaterializationError(
            "In-place materialization requires retain or exclude actions for skipped entries: "
            + ", ".join(sorted(missing))
        )


def _is_content_empty_result(connection: sqlite3.Connection, plan_id: str) -> bool:
    """Return the persisted approval fact; never infer it from mutable output rows."""
    row = connection.execute(
        "SELECT content_empty FROM final_plan_projection WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    return row is not None and bool(row["content_empty"])


@_run_workspace_refusal
def content_empty_result(analysis_run: Path, plan_id: str) -> bool:
    _, database_path = resolve_run_directory(analysis_run)
    with open_read_only(database_path) as connection:
        select_plan_row(connection, plan_id)
        return _is_content_empty_result(connection, plan_id)


def _plan_structural_roots(connection: sqlite3.Connection, plan_id: str) -> set[str]:
    plan = connection.execute(
        "SELECT schema_version FROM consolidation_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    try:
        table = (
            "plan_projection_union_roots"
            if plan is not None and int(plan["schema_version"]) >= 3
            else "plan_structural_roots"
        )
        return {
            str(row[0])
            for row in connection.execute(
                f"SELECT root_relative_path FROM {table} WHERE plan_id = ?", (plan_id,)
            )
        }
    except sqlite3.OperationalError:
        return set()


def _plan_structural_union_count(
    connection: sqlite3.Connection, plan_id: str, run_id: str, roots: set[str]
) -> int:
    for table in ("plan_projection_unions", "plan_structural_unions"):
        try:
            count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE plan_id = ?", (plan_id,)
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            count = 0
        if count:
            return count
    if not roots:
        return 0
    try:
        rows = connection.execute(
            "SELECT digest, root_relative_path FROM directory_fingerprints "
            "WHERE run_id = ? ORDER BY digest, root_relative_path",
            (run_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return len(roots) // 2
    components: dict[bytes, set[str]] = {}
    for row in rows:
        components.setdefault(row["digest"], set()).add(str(row["root_relative_path"]))
    exact_roots: set[str] = set()
    exact_count = 0
    for component in components.values():
        if len(component) > 1 and component <= roots:
            exact_count += 1
            exact_roots.update(component)
    return exact_count + (len(roots - exact_roots) // 2)


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _is_regular_no_follow(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directories(paths: Iterable[Path]) -> None:
    for path in sorted({path.resolve(strict=True) for path in paths}, key=str):
        _fsync_directory(path)


def _has_plan_event(connection: sqlite3.Connection, plan_id: str, event: str) -> bool:
    if not table_exists(connection, "materialization_events"):
        return False
    return bool(
        connection.execute(
            "SELECT COUNT(*) FROM materialization_events "
            "WHERE plan_id = ? AND event = ? AND operation_index IS NULL",
            (plan_id, event),
        ).fetchone()[0]
    )


def _has_terminal_failure(connection: sqlite3.Connection, plan_id: str) -> bool:
    return bool(
        connection.execute(
            "SELECT COUNT(*) FROM materialization_events WHERE plan_id = ? AND event = ?",
            (plan_id, EVENT_MATERIALIZATION_FAILED),
        ).fetchone()[0]
    )


def _completed_indexes(connection: sqlite3.Connection, plan_id: str) -> set[int]:
    return {
        int(row[0])
        for row in connection.execute(
            "SELECT operation_index FROM materialization_events "
            "WHERE plan_id = ? AND event = ? AND operation_index IS NOT NULL",
            (plan_id, EVENT_FILE_COMPLETED),
        )
    }


def _operation_failure_message(plan_id: str, row: sqlite3.Row, reason: str) -> str:
    return (
        "materialization failed: "
        f"plan {plan_id} operation {int(row['operation_index'])} "
        f"source {row['source_relative_path']!s} "
        f"output {row['output_relative_path']!s}: {reason}"
    )


def _raise_operation_failure(
    connection: sqlite3.Connection,
    plan_id: str,
    row: sqlite3.Row,
    reason: str,
    *,
    conflict: bool = False,
) -> NoReturn:
    index = int(row["operation_index"])
    if conflict:
        journal_append(
            connection,
            plan_id,
            EVENT_RECONCILIATION,
            index,
            f"observed conflict: {reason}",
        )
    journal_append(connection, plan_id, EVENT_MATERIALIZATION_FAILED, index, reason)
    raise MaterializationError(_operation_failure_message(plan_id, row, reason))


def _revalidate_structural_snapshot(
    connection: sqlite3.Connection, run_id: str, selected_root: Path, plan_id: str
) -> set[str]:
    roots = _plan_structural_roots(connection, plan_id)
    try:
        revalidate_structural_snapshot(connection, run_id, selected_root, roots)
    except RuntimeError as error:
        raise MaterializationError(str(error)) from error
    return roots


def _source_same_filesystem(selected_root: Path, destination: Path) -> bool:
    return (
        selected_root.stat().st_dev
        == nearest_existing_directory(destination).stat().st_dev
    )


def _destination_case_sensitive(destination: Path) -> bool:
    """Probe the actual destination volume without retaining a filesystem change."""
    parent = nearest_existing_directory(destination)
    probe = parent / f".filesystem-organizer-case-probe-{uuid.uuid4().hex}"
    alternate = probe.with_name(probe.name.upper())
    try:
        probe.touch(exist_ok=False)
        return not alternate.exists()
    except OSError as error:
        raise MaterializationError(
            f"Cannot determine destination case compatibility: {parent}"
        ) from error
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass


def _validate_destination_compatibility(
    entries: list[sqlite3.Row], destination: Path
) -> None:
    """Check destination-dependent names before any staging is admitted.

    Names are checked against the actual selected volume rather than the host
    platform, so a destination change necessarily receives a fresh probe.
    """
    paths = [str(entry["output_relative_path"]) for entry in entries]
    case_sensitive = _destination_case_sensitive(destination)
    folded: dict[str, str] = {}
    forbidden = set('<>:"/\\|?*')
    for path in paths:
        if os.name == "nt" and (
            any(character in forbidden for character in path)
            or any(part.rstrip(". ") != part for part in path.split("/"))
        ):
            raise MaterializationError(
                f"Destination does not support output name: {path}"
            )
        key = path if case_sensitive else path.casefold()
        prior = folded.get(key)
        if prior is not None and prior != path:
            raise MaterializationError(
                f"Destination has a case-equivalent output collision: {prior}, {path}"
            )
        folded[key] = path


def _probe_persisted_executor(
    selected_root: Path, executor_kind: str, executor_version: int
) -> None:
    """Reprobe exactly the persisted executor; never switch at execution time."""
    supported_version = (
        executor_version
        in {RENAME_PROTECTION_V2_VERSION, CURRENT_RENAME_PROTECTION_VERSION}
        if executor_kind == EXECUTOR_RENAME_PROTECTION
        else executor_version == 1
    )
    if not supported_version:
        raise MaterializationError(
            f"Unsupported in-place executor version: {executor_version}"
        )
    try:
        if executor_kind == EXECUTOR_HARD_LINK_PROTECTION:
            probe_hard_link_support(selected_root)
        elif executor_kind == EXECUTOR_RENAME_PROTECTION:
            probe_rename_protection_support(selected_root)
        else:
            raise MaterializationError(
                f"Unsupported in-place executor: {executor_kind}"
            )
    except (LinuxFilesystemError, OSError) as error:
        raise MaterializationError(str(error)) from error


def _require_admitted_execution_mode(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    in_place: bool,
    allow_compatibility_selection: bool,
) -> tuple[str, int]:
    """Read durable intent, optionally selecting a provisional legacy intent."""
    if not table_exists(connection, "analysis_run_execution_intent"):
        if not allow_compatibility_selection:
            raise MaterializationError(
                "Legacy Analysis Run requires explicit compatibility preflight"
            )
        selected = admit_execution(
            Path(
                str(
                    connection.execute(
                        "SELECT selected_backup_root FROM analysis_runs WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                )
            ),
            in_place=in_place,
        )
        return selected.executor_kind, selected.executor_version
    columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(analysis_run_execution_intent)"
        )
    }
    intent = connection.execute(
        "SELECT * FROM analysis_run_execution_intent WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if intent is None:
        if not allow_compatibility_selection:
            raise MaterializationError(
                "Analysis Run requires explicit compatibility preflight"
            )
        selected = admit_execution(
            Path(
                str(
                    connection.execute(
                        "SELECT selected_backup_root FROM analysis_runs WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                )
            ),
            in_place=in_place,
        )
        return selected.executor_kind, selected.executor_version
    requested_mode = (
        EXECUTION_MODE_IN_PLACE if in_place else EXECUTION_MODE_SOURCE_PRESERVING
    )
    admitted_mode = str(intent["execution_mode"])
    if admitted_mode != requested_mode:
        raise MaterializationError(
            f"Analysis Run admitted execution mode is {admitted_mode}; requested "
            f"mode is {requested_mode}"
        )
    executor_kind = str(intent["executor_kind"])
    executor_version = (
        int(intent["executor_version"]) if "executor_version" in columns else 1
    )
    # RenameProtection v2 can consume the unchanged Analysis Run and finalized
    # Plan admitted for v1.  Attempt supersession is validated at execution.
    if executor_kind == EXECUTOR_RENAME_PROTECTION and executor_version == 1:
        executor_version = RENAME_PROTECTION_V2_VERSION
    supported = (
        {EXECUTOR_HARD_LINK_PROTECTION, EXECUTOR_RENAME_PROTECTION}
        if in_place
        else {EXECUTOR_SOURCE_PRESERVING}
    )
    if executor_kind not in supported:
        raise MaterializationError(
            "Analysis Run execution intent selects an unsupported executor"
        )
    return executor_kind, executor_version


def _preflight_materialization(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None,
    *,
    in_place: bool,
    revalidate_sources: bool,
    revalidate_structural_sources: bool,
    allow_compatibility_selection: bool = False,
) -> MaterializationPreflight:
    if not plan_id:
        raise MaterializationError(
            "Materialization requires an explicit Consolidation Plan identifier"
        )
    run_path, database_path = resolve_run_directory(analysis_run)
    connection = open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        plan = select_plan_row(connection, plan_id)
        resolved_plan_id = str(plan["plan_id"])
        if plan["status"] != PLAN_STATUS_FINALIZED:
            raise MaterializationError(
                f"Consolidation Plan is not finalized: {resolved_plan_id}"
            )
        run_id = str(plan["run_id"])
        executor_kind, executor_version = _require_admitted_execution_mode(
            connection,
            run_id,
            in_place=in_place,
            allow_compatibility_selection=allow_compatibility_selection,
        )
        selected_root = Path(
            str(
                connection.execute(
                    "SELECT selected_backup_root FROM analysis_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()["selected_backup_root"]
            )
        )
        if in_place:
            _require_in_place_skipped_actions(connection, resolved_plan_id)
            if destination is not None:
                raise MaterializationError("--in-place does not accept --destination")
            resolved_destination = selected_root
        else:
            resolved_destination = (
                Path(destination).expanduser().resolve(strict=False)
                if destination is not None
                else Path(str(plan["intended_destination"]))
                .expanduser()
                .resolve(strict=False)
            )
            require_outside_selected_root(
                resolved_destination,
                "Materialized Consolidation destination",
                selected_root,
            )
        if in_place and not _source_same_filesystem(
            selected_root, resolved_destination
        ):
            raise MaterializationError(
                "Materialized Consolidation destination must be on the same filesystem "
                "as the Selected Backup Root"
            )
        recovery_state = "not-started"
        if in_place:
            if (
                executor_kind == EXECUTOR_RENAME_PROTECTION
                and executor_version == RENAME_PROTECTION_V2_VERSION
            ):
                staging_candidates = list(
                    selected_root.glob(
                        f".filesystem-organizer-staging-v2-{resolved_plan_id}-*"
                    )
                ) + [selected_root / rename_v1_staging_name(resolved_plan_id)]
            elif executor_kind == EXECUTOR_RENAME_PROTECTION:
                # V3 has no ordinary staging namespace to enumerate.  Its rare
                # pivot ownership belongs to the v3 executor's sparse recovery.
                staging_candidates = []
            else:
                staging_candidates = [
                    selected_root / _in_place_staging_name(resolved_plan_id)
                ]
            occupied_staging = next(
                (
                    staging
                    for staging in staging_candidates
                    if _path_entry_exists(staging)
                ),
                None,
            )
            if occupied_staging is not None:
                manifest = (
                    connection.execute(
                        "SELECT mode FROM execution_manifests WHERE plan_id = ?",
                        (resolved_plan_id,),
                    ).fetchone()
                    if table_exists(connection, "execution_manifests")
                    else None
                )
                if manifest is None or manifest["mode"] != EXECUTION_MODE_IN_PLACE:
                    raise MaterializationError(
                        "In-place staging directory has an unexpected occupant: "
                        f"{occupied_staging}"
                    )
                recovery_state = (
                    "staging-present; execution will reconcile proven state"
                )
            _probe_persisted_executor(selected_root, executor_kind, executor_version)
        if not in_place and resolved_destination.exists():
            recovery_state = _existing_destination_recovery_state(
                connection,
                resolved_plan_id,
                run_id,
                selected_root,
                resolved_destination,
            )
        roots = (
            _revalidate_structural_snapshot(
                connection, run_id, selected_root, resolved_plan_id
            )
            if revalidate_structural_sources
            else _plan_structural_roots(connection, resolved_plan_id)
        )
        entries = _plan_entries(connection, resolved_plan_id)
        _validate_destination_compatibility(entries, resolved_destination)
        if revalidate_sources:
            for entry in entries:
                if entry["entry_kind"] == "file":
                    _validate_source_metadata_observation(
                        selected_root / str(entry["source_relative_path"]),
                        int(entry["expected_byte_size"]),
                        entry["evidence_mtime_ns"],
                    )
        union_count = _plan_structural_union_count(
            connection, resolved_plan_id, run_id, roots
        )
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    files = [entry for entry in entries if entry["entry_kind"] == "file"]
    total_bytes = sum(int(entry["expected_byte_size"]) for entry in files)
    free_bytes = shutil.disk_usage(
        nearest_existing_directory(resolved_destination)
    ).free
    staging_metadata_bytes = ((len(files) + len(entries) + 3) * 4096) + (1024 * 1024)
    return MaterializationPreflight(
        analysis_run=str(run_path),
        plan_id=resolved_plan_id,
        run_id=run_id,
        selected_backup_root=str(selected_root),
        destination=str(resolved_destination),
        operation_count=len(files),
        structural_union_count=union_count,
        explicit_directory_count=len(entries) - len(files),
        total_bytes=total_bytes,
        free_bytes=int(free_bytes),
        sufficient_free_space=(
            free_bytes >= staging_metadata_bytes
            if in_place
            else free_bytes >= total_bytes
        ),
        mode=EXECUTION_MODE_IN_PLACE if in_place else EXECUTION_MODE_SOURCE_PRESERVING,
        executor_kind=executor_kind,
        executor_version=executor_version,
        staging_metadata_bytes=staging_metadata_bytes if in_place else 0,
        recovery_state=recovery_state,
    )


def _existing_destination_recovery_state(
    connection: sqlite3.Connection,
    plan_id: str,
    run_id: str,
    selected_root: Path,
    destination: Path,
) -> str:
    """Accept only a final root proven to be an interrupted owned publication."""
    if not table_exists(connection, "materialization_attempts"):
        raise MaterializationError(
            f"Materialized Consolidation destination already exists: {destination}"
        )
    attempt = connection.execute(
        "SELECT attempt.attempt_id, attempt.state, manifest.attempt_id AS manifest_attempt_id, "
        "manifest.manifest_version, manifest.mode, manifest.run_id, "
        "manifest.selected_backup_root, manifest.canonical_destination, "
        "manifest.finalized_projection_fingerprint, manifest.staging_owner_attempt_id, "
        "manifest.staging_root_dev, manifest.staging_root_ino "
        "FROM materialization_attempts AS attempt "
        "LEFT JOIN execution_manifests AS manifest USING (attempt_id) "
        "WHERE attempt.plan_id = ? ORDER BY attempt.created_at DESC, attempt.attempt_id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    if attempt is None:
        raise MaterializationError(
            f"Materialized Consolidation destination already exists: {destination}"
        )
    if attempt["state"] not in {"STAGING_DURABLE", "PUBLISHED"}:
        raise MaterializationError(
            f"Materialized Consolidation destination already exists: {destination}"
        )
    projection = connection.execute(
        "SELECT fingerprint FROM final_plan_projection WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    ambiguous = (
        attempt["manifest_attempt_id"] is None
        or int(attempt["manifest_version"] or 0) != EXECUTION_MANIFEST_VERSION
        or attempt["mode"] != EXECUTION_MODE_SOURCE_PRESERVING
        or attempt["run_id"] != run_id
        or attempt["selected_backup_root"] != str(selected_root)
        or attempt["canonical_destination"] != str(destination)
        or projection is None
        or attempt["finalized_projection_fingerprint"] != projection["fingerprint"]
        or attempt["staging_owner_attempt_id"] != attempt["attempt_id"]
        or attempt["staging_root_dev"] is None
        or attempt["staging_root_ino"] is None
        or _partial_destination(destination, plan_id).exists()
    )
    try:
        destination_stat = destination.lstat()
    except OSError as error:
        raise MaterializationError(
            f"needs-attention: recovery-ambiguous final destination: {destination}"
        ) from error
    if (
        ambiguous
        or not stat.S_ISDIR(destination_stat.st_mode)
        or stat.S_ISLNK(destination_stat.st_mode)
        or (destination_stat.st_dev, destination_stat.st_ino)
        != (int(attempt["staging_root_dev"]), int(attempt["staging_root_ino"]))
    ):
        raise MaterializationError(
            f"needs-attention: recovery-ambiguous final destination: {destination}"
        )
    return str(attempt["state"])


def _current_evidence_key(
    observed: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _remember_link_updated_evidence(
    source: Path,
    expected_size: int,
    expected_digest: str | None,
    verified_evidence: dict[tuple[int, int, int, int, int, int], tuple[int, str]],
) -> None:
    """Carry verified bytes across the ctime-only change caused by link(2)."""
    if expected_digest is None:
        return
    verified_evidence[_current_evidence_key(source.lstat())] = (
        expected_size,
        expected_digest,
    )


def _validate_source_before_copy(
    source: Path,
    expected_size: int,
    expected_digest: str | None,
    verified_evidence: dict[tuple[int, int, int, int, int, int], tuple[int, str]]
    | None = None,
    telemetry: MaterializationTelemetry | None = None,
) -> None:
    try:
        observed = source.lstat()
    except FileNotFoundError as error:
        raise MaterializationError("source file is missing") from error
    except OSError as error:
        raise MaterializationError("source file is unreadable") from error
    if not stat.S_ISREG(observed.st_mode) or observed.st_size != expected_size:
        raise MaterializationError(
            "source file content no longer matches the plan's content identity"
        )
    evidence_key = _current_evidence_key(observed)
    if expected_digest is None:
        return
    cached = (
        verified_evidence.get(evidence_key) if verified_evidence is not None else None
    )
    if cached is not None:
        observed_size, observed_digest = cached
    else:
        try:
            observed_size, observed_digest = digest_file(source)
        except FileNotFoundError as error:
            raise MaterializationError("source file is missing") from error
        except OSError as error:
            raise MaterializationError("source file is unreadable") from error
        if verified_evidence is not None:
            verified_evidence[evidence_key] = (observed_size, observed_digest)
        if telemetry is not None:
            telemetry.revalidated_files += 1
            telemetry.revalidated_bytes += observed_size
    if observed_size != expected_size or (
        expected_digest is not None and observed_digest != expected_digest
    ):
        raise MaterializationError(
            "source file content no longer matches the plan's content identity"
        )


def _validate_source_metadata_observation(
    source: Path, expected_size: int, expected_modified_ns: int | None
) -> None:
    try:
        observed = source.lstat()
    except FileNotFoundError as error:
        raise MaterializationError("source file is missing") from error
    except OSError as error:
        raise MaterializationError("source file is unreadable") from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_size != expected_size
        or (
            expected_modified_ns is not None
            and observed.st_mtime_ns != int(expected_modified_ns)
        )
    ):
        raise MaterializationError("source file metadata no longer matches the plan")


@_run_workspace_refusal
def preflight_materialization(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None = None,
    *,
    in_place: bool = False,
) -> MaterializationPreflight:
    provisional = _preflight_materialization(
        analysis_run,
        plan_id,
        destination,
        in_place=in_place,
        revalidate_sources=True,
        revalidate_structural_sources=True,
        allow_compatibility_selection=True,
    )
    if _persist_compatibility_intent_if_missing(provisional):
        return _preflight_materialization(
            analysis_run,
            plan_id,
            destination,
            in_place=in_place,
            revalidate_sources=True,
            revalidate_structural_sources=True,
        )
    return provisional


@_run_workspace_refusal
def preflight_materialization_for_execution(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None = None,
    *,
    in_place: bool = False,
) -> MaterializationPreflight:
    return _preflight_materialization(
        analysis_run,
        plan_id,
        destination,
        in_place=in_place,
        revalidate_sources=False,
        revalidate_structural_sources=False,
    )


def _persist_compatibility_intent_if_missing(
    preflight: MaterializationPreflight,
) -> bool:
    """Durably bind a validated legacy run before its first attempt."""
    _run_path, database_path = resolve_run_directory(Path(preflight.analysis_run))
    with open_read_only(database_path) as observed:
        if (
            table_exists(observed, "analysis_run_execution_intent")
            and observed.execute(
                "SELECT 1 FROM analysis_run_execution_intent WHERE run_id=?",
                (preflight.run_id,),
            ).fetchone()
            is not None
        ):
            return False
    with open_read_write(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS analysis_run_execution_intent ("
                "run_id TEXT PRIMARY KEY, execution_mode TEXT NOT NULL, "
                "executor_kind TEXT NOT NULL, executor_version INTEGER NOT NULL, "
                "admission_version INTEGER NOT NULL, admitted_at TEXT NOT NULL, "
                "FOREIGN KEY (run_id) REFERENCES analysis_runs(run_id))"
            )
            intent_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(analysis_run_execution_intent)"
                )
            }
            if "executor_version" not in intent_columns:
                connection.execute(
                    "ALTER TABLE analysis_run_execution_intent ADD COLUMN "
                    "executor_version INTEGER NOT NULL DEFAULT 1"
                )
            existing = connection.execute(
                "SELECT 1 FROM analysis_run_execution_intent WHERE run_id=?",
                (preflight.run_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return False
            if table_exists(connection, "materialization_attempts"):
                attempt = connection.execute(
                    "SELECT 1 FROM materialization_attempts WHERE plan_id=? LIMIT 1",
                    (preflight.plan_id,),
                ).fetchone()
                if attempt is not None:
                    raise MaterializationError(
                        "Compatibility admission refuses a run with an existing attempt"
                    )
            selected_root = Path(preflight.selected_backup_root)
            if any(selected_root.glob(".filesystem-organizer-staging*")):
                raise MaterializationError(
                    "Compatibility admission refuses existing staging"
                )
            connection.execute(
                "INSERT INTO analysis_run_execution_intent "
                "(run_id, execution_mode, executor_kind, executor_version, "
                "admission_version, admitted_at) VALUES (?,?,?,?,1,datetime('now'))",
                (
                    preflight.run_id,
                    preflight.mode,
                    preflight.executor_kind,
                    preflight.executor_version,
                ),
            )
            connection.commit()
            return True
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def render_preflight_summary(preflight: MaterializationPreflight) -> str:
    sufficient = "sufficient" if preflight.sufficient_free_space else "insufficient"
    in_place_lines = (
        (
            f"  In-place staging metadata reservation: {preflight.staging_metadata_bytes} bytes (includes margin)",
            f"  Recovery state: {preflight.recovery_state}",
            f"  Executor: {preflight.executor_kind} v{preflight.executor_version}",
            *(
                (
                    "  No hard-link protection; RenameProtection temporarily keeps one owned pathname.",
                    "  Recovery is forward-only and every unexplained destination occupant is refused.",
                )
                if preflight.executor_kind == EXECUTOR_RENAME_PROTECTION
                else ()
            ),
            "  Source must remain quiescent while destructive execution runs.",
        )
        if preflight.mode == EXECUTION_MODE_IN_PLACE
        else ()
    )
    return "\n".join(
        (
            "Materialize Consolidation Plan",
            f"  Plan ID: {preflight.plan_id}",
            f"  Source (Selected Backup Root): {preflight.selected_backup_root}",
            f"  Destination: {preflight.destination}",
            f"  Operation count: {preflight.operation_count}",
            f"  Structural Union count: {preflight.structural_union_count}",
            f"  Explicit directory count: {preflight.explicit_directory_count}",
            f"  Total bytes: {preflight.total_bytes}",
            f"  Free space at destination: {preflight.free_bytes} bytes ({sufficient})",
            *in_place_lines,
            "",
        )
    )


def _ensure_execution_manifest(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    staging_directory_name: str,
    *,
    force_new_attempt: bool = False,
) -> list[sqlite3.Row]:
    manifest = connection.execute(
        "SELECT attempt_id, manifest_version, mode, run_id, selected_backup_root, "
        "canonical_destination, finalized_projection_fingerprint "
        "FROM execution_manifests WHERE plan_id = ? "
        "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
        (preflight.plan_id,),
    ).fetchone()
    if manifest is None or force_new_attempt:
        entries = _plan_entries(connection, preflight.plan_id)
        attempt_id = str(uuid.uuid4())
        connection.execute("BEGIN IMMEDIATE")
        try:
            projection_fingerprint = str(
                connection.execute(
                    "SELECT fingerprint FROM final_plan_projection WHERE plan_id = ?",
                    (preflight.plan_id,),
                ).fetchone()[0]
            )
            file_count = sum(row["entry_kind"] == "file" for row in entries)
            connection.execute(
                "INSERT INTO materialization_attempts "
                "(attempt_id, attempt_version, plan_id, state, created_at, updated_at, "
                "completed_at, planned_file_count, planned_byte_count) "
                "VALUES (?, ?, ?, 'ADMITTED', datetime('now'), datetime('now'), NULL, ?, ?)",
                (
                    attempt_id,
                    ATTEMPT_SCHEMA_VERSION,
                    preflight.plan_id,
                    file_count,
                    preflight.total_bytes,
                ),
            )
            connection.execute(
                "INSERT INTO execution_manifests "
                "(attempt_id, plan_id, manifest_version, mode, run_id, "
                "selected_backup_root, canonical_destination, "
                "finalized_projection_fingerprint, staging_owner_attempt_id, "
                "staging_relative_path, staging_root_dev, staging_root_ino, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, datetime('now'))",
                (
                    attempt_id,
                    preflight.plan_id,
                    EXECUTION_MANIFEST_VERSION,
                    preflight.mode,
                    preflight.run_id,
                    preflight.selected_backup_root,
                    preflight.destination,
                    projection_fingerprint,
                    attempt_id,
                    staging_directory_name,
                ),
            )
            connection.executemany(
                "INSERT INTO execution_manifest_entries "
                "(attempt_id, operation_index, entry_kind, source_relative_path, "
                "output_relative_path, expected_byte_size, algorithm, algorithm_version, "
                "digest, expected_modified_ns, temporary_relative_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        attempt_id,
                        int(row["operation_index"]),
                        str(row["entry_kind"]),
                        row["source_relative_path"],
                        str(row["output_relative_path"]),
                        int(row["expected_byte_size"] or 0),
                        row["algorithm"],
                        row["algorithm_version"],
                        row["digest"],
                        row["evidence_mtime_ns"],
                        f"{staging_directory_name}/{preflight.plan_id}/{int(row['operation_index'])}.part"
                        if row["entry_kind"] == "file"
                        else None,
                    )
                    for row in entries
                ],
            )
        except BaseException:
            connection.rollback()
            raise
        connection.commit()
    elif (
        int(manifest["manifest_version"]) != EXECUTION_MANIFEST_VERSION
        or manifest["mode"] != preflight.mode
        or manifest["run_id"] != preflight.run_id
        or manifest["selected_backup_root"] != preflight.selected_backup_root
        or manifest["canonical_destination"] != preflight.destination
        or manifest["finalized_projection_fingerprint"]
        != connection.execute(
            "SELECT fingerprint FROM final_plan_projection WHERE plan_id = ?",
            (preflight.plan_id,),
        ).fetchone()[0]
    ):
        raise MaterializationError(
            "Execution manifest does not match this finalized plan, source, and destination"
        )
    return connection.execute(
        "SELECT entry.operation_index, entry.entry_kind, entry.source_relative_path, "
        "entry.output_relative_path, entry.expected_byte_size, entry.algorithm, "
        "entry.algorithm_version, entry.digest, entry.expected_modified_ns, "
        "entry.temporary_relative_path, inventory.modified_ns AS observed_modified_ns "
        "FROM execution_manifest_entries AS entry "
        "JOIN execution_manifests AS manifest USING (attempt_id) "
        "LEFT JOIN inventory_entries AS inventory ON inventory.run_id = manifest.run_id "
        "AND inventory.relative_path = entry.source_relative_path "
        "WHERE entry.attempt_id = (SELECT attempt_id FROM execution_manifests "
        "WHERE plan_id = ? ORDER BY created_at DESC, attempt_id DESC LIMIT 1) "
        "ORDER BY entry.operation_index",
        (preflight.plan_id,),
    ).fetchall()


def _complete_materialization_attempt(
    connection: sqlite3.Connection,
    plan_id: str,
    cloned_file_count: int,
    cloned_byte_count: int,
    streamed_file_count: int,
    streamed_byte_count: int,
) -> None:
    """Close the current legacy execution as one compact successful Attempt."""
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE materialization_attempts SET state = 'COMPLETE', "
        "updated_at = datetime('now'), completed_at = datetime('now'), "
        "cloned_file_count = ?, cloned_byte_count = ?, "
        "streamed_file_count = ?, streamed_byte_count = ? "
        "WHERE attempt_id = (SELECT attempt_id FROM execution_manifests "
        "WHERE plan_id = ? ORDER BY created_at DESC, attempt_id DESC LIMIT 1)",
        (
            cloned_file_count,
            cloned_byte_count,
            streamed_file_count,
            streamed_byte_count,
            plan_id,
        ),
    )
    connection.commit()


def _set_attempt_state(
    connection: sqlite3.Connection, plan_id: str, state: str
) -> None:
    """Persist one coarse source-preserving execution phase transition."""
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE materialization_attempts SET state = ?, updated_at = datetime('now'), "
        "completed_at = NULL, failure_classification = NULL, failure_detail = NULL "
        "WHERE attempt_id = (SELECT attempt_id FROM execution_manifests "
        "WHERE plan_id = ? ORDER BY created_at DESC, attempt_id DESC LIMIT 1)",
        (state, plan_id),
    )
    connection.commit()


def _mark_attempt_failed(
    connection: sqlite3.Connection, plan_id: str, classification: str, detail: str
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE materialization_attempts SET state = 'FAILED', "
        "updated_at = datetime('now'), completed_at = datetime('now'), "
        "failure_classification = ?, failure_detail = ? "
        "WHERE attempt_id = (SELECT attempt_id FROM execution_manifests "
        "WHERE plan_id = ? ORDER BY created_at DESC, attempt_id DESC LIMIT 1)",
        (classification, detail, plan_id),
    )
    connection.commit()


def _attempt_transfer_totals(
    connection: sqlite3.Connection, plan_id: str
) -> tuple[int, int, int, int]:
    row = connection.execute(
        "SELECT "
        "COALESCE(SUM(transfer_kind = 'native-clone'), 0), "
        "COALESCE(SUM(CASE WHEN transfer_kind = 'native-clone' "
        "THEN observed_byte_size ELSE 0 END), 0), "
        "COALESCE(SUM(transfer_kind = 'streamed'), 0), "
        "COALESCE(SUM(CASE WHEN transfer_kind = 'streamed' "
        "THEN observed_byte_size ELSE 0 END), 0) "
        "FROM execution_file_evidence WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    assert row is not None
    return (int(row[0]), int(row[1]), int(row[2]), int(row[3]))


def _reconcile_published_destination(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    final_root: Path,
) -> bool:
    """Finish only a publication proven by the durable staging-root identity."""
    _existing_destination_recovery_state(
        connection,
        preflight.plan_id,
        preflight.run_id,
        Path(preflight.selected_backup_root),
        final_root,
    )
    try:
        _fsync_directory(final_root.parent)
    except OSError as error:
        _mark_attempt_failed(
            connection,
            preflight.plan_id,
            "io-integrity-manual-attention",
            "recovery parent-directory fsync failed",
        )
        raise MaterializationError(
            f"needs-attention: recovery-ambiguous final destination: {final_root}"
        ) from error
    _set_attempt_state(connection, preflight.plan_id, "PUBLISHED")
    _complete_materialization_attempt(
        connection,
        preflight.plan_id,
        *_attempt_transfer_totals(connection, preflight.plan_id),
    )
    return True


def _partial_destination(destination: Path, plan_id: str) -> Path:
    """Name the complete owned normal-path staging tree beside its final path."""
    return destination.parent / f".{destination.name}.partial-{plan_id}"


def _partial_owner_payload(connection: sqlite3.Connection, plan_id: str) -> str:
    row = connection.execute(
        "SELECT manifest.attempt_id, manifest.canonical_destination, "
        "manifest.finalized_projection_fingerprint FROM execution_manifests AS manifest "
        "WHERE manifest.plan_id = ? ORDER BY manifest.created_at DESC, "
        "manifest.attempt_id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    if row is None:
        raise MaterializationError(
            "owned partial destination has no execution manifest"
        )
    return "\n".join(str(value) for value in row) + "\n"


def _publication_os_error(error: BaseException) -> OSError | None:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, OSError):
            return current
        current = current.__cause__
    return None


def _publication_failure(error: LinuxFilesystemError) -> tuple[str, str]:
    operating_system_error = _publication_os_error(error)
    code = operating_system_error.errno if operating_system_error is not None else None
    if code == errno.EROFS:
        classification = "retryable-environment"
        detail = "destination filesystem became read-only during atomic publication"
    elif code in {errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR, errno.ENOTDIR}:
        classification = "destination-conflict"
        detail = "final destination appeared before or during publication"
    elif code in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP} and not isinstance(
        error, LockedRenameFallbackError
    ):
        classification = "unsupported-capability"
        detail = "destination filesystem rejected atomic no-replace publication"
    else:
        classification = "io-integrity-manual-attention"
        detail = (
            "locked rename fallback publication failed"
            if isinstance(error, LockedRenameFallbackError)
            else "atomic no-replace publication failed"
        )
    if code is not None:
        detail += f" ({errno.errorcode.get(code, code)}: {os.strerror(code)})"
    return classification, detail


def _publish_durable_staging(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    partial_root: Path,
    final_root: Path,
) -> None:
    """Publish a durable tree or preserve exact retry evidence on failure."""
    try:
        publish_directory(partial_root, final_root)
    except LinuxFilesystemError as error:
        classification, detail = _publication_failure(error)
        _mark_attempt_failed(connection, preflight.plan_id, classification, detail)
        raise MaterializationError(
            f"publication failed ({classification}): {detail}; "
            f"durable staged tree preserved at {partial_root}"
        ) from error


def _expected_staging_paths(
    entries: list[sqlite3.Row],
) -> tuple[set[str], set[str]]:
    files = {
        str(row["output_relative_path"])
        for row in entries
        if row["entry_kind"] == "file"
    }
    directories = {
        str(row["output_relative_path"])
        for row in entries
        if row["entry_kind"] == "directory"
    }
    for path in files | set(directories):
        parent = Path(path).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return files, directories


def _staging_identity_requires_rebind(
    attempt: sqlite3.Row,
    observed_root: os.stat_result,
    partial_root: Path,
    *,
    kind: str,
) -> bool:
    """Accept an inode change only on the already-recorded destination device."""
    expected_identity = (attempt["staging_root_dev"], attempt["staging_root_ino"])
    if (
        not stat.S_ISDIR(observed_root.st_mode)
        or stat.S_ISLNK(observed_root.st_mode)
        or None in expected_identity
    ):
        raise MaterializationError(
            f"needs-attention: {kind} staging ownership is ambiguous: {partial_root}"
        )
    expected_device, expected_inode = (int(value) for value in expected_identity)
    if observed_root.st_dev != expected_device:
        raise MaterializationError(
            f"needs-attention: {kind} staging ownership is ambiguous: {partial_root}"
        )
    return observed_root.st_ino != expected_inode


def _validate_staging_owner_payload(
    connection: sqlite3.Connection,
    plan_id: str,
    partial_root: Path,
    *,
    kind: str,
) -> None:
    owner_file = partial_root / PARTIAL_OWNER_FILE
    try:
        if not _is_regular_no_follow(owner_file):
            raise MaterializationError(
                f"needs-attention: {kind} staging ownership is ambiguous: {partial_root}"
            )
        owner_payload = owner_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise MaterializationError(
            f"needs-attention: {kind} staging ownership is ambiguous: {partial_root}"
        ) from error
    if owner_payload != _partial_owner_payload(connection, plan_id):
        raise MaterializationError(
            f"needs-attention: {kind} staging ownership is ambiguous: {partial_root}"
        )


def _rebind_staging_identity(
    connection: sqlite3.Connection,
    attempt: sqlite3.Row,
    partial_root: Path,
    observed_root: os.stat_result,
    *,
    kind: str,
) -> None:
    """Persist a fully revalidated same-device identity change."""
    try:
        current_root = partial_root.lstat()
    except OSError as error:
        raise MaterializationError(
            f"needs-attention: {kind} staging changed during identity revalidation: {partial_root}"
        ) from error
    if (
        not stat.S_ISDIR(current_root.st_mode)
        or stat.S_ISLNK(current_root.st_mode)
        or (current_root.st_dev, current_root.st_ino)
        != (observed_root.st_dev, observed_root.st_ino)
    ):
        raise MaterializationError(
            f"needs-attention: {kind} staging changed during identity revalidation: {partial_root}"
        )
    cursor = connection.execute(
        "UPDATE execution_manifests SET staging_root_dev = ?, staging_root_ino = ? "
        "WHERE attempt_id = ? AND staging_root_dev = ? AND staging_root_ino = ?",
        (
            current_root.st_dev,
            current_root.st_ino,
            str(attempt["attempt_id"]),
            int(attempt["staging_root_dev"]),
            int(attempt["staging_root_ino"]),
        ),
    )
    if cursor.rowcount != 1:
        connection.rollback()
        raise MaterializationError(
            f"needs-attention: {kind} staging identity changed concurrently: {partial_root}"
        )
    connection.commit()


def _validate_reusable_durable_partial(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    partial_root: Path,
    entries: list[sqlite3.Row],
    telemetry: MaterializationTelemetry,
    reporter: ProgressReporter,
) -> bool:
    """Prove a complete staged tree is owned and byte-equivalent before reuse."""
    attempt = connection.execute(
        "SELECT attempt.attempt_id, attempt.state, attempt.failure_classification, "
        "manifest.staging_root_dev, manifest.staging_root_ino "
        "FROM materialization_attempts AS attempt "
        "JOIN execution_manifests AS manifest USING (attempt_id) "
        "WHERE attempt.plan_id = ? ORDER BY attempt.created_at DESC, "
        "attempt.attempt_id DESC LIMIT 1",
        (preflight.plan_id,),
    ).fetchone()
    if attempt is None:
        return False
    state = str(attempt["state"])
    if state not in {"STAGING_DURABLE", "FAILED"}:
        return False
    if state == "FAILED" and attempt["failure_classification"] not in {
        "retryable-environment",
        "unsupported-capability",
    }:
        raise MaterializationError(
            f"needs-attention: staged publication cannot be retried automatically: {partial_root}"
        )
    try:
        observed_root = partial_root.lstat()
    except OSError as error:
        raise MaterializationError(
            f"needs-attention: durable staging identity is unavailable: {partial_root}"
        ) from error
    identity_rebind_required = _staging_identity_requires_rebind(
        attempt, observed_root, partial_root, kind="durable"
    )
    owner_file = partial_root / PARTIAL_OWNER_FILE
    if _path_entry_exists(owner_file):
        _validate_staging_owner_payload(
            connection, preflight.plan_id, partial_root, kind="durable"
        )

    expected_files, expected_directories = _expected_staging_paths(entries)
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    for root, directory_names, file_names in os.walk(partial_root, followlinks=False):
        root_path = Path(root)
        for name in directory_names:
            path = root_path / name
            relative = path.relative_to(partial_root).as_posix()
            if path.is_symlink():
                raise MaterializationError(
                    f"needs-attention: durable staging integrity check failed: {path}"
                )
            observed_directories.add(relative)
        for name in file_names:
            path = root_path / name
            relative = path.relative_to(partial_root).as_posix()
            if path.is_symlink() or not _is_regular_no_follow(path):
                raise MaterializationError(
                    f"needs-attention: durable staging integrity check failed: {path}"
                )
            if relative == PARTIAL_OWNER_FILE:
                continue
            observed_files.add(relative)
    if observed_files != expected_files or observed_directories != expected_directories:
        raise MaterializationError(
            f"needs-attention: durable staging structure does not match the finalized Plan: {partial_root}"
        )

    selected_root = Path(preflight.selected_backup_root)
    for row in entries:
        if row["entry_kind"] != "file":
            continue
        source = selected_root / str(row["source_relative_path"])
        staged = partial_root / str(row["output_relative_path"])
        telemetry.report(reporter, "recovery verification")
        source_size, source_digest = _stable_recovery_digest(
            source, telemetry, source=True
        )
        staged_size, staged_digest = _stable_recovery_digest(staged, telemetry)
        if source_size != staged_size or source_digest != staged_digest:
            raise MaterializationError(
                f"needs-attention: durable staging bytes do not match the source: {staged}"
            )
        reporter.advance()
    if identity_rebind_required:
        _rebind_staging_identity(
            connection,
            attempt,
            partial_root,
            observed_root,
            kind="durable",
        )
    return True


def _latest_attempt_staging_row(
    connection: sqlite3.Connection, plan_id: str
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        connection.execute(
            "SELECT attempt.attempt_id, attempt.state, manifest.staging_root_dev, "
            "manifest.staging_root_ino FROM materialization_attempts AS attempt "
            "JOIN execution_manifests AS manifest USING (attempt_id) "
            "WHERE attempt.plan_id = ? ORDER BY attempt.created_at DESC, "
            "attempt.attempt_id DESC LIMIT 1",
            (plan_id,),
        ).fetchone(),
    )


def _staging_file_evidence(
    connection: sqlite3.Connection, attempt_id: str
) -> dict[int, sqlite3.Row]:
    return {
        int(row["entry_index"]): row
        for row in connection.execute(
            "SELECT entry_index, source_relative_path, observed_byte_size, "
            "observed_mtime_ns, transfer_kind, algorithm, algorithm_version, digest "
            "FROM execution_file_evidence WHERE attempt_id = ? ORDER BY entry_index",
            (attempt_id,),
        )
    }


def _stable_recovery_digest(
    path: Path,
    telemetry: MaterializationTelemetry | None = None,
    *,
    source: bool = False,
) -> tuple[int, str]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise MaterializationError(
                f"needs-attention: recovery path is not a regular file: {path}"
            )
        hasher = blake3()

        def update_digest(chunk: bytes) -> None:
            hasher.update(chunk)
            if telemetry is not None:
                if source:
                    telemetry.recovery_source_bytes += len(chunk)
                else:
                    telemetry.recovery_staged_bytes += len(chunk)

        total, stable = stable_read(path, before, on_chunk=update_digest)
        after_path = path.lstat()
    except OSError as error:
        raise MaterializationError(
            f"needs-attention: recovery path cannot be read safely: {path}"
        ) from error
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if not stable or not all(
        getattr(before, field) == getattr(after_path, field) for field in stable_fields
    ):
        raise MaterializationError(
            f"needs-attention: recovery path changed while being verified: {path}"
        )
    return total, hasher.hexdigest()


def _validate_resumable_partial(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    partial_root: Path,
    entries: list[sqlite3.Row],
    telemetry: MaterializationTelemetry,
    reporter: ProgressReporter,
) -> set[int] | None:
    """Prove and reconcile a partially completed source-preserving staging tree."""
    attempt = _latest_attempt_staging_row(connection, preflight.plan_id)
    if attempt is None or attempt["state"] != "STAGING_STARTED":
        return None
    try:
        observed_root = partial_root.lstat()
    except OSError as error:
        raise MaterializationError(
            f"needs-attention: partial staging identity is unavailable: {partial_root}"
        ) from error
    identity_rebind_required = _staging_identity_requires_rebind(
        attempt, observed_root, partial_root, kind="partial"
    )
    _validate_staging_owner_payload(
        connection, preflight.plan_id, partial_root, kind="partial"
    )

    files = [row for row in entries if row["entry_kind"] == "file"]
    expected_files, expected_directories = _expected_staging_paths(entries)
    temporary_by_index = {
        int(row["operation_index"]): f".partial-{int(row['operation_index'])}"
        for row in files
    }
    allowed_files = (
        expected_files | set(temporary_by_index.values()) | {PARTIAL_OWNER_FILE}
    )
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    for root, directory_names, file_names in os.walk(partial_root, followlinks=False):
        root_path = Path(root)
        for name in directory_names:
            path = root_path / name
            relative = path.relative_to(partial_root).as_posix()
            if path.is_symlink():
                raise MaterializationError(
                    f"needs-attention: partial staging structure is ambiguous: {path}"
                )
            observed_directories.add(relative)
        for name in file_names:
            path = root_path / name
            relative = path.relative_to(partial_root).as_posix()
            if path.is_symlink() or not _is_regular_no_follow(path):
                raise MaterializationError(
                    f"needs-attention: partial staging structure is ambiguous: {path}"
                )
            observed_files.add(relative)
    if (
        not observed_files <= allowed_files
        or not observed_directories <= expected_directories
    ):
        raise MaterializationError(
            f"needs-attention: partial staging structure is ambiguous: {partial_root}"
        )

    evidence_by_index = _staging_file_evidence(connection, str(attempt["attempt_id"]))
    indexes = [int(row["operation_index"]) for row in files]
    completed_count = 0
    while (
        completed_count < len(indexes) and indexes[completed_count] in evidence_by_index
    ):
        completed_count += 1
    completed = set(indexes[:completed_count])
    if set(evidence_by_index) != completed:
        raise MaterializationError(
            f"needs-attention: partial staging completion evidence is ambiguous: {partial_root}"
        )

    selected_root = Path(preflight.selected_backup_root)
    for row in files[:completed_count]:
        index = int(row["operation_index"])
        evidence = evidence_by_index[index]
        output = partial_root / str(row["output_relative_path"])
        temporary = partial_root / temporary_by_index[index]
        source = selected_root / str(row["source_relative_path"])
        if (
            str(evidence["source_relative_path"]) != str(row["source_relative_path"])
            or int(evidence["observed_byte_size"]) != int(row["expected_byte_size"])
            or not _is_regular_no_follow(output)
            or _path_entry_exists(temporary)
        ):
            raise MaterializationError(
                f"needs-attention: completed staged file evidence is ambiguous: {output}"
            )
        _validate_source_metadata_observation(
            source,
            int(row["expected_byte_size"]),
            row["expected_modified_ns"],
        )
        telemetry.report(reporter, "recovery verification")
        source_size, source_digest = _stable_recovery_digest(
            source, telemetry, source=True
        )
        staged_size, staged_digest = _stable_recovery_digest(output, telemetry)
        if (
            source_size != int(row["expected_byte_size"])
            or staged_size != source_size
            or staged_digest != source_digest
            or (
                evidence["digest"] is not None
                and str(evidence["digest"]) != source_digest
            )
        ):
            raise MaterializationError(
                f"needs-attention: completed staged file does not match its source: {output}"
            )
        reporter.advance()

    incomplete_artifacts: set[str] = set()
    for row in files[completed_count:]:
        index = int(row["operation_index"])
        artifacts = {
            str(row["output_relative_path"]),
            temporary_by_index[index],
        }
        if artifacts <= observed_files:
            raise MaterializationError(
                f"needs-attention: partial staging structure is ambiguous: {partial_root}"
            )
        incomplete_artifacts.update(artifacts)
    unexplained = observed_files - {
        PARTIAL_OWNER_FILE,
        *(str(row["output_relative_path"]) for row in files[:completed_count]),
        *incomplete_artifacts,
    }
    if unexplained:
        raise MaterializationError(
            f"needs-attention: partial staging structure is ambiguous: {partial_root}"
        )
    if identity_rebind_required:
        _rebind_staging_identity(
            connection,
            attempt,
            partial_root,
            observed_root,
            kind="partial",
        )
    for relative in incomplete_artifacts & observed_files:
        path = partial_root / relative
        path.unlink()
    if incomplete_artifacts & observed_files:
        sync_filesystem(partial_root)
    return completed


def _discard_proven_partial(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    partial_root: Path,
) -> None:
    """Discard only a partial tree bound to this exact durable manifest."""
    if partial_root.is_symlink() or not partial_root.is_dir():
        raise MaterializationError(
            f"needs-attention: partial destination is not a safe directory: {partial_root}"
        )
    owner_file = partial_root / PARTIAL_OWNER_FILE
    try:
        payload = owner_file.read_text(encoding="utf-8")
    except OSError as error:
        raise MaterializationError(
            f"needs-attention: partial destination ownership is ambiguous: {partial_root}"
        ) from error
    if payload != _partial_owner_payload(connection, preflight.plan_id):
        raise MaterializationError(
            f"needs-attention: partial destination ownership is ambiguous: {partial_root}"
        )
    shutil.rmtree(partial_root)
    sync_filesystem(partial_root.parent)


def _validate_manifest_algorithms(entries: list[sqlite3.Row], plan_id: str) -> None:
    if any(
        row["entry_kind"] == "file"
        and (
            (row["algorithm"] is None) != (row["digest"] is None)
            or (
                row["algorithm"] is not None
                and (
                    row["algorithm"] != CONTENT_IDENTITY_ALGORITHM
                    or int(row["algorithm_version"])
                    != CONTENT_IDENTITY_ALGORITHM_VERSION
                )
            )
        )
        for row in entries
    ):
        raise MaterializationError(
            f"Consolidation Plan {plan_id} uses an unsupported content-identity algorithm"
        )


def _validate_legacy_operation_algorithms(
    connection: sqlite3.Connection, plan_id: str
) -> None:
    """Reject a plan whose original operation evidence no longer matches BLAKE3."""
    rows = connection.execute(
        "SELECT algorithm, algorithm_version FROM plan_operations WHERE plan_id = ?",
        (plan_id,),
    ).fetchall()
    if any(
        row["algorithm"] is not None
        and (
            row["algorithm"] != CONTENT_IDENTITY_ALGORITHM
            or int(row["algorithm_version"]) != CONTENT_IDENTITY_ALGORITHM_VERSION
        )
        for row in rows
    ):
        raise MaterializationError(
            f"Consolidation Plan {plan_id} uses an unsupported content-identity algorithm"
        )


def _copy_to_temporary(
    source: Path, temporary: Path, row: sqlite3.Row
) -> tuple[int, str]:
    expected_size = int(row["expected_byte_size"])
    expected_digest = row["digest"]
    try:
        before_path = source.lstat()
    except FileNotFoundError as error:
        raise MaterializationError("source file is missing") from error
    except OSError as error:
        raise MaterializationError("source file is unreadable") from error
    if not stat.S_ISREG(before_path.st_mode) or before_path.st_size != expected_size:
        raise MaterializationError("source file byte size no longer matches the plan")
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        temporary_fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except BaseException:
        os.close(source_fd)
        raise
    try:
        opened = os.fstat(source_fd)
        hasher = blake3()
        total = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            hasher.update(chunk)
            _write_all(temporary_fd, chunk)
            _maybe_crash("copying")
        after_fd = os.fstat(source_fd)
        after_path = source.lstat()
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if total != expected_size or not all(
            getattr(before_path, field)
            == getattr(opened, field)
            == getattr(after_fd, field)
            == getattr(after_path, field)
            for field in stable
        ):
            raise MaterializationError("source file changed while being read")
        observed_digest = hasher.hexdigest()
        if expected_digest is not None and observed_digest != expected_digest:
            raise MaterializationError(
                "source file content no longer matches the plan's content identity"
            )
        try:
            os.fchmod(temporary_fd, stat.S_IMODE(before_path.st_mode))
            os.utime(
                temporary_fd, ns=(before_path.st_atime_ns, before_path.st_mtime_ns)
            )
        except OSError:
            # Mode and mtime are deliberately best effort; copied bytes remain valid.
            pass
    finally:
        os.close(temporary_fd)
        os.close(source_fd)
    return total, observed_digest


def _write_all(descriptor: int, chunk: bytes) -> None:
    """Write one streamed source chunk completely before reading the next one."""
    view = memoryview(chunk)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("temporary file write made no progress")
        view = view[written:]


def _in_place_staging_name(plan_id: str) -> str:
    return f".filesystem-organizer-staging-{plan_id}"


def _validate_source_before_link(
    source: Path,
    row: sqlite3.Row,
    expected_modified_ns: int,
    verified_evidence: dict[tuple[int, int, int, int, int, int], tuple[int, str]]
    | None = None,
    telemetry: MaterializationTelemetry | None = None,
) -> None:
    before = source.lstat()
    if before.st_mtime_ns != expected_modified_ns:
        raise MaterializationError(
            "source file modification time no longer matches the plan"
        )
    _validate_source_before_copy(
        source,
        int(row["expected_byte_size"]),
        row["digest"],
        verified_evidence,
        telemetry,
    )
    after = source.lstat()
    if not stat.S_ISREG(after.st_mode):
        raise MaterializationError("source file is not a regular file")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise MaterializationError("source file changed while being validated")


def _remove_empty_directories(
    selected_root: Path,
    staging_root: Path,
    affected_paths: Iterable[Path],
    protected_directories: set[Path],
) -> None:
    directories: set[Path] = set()
    for path in affected_paths:
        parent = path if path.is_dir() else path.parent
        while parent != selected_root and parent != staging_root:
            if selected_root in parent.parents and parent not in protected_directories:
                directories.add(parent)
            parent = parent.parent
    ordered = sorted(directories, key=lambda path: len(path.parts), reverse=True)
    for directory in ordered:
        try:
            directory.rmdir()
        except OSError:
            pass


def _materialize_in_place(
    preflight: MaterializationPreflight, progress: ProgressReporter
) -> dict[str, object]:
    progress.start("Preparing in-place materialization")
    run_path, database_path = resolve_run_directory(Path(preflight.analysis_run))
    selected_root = Path(preflight.selected_backup_root)
    staging_root = selected_root / _in_place_staging_name(preflight.plan_id)
    protected_root = staging_root / "protected"
    telemetry = MaterializationTelemetry(phase="admission")
    verified_evidence: dict[tuple[int, int, int, int, int, int], tuple[int, str]] = {}
    filesystem_sync = FilesystemSync(selected_root)
    connection = open_read_write(database_path)
    persisted_progress: WorkflowProgress | None = None
    try:
        connection.execute("PRAGMA synchronous = FULL")
        ensure_journal_schema(connection)
        if _has_terminal_failure(connection, preflight.plan_id):
            raise MaterializationError(
                "Consolidation Plan stopped in a needs-attention state after a recorded "
                f"materialization failure: {preflight.plan_id}; completing requires a new Analysis Run and plan"
            )
        entries = _ensure_execution_manifest(
            connection, preflight, _in_place_staging_name(preflight.plan_id)
        )
        attempt_id = str(
            connection.execute(
                "SELECT attempt_id FROM execution_manifests WHERE plan_id=? "
                "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
                (preflight.plan_id,),
            ).fetchone()[0]
        )
        persisted_progress = WorkflowProgress(
            connection,
            "materialization",
            preflight.run_id,
            plan_id=preflight.plan_id,
            attempt_id=attempt_id,
            phase="admission",
            total_count=preflight.operation_count,
            unit="files",
        )
        _validate_manifest_algorithms(entries, preflight.plan_id)
        _validate_legacy_operation_algorithms(connection, preflight.plan_id)
        files = [row for row in entries if row["entry_kind"] == "file"]
        directories = [row for row in entries if row["entry_kind"] == "directory"]
        excluded_files = _excluded_file_entries(connection, preflight.plan_id)
        skipped_exclusions = _skipped_exclusions(connection, preflight.plan_id)
        snapshot_files = {
            str(row["relative_path"]): (
                int(row["observed_byte_size"]),
                int(row["modified_ns"]),
                str(row["digest"]),
            )
            for row in connection.execute(
                "SELECT inventory.relative_path, inventory.observed_byte_size, "
                "inventory.modified_ns, identity.digest FROM inventory_entries AS inventory "
                "LEFT JOIN content_identities AS identity ON identity.run_id = inventory.run_id "
                "AND identity.relative_path = inventory.relative_path "
                "WHERE inventory.run_id = ? AND inventory.entry_kind = 'regular-file' "
                "AND inventory.read_outcome = 'successful' "
                "AND (identity.read_outcome IS NULL OR identity.read_outcome = 'successful')",
                (preflight.run_id,),
            )
        }
        duplicate_rows = connection.execute(
            "SELECT identity.relative_path, identity.byte_size, identity.digest "
            "FROM content_identities AS identity JOIN inventory_entries AS inventory "
            "ON inventory.run_id = identity.run_id AND inventory.relative_path = identity.relative_path "
            "WHERE identity.run_id = ? AND inventory.entry_kind = 'regular-file' "
            "AND identity.read_outcome = 'successful'",
            (preflight.run_id,),
        ).fetchall()
        if _has_plan_event(
            connection, preflight.plan_id, EVENT_IN_PLACE_STAGING_REMOVED
        ) and not _has_plan_event(
            connection, preflight.plan_id, EVENT_MATERIALIZATION_COMPLETE
        ):
            for row in files:
                final = selected_root / str(row["output_relative_path"])
                if not _is_regular_no_follow(final) or final.stat().st_size != int(
                    row["expected_byte_size"]
                ):
                    raise MaterializationError(
                        "needs-attention: durable output is incomplete after staging removal"
                    )
                if row["digest"] is not None:
                    _validate_source_before_copy(
                        final,
                        int(row["expected_byte_size"]),
                        str(row["digest"]),
                        verified_evidence,
                        telemetry,
                    )
                else:
                    _validate_source_metadata_observation(
                        final,
                        int(row["expected_byte_size"]),
                        row["expected_modified_ns"],
                    )
            for row in directories:
                final = selected_root / str(row["output_relative_path"])
                if not final.is_dir() or final.is_symlink():
                    raise MaterializationError(
                        "needs-attention: durable output directory is incomplete "
                        "after staging removal"
                    )
            journal_append(
                connection,
                preflight.plan_id,
                EVENT_MATERIALIZATION_COMPLETE,
                detail="recovered completion after durable staging removal",
            )
            _complete_materialization_attempt(
                connection,
                preflight.plan_id,
                0,
                0,
                len(files),
                preflight.total_bytes,
            )
            telemetry.phase = "complete"
            persisted_progress.complete(metrics=telemetry.snapshot())
            return {
                "analysis_run": str(run_path),
                "destination": str(selected_root),
                "operation_count": len(files),
                "explicit_directory_count": len(directories),
                "plan_id": preflight.plan_id,
                "run_id": preflight.run_id,
                "status": "materialized-in-place",
                "telemetry": telemetry.snapshot(),
                "total_bytes": preflight.total_bytes,
            }
        if _has_plan_event(
            connection, preflight.plan_id, EVENT_MATERIALIZATION_COMPLETE
        ):
            for row in files:
                final = selected_root / str(row["output_relative_path"])
                if not _is_regular_no_follow(final) or final.stat().st_size != int(
                    row["expected_byte_size"]
                ):
                    _raise_operation_failure(
                        connection,
                        preflight.plan_id,
                        row,
                        "final manifest check: planned output missing or size mismatch",
                        conflict=True,
                    )
                if staging_root.exists():
                    _validate_source_before_copy(
                        final,
                        int(row["expected_byte_size"]),
                        row["digest"],
                        verified_evidence,
                        telemetry,
                    )
            for row in directories:
                final = selected_root / str(row["output_relative_path"])
                if not final.is_dir() or final.is_symlink():
                    _raise_operation_failure(
                        connection,
                        preflight.plan_id,
                        row,
                        "final manifest check: planned directory is missing",
                        conflict=True,
                    )
            if staging_root.exists():
                filesystem_sync.sync(selected_root)
                if not _has_plan_event(
                    connection, preflight.plan_id, EVENT_IN_PLACE_OUTPUT_DURABLE
                ):
                    journal_append(
                        connection,
                        preflight.plan_id,
                        EVENT_IN_PLACE_OUTPUT_DURABLE,
                        detail="legacy output reverified and made durable",
                    )
                shutil.rmtree(staging_root)
                filesystem_sync.sync(selected_root)
                if not _has_plan_event(
                    connection, preflight.plan_id, EVENT_IN_PLACE_STAGING_REMOVED
                ):
                    journal_append(
                        connection,
                        preflight.plan_id,
                        EVENT_IN_PLACE_STAGING_REMOVED,
                        detail="legacy in-place staging absence/removal confirmed durable",
                    )
            _complete_materialization_attempt(
                connection, preflight.plan_id, 0, 0, len(files), preflight.total_bytes
            )
            telemetry.phase = "complete"
            persisted_progress.complete(metrics=telemetry.snapshot())
            return {
                "analysis_run": str(run_path),
                "destination": str(selected_root),
                "operation_count": len(files),
                "explicit_directory_count": len(directories),
                "plan_id": preflight.plan_id,
                "run_id": preflight.run_id,
                "status": "materialized-in-place",
                "telemetry": telemetry.snapshot(),
                "total_bytes": preflight.total_bytes,
            }
        sources = [str(row["source_relative_path"]) for row in files]
        if len(sources) != len(set(sources)):
            raise MaterializationError(
                "In-place execution requires one unique retained source for every planned output"
            )
        if not _has_plan_event(connection, preflight.plan_id, EVENT_PLAN_ADMITTED):
            if staging_root.exists():
                raise MaterializationError(
                    f"In-place staging directory already exists: {staging_root}"
                )
            journal_append(
                connection,
                preflight.plan_id,
                EVENT_PLAN_ADMITTED,
                detail="in-place execution manifest admitted",
            )
            _maybe_crash("admission")
        protection_complete = _has_plan_event(
            connection, preflight.plan_id, EVENT_IN_PLACE_PROTECTION_COMPLETE
        )
        if not protection_complete and not _has_plan_event(
            connection, preflight.plan_id, EVENT_MATERIALIZATION_COMPLETE
        ):
            _revalidate_structural_snapshot(
                connection, preflight.run_id, selected_root, preflight.plan_id
            )
        progress.complete()
        progress.start("Protecting originals", len(files), unit="files")
        telemetry.report(progress, "protection")
        persisted_progress.phase("protection", total_count=len(files), unit="files")
        if not protection_complete:
            staging_root.mkdir(mode=0o700, exist_ok=True)
            protected_root.mkdir(mode=0o700, exist_ok=True)
            dirty_directories: set[Path] = {selected_root, staging_root, protected_root}
            for row in files:
                protected = protected_root / str(int(row["operation_index"]))
                expected_modified_ns = snapshot_files[str(row["source_relative_path"])][
                    1
                ]
                if _path_entry_exists(protected):
                    _validate_source_before_link(
                        protected,
                        row,
                        expected_modified_ns,
                        verified_evidence,
                        telemetry,
                    )
                    source = selected_root / str(row["source_relative_path"])
                    if not _path_entry_exists(source):
                        raise MaterializationError(
                            f"needs-attention: retained source is missing before protection completes: {source}"
                        )
                    source_stat = source.lstat()
                    protected_stat = protected.lstat()
                    if (source_stat.st_dev, source_stat.st_ino) != (
                        protected_stat.st_dev,
                        protected_stat.st_ino,
                    ):
                        raise MaterializationError(
                            f"needs-attention: staging entry is not the retained source hard link: {protected}"
                        )
                    progress.advance()
                    continue
                source = selected_root / str(row["source_relative_path"])
                _validate_source_before_link(
                    source,
                    row,
                    expected_modified_ns,
                    verified_evidence,
                    telemetry,
                )
                os.link(source, protected)
                _remember_link_updated_evidence(
                    source,
                    int(row["expected_byte_size"]),
                    row["digest"],
                    verified_evidence,
                )
                dirty_directories.add(protected.parent)
                telemetry.protected_files += 1
                telemetry.protected_bytes += int(row["expected_byte_size"])
                progress.advance()
                persisted_progress.update(telemetry.protected_files)
            # Excluded bytes are protected before any destructive publication.
            # They deliberately are not execution-manifest placements.
            for row in excluded_files:
                source = selected_root / str(row["source_relative_path"])
                protected = (
                    protected_root / "excluded" / str(row["source_relative_path"])
                )
                if _path_entry_exists(protected):
                    continue
                protected.parent.mkdir(parents=True, exist_ok=True)
                _validate_source_before_copy(
                    source,
                    int(row["expected_byte_size"]),
                    row["digest"],
                    verified_evidence,
                    telemetry,
                )
                os.link(source, protected)
                _remember_link_updated_evidence(
                    source,
                    int(row["expected_byte_size"]),
                    row["digest"],
                    verified_evidence,
                )
                dirty_directories.add(protected.parent)
                telemetry.protected_files += 1
                telemetry.protected_bytes += int(row["expected_byte_size"])
                persisted_progress.update(telemetry.protected_files)
            for relative_path in skipped_exclusions:
                source = selected_root / relative_path
                protected = protected_root / "unverified" / relative_path
                if _path_entry_exists(protected):
                    continue
                protected.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, protected, follow_symlinks=False)
                except OSError as error:
                    raise MaterializationError(
                        f"Unverified Exclusion cannot stage safely: {source}"
                    ) from error
                dirty_directories.add(protected.parent)
                telemetry.protected_files += 1
                persisted_progress.update(telemetry.protected_files)
            sync_started = time.monotonic()
            filesystem_sync.sync(selected_root)
            telemetry.checkpoint_sync_seconds += time.monotonic() - sync_started
            telemetry.checkpoints += 1
            journal_append(
                connection,
                preflight.plan_id,
                EVENT_IN_PLACE_PROTECTION_COMPLETE,
                detail="every retained planned file has a durable hard-link protection",
            )
            _set_attempt_state(connection, preflight.plan_id, "STAGING_DURABLE")
            _maybe_crash("postprotect")
        else:
            for row in files:
                protected = protected_root / str(int(row["operation_index"]))
                if not _path_entry_exists(protected):
                    raise MaterializationError(
                        f"needs-attention: protected staging link is missing: {protected}"
                    )
                _validate_source_before_link(
                    protected,
                    row,
                    snapshot_files[str(row["source_relative_path"])][1],
                    verified_evidence,
                    telemetry,
                )
                progress.advance()
        progress.complete()
        retained_sources = set(sources)
        retained_identities = {
            (int(row["expected_byte_size"]), str(row["digest"]))
            for row in files
            if row["digest"] is not None
        }
        excluded_identities = {
            (int(row["expected_byte_size"]), str(row["digest"]))
            for row in excluded_files
            if row["digest"] is not None
        }
        planned_outputs = {str(row["output_relative_path"]) for row in files}
        telemetry.report(progress, "payload-proof")
        persisted_progress.phase("payload-proof", total_count=len(files), unit="files")
        for duplicate in duplicate_rows:
            relative_path = str(duplicate["relative_path"])
            identity = (int(duplicate["byte_size"]), str(duplicate["digest"]))
            if (
                relative_path not in retained_sources
                and relative_path not in planned_outputs
                and identity in retained_identities | excluded_identities
            ):
                candidate = selected_root / relative_path
                if _path_entry_exists(candidate):
                    _validate_source_before_copy(
                        candidate,
                        identity[0],
                        identity[1],
                        verified_evidence,
                        telemetry,
                    )
                elif not protection_complete:
                    raise MaterializationError(
                        f"needs-attention: proof candidate is missing: {candidate}"
                    )
        for row in excluded_files:
            candidate = selected_root / str(row["source_relative_path"])
            if _path_entry_exists(candidate):
                _validate_source_before_copy(
                    candidate,
                    int(row["expected_byte_size"]),
                    row["digest"],
                    verified_evidence,
                    telemetry,
                )
            elif not protection_complete:
                raise MaterializationError(
                    f"needs-attention: excluded candidate is missing: {candidate}"
                )
        for row in directories:
            directory = selected_root / str(row["output_relative_path"])
            if _path_entry_exists(directory) and not directory.is_dir():
                _raise_operation_failure(
                    connection,
                    preflight.plan_id,
                    row,
                    "final path is not a directory",
                    conflict=True,
                )
            directory.mkdir(parents=True, exist_ok=True)
        outcomes: list[tuple[str, int | None, str | None]] = []
        completed = _completed_indexes(connection, preflight.plan_id)
        progress.start("Publishing files", len(files), unit="files")
        telemetry.report(progress, "publication")
        persisted_progress.phase("publication", total_count=len(files), unit="files")
        checkpoint_policy = _CheckpointPolicy(time.monotonic())
        checkpoint_files = 0
        checkpoint_bytes = 0

        def checkpoint_publication() -> None:
            nonlocal checkpoint_files, checkpoint_bytes, outcomes
            if checkpoint_files == 0 and not outcomes:
                return
            _maybe_crash("in-place-publication-before-sync")
            sync_started = time.monotonic()
            filesystem_sync.sync(selected_root)
            sync_seconds = time.monotonic() - sync_started
            telemetry.checkpoint_sync_seconds += sync_seconds
            telemetry.checkpoints += 1
            _maybe_crash("in-place-publication-after-sync")
            database_started = time.monotonic()
            journal_append_many(
                connection,
                preflight.plan_id,
                [
                    *outcomes,
                    (
                        EVENT_IN_PLACE_PUBLICATION_CHECKPOINT,
                        None,
                        f"{checkpoint_files} files and {checkpoint_bytes} bytes durable",
                    ),
                ],
            )
            telemetry.checkpoint_database_seconds += time.monotonic() - database_started
            checkpoint_policy.confirmed(sync_seconds)
            outcomes = []
            checkpoint_files = 0
            checkpoint_bytes = 0
            _maybe_crash("in-place-publication-after-commit")

        for row in files:
            index = int(row["operation_index"])
            final = selected_root / str(row["output_relative_path"])
            protected = protected_root / str(index)
            if _path_entry_exists(final):
                if final == selected_root / str(row["source_relative_path"]):
                    _validate_source_before_link(
                        final,
                        row,
                        snapshot_files[str(row["source_relative_path"])][1],
                        verified_evidence,
                        telemetry,
                    )
                    if index not in completed:
                        outcomes.append(
                            (
                                EVENT_FILE_COMPLETED,
                                index,
                                "retained planned source already occupies final path",
                            )
                        )
                        completed.add(index)
                    progress.advance()
                    checkpoint_files += 1
                    checkpoint_bytes += int(row["expected_byte_size"])
                    telemetry.published_files += 1
                    telemetry.published_bytes += int(row["expected_byte_size"])
                    persisted_progress.update(telemetry.published_files)
                    if checkpoint_policy.due(checkpoint_files, checkpoint_bytes):
                        checkpoint_publication()
                    continue
                output_relative_path = str(row["output_relative_path"])
                snapshot = snapshot_files.get(output_relative_path)
                expected_identity = (
                    int(row["expected_byte_size"]),
                    str(row["digest"]),
                )
                if (
                    _is_regular_no_follow(final)
                    and digest_file(final) == expected_identity
                ):
                    source = selected_root / str(row["source_relative_path"])
                    if source != final and _path_entry_exists(source):
                        _validate_source_before_link(
                            source,
                            row,
                            snapshot_files[str(row["source_relative_path"])][1],
                            verified_evidence,
                            telemetry,
                        )
                        source.unlink()
                    if index not in completed:
                        outcomes.append(
                            (
                                EVENT_FILE_COMPLETED,
                                index,
                                "published final reconciled from durable in-place protection",
                            )
                        )
                        completed.add(index)
                    progress.advance()
                    checkpoint_files += 1
                    checkpoint_bytes += int(row["expected_byte_size"])
                    telemetry.published_files += 1
                    telemetry.published_bytes += int(row["expected_byte_size"])
                    persisted_progress.update(telemetry.published_files)
                    if checkpoint_policy.due(checkpoint_files, checkpoint_bytes):
                        checkpoint_publication()
                    continue
                if (
                    snapshot is not None
                    and _is_regular_no_follow(final)
                    and final.stat().st_size == snapshot[0]
                    and final.stat().st_mtime_ns == snapshot[1]
                    and digest_file(final) == (snapshot[0], snapshot[2])
                    and output_relative_path in retained_sources
                ):
                    final.unlink()
                else:
                    _raise_operation_failure(
                        connection,
                        preflight.plan_id,
                        row,
                        "output path already exists",
                        conflict=True,
                    )
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(protected, final)
            except FileExistsError as error:
                raise _DestinationConflict("output path already exists") from error
            _remember_link_updated_evidence(
                protected,
                int(row["expected_byte_size"]),
                row["digest"],
                verified_evidence,
            )
            source = selected_root / str(row["source_relative_path"])
            if source != final and _path_entry_exists(source):
                _validate_source_before_link(
                    source,
                    row,
                    snapshot_files[str(row["source_relative_path"])][1],
                    verified_evidence,
                    telemetry,
                )
                source.unlink()
            if index not in completed:
                outcomes.append(
                    (
                        EVENT_FILE_COMPLETED,
                        index,
                        "published from durable in-place protection",
                    )
                )
                completed.add(index)
            _maybe_crash("postpublish")
            progress.advance()
            checkpoint_files += 1
            checkpoint_bytes += int(row["expected_byte_size"])
            telemetry.published_files += 1
            telemetry.published_bytes += int(row["expected_byte_size"])
            persisted_progress.update(telemetry.published_files)
            if checkpoint_policy.due(checkpoint_files, checkpoint_bytes):
                checkpoint_publication()
        checkpoint_publication()
        progress.complete()

        def checkpoint_cleanup(
            event: str,
            files_in_batch: int,
            bytes_in_batch: int,
            policy: _CheckpointPolicy,
            crash_prefix: str,
        ) -> None:
            if files_in_batch == 0:
                return
            _maybe_crash(f"{crash_prefix}-before-sync")
            sync_started = time.monotonic()
            filesystem_sync.sync(selected_root)
            sync_seconds = time.monotonic() - sync_started
            telemetry.checkpoint_sync_seconds += sync_seconds
            telemetry.checkpoints += 1
            _maybe_crash(f"{crash_prefix}-after-sync")
            database_started = time.monotonic()
            journal_append(
                connection,
                preflight.plan_id,
                event,
                detail=f"{files_in_batch} files and {bytes_in_batch} bytes durable",
            )
            telemetry.checkpoint_database_seconds += time.monotonic() - database_started
            policy.confirmed(sync_seconds)
            _maybe_crash(f"{crash_prefix}-after-commit")

        # Only exact, successfully verified noncanonical members of retained content
        # are eligible for permanent removal.
        telemetry.report(progress, "duplicate-cleanup")
        duplicate_candidates = len(duplicate_rows)
        persisted_progress.phase(
            "duplicate-cleanup",
            total_count=duplicate_candidates,
            unit="files",
        )
        cleanup_policy = _CheckpointPolicy(time.monotonic())
        cleanup_files = 0
        cleanup_bytes = 0
        duplicate_processed = 0
        for duplicate in duplicate_rows:
            duplicate_processed += 1
            relative_path = str(duplicate["relative_path"])
            if (
                relative_path in retained_sources
                or relative_path in planned_outputs
                or (int(duplicate["byte_size"]), str(duplicate["digest"]))
                not in retained_identities
            ):
                continue
            candidate = selected_root / relative_path
            if _path_entry_exists(candidate):
                snapshot = snapshot_files[relative_path]
                observed = candidate.lstat()
                if (
                    not stat.S_ISREG(observed.st_mode)
                    or observed.st_size != snapshot[0]
                    or observed.st_mtime_ns != snapshot[1]
                ):
                    raise MaterializationError(
                        f"needs-attention: duplicate candidate drifted: {candidate}"
                    )
                _validate_source_before_copy(
                    candidate,
                    int(duplicate["byte_size"]),
                    str(duplicate["digest"]),
                    verified_evidence,
                    telemetry,
                )
                candidate.unlink()
                cleanup_files += 1
                cleanup_bytes += int(duplicate["byte_size"])
                telemetry.duplicate_cleaned_files += 1
                telemetry.duplicate_cleaned_bytes += int(duplicate["byte_size"])
                if cleanup_policy.due(cleanup_files, cleanup_bytes):
                    checkpoint_cleanup(
                        EVENT_IN_PLACE_DUPLICATE_CLEANUP_CHECKPOINT,
                        cleanup_files,
                        cleanup_bytes,
                        cleanup_policy,
                        "in-place-duplicate-cleanup",
                    )
                    cleanup_files = 0
                    cleanup_bytes = 0
            persisted_progress.update(duplicate_processed)
        checkpoint_cleanup(
            EVENT_IN_PLACE_DUPLICATE_CLEANUP_CHECKPOINT,
            cleanup_files,
            cleanup_bytes,
            cleanup_policy,
            "in-place-duplicate-cleanup",
        )
        journal_append(
            connection,
            preflight.plan_id,
            EVENT_IN_PLACE_DUPLICATE_CLEANUP_COMPLETE,
            detail=(
                f"{telemetry.duplicate_cleaned_files} exact duplicate files durable"
            ),
        )
        telemetry.report(progress, "exclusion-cleanup")
        persisted_progress.phase(
            "exclusion-cleanup",
            total_count=(
                len(skipped_exclusions) + len(excluded_files) + len(duplicate_rows)
            ),
            unit="files",
        )
        cleanup_policy = _CheckpointPolicy(time.monotonic())
        cleanup_files = 0
        cleanup_bytes = 0
        exclusion_processed = 0
        for relative_path in skipped_exclusions:
            candidate = selected_root / relative_path
            if _path_entry_exists(candidate):
                candidate.unlink()
                cleanup_files += 1
                telemetry.exclusion_cleaned_files += 1
                if cleanup_policy.due(cleanup_files, cleanup_bytes):
                    checkpoint_cleanup(
                        EVENT_IN_PLACE_EXCLUSION_CLEANUP_CHECKPOINT,
                        cleanup_files,
                        cleanup_bytes,
                        cleanup_policy,
                        "in-place-exclusion-cleanup",
                    )
                    cleanup_files = 0
                    cleanup_bytes = 0
            exclusion_processed += 1
            persisted_progress.update(exclusion_processed)
        # An explicit exclusion is a reviewed instruction for this observed
        # source path even when it was not a content-identity candidate.
        for row in excluded_files:
            candidate = selected_root / str(row["source_relative_path"])
            if _path_entry_exists(candidate):
                _validate_source_before_copy(
                    candidate,
                    int(row["expected_byte_size"]),
                    row["digest"],
                    verified_evidence,
                    telemetry,
                )
                candidate.unlink()
                cleanup_files += 1
                cleanup_bytes += int(row["expected_byte_size"])
                telemetry.exclusion_cleaned_files += 1
                telemetry.exclusion_cleaned_bytes += int(row["expected_byte_size"])
                if cleanup_policy.due(cleanup_files, cleanup_bytes):
                    checkpoint_cleanup(
                        EVENT_IN_PLACE_EXCLUSION_CLEANUP_CHECKPOINT,
                        cleanup_files,
                        cleanup_bytes,
                        cleanup_policy,
                        "in-place-exclusion-cleanup",
                    )
                    cleanup_files = 0
                    cleanup_bytes = 0
            exclusion_processed += 1
            persisted_progress.update(exclusion_processed)
        # An explicit User Exclusion owns the whole proven identity, not merely
        # the selected canonical occurrence.  The hard links above remain until
        # the completion event and staging cleanup, preserving forward recovery.
        for duplicate in duplicate_rows:
            exclusion_processed += 1
            identity = (int(duplicate["byte_size"]), str(duplicate["digest"]))
            if identity not in excluded_identities:
                persisted_progress.update(exclusion_processed)
                continue
            candidate = selected_root / str(duplicate["relative_path"])
            if _path_entry_exists(candidate):
                snapshot = snapshot_files[str(duplicate["relative_path"])]
                if (
                    not _is_regular_no_follow(candidate)
                    or candidate.stat().st_mtime_ns != snapshot[1]
                ):
                    raise MaterializationError(
                        f"needs-attention: excluded candidate drifted: {candidate}"
                    )
                _validate_source_before_copy(
                    candidate,
                    identity[0],
                    identity[1],
                    verified_evidence,
                    telemetry,
                )
                candidate.unlink()
                cleanup_files += 1
                cleanup_bytes += identity[0]
                telemetry.exclusion_cleaned_files += 1
                telemetry.exclusion_cleaned_bytes += identity[0]
                if cleanup_policy.due(cleanup_files, cleanup_bytes):
                    checkpoint_cleanup(
                        EVENT_IN_PLACE_EXCLUSION_CLEANUP_CHECKPOINT,
                        cleanup_files,
                        cleanup_bytes,
                        cleanup_policy,
                        "in-place-exclusion-cleanup",
                    )
                    cleanup_files = 0
                    cleanup_bytes = 0
            persisted_progress.update(exclusion_processed)
        checkpoint_cleanup(
            EVENT_IN_PLACE_EXCLUSION_CLEANUP_CHECKPOINT,
            cleanup_files,
            cleanup_bytes,
            cleanup_policy,
            "in-place-exclusion-cleanup",
        )
        affected_paths = [
            selected_root / str(row["source_relative_path"])
            for row in files + excluded_files
        ] + [selected_root / path for path in skipped_exclusions]
        _remove_empty_directories(
            selected_root,
            staging_root,
            affected_paths,
            {selected_root / str(row["output_relative_path"]) for row in directories},
        )
        sync_started = time.monotonic()
        filesystem_sync.sync(selected_root)
        telemetry.checkpoint_sync_seconds += time.monotonic() - sync_started
        telemetry.checkpoints += 1
        journal_append(
            connection,
            preflight.plan_id,
            EVENT_IN_PLACE_EXCLUSION_CLEANUP_COMPLETE,
            detail=(f"{telemetry.exclusion_cleaned_files} excluded files durable"),
        )
        telemetry.report(progress, "final-verification")
        persisted_progress.phase(
            "final-verification", total_count=len(files), unit="files"
        )
        for verified_count, row in enumerate(files, start=1):
            final = selected_root / str(row["output_relative_path"])
            if not _is_regular_no_follow(final) or final.stat().st_size != int(
                row["expected_byte_size"]
            ):
                _raise_operation_failure(
                    connection,
                    preflight.plan_id,
                    row,
                    "final manifest check: planned output missing or size mismatch",
                    conflict=True,
                )
            persisted_progress.update(verified_count)
        sync_started = time.monotonic()
        filesystem_sync.sync(selected_root)
        telemetry.checkpoint_sync_seconds += time.monotonic() - sync_started
        telemetry.checkpoints += 1
        journal_append(
            connection,
            preflight.plan_id,
            EVENT_IN_PLACE_OUTPUT_DURABLE,
            detail="final namespace, cleanup, and manifest verification are durable",
        )
        _set_attempt_state(connection, preflight.plan_id, "PUBLISHED")
        telemetry.published()
        _maybe_crash("in-place-output-durable")
        shutil.rmtree(staging_root)
        filesystem_sync.sync(selected_root)
        journal_append(
            connection,
            preflight.plan_id,
            EVENT_IN_PLACE_STAGING_REMOVED,
            detail="protection staging removal is durable",
        )
        _maybe_crash("in-place-staging-removed")
        journal_append(
            connection,
            preflight.plan_id,
            EVENT_MATERIALIZATION_COMPLETE,
            detail="in-place execution completed after durable staging removal",
        )
        _maybe_crash("postcomplete")
        _complete_materialization_attempt(
            connection, preflight.plan_id, 0, 0, len(files), preflight.total_bytes
        )
        telemetry.phase = "complete"
        persisted_progress.complete(metrics=telemetry.snapshot())
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if persisted_progress is not None:
            try:
                persisted_progress.fail(type(error).__name__)
            except sqlite3.Error:
                pass
        raise
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        filesystem_sync.close()
    return {
        "analysis_run": str(run_path),
        "destination": str(selected_root),
        "operation_count": len(files),
        "explicit_directory_count": len(directories),
        "plan_id": preflight.plan_id,
        "run_id": preflight.run_id,
        "status": "materialized-in-place",
        "telemetry": telemetry.snapshot(),
        "total_bytes": preflight.total_bytes,
    }


def _supersede_safe_rename_v1_attempt(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    selected_root: Path,
) -> bool:
    """Close a proof-only v1 attempt after proving owned staging is payload-empty."""
    if not table_exists(connection, "rename_protection_manifests"):
        return False
    latest = connection.execute(
        "SELECT attempts.attempt_id, attempts.state, manifests.staging_relative_path, "
        "rename.operation_set_digest, rename.owner_secret, rename.executor_version "
        "FROM materialization_attempts AS attempts "
        "JOIN execution_manifests AS manifests USING(attempt_id) "
        "JOIN rename_protection_manifests AS rename USING(attempt_id) "
        "WHERE attempts.plan_id=? ORDER BY attempts.created_at DESC, "
        "attempts.attempt_id DESC LIMIT 1",
        (preflight.plan_id,),
    ).fetchone()
    if latest is None or int(latest["executor_version"]) != 1:
        return False
    attempt_id = str(latest["attempt_id"])
    ensure_rename_v2_schema(connection)
    superseded = connection.execute(
        "SELECT state FROM rename_protection_v2_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    staging = selected_root / str(latest["staging_relative_path"])
    already_superseded = superseded is not None and superseded[0] == "SUPERSEDED"
    if not already_superseded:
        non_pending = connection.execute(
            "SELECT COUNT(*) FROM rename_protection_operations "
            "WHERE attempt_id=? AND state!='PENDING'",
            (attempt_id,),
        ).fetchone()[0]
        if non_pending:
            raise MaterializationError(
                "RenameProtection v1 supersession refuses an attempt with namespace work"
            )
        staging = selected_root / str(latest["staging_relative_path"])
        if staging.is_symlink() or not staging.is_dir():
            raise MaterializationError(
                "RenameProtection v1 supersession requires ownership-valid empty staging"
            )
        try:
            owner = json.loads(
                (staging / RENAME_V1_OWNER_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise MaterializationError(
                "RenameProtection v1 supersession cannot prove staging ownership"
            ) from error
        if not (
            owner.get("attempt_id") == attempt_id
            and owner.get("plan_id") == preflight.plan_id
            and owner.get("secret") == latest["owner_secret"]
            and owner.get("operation_set_digest") == latest["operation_set_digest"]
            and owner.get("executor_version") == 1
        ):
            raise MaterializationError(
                "RenameProtection v1 supersession refuses foreign staging ownership"
            )
        for root, directory_names, file_names in os.walk(staging, followlinks=False):
            root_path = Path(root)
            for directory_name in directory_names:
                if (root_path / directory_name).is_symlink():
                    raise MaterializationError(
                        "RenameProtection v1 supersession refuses unexpected staged payload"
                    )
            relative_files = {
                (root_path / name).relative_to(staging).as_posix()
                for name in file_names
            }
            if relative_files - {RENAME_V1_OWNER_FILE}:
                raise MaterializationError(
                    "RenameProtection v1 supersession refuses unexpected staged payload"
                )
        connection.execute("BEGIN IMMEDIATE")
        try:
            if latest["state"] != "FAILED":
                connection.execute(
                    "UPDATE materialization_attempts SET state='FAILED', "
                    "updated_at=datetime('now'), completed_at=datetime('now'), "
                    "failure_classification='internal-failure', "
                    "failure_detail='safely superseded by RenameProtection v2' "
                    "WHERE attempt_id=?",
                    (attempt_id,),
                )
            connection.execute(
                "INSERT OR REPLACE INTO rename_protection_v2_attempts "
                "(attempt_id, plan_id, policy_version, state, created_at, updated_at, "
                "completed_at, metrics_json) VALUES "
                "(?,?,2,'SUPERSEDED',datetime('now'),datetime('now'),datetime('now'),'{}')",
                (attempt_id, preflight.plan_id),
            )
            if table_exists(connection, "workflow_progress"):
                connection.execute(
                    "UPDATE workflow_progress SET state='failed', phase='superseded', "
                    "updated_at=datetime('now') WHERE attempt_id=? AND state='active'",
                    (attempt_id,),
                )
        except BaseException:
            connection.rollback()
            raise
        connection.commit()
    _maybe_crash("rename-protection-v1-supersession-intent")
    # The durable supersession record authorizes finishing this already-proven
    # empty staging removal, including after its owner marker was removed.
    if _path_entry_exists(staging):
        if staging.is_symlink() or not staging.is_dir():
            raise MaterializationError(
                "RenameProtection v1 supersession refuses foreign staging"
            )
        # A crash may have removed the owner marker already. Never recursively
        # delete newly encountered payloads when completing supersession.
        for directory, directory_names, file_names in os.walk(
            staging, topdown=False, followlinks=False
        ):
            path = Path(directory)
            if any((path / name).is_symlink() for name in directory_names):
                raise MaterializationError(
                    "RenameProtection v1 supersession refuses unexpected staged payload"
                )
            for name in file_names:
                if path != staging or name != RENAME_V1_OWNER_FILE:
                    raise MaterializationError(
                        "RenameProtection v1 supersession refuses unexpected staged payload"
                    )
                owner_path = path / name
                try:
                    owner = json.loads(owner_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as error:
                    raise MaterializationError(
                        "RenameProtection v1 supersession cannot prove staging ownership"
                    ) from error
                if (
                    owner_path.is_symlink()
                    or owner.get("secret") != latest["owner_secret"]
                    or owner.get("attempt_id") != attempt_id
                ):
                    raise MaterializationError(
                        "RenameProtection v1 supersession refuses foreign staging"
                    )
                owner_path.unlink()
                _maybe_crash("rename-protection-v1-supersession-owner-removed")
            path.rmdir()
        synchronizer = FilesystemSync(selected_root)
        try:
            synchronizer.sync(selected_root)
        finally:
            synchronizer.close()
    _maybe_crash("rename-protection-v1-supersession-removed")
    successor = connection.execute(
        "SELECT 1 FROM execution_manifests AS manifests "
        "LEFT JOIN rename_protection_manifests AS legacy USING(attempt_id) "
        "WHERE manifests.plan_id=? AND legacy.attempt_id IS NULL LIMIT 1",
        (preflight.plan_id,),
    ).fetchone()
    return successor is None


def _materialize_rename_protection_v2(
    preflight: MaterializationPreflight, progress: ProgressReporter
) -> dict[str, object]:
    """Execute a persisted metadata-first RenameProtection v2 attempt."""
    run_path, database_path = resolve_run_directory(Path(preflight.analysis_run))
    selected_root = Path(preflight.selected_backup_root)
    telemetry = MaterializationTelemetry(
        version=5,
        phase="admission",
        executor_kind=EXECUTOR_RENAME_PROTECTION,
        executor_version=RENAME_PROTECTION_V2_VERSION,
    )
    connection = open_read_write(database_path)
    persisted_progress: WorkflowProgress | None = None
    try:
        connection.execute("PRAGMA synchronous = FULL")
        ensure_journal_schema(connection)
        ensure_rename_v2_schema(connection)
        superseded_v1 = _supersede_safe_rename_v1_attempt(
            connection, preflight, selected_root
        )
        placeholder = f".filesystem-organizer-staging-v2-{preflight.plan_id}-pending"
        _ensure_execution_manifest(
            connection,
            preflight,
            placeholder,
            force_new_attempt=superseded_v1,
        )
        attempt = connection.execute(
            "SELECT attempts.attempt_id, attempts.state, projection.revision, "
            "projection.fingerprint FROM materialization_attempts AS attempts "
            "JOIN execution_manifests AS manifests USING(attempt_id) "
            "JOIN final_plan_projection AS projection ON projection.plan_id=attempts.plan_id "
            "WHERE attempts.plan_id=? AND NOT EXISTS ("
            "SELECT 1 FROM rename_protection_v2_attempts AS old "
            "WHERE old.attempt_id=attempts.attempt_id AND old.state='SUPERSEDED') "
            + ("AND manifests.staging_relative_path=? " if superseded_v1 else "")
            + "ORDER BY attempts.created_at DESC, attempts.attempt_id DESC LIMIT 1",
            (
                (preflight.plan_id, placeholder)
                if superseded_v1
                else (preflight.plan_id,)
            ),
        ).fetchone()
        if attempt is None:
            raise MaterializationError("RenameProtection attempt is missing")
        if attempt["state"] == "COMPLETE":
            telemetry.phase = "complete"
            return {
                "analysis_run": str(run_path),
                "destination": str(selected_root),
                "executor_kind": preflight.executor_kind,
                "executor_version": RENAME_PROTECTION_V2_VERSION,
                "operation_count": preflight.operation_count,
                "explicit_directory_count": preflight.explicit_directory_count,
                "plan_id": preflight.plan_id,
                "run_id": preflight.run_id,
                "status": "materialized-in-place",
                "telemetry": telemetry.snapshot(),
                "total_bytes": preflight.total_bytes,
            }
        attempt_id = str(attempt["attempt_id"])
        staging_root = selected_root / rename_v2_staging_name(
            preflight.plan_id, attempt_id
        )
        connection.execute(
            "UPDATE execution_manifests SET staging_relative_path=? "
            "WHERE attempt_id=? AND staging_relative_path=?",
            (staging_root.name, attempt_id, placeholder),
        )
        connection.commit()
        persisted_progress = WorkflowProgress(
            connection,
            "materialization",
            preflight.run_id,
            plan_id=preflight.plan_id,
            attempt_id=attempt_id,
            phase="admission",
            total_count=preflight.operation_count,
            unit="files",
        )
        context = RenameContext(
            attempt_id=attempt_id,
            plan_id=preflight.plan_id,
            run_id=preflight.run_id,
            revision=int(attempt["revision"]),
            fingerprint=str(attempt["fingerprint"]),
            selected_root=selected_root,
            staging_root=staging_root,
        )
        execute_rename_protection_v2(
            connection,
            context,
            progress,
            telemetry,
            persisted_progress,
            _maybe_crash,
        )
    except RenameProtectionV2NeedsAttention as error:
        if connection.in_transaction:
            connection.rollback()
        raise MaterializationError(str(error)) from error
    except RenameProtectionV2Error as error:
        if connection.in_transaction:
            connection.rollback()
        connection.execute(
            "UPDATE rename_protection_v2_attempts SET state='FAILED', "
            "updated_at=datetime('now'), completed_at=datetime('now') "
            "WHERE attempt_id=(SELECT attempt_id FROM execution_manifests "
            "WHERE plan_id=? ORDER BY created_at DESC, attempt_id DESC LIMIT 1)",
            (preflight.plan_id,),
        )
        connection.execute(
            "UPDATE materialization_attempts SET state='FAILED', "
            "updated_at=datetime('now'), completed_at=datetime('now'), "
            "failure_classification='io-integrity-manual-attention', "
            "failure_detail=? WHERE attempt_id=(SELECT attempt_id FROM execution_manifests "
            "WHERE plan_id=? ORDER BY created_at DESC, attempt_id DESC LIMIT 1)",
            (str(error), preflight.plan_id),
        )
        connection.commit()
        if persisted_progress is not None:
            persisted_progress.fail(type(error).__name__)
        raise MaterializationError(str(error)) from error
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "analysis_run": str(run_path),
        "destination": str(selected_root),
        "executor_kind": preflight.executor_kind,
        "executor_version": RENAME_PROTECTION_V2_VERSION,
        "operation_count": preflight.operation_count,
        "explicit_directory_count": preflight.explicit_directory_count,
        "plan_id": preflight.plan_id,
        "run_id": preflight.run_id,
        "status": "materialized-in-place",
        "telemetry": telemetry.snapshot(),
        "total_bytes": preflight.total_bytes,
    }


@_run_workspace_refusal
def materialize_consolidation_plan(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None = None,
    *,
    in_place: bool = False,
    progress: ProgressReporter | None = None,
    admission: MaterializationPreflight | None = None,
) -> dict[str, object]:
    """Run execution under non-blocking Run and destination locks.

    A lightweight destination lookup selects the deterministic lock sidecar.
    The complete execution preflight runs once after the applicable locks are
    held, so the lookup grants no authorization.
    """
    try:
        run_path, database_path = resolve_run_directory(analysis_run)
        with nonblocking_lock(run_path / ".filesystem-organizer.lock"):
            if in_place:
                locked_preflight = preflight_materialization_for_execution(
                    analysis_run, plan_id, destination, in_place=True
                )
                return _materialize_consolidation_plan(
                    locked_preflight, in_place=True, progress=progress
                )
            if admission is not None:
                lock_destination = Path(admission.destination)
            elif destination is not None:
                lock_destination = destination.expanduser().resolve(strict=False)
            else:
                with open_read_only(database_path) as connection:
                    lock_destination = Path(
                        str(
                            select_plan_row(connection, plan_id)["intended_destination"]
                        )
                    )
            with nonblocking_lock(destination_lock_path(lock_destination)):
                locked_preflight = preflight_materialization_for_execution(
                    analysis_run, plan_id, destination, in_place=False
                )
                return _materialize_consolidation_plan(
                    locked_preflight,
                    in_place=False,
                    progress=progress,
                )
    except LinuxFilesystemError as error:
        raise MaterializationError(str(error)) from error


def _materialize_consolidation_plan(
    preflight: MaterializationPreflight,
    *,
    in_place: bool = False,
    progress: ProgressReporter | None = None,
) -> dict[str, object]:
    reporter = progress or NullProgressReporter()
    if in_place:
        if preflight.executor_kind == EXECUTOR_RENAME_PROTECTION:
            return _materialize_rename_protection(preflight, reporter)
        return _materialize_in_place(preflight, reporter)
    return _materialize_normal_phase(preflight, reporter)


def _materialize_rename_protection(
    preflight: MaterializationPreflight, reporter: ProgressReporter
) -> dict[str, object]:
    """Dispatch solely from the persisted RenameProtection executor version."""
    if preflight.executor_version == RENAME_PROTECTION_V2_VERSION:
        return _materialize_rename_protection_v2(preflight, reporter)
    if preflight.executor_version == CURRENT_RENAME_PROTECTION_VERSION:
        return _materialize_rename_protection_v3(preflight, reporter)
    raise MaterializationError(
        f"Unsupported RenameProtection executor version: {preflight.executor_version}"
    )


@dataclass(frozen=True)
class _V3Operation:
    """One deterministic namespace mutation derived from the finalized Plan.

    This intentionally is not a persisted operation model.  Its lifetime is one
    invocation; reloading the finalized plan recreates the same ordinals.
    """

    ordinal: int
    action: str
    source: str | None
    destination: str | None


def _v3_operations(connection: sqlite3.Connection, plan_id: str) -> list[_V3Operation]:
    entries = _plan_entries(connection, plan_id)
    operations: list[_V3Operation] = []
    owned_sources: set[str] = set()
    retained_identities: set[tuple[int, str]] = set()

    def append(action: str, source: str, destination: str | None = None) -> None:
        operations.append(_V3Operation(len(operations), action, source, destination))

    # Final-plan entry order is part of v3's executor-version ordering.
    for row in entries:
        if row["entry_kind"] == "file":
            source = str(row["source_relative_path"])
            destination = str(row["output_relative_path"])
            owned_sources.add(source)
            identity = (
                (int(row["expected_byte_size"]), str(row["digest"]))
                if row["digest"] is not None
                else None
            )
            if identity is not None:
                retained_identities.add(identity)
            append("PLACE", source, destination)
    # Direct cleanup is deliberately derived separately: no identity groups,
    # quarantine, or alternate-copy promotion are needed at execution time.
    for row in _excluded_file_entries(connection, plan_id):
        source = str(row["source_relative_path"])
        owned_sources.add(source)
        append("UNLINK", source)

    # The finalized Plan already proves equality.  Delete its unselected exact
    # duplicate occurrences directly; v3 must not rehash, promote, or stage.
    run_id = connection.execute(
        "SELECT run_id FROM consolidation_plans WHERE plan_id=?", (plan_id,)
    ).fetchone()
    if run_id is None:
        raise MaterializationError("finalized Plan has no Analysis Run")
    for row in connection.execute(
        "SELECT identity.relative_path, identity.byte_size, identity.digest "
        "FROM content_identities AS identity JOIN inventory_entries AS inventory "
        "ON inventory.run_id=identity.run_id "
        "AND inventory.relative_path=identity.relative_path "
        "WHERE identity.run_id=? AND identity.read_outcome='successful' "
        "AND inventory.entry_kind='regular-file' ORDER BY identity.relative_path",
        (str(run_id[0]),),
    ):
        source = str(row["relative_path"])
        identity = (int(row["byte_size"]), str(row["digest"]))
        if (
            identity in retained_identities
            and source not in owned_sources
        ):
            owned_sources.add(source)
            append("UNLINK", source)
    for row in _skipped_exclusion_entries(connection, plan_id):
        relative_path = str(row["relative_path"])
        if row["entry_kind"] is None:
            raise MaterializationError(
                f"finalized skipped exclusion has no inventory entry: {relative_path}"
            )
        owned_sources.add(relative_path)
        append(
            "RMDIR" if row["entry_kind"] == "directory" else "UNLINK",
            relative_path,
        )

    # Remove only Plan-affected source directories, deepest first.  These are
    # scheduled namespace vacators, not a broad empty-directory sweep.
    explicit_directories = {
        str(row["output_relative_path"])
        for row in entries
        if row["entry_kind"] == "directory"
    }
    required_directories = set(explicit_directories)
    for operation in operations:
        if operation.destination is None:
            continue
        parent = Path(operation.destination).parent
        while parent != Path("."):
            required_directories.add(parent.as_posix())
            parent = parent.parent
    affected_directories: set[str] = set()
    for source in owned_sources:
        parent = Path(source).parent
        while parent != Path("."):
            affected_directories.add(parent.as_posix())
            parent = parent.parent
    for directory in sorted(
        affected_directories - required_directories,
        key=lambda value: (-len(Path(value).parts), value),
    ):
        append("RMDIR", directory)
    return operations


def _v3_proven_duplicate_destination_occupants(
    connection: sqlite3.Connection, plan_id: str
) -> dict[str, tuple[int, str]]:
    """Return occupied outputs proven equal to their planned replacement.

    This is a defensive reconciliation path for an occupied destination that
    reaches strict no-replace placement despite the schedule's vacator.
    """
    entries = _plan_entries(connection, plan_id)
    owned_sources = {
        str(row["source_relative_path"])
        for row in entries
        if row["entry_kind"] == "file"
    }
    planned_output_identities = {
        str(row["output_relative_path"]): (
            int(row["expected_byte_size"]),
            str(row["digest"]),
        )
        for row in entries
        if row["entry_kind"] == "file" and row["digest"] is not None
    }
    run_id = connection.execute(
        "SELECT run_id FROM consolidation_plans WHERE plan_id=?", (plan_id,)
    ).fetchone()
    if run_id is None:
        raise MaterializationError("finalized Plan has no Analysis Run")
    occupants: dict[str, tuple[int, str]] = {}
    for row in connection.execute(
        "SELECT relative_path, byte_size, digest FROM content_identities "
        "WHERE run_id=? AND read_outcome='successful' ORDER BY relative_path",
        (str(run_id[0]),),
    ):
        path = str(row["relative_path"])
        identity = (int(row["byte_size"]), str(row["digest"]))
        if (
            path not in owned_sources
            and planned_output_identities.get(path) == identity
        ):
            occupants[path] = identity
    return occupants


def _v3_prepare_parents(
    root: Path, operations: list[_V3Operation], explicit_directories: Iterable[str] = ()
) -> int:
    """Create each required parent once, outside the ordinary PLACE path."""
    parents: set[Path] = set()
    for directory in explicit_directories:
        parent = root / directory
        while parent != root:
            parents.add(parent)
            parent = parent.parent
    for operation in operations:
        if operation.destination is None:
            continue
        parent = root / Path(operation.destination).parent
        while parent != root:
            parents.add(parent)
            parent = parent.parent
    created = 0
    for parent in sorted(parents, key=lambda path: (len(path.parts), str(path))):
        try:
            parent.mkdir()
            created += 1
        except FileExistsError:
            if not parent.is_dir():
                raise
    return created


def _v3_prune_empty_descendant_directories_no_follow(directory: Path) -> None:
    """Remove only empty directories below one planned RMDIR, without following links."""

    def prune(descriptor: int) -> None:
        for name in os.listdir(descriptor):
            try:
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as error:
                if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                    continue
                raise
            try:
                prune(child_descriptor)
            finally:
                os.close(child_descriptor)
            try:
                os.rmdir(name, dir_fd=descriptor)
            except OSError as error:
                if error.errno in {
                    errno.ENOENT,
                    errno.ENOTDIR,
                    errno.ENOTEMPTY,
                    errno.EEXIST,
                }:
                    continue
                raise

    descriptor = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        prune(descriptor)
    finally:
        os.close(descriptor)


def _materialize_rename_protection_v3(
    preflight: MaterializationPreflight, progress: ProgressReporter
) -> dict[str, object]:
    """Near-raw v3 executor.

    Ordinary Placement has one path through this function: a single raw
    ``renameat2(RENAME_NOREPLACE)``. Ordinary success accounting stays in memory;
    SQLite is touched only for admission, exceptional namespace-reuse receipts,
    sparse failures, coarse checkpoints, and completion.
    """
    run_path, database_path = resolve_run_directory(Path(preflight.analysis_run))
    root = Path(preflight.selected_backup_root)
    telemetry = MaterializationTelemetry(
        version=5,
        phase="admission",
        executor_kind=EXECUTOR_RENAME_PROTECTION,
        executor_version=CURRENT_RENAME_PROTECTION_VERSION,
    )
    connection = open_read_write(database_path)
    progress_pending = 0
    progress_started = False
    try:
        ensure_rename_v3_schema(connection)
        projection = connection.execute(
            "SELECT revision, fingerprint FROM final_plan_projection WHERE plan_id=?",
            (preflight.plan_id,),
        ).fetchone()
        plan = connection.execute(
            "SELECT schema_version FROM consolidation_plans WHERE plan_id=?",
            (preflight.plan_id,),
        ).fetchone()
        if projection is None or plan is None:
            raise MaterializationError("finalized Plan binding is unavailable")
        existing = connection.execute(
            "SELECT attempt_id FROM rename_protection_v3_attempts WHERE plan_id=? "
            "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
            (preflight.plan_id,),
        ).fetchone()
        attempt_id = str(existing[0]) if existing else str(uuid.uuid4())
        bind_rename_v3_attempt(
            connection,
            RenameV3AttemptBinding(
                attempt_id,
                preflight.plan_id,
                int(plan[0]),
                int(projection[0]),
                str(projection[1]),
            ),
        )
        connection.execute(
            "UPDATE rename_protection_v3_attempts SET state='EXECUTING', updated_at=datetime('now') WHERE attempt_id=?",
            (attempt_id,),
        )
        connection.commit()

        operations = _v3_operations(connection, preflight.plan_id)
        proven_duplicate_occupants = _v3_proven_duplicate_destination_occupants(
            connection, preflight.plan_id
        )
        progress.start("Materializing RenameProtection v3", total=len(operations))
        progress_started = True
        explicit_directories = {
            str(row["output_relative_path"])
            for row in _plan_entries(connection, preflight.plan_id)
            if row["entry_kind"] == "directory"
        }
        mkdir_count = _v3_prepare_parents(root, operations)
        persisted = load_rename_v3_progress(connection, attempt_id)
        high_watermark, prior_metrics = persisted or (-1, {})
        if high_watermark > len(operations) - 1:
            raise MaterializationError(
                "RenameProtection v3 checkpoint exceeds its deterministic schedule"
            )
        actual_failures = {
            failure.ordinal: failure
            for failure in load_rename_v3_failures(connection, attempt_id)
        }
        if any(ordinal >= len(operations) for ordinal in actual_failures):
            raise MaterializationError(
                "RenameProtection v3 failure exceeds its deterministic schedule"
            )
        # A destination is blocked only by the one planned operation which
        # vacates that namespace path.  This is the complete in-memory graph.
        vacator = {
            operation.source: operation.ordinal
            for operation in operations
            if operation.source
        }
        dependents: dict[int, list[int]] = {
            operation.ordinal: [] for operation in operations
        }
        pending: dict[int, int] = {operation.ordinal: 0 for operation in operations}
        prerequisites: dict[int, set[int]] = {
            operation.ordinal: set() for operation in operations
        }

        def depends_on(ordinal: int, predecessor: int) -> None:
            if predecessor == ordinal or predecessor in prerequisites[ordinal]:
                return
            prerequisites[ordinal].add(predecessor)
            pending[ordinal] += 1
            dependents[predecessor].append(ordinal)

        reuse_vacators: set[int] = set()
        for operation in operations:
            if operation.destination and operation.destination in vacator:
                predecessor = vacator[operation.destination]
                depends_on(operation.ordinal, predecessor)
                if predecessor != operation.ordinal:
                    reuse_vacators.add(predecessor)
        # Empty output directories may reuse a source pathname after execution.
        # Their vacators need the same replay protection as file destinations.
        for directory in explicit_directories:
            path = Path(directory)
            while path != Path("."):
                directory_predecessor = vacator.get(path.as_posix())
                if directory_predecessor is not None:
                    reuse_vacators.add(directory_predecessor)
                path = path.parent
        # A directory vacator becomes ready only after Plan-owned descendants
        # leave it.  Enumerating ancestors of each source is linear in the
        # pathname representation, unlike a whole-operation pair scan.
        rmdir_vacators = {
            cast(str, operation.source): operation.ordinal
            for operation in operations
            if operation.action == "RMDIR"
        }
        for operation in operations:
            if operation.source is None:
                continue
            parent = Path(operation.source).parent
            while parent != Path("."):
                rmdir_predecessor = rmdir_vacators.get(parent.as_posix())
                if rmdir_predecessor is not None:
                    depends_on(rmdir_predecessor, operation.ordinal)
                parent = parent.parent
        # A checkpoint is a prefix, not merely the greatest ordinal ever seen.
        # Seed that prefix from its documented meaning: every non-failed,
        # non-derived-blocked operation through it has already succeeded.
        prior_blocked: set[int] = set()
        blocked_frontier = list(actual_failures)
        while blocked_frontier:
            blocked_ordinal = blocked_frontier.pop()
            for dependent in dependents[blocked_ordinal]:
                if dependent not in prior_blocked:
                    prior_blocked.add(dependent)
                    blocked_frontier.append(dependent)

        vacated = load_rename_v3_vacated(connection, attempt_id)
        if any(ordinal >= len(operations) for ordinal in vacated):
            raise MaterializationError(
                "RenameProtection v3 receipt exceeds its schedule"
            )
        considered_ordinals = [False] * len(operations)
        for ordinal in sorted(set(range(high_watermark + 1)) | vacated):
            if ordinal not in vacated and (
                ordinal in actual_failures or ordinal in prior_blocked
            ):
                continue
            considered_ordinals[ordinal] = True
            for dependent in dependents[ordinal]:
                pending[dependent] -= 1

        # Failed operations are retried only once their prerequisites are
        # genuinely available; unrelated work remains ready in the same pass.
        ready = [
            ordinal
            for ordinal, count in pending.items()
            if count == 0 and not considered_ordinals[ordinal]
        ]
        # A heap is the specialized deterministic ready queue.  Do not sort or
        # shift an ever-growing list after every successful namespace change.
        heapq.heapify(ready)
        progress.advance(sum(considered_ordinals))
        considered_since_checkpoint = 0
        last_progress = time.monotonic()
        last_checkpoint = time.monotonic()
        rename_count = int(prior_metrics.get("rename_count", 0))
        unlink_count = int(prior_metrics.get("unlink_count", 0))
        rmdir_count = int(prior_metrics.get("rmdir_count", 0))
        checkpoint_count = int(prior_metrics.get("sqlite_checkpoint_count", 0))
        pivot_count = int(prior_metrics.get("pivot_count", 0))

        def succeeded(ordinal: int) -> None:
            """Receipt exceptional vacators before any successor can reuse the name."""
            nonlocal progress_pending
            if ordinal in reuse_vacators and ordinal not in vacated:
                record_rename_v3_vacated(connection, attempt_id, [ordinal])
                vacated.add(ordinal)
                _maybe_crash("rename-protection-v3-after-vacator-receipt")
            if ordinal in actual_failures:
                resolve_rename_v3_failure(connection, attempt_id, ordinal)
                del actual_failures[ordinal]
            considered_ordinals[ordinal] = True
            progress_pending += 1
            for dependent in dependents[ordinal]:
                pending[dependent] -= 1
                if pending[dependent] == 0:
                    heapq.heappush(ready, dependent)

        def checkpoint_if_due(*, force: bool = False) -> None:
            nonlocal \
                high_watermark, \
                last_checkpoint, \
                checkpoint_count, \
                considered_since_checkpoint
            nonlocal progress_pending, last_progress
            now = time.monotonic()
            if progress_pending and (
                force or progress_pending >= 256 or now - last_progress >= 0.2
            ):
                progress.advance(progress_pending)
                progress_pending = 0
                last_progress = now
            if not force and (
                considered_since_checkpoint < 50_000 and now - last_checkpoint < 60.0
            ):
                return
            # Prefix advancement belongs to a coarse checkpoint.  Recomputing
            # it before every successful syscall would make this path O(N^2).
            watermark = high_watermark
            while (
                watermark + 1 < len(operations) and considered_ordinals[watermark + 1]
            ):
                watermark += 1
            checkpoint_rename_v3(
                connection,
                attempt_id,
                high_watermark=watermark,
                metrics={
                    "rename_count": rename_count,
                    "unlink_count": unlink_count,
                    "rmdir_count": rmdir_count,
                    "mkdir_count": mkdir_count,
                    "metadata_observation_count": 0,
                    "syncfs_count": 0,
                    "sqlite_checkpoint_count": checkpoint_count + 1,
                    "pivot_count": pivot_count,
                },
            )
            checkpoint_count += 1
            high_watermark = watermark
            considered_since_checkpoint = 0
            last_checkpoint = time.monotonic()

        def source_present(path: Path) -> bool:
            try:
                return _path_entry_exists(path)
            except NotADirectoryError:
                # A completed trapped replacement may now occupy an ancestor.
                return False

        def drain_ready() -> None:
            nonlocal \
                considered_since_checkpoint, \
                rename_count, \
                rmdir_count, \
                unlink_count
            while ready:
                ordinal = heapq.heappop(ready)
                if considered_ordinals[ordinal]:
                    continue
                operation = operations[ordinal]
                considered_since_checkpoint += 1
                try:
                    source = root / cast(str, operation.source)
                    if operation.action == "PLACE":
                        # A same-path Placement is already satisfied.  It remains
                        # in the deterministic schedule and success accounting,
                        # but must not call RENAME_NOREPLACE against itself.
                        if operation.source != operation.destination:
                            # Do not insert observations on this normal path.
                            rename_no_replace_raw(
                                source, root / cast(str, operation.destination)
                            )
                        rename_count += 1
                        _maybe_crash("rename-protection-v3-after-place")
                    elif operation.action == "RMDIR":
                        try:
                            source.rmdir()
                        except OSError as error:
                            if error.errno != errno.ENOTEMPTY:
                                raise
                            _v3_prune_empty_descendant_directories_no_follow(source)
                            source.rmdir()
                        rmdir_count += 1
                    else:
                        source.unlink()
                        unlink_count += 1
                except OSError as error:
                    # Reconciliation is deliberately confined to a failed resumed
                    # syscall.  A completed, uncheckpointed transition can then be
                    # adopted without putting observations on the ordinary path.
                    if operation.action == "PLACE":
                        destination = root / cast(str, operation.destination)
                        replacement_identity = proven_duplicate_occupants.get(
                            cast(str, operation.destination)
                        )
                        if (
                            error.errno == errno.EEXIST
                            and replacement_identity is not None
                            and source_present(source)
                        ):
                            try:
                                _validate_source_before_copy(
                                    destination,
                                    replacement_identity[0],
                                    replacement_identity[1],
                                )
                            except MaterializationError:
                                pass
                            else:
                                destination.unlink()
                                unlink_count += 1
                                rename_no_replace_raw(source, destination)
                                rename_count += 1
                                succeeded(ordinal)
                                checkpoint_if_due()
                                continue
                        completed = not source_present(source) and _path_entry_exists(
                            destination
                        )
                    else:
                        completed = not source_present(source)
                    if completed:
                        if operation.action == "PLACE":
                            rename_count += 1
                        elif operation.action == "RMDIR":
                            rmdir_count += 1
                        else:
                            unlink_count += 1
                        succeeded(ordinal)
                        checkpoint_if_due()
                        continue
                    record_rename_v3_failure(
                        connection,
                        attempt_id,
                        RenameV3Failure(
                            ordinal,
                            f"{operation.action}:{operation.source}->{operation.destination or ''}",
                            "os-error",
                            error.errno,
                            str(error),
                        ),
                    )
                    actual_failures[ordinal] = RenameV3Failure(
                        ordinal,
                        f"{operation.action}:{operation.source}->{operation.destination or ''}",
                        "os-error",
                        error.errno,
                        str(error),
                    )
                    considered_ordinals[ordinal] = True
                    # Dependents remain derived-blocked in memory; unrelated ready
                    # work continues and no synthetic blocked rows are written.
                    checkpoint_if_due()
                    continue
                succeeded(ordinal)
                checkpoint_if_due()

        # The ready queue intentionally handles every acyclic vacancy chain.
        # What remains can be a real cycle (or a concrete conflict), never a
        # reason to put staging on the ordinary Placement path.  A cycle owns
        # exactly one temporary pathname and uses N+1 no-replace renames.
        residual: set[int] = set()
        active_pivot = load_rename_v3_active_pivot(connection, attempt_id)

        def cycle_from(start: int) -> list[int] | None:
            cycle: list[int] = []
            seen: set[int] = set()
            current = start
            while current not in seen:
                if current not in residual:
                    return None
                operation = operations[current]
                if operation.action != "PLACE" or operation.destination is None:
                    return None
                predecessor = vacator.get(operation.destination)
                if predecessor is None:
                    return None
                seen.add(current)
                cycle.append(current)
                current = predecessor
            return cycle if current == start else None

        def advance_pivot_phase(
            pivot_state: RenameV3ActivePivot, phase: str
        ) -> RenameV3ActivePivot:
            updated = RenameV3ActivePivot(**{**pivot_state.__dict__, "phase": phase})
            set_rename_v3_active_pivot(connection, attempt_id, updated)
            _maybe_crash("rename-protection-v3-after-pivot-phase")
            return updated

        def recover_cycle_pivot(
            pivot_state: RenameV3ActivePivot, cycle: list[int]
        ) -> bool:
            """Finish a persisted exceptional cycle without adopting ambiguity.

            Every observation here is recovery-only: ordinary Placements never
            enter this helper.  A temporary name is attributable only when its
            recorded source has disappeared; a second candidate pathname is an
            ownership contradiction rather than an invitation to guess.
            """
            nonlocal rename_count
            pivot_ordinal = cycle[0]
            pivot = operations[pivot_ordinal]
            assert pivot.source is not None and pivot.destination is not None
            temporary = root / pivot_state.temporary_relative_path
            source = root / pivot.source
            destination = root / pivot.destination
            phase = pivot_state.phase
            if phase == "INTENT":
                source_present = _path_entry_exists(source)
                temporary_present = _path_entry_exists(temporary)
                if source_present and not temporary_present:
                    rename_no_replace_raw(source, temporary)
                    rename_count += 1
                    _maybe_crash("rename-protection-v3-after-pivot")
                    pivot_state = advance_pivot_phase(pivot_state, "PIVOTED")
                    phase = pivot_state.phase
                elif not source_present and temporary_present:
                    pivot_state = advance_pivot_phase(pivot_state, "PIVOTED")
                    phase = pivot_state.phase
                else:
                    raise MaterializationError(
                        "RenameProtection v3 pivot ownership is ambiguous"
                    )
            completed = 0
            if phase.startswith("VACATED:"):
                try:
                    completed = int(phase.removeprefix("VACATED:"))
                except ValueError as error:
                    raise MaterializationError(
                        "RenameProtection v3 pivot phase is invalid"
                    ) from error
            for index, ordinal in enumerate(reversed(cycle[1:]), start=1):
                operation = operations[ordinal]
                assert (
                    operation.source is not None and operation.destination is not None
                )
                source = root / operation.source
                destination = root / operation.destination
                if index <= completed:
                    # Later cycle moves may already have repopulated this source.
                    continue
                source_present = _path_entry_exists(source)
                destination_present = _path_entry_exists(destination)
                if source_present and not destination_present:
                    rename_no_replace_raw(source, destination)
                    rename_count += 1
                    _maybe_crash("rename-protection-v3-after-pivot-vacate")
                elif not source_present and destination_present:
                    pass
                else:
                    raise MaterializationError(
                        "RenameProtection v3 pivot ownership is ambiguous"
                    )
                pivot_state = advance_pivot_phase(pivot_state, f"VACATED:{index}")
            temporary_present = _path_entry_exists(temporary)
            destination_present = _path_entry_exists(root / pivot.destination)
            if temporary_present and not destination_present:
                rename_no_replace_raw(temporary, root / pivot.destination)
                rename_count += 1
                _maybe_crash("rename-protection-v3-after-pivot-publish")
            elif not temporary_present and destination_present:
                pass
            else:
                raise MaterializationError(
                    "RenameProtection v3 pivot ownership is ambiguous"
                )
            record_rename_v3_vacated(connection, attempt_id, cycle, finish_pivot=True)
            vacated.update(cycle)
            _maybe_crash("rename-protection-v3-after-pivot-receipt")
            for ordinal in cycle:
                succeeded(ordinal)
                residual.discard(ordinal)
            return True

        def recover_trapped_directory_pivot(
            pivot_state: RenameV3ActivePivot, rmdir_ordinal: int
        ) -> bool:
            """Replace a nonempty directory only when its payload is trapped in it."""
            nonlocal rename_count, rmdir_count
            pivot_ordinal = int(pivot_state.pivot_key.removeprefix("trapped:"))
            pivot = operations[pivot_ordinal]
            assert pivot.source is not None and pivot.destination is not None
            temporary = root / pivot_state.temporary_relative_path
            source = root / pivot.source
            directory = root / cast(str, operations[rmdir_ordinal].source)
            phase = pivot_state.phase
            if phase == "INTENT":
                source_present = _path_entry_exists(source)
                temporary_present = _path_entry_exists(temporary)
                if source_present and not temporary_present:
                    rename_no_replace_raw(source, temporary)
                    rename_count += 1
                    _maybe_crash("rename-protection-v3-after-pivot")
                    pivot_state = advance_pivot_phase(pivot_state, "PIVOTED")
                    phase = pivot_state.phase
                elif not source_present and temporary_present:
                    pivot_state = advance_pivot_phase(pivot_state, "PIVOTED")
                    phase = pivot_state.phase
                else:
                    raise MaterializationError(
                        "RenameProtection v3 pivot ownership is ambiguous"
                    )
            if phase == "PIVOTED":
                try:
                    try:
                        directory.rmdir()
                    except OSError as error:
                        if error.errno != errno.ENOTEMPTY:
                            raise
                        _v3_prune_empty_descendant_directories_no_follow(directory)
                        directory.rmdir()
                    rmdir_count += 1
                    _maybe_crash("rename-protection-v3-after-pivot-rmdir")
                except FileNotFoundError:
                    pass
                pivot_state = advance_pivot_phase(pivot_state, "VACATED:1")
            if pivot_state.phase != "VACATED:1":
                raise MaterializationError("RenameProtection v3 pivot phase is invalid")
            temporary_present = _path_entry_exists(temporary)
            destination_present = _path_entry_exists(root / pivot.destination)
            if temporary_present and not destination_present:
                rename_no_replace_raw(temporary, root / pivot.destination)
                rename_count += 1
                _maybe_crash("rename-protection-v3-after-pivot-publish")
            elif not temporary_present and destination_present:
                pass
            else:
                raise MaterializationError(
                    "RenameProtection v3 pivot ownership is ambiguous"
                )
            finished = [pivot_ordinal, rmdir_ordinal]
            record_rename_v3_vacated(
                connection, attempt_id, finished, finish_pivot=True
            )
            vacated.update(finished)
            _maybe_crash("rename-protection-v3-after-pivot-receipt")
            for ordinal in finished:
                succeeded(ordinal)
                residual.discard(ordinal)
            return True

        while True:
            drain_ready()
            residual = {
                ordinal
                for ordinal in range(len(operations))
                if not considered_ordinals[ordinal]
            }
            if not residual:
                break
            if active_pivot is not None:
                if active_pivot.pivot_key.startswith("cycle:"):
                    cycle = cycle_from(
                        int(active_pivot.pivot_key.removeprefix("cycle:"))
                    )
                    if cycle is None:
                        raise MaterializationError(
                            "RenameProtection v3 active pivot does not match its schedule"
                        )
                    recover_cycle_pivot(active_pivot, cycle)
                elif active_pivot.pivot_key.startswith("trapped:"):
                    pivot_ordinal = int(active_pivot.pivot_key.removeprefix("trapped:"))
                    pivot = operations[pivot_ordinal]
                    rmdir_ordinal = rmdir_vacators.get(cast(str, pivot.destination))
                    if rmdir_ordinal is None:
                        raise MaterializationError(
                            "RenameProtection v3 active pivot does not match its schedule"
                        )
                    recover_trapped_directory_pivot(active_pivot, rmdir_ordinal)
                else:
                    raise MaterializationError(
                        "RenameProtection v3 pivot key is invalid"
                    )

                active_pivot = None
                checkpoint_if_due()
                continue
            selected_cycle = next(
                (
                    cycle
                    for start in sorted(residual)
                    if (cycle := cycle_from(start)) is not None
                ),
                None,
            )
            trapped = next(
                (
                    ordinal
                    for ordinal in sorted(residual)
                    if operations[ordinal].action == "PLACE"
                    and operations[ordinal].destination in rmdir_vacators
                    and rmdir_vacators[cast(str, operations[ordinal].destination)]
                    in residual
                    and Path(cast(str, operations[ordinal].destination))
                    in Path(cast(str, operations[ordinal].source)).parents
                ),
                None,
            )
            if selected_cycle is None and trapped is None:
                break
            pivot_ordinal = (
                selected_cycle[0] if selected_cycle is not None else cast(int, trapped)
            )
            pivot = operations[pivot_ordinal]
            assert pivot.source is not None and pivot.destination is not None
            pivot_state = RenameV3ActivePivot(
                pivot_key=f"{'cycle' if selected_cycle is not None else 'trapped'}:{pivot_ordinal}",
                source_relative_path=pivot.source,
                temporary_relative_path=f".filesystem-organizer-v3-pivot-{attempt_id}-{pivot_ordinal}",
                destination_relative_path=pivot.destination,
                phase="INTENT",
            )
            set_rename_v3_active_pivot(connection, attempt_id, pivot_state)
            pivot_count += 1
            try:
                if selected_cycle is not None:
                    recover_cycle_pivot(pivot_state, selected_cycle)
                else:
                    recover_trapped_directory_pivot(
                        pivot_state, rmdir_vacators[pivot.destination]
                    )
            except OSError as error:
                failure = RenameV3Failure(
                    pivot_ordinal,
                    f"PIVOT:{pivot.source}->{pivot.destination}",
                    "os-error",
                    error.errno,
                    str(error),
                )
                record_rename_v3_failure(connection, attempt_id, failure)
                actual_failures[pivot_ordinal] = failure
                break
            checkpoint_if_due()

        # Remaining operations are either genuine residual cycles/conflicts or
        # descendants of an actual failure.  Only the latter are derived here;
        # neither category produces synthetic persistent rows.
        blocked = {
            ordinal
            for ordinal in range(len(operations))
            if not considered_ordinals[ordinal]
        }
        if not actual_failures and not blocked:
            # Standalone explicit directories need no operation ordinal. Create
            # them only after their possible file vacators have completed.
            mkdir_count += _v3_prepare_parents(root, [], explicit_directories)
            _maybe_crash("rename-protection-v3-after-directories")
        metrics: dict[str, int | float] = {
            "rename_count": rename_count,
            "unlink_count": unlink_count,
            "rmdir_count": rmdir_count,
            "mkdir_count": mkdir_count,
            "metadata_observation_count": 0,
            "syncfs_count": 0,
            "sqlite_checkpoint_count": checkpoint_count + 1,
            "pivot_count": pivot_count,
            "failure_count": len(actual_failures),
            "blocked_count": len(blocked),
        }
        watermark = high_watermark
        while watermark + 1 < len(operations) and considered_ordinals[watermark + 1]:
            watermark += 1
        checkpoint_rename_v3(
            connection, attempt_id, high_watermark=watermark, metrics=metrics
        )
        state = "COMPLETE" if not actual_failures and not blocked else "NEEDS_ATTENTION"
        connection.execute(
            "UPDATE rename_protection_v3_attempts SET state=?, updated_at=datetime('now') WHERE attempt_id=?",
            (state, attempt_id),
        )
        connection.commit()
        telemetry.phase = "complete" if state == "COMPLETE" else "needs-attention"
        telemetry.rename_count = rename_count
        telemetry.deleted_files = unlink_count
        telemetry.checkpoints = checkpoint_count + 1
        outcome: dict[str, object] = {
            "analysis_run": str(run_path),
            "destination": str(root),
            "executor_kind": preflight.executor_kind,
            "executor_version": CURRENT_RENAME_PROTECTION_VERSION,
            "operation_count": preflight.operation_count,
            "explicit_directory_count": preflight.explicit_directory_count,
            "plan_id": preflight.plan_id,
            "run_id": preflight.run_id,
            "status": "materialized-in-place"
            if state == "COMPLETE"
            else "needs-attention",
            "telemetry": telemetry.snapshot(),
            "total_bytes": preflight.total_bytes,
            "executor_metrics": metrics,
            "attempt_id": attempt_id,
        }
        if state != "COMPLETE":
            raise MaterializationError(
                "RenameProtection v3 needs attention: "
                f"{len(actual_failures)} failures, {len(blocked)} blocked"
            )
        return outcome
    finally:
        if progress_started:
            if progress_pending:
                progress.advance(progress_pending)
            progress.complete()
        connection.close()


def _checkpoint_staged_files(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    partial_root: Path,
    pending: list[tuple[sqlite3.Row, str, str | None]],
    synchronizer: FilesystemSync,
    telemetry: MaterializationTelemetry,
    reporter: ProgressReporter,
) -> None:
    """Make one bounded file batch durable before committing its evidence."""
    if not pending:
        return
    telemetry.report(reporter, "checkpoint synchronization")
    started = time.monotonic()
    _maybe_crash("checkpoint-before-sync")
    synchronizer.sync(partial_root)
    telemetry.checkpoint_sync_seconds += time.monotonic() - started
    _maybe_crash("checkpoint-after-sync")
    started = time.monotonic()
    connection.execute("BEGIN IMMEDIATE")
    try:
        attempt_id = connection.execute(
            "SELECT attempt_id FROM execution_manifests WHERE plan_id = ? "
            "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
            (preflight.plan_id,),
        ).fetchone()[0]
        connection.executemany(
            "INSERT OR REPLACE INTO execution_file_evidence VALUES (?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?)",
            [
                (
                    attempt_id,
                    preflight.plan_id,
                    preflight.run_id,
                    int(row["operation_index"]),
                    str(row["source_relative_path"]),
                    int(row["expected_byte_size"]),
                    int(row["observed_modified_ns"]),
                    transfer_kind,
                    CONTENT_IDENTITY_ALGORITHM if digest is not None else None,
                    CONTENT_IDENTITY_ALGORITHM_VERSION if digest is not None else None,
                    digest,
                )
                for row, transfer_kind, digest in pending
            ],
        )
    except BaseException:
        connection.rollback()
        raise
    _maybe_crash("checkpoint-before-commit")
    connection.commit()
    telemetry.checkpoint_database_seconds += time.monotonic() - started
    telemetry.checkpoints += 1
    telemetry.durable_files += len(pending)
    telemetry.durable_bytes += sum(
        int(row["expected_byte_size"]) for row, _, _ in pending
    )
    telemetry.report(reporter, "copying")
    _maybe_crash("checkpoint-after-commit")


def _materialize_normal_phase(
    preflight: MaterializationPreflight,
    reporter: ProgressReporter,
) -> dict[str, object]:
    """Build one complete owned partial tree, then publish it atomically."""
    telemetry = MaterializationTelemetry()
    reporter.start("Admission and source revalidation")
    telemetry.report(reporter, "admission/revalidation")
    if not preflight.sufficient_free_space:
        raise MaterializationError(
            "Insufficient free space at the Materialized Consolidation destination: "
            f"{preflight.free_bytes} bytes free, {preflight.total_bytes} bytes required"
        )
    run_path, database_path = resolve_run_directory(Path(preflight.analysis_run))
    final_root = Path(preflight.destination)
    selected_root = Path(preflight.selected_backup_root)
    partial_root = _partial_destination(final_root, preflight.plan_id)
    synchronizer = FilesystemSync(final_root.parent)
    connection = open_read_write(database_path)
    persisted_progress: WorkflowProgress | None = None
    try:
        connection.execute("PRAGMA synchronous = FULL")
        ensure_journal_schema(connection)
        if final_root.exists():
            _reconcile_published_destination(connection, preflight, final_root)
            telemetry.published()
            cloned_files, cloned_bytes, streamed_files, streamed_bytes = (
                _attempt_transfer_totals(connection, preflight.plan_id)
            )
            telemetry.reused_files = cloned_files + streamed_files
            telemetry.durable_files = telemetry.reused_files
            telemetry.durable_bytes = cloned_bytes + streamed_bytes
            telemetry.report(reporter, "complete")
            return {
                "telemetry": telemetry.snapshot(),
                "analysis_run": str(run_path),
                "destination": str(final_root),
                "operation_count": preflight.operation_count,
                "explicit_directory_count": preflight.explicit_directory_count,
                "plan_id": preflight.plan_id,
                "run_id": preflight.run_id,
                "status": "materialized",
                "total_bytes": preflight.total_bytes,
                "cloned_file_count": cloned_files,
                "cloned_byte_count": cloned_bytes,
                "streamed_file_count": streamed_files,
                "streamed_byte_count": streamed_bytes,
            }
        entries = _ensure_execution_manifest(connection, preflight, partial_root.name)
        attempt_id = str(
            connection.execute(
                "SELECT attempt_id FROM execution_manifests WHERE plan_id=? "
                "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
                (preflight.plan_id,),
            ).fetchone()[0]
        )
        persisted_progress = WorkflowProgress(
            connection,
            "materialization",
            preflight.run_id,
            plan_id=preflight.plan_id,
            attempt_id=attempt_id,
            phase="admission/revalidation",
            total_count=preflight.operation_count,
            unit="files",
        )
        _validate_manifest_algorithms(entries, preflight.plan_id)
        _validate_legacy_operation_algorithms(connection, preflight.plan_id)
        _maybe_crash("admission")
        _revalidate_structural_snapshot(
            connection, preflight.run_id, selected_root, preflight.plan_id
        )
        telemetry.report(reporter, "admission/revalidation complete")
        files = [row for row in entries if row["entry_kind"] == "file"]
        directories = [row for row in entries if row["entry_kind"] == "directory"]
        reporter.start("Recovery verification", len(files), unit="files")
        persisted_progress.phase(
            "recovery-verification", total_count=len(files), unit="files"
        )
        if partial_root.exists() and _validate_reusable_durable_partial(
            connection, preflight, partial_root, entries, telemetry, reporter
        ):
            telemetry.reused_files = len(files)
            telemetry.durable_files = len(files)
            telemetry.durable_bytes = sum(
                int(row["expected_byte_size"]) for row in files
            )
            _set_attempt_state(connection, preflight.plan_id, "STAGING_DURABLE")
            owner_file = partial_root / PARTIAL_OWNER_FILE
            owner_file.unlink(missing_ok=True)
            synchronizer.sync(partial_root)
            telemetry.report(reporter, "publication")
            _publish_durable_staging(connection, preflight, partial_root, final_root)
            synchronizer.sync(final_root.parent)
            telemetry.published()
            _set_attempt_state(connection, preflight.plan_id, "PUBLISHED")
            _complete_materialization_attempt(
                connection,
                preflight.plan_id,
                *_attempt_transfer_totals(connection, preflight.plan_id),
            )
            cloned_files, cloned_bytes, streamed_files, streamed_bytes = (
                _attempt_transfer_totals(connection, preflight.plan_id)
            )
            telemetry.report(reporter, "complete")
            persisted_progress.complete(metrics=telemetry.snapshot())
            return {
                "telemetry": telemetry.snapshot(),
                "analysis_run": str(run_path),
                "destination": str(final_root),
                "operation_count": len(files),
                "explicit_directory_count": len(directories),
                "plan_id": preflight.plan_id,
                "run_id": preflight.run_id,
                "status": "materialized",
                "total_bytes": preflight.total_bytes,
                "cloned_file_count": cloned_files,
                "cloned_byte_count": cloned_bytes,
                "streamed_file_count": streamed_files,
                "streamed_byte_count": streamed_bytes,
            }
        completed_indexes: set[int] = set()
        if partial_root.exists():
            resumable_indexes = _validate_resumable_partial(
                connection, preflight, partial_root, entries, telemetry, reporter
            )
            if resumable_indexes is None:
                _discard_proven_partial(connection, preflight, partial_root)
            else:
                completed_indexes = resumable_indexes
                telemetry.reused_files = len(completed_indexes)
                telemetry.durable_files = len(completed_indexes)
                telemetry.durable_bytes = sum(
                    int(row["expected_byte_size"])
                    for row in files
                    if int(row["operation_index"]) in completed_indexes
                )
        if not partial_root.exists():
            partial_root.mkdir(mode=0o700)
            owner_file = partial_root / PARTIAL_OWNER_FILE
            owner_file.write_text(
                _partial_owner_payload(connection, preflight.plan_id), encoding="utf-8"
            )
            with owner_file.open("rb") as owner_descriptor:
                os.fsync(owner_descriptor.fileno())
            _fsync_directory(partial_root)
            _fsync_directory(partial_root.parent)
            root_stat = partial_root.stat()
            connection.execute(
                "UPDATE execution_manifests SET staging_root_dev = ?, staging_root_ino = ? "
                "WHERE plan_id = ?",
                (root_stat.st_dev, root_stat.st_ino, preflight.plan_id),
            )
            connection.commit()
            _set_attempt_state(connection, preflight.plan_id, "STAGING_STARTED")
        owner_file = partial_root / PARTIAL_OWNER_FILE
        for row in directories:
            (partial_root / str(row["output_relative_path"])).mkdir(
                parents=True, exist_ok=True
            )
        reporter.start("Staging Materialized Consolidation", len(files), unit="files")
        persisted_progress.phase("staging", total_count=len(files), unit="files")
        pending_evidence: list[tuple[sqlite3.Row, str, str | None]] = []
        pending_bytes = 0
        clone_capabilities = CloneCapabilities(try_native_clone)
        checkpoint_policy = _CheckpointPolicy(time.monotonic())
        for row in files:
            if int(row["operation_index"]) in completed_indexes:
                reporter.advance()
                continue
            output = partial_root / str(row["output_relative_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = partial_root / f".partial-{int(row['operation_index'])}"
            source = selected_root / str(row["source_relative_path"])
            _validate_source_metadata_observation(
                source,
                int(row["expected_byte_size"]),
                row["expected_modified_ns"],
            )
            clone_result = clone_capabilities.clone(source, output)
            if clone_result is CloneResult.CLONED:
                _validate_source_metadata_observation(
                    source,
                    int(row["expected_byte_size"]),
                    row["expected_modified_ns"],
                )
                source_metadata = source.stat()
                try:
                    os.chmod(output, stat.S_IMODE(source_metadata.st_mode))
                    os.utime(
                        output,
                        ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
                    )
                except OSError:
                    pass
                size = int(row["expected_byte_size"])
                transfer_kind = "native-clone"
                digest: str | None = None
            else:
                try:
                    size, digest = _copy_to_temporary(source, temporary, row)
                except OSError as error:
                    raise MaterializationError(
                        f"streamed copy failed: {source}"
                    ) from error
                os.replace(temporary, output)
                transfer_kind = "streamed"
            _maybe_crash("postwritten")
            telemetry.copied_files += 1
            telemetry.copied_bytes += size
            telemetry.clone_attempts = clone_capabilities.attempts
            telemetry.report(reporter, "copying")
            pending_evidence.append((row, transfer_kind, digest))
            pending_bytes += size
            if checkpoint_policy.due(len(pending_evidence), pending_bytes):
                previous_sync_seconds = telemetry.checkpoint_sync_seconds
                _checkpoint_staged_files(
                    connection,
                    preflight,
                    partial_root,
                    pending_evidence,
                    synchronizer,
                    telemetry,
                    reporter,
                )
                checkpoint_policy.confirmed(
                    telemetry.checkpoint_sync_seconds - previous_sync_seconds
                )
                pending_evidence.clear()
                pending_bytes = 0
            reporter.advance()
            if size != int(row["expected_byte_size"]):
                raise MaterializationError(
                    "streamed source byte size changed during staging"
                )
        _checkpoint_staged_files(
            connection,
            preflight,
            partial_root,
            pending_evidence,
            synchronizer,
            telemetry,
            reporter,
        )
        reporter.complete()
        synchronizer.sync(partial_root)
        root_stat = partial_root.stat()
        connection.execute(
            "UPDATE execution_manifests SET staging_root_dev = ?, staging_root_ino = ? "
            "WHERE plan_id = ?",
            (root_stat.st_dev, root_stat.st_ino, preflight.plan_id),
        )
        connection.commit()
        _set_attempt_state(connection, preflight.plan_id, "STAGING_DURABLE")
        _maybe_crash("staging-durable")
        owner_file.unlink()
        synchronizer.sync(partial_root)
        telemetry.report(reporter, "publication")
        persisted_progress.phase("publication", total_count=1, unit="phase")
        _publish_durable_staging(connection, preflight, partial_root, final_root)
        synchronizer.sync(final_root.parent)
        telemetry.published()
        _set_attempt_state(connection, preflight.plan_id, "PUBLISHED")
        _maybe_crash("published")
        (
            cloned_file_count,
            cloned_byte_count,
            streamed_file_count,
            streamed_byte_count,
        ) = _attempt_transfer_totals(connection, preflight.plan_id)
        telemetry.report(reporter, "complete")
        _complete_materialization_attempt(
            connection,
            preflight.plan_id,
            cloned_file_count,
            cloned_byte_count,
            streamed_file_count,
            streamed_byte_count,
        )
        persisted_progress.complete(metrics=telemetry.snapshot())
    except LinuxFilesystemError as error:
        if persisted_progress is not None:
            try:
                persisted_progress.fail(type(error).__name__)
            except sqlite3.Error:
                pass
        raise MaterializationError(str(error)) from error
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if persisted_progress is not None:
            try:
                persisted_progress.fail(type(error).__name__)
            except sqlite3.Error:
                pass
        raise
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        synchronizer.close()
    return {
        "telemetry": telemetry.snapshot(),
        "analysis_run": str(run_path),
        "destination": str(final_root),
        "operation_count": len(files),
        "explicit_directory_count": len(directories),
        "plan_id": preflight.plan_id,
        "run_id": preflight.run_id,
        "status": "materialized",
        "total_bytes": preflight.total_bytes,
        "cloned_file_count": cloned_file_count,
        "cloned_byte_count": cloned_byte_count,
        "streamed_file_count": streamed_file_count,
        "streamed_byte_count": streamed_byte_count,
    }
