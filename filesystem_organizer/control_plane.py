from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from typing import cast

from .consolidation_plan import (
    PLAN_SCHEMA_VERSION,
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_FINALIZED,
)
from .materialization import (
    EVENT_MATERIALIZATION_COMPLETE,
    EVENT_MATERIALIZATION_FAILED,
    PARTIAL_OWNER_FILE,
    _v3_operations,
)
from .rename_protection import OWNER_FILE as RENAME_OWNER_FILE
from .rename_protection_v2 import OWNER_FILE as RENAME_V2_OWNER_FILE
from .run_workspace import (
    DATABASE_NAME,
    SCHEMA_VERSION,
    read_only_transaction,
    table_exists,
)
from .workflow_progress import workflow_progress_payload
from .workspace_cleanup import WorkspaceCleanupError, read_completion_record

RUN_STATUSES = {"scanning", "hashing", "structural-analysis", "complete"}


class ControlPlaneError(Exception):
    """A safe, user-facing refusal of an agent control-plane operation."""


def _v3_blocked_count(
    connection: sqlite3.Connection, plan_id: str, failed_ordinals: set[int]
) -> int:
    """Derive, rather than persist, dependency blocking for v3 status."""
    operations = _v3_operations(connection, plan_id)
    vacator = {
        operation.source: operation.ordinal
        for operation in operations
        if operation.source
    }
    dependents: dict[int, list[int]] = {
        operation.ordinal: [] for operation in operations
    }
    for operation in operations:
        if operation.destination in vacator:
            predecessor = vacator[operation.destination]
            if predecessor != operation.ordinal:
                dependents[predecessor].append(operation.ordinal)
    rmdir_vacators = {
        str(operation.source): operation.ordinal
        for operation in operations
        if operation.action == "RMDIR"
    }
    for operation in operations:
        if operation.source is None:
            continue
        parent = Path(operation.source).parent
        while parent != Path("."):
            rmdir_predecessor = rmdir_vacators.get(parent.as_posix())
            if rmdir_predecessor is not None and rmdir_predecessor != operation.ordinal:
                dependents[operation.ordinal].append(rmdir_predecessor)
            parent = parent.parent
    blocked: set[int] = set()
    frontier = list(failed_ordinals)
    while frontier:
        failed = frontier.pop()
        for dependent in dependents.get(failed, []):
            if dependent not in blocked:
                blocked.add(dependent)
                frontier.append(dependent)
    return len(blocked)


def _attempt_recovery_details(
    connection: sqlite3.Connection, attempt: sqlite3.Row
) -> dict[str, object]:
    manifest_present = all(
        attempt[field] is not None
        for field in (
            "mode",
            "selected_backup_root",
            "canonical_destination",
            "staging_relative_path",
        )
    )
    staging_path: Path | None = None
    if manifest_present:
        canonical_destination = Path(str(attempt["canonical_destination"]))
        staging_base = (
            Path(str(attempt["selected_backup_root"]))
            if attempt["mode"] == "in-place"
            else canonical_destination.parent
        )
        staging_path = staging_base / str(attempt["staging_relative_path"])
    expected_identity = (attempt["staging_root_dev"], attempt["staging_root_ino"])
    try:
        observed = staging_path.lstat() if staging_path is not None else None
    except OSError:
        observed = None
    if attempt["rename_v2_policy_version"] is not None:
        ownership_proven = False
        if observed is not None and staging_path is not None:
            try:
                owner = json.loads(
                    (staging_path / RENAME_V2_OWNER_FILE).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                pass
            else:
                ownership_proven = bool(
                    stat.S_ISDIR(observed.st_mode)
                    and not stat.S_ISLNK(observed.st_mode)
                    and owner.get("attempt_id") == attempt["attempt_id"]
                    and owner.get("plan_id") == attempt["plan_id"]
                    and owner.get("secret") == attempt["rename_v2_owner_secret"]
                    and owner.get("operation_set_digest")
                    == attempt["rename_v2_operation_set_digest"]
                    and owner.get("executor_version") == 2
                )
        operation_counts = {
            str(row["state"]): int(row["operation_count"])
            for row in connection.execute(
                "SELECT state, COUNT(*) AS operation_count "
                "FROM rename_protection_v2_operations WHERE attempt_id=? GROUP BY state",
                (attempt["attempt_id"],),
            )
        }
        attention = [
            {
                "operation_id": str(row["operation_id"]),
                "error_class": str(row["error_class"]),
                "errno": row["errno"],
                "message": str(row["message"]),
                "phase": str(row["phase"]),
                "retry_count": int(row["retry_count"]),
                "locations": json.loads(str(row["locations_json"])),
                "reason": str(row["reason"]),
            }
            for row in connection.execute(
                "SELECT * FROM rename_protection_v2_attention "
                "WHERE attempt_id=? ORDER BY operation_id",
                (attempt["attempt_id"],),
            )
        ]
        attempt_state = str(attempt["rename_v2_state"])
        resume_candidate = ownership_proven and attempt_state in {
            "EXECUTING",
            "NEEDS_ATTENTION",
        }
        return {
            "attempt_id": str(attempt["attempt_id"]),
            "attempt_state": attempt_state,
            "executor_kind": "rename-protection",
            "executor_version": 2,
            "failure_classification": attempt["failure_classification"],
            "failure_detail": attempt["failure_detail"],
            "staging_path": str(staging_path),
            "staging_present": observed is not None,
            "staging_ownership": "proven" if ownership_proven else "not-proven",
            "staging_identity": "marker-bound" if ownership_proven else "not-proven",
            "staging_identity_revalidation_required": False,
            "resume_candidate": resume_candidate,
            "resume_strategy": (
                "reconcile durable component operations; skip completed work"
                if resume_candidate
                else None
            ),
            "operation_counts": operation_counts,
            "attention": attention,
            "blocked_dependencies": operation_counts.get("BLOCKED_BY_DEPENDENCY", 0),
            "payload_reads": json.loads(str(attempt["rename_v2_metrics_json"])),
        }
    if attempt["rename_executor_kind"] == "rename-protection":
        ownership_proven = False
        if observed is not None and staging_path is not None:
            try:
                owner = json.loads(
                    (staging_path / RENAME_OWNER_FILE).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                pass
            else:
                ownership_proven = bool(
                    stat.S_ISDIR(observed.st_mode)
                    and not stat.S_ISLNK(observed.st_mode)
                    and owner.get("attempt_id") == attempt["attempt_id"]
                    and owner.get("plan_id") == attempt["plan_id"]
                    and owner.get("secret") == attempt["rename_owner_secret"]
                    and owner.get("operation_set_digest")
                    == attempt["rename_operation_set_digest"]
                    and owner.get("executor_kind") == "rename-protection"
                )
        resume_candidate = ownership_proven and attempt["state"] in {
            "ADMITTED",
            "STAGING_STARTED",
            "STAGING_DURABLE",
            "PUBLISHED",
        }
        return {
            "attempt_id": str(attempt["attempt_id"]),
            "attempt_state": str(attempt["state"]),
            "executor_kind": "rename-protection",
            "executor_version": int(attempt["rename_executor_version"]),
            "failure_classification": attempt["failure_classification"],
            "failure_detail": attempt["failure_detail"],
            "staging_path": str(staging_path),
            "staging_present": observed is not None,
            "staging_ownership": "proven" if ownership_proven else "not-proven",
            "staging_identity": "marker-bound" if ownership_proven else "not-proven",
            "staging_identity_revalidation_required": False,
            "resume_candidate": resume_candidate,
            "resume_strategy": "forward-reconcile-source-staged-final"
            if resume_candidate
            else None,
        }
    ownership_proven = bool(
        observed is not None
        and stat.S_ISDIR(observed.st_mode)
        and not stat.S_ISLNK(observed.st_mode)
        and None not in expected_identity
        and (observed.st_dev, observed.st_ino)
        == (int(expected_identity[0]), int(expected_identity[1]))
    )
    retryable_failure = attempt["state"] == "FAILED" and attempt[
        "failure_classification"
    ] in {"retryable-environment", "unsupported-capability"}
    durable_recovery = attempt["state"] == "STAGING_DURABLE" or retryable_failure
    identity_revalidation_required = False
    if (
        observed is not None
        and stat.S_ISDIR(observed.st_mode)
        and not stat.S_ISLNK(observed.st_mode)
        and None not in expected_identity
        and observed.st_dev == int(expected_identity[0])
        and observed.st_ino != int(expected_identity[1])
        and attempt["staging_owner_attempt_id"] == attempt["attempt_id"]
        and attempt["finalized_projection_fingerprint"] is not None
        and staging_path is not None
    ):
        owner_file = staging_path / PARTIAL_OWNER_FILE
        owner_marker_absent = False
        owner_marker_matches = False
        try:
            owner_stat = owner_file.lstat()
        except FileNotFoundError:
            owner_marker_absent = True
        except OSError:
            pass
        else:
            if stat.S_ISREG(owner_stat.st_mode) and not stat.S_ISLNK(
                owner_stat.st_mode
            ):
                try:
                    owner_payload = owner_file.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    pass
                else:
                    expected_payload = (
                        "\n".join(
                            (
                                str(attempt["attempt_id"]),
                                str(attempt["canonical_destination"]),
                                str(attempt["finalized_projection_fingerprint"]),
                            )
                        )
                        + "\n"
                    )
                    owner_marker_matches = owner_payload == expected_payload
        identity_revalidation_required = owner_marker_matches or (
            durable_recovery and owner_marker_absent
        )
    resume_candidate = (ownership_proven or identity_revalidation_required) and (
        attempt["state"] in {"STAGING_STARTED", "STAGING_DURABLE"} or retryable_failure
    )
    return {
        "attempt_id": str(attempt["attempt_id"]),
        "attempt_state": str(attempt["state"]),
        "failure_classification": attempt["failure_classification"],
        "failure_detail": attempt["failure_detail"],
        "staging_path": str(staging_path) if staging_path is not None else None,
        "staging_present": observed is not None,
        "staging_ownership": (
            "proven"
            if ownership_proven
            else (
                "revalidation-required"
                if identity_revalidation_required
                else "not-proven"
            )
        ),
        "staging_identity": (
            "matches"
            if ownership_proven
            else (
                "changed-on-same-device"
                if identity_revalidation_required
                else "not-proven"
            )
        ),
        "staging_identity_revalidation_required": identity_revalidation_required,
        "resume_candidate": resume_candidate,
        "resume_strategy": (
            (
                "reuse-durable-completed-files-and-recopy-incomplete"
                if attempt["state"] == "STAGING_STARTED"
                else "reuse-after-source-and-staging-integrity-revalidation"
            )
            if resume_candidate
            else None
        ),
    }


def _safe_database_path(run_path: Path) -> Path:
    database_path = run_path / DATABASE_NAME
    if database_path.is_symlink() or not database_path.is_file():
        raise ControlPlaneError(
            f"Analysis Run directory is missing a safe {DATABASE_NAME}: {run_path}"
        )
    return database_path


def _validated_run_row(connection: sqlite3.Connection, run_path: Path) -> sqlite3.Row:
    rows = connection.execute("SELECT * FROM analysis_runs").fetchall()
    if len(rows) != 1:
        raise ControlPlaneError(
            f"Analysis Run directory must contain exactly one run identity: {run_path}"
        )
    row = rows[0]
    schema_version = int(row["schema_version"])
    if schema_version != SCHEMA_VERSION:
        raise ControlPlaneError(
            f"Unsupported Analysis Run schema version {schema_version}: {run_path}"
        )
    status = str(row["status"])
    if status not in RUN_STATUSES:
        raise ControlPlaneError(
            f"Unsupported Analysis Run status {status!r}: {run_path}"
        )
    return cast(sqlite3.Row, row)


def _run_summary(run_path: Path) -> dict[str, object]:
    try:
        completion = read_completion_record(run_path)
    except WorkspaceCleanupError as error:
        raise ControlPlaneError(str(error)) from error
    if completion is not None:
        status = {**cast(dict[str, object], completion["status"])}
        status["analysis_run"] = str(run_path)
        status["run_workspace_root"] = str(run_path.parent.parent)
        return {
            field: status[field]
            for field in (
                "analysis_run",
                "completed_at",
                "run_id",
                "run_workspace_root",
                "selected_backup_root",
                "snapshot_id",
                "started_at",
                "status",
            )
        }
    database_path = _safe_database_path(run_path)

    with read_only_transaction(database_path) as connection:
        row = _validated_run_row(connection, run_path)
        return {
            "analysis_run": str(run_path),
            "completed_at": row["completed_at"],
            "run_id": str(row["run_id"]),
            "run_workspace_root": str(run_path.parent.parent),
            "selected_backup_root": str(row["selected_backup_root"]),
            "snapshot_id": str(row["snapshot_id"]),
            "started_at": str(row["started_at"]),
            "status": str(row["status"]),
        }


def _plan_summaries(
    connection: sqlite3.Connection, run_id: str, run_path: Path
) -> list[dict[str, object]]:
    if table_exists(connection, "consolidation_plans"):
        rows = connection.execute(
            "SELECT * FROM consolidation_plans WHERE run_id = ? "
            "ORDER BY created_at, plan_id",
            (run_id,),
        ).fetchall()
    else:
        return []

    if table_exists(connection, "materialization_events"):
        event_rows = connection.execute(
            "SELECT plan_id, event FROM materialization_events ORDER BY plan_id, seq"
        ).fetchall()
    else:
        event_rows = []
    events_by_plan: dict[str, list[str]] = {}
    for event_row in event_rows:
        events_by_plan.setdefault(str(event_row["plan_id"]), []).append(
            str(event_row["event"])
        )
    v3_attempts: dict[str, sqlite3.Row] = {}
    if table_exists(connection, "rename_protection_v3_attempts"):
        for attempt in connection.execute(
            "SELECT * FROM rename_protection_v3_attempts "
            "ORDER BY created_at DESC, attempt_id DESC"
        ):
            v3_attempts.setdefault(str(attempt["plan_id"]), attempt)
    if table_exists(connection, "materialization_attempts"):
        rename_columns = (
            "rename.executor_kind AS rename_executor_kind, "
            "rename.executor_version AS rename_executor_version, "
            "rename.operation_set_digest AS rename_operation_set_digest, "
            "rename.owner_secret AS rename_owner_secret, "
            if table_exists(connection, "rename_protection_manifests")
            else "NULL AS rename_executor_kind, NULL AS rename_executor_version, "
            "NULL AS rename_operation_set_digest, NULL AS rename_owner_secret, "
        )
        rename_join = (
            "LEFT JOIN rename_protection_manifests AS rename USING (attempt_id) "
            if table_exists(connection, "rename_protection_manifests")
            else ""
        )
        rename_v2_columns = (
            "rename_v2.policy_version AS rename_v2_policy_version, "
            "rename_v2.state AS rename_v2_state, "
            "rename_v2.metrics_json AS rename_v2_metrics_json, "
            "rename_v2_manifest.operation_set_digest AS rename_v2_operation_set_digest, "
            "rename_v2_manifest.owner_secret AS rename_v2_owner_secret "
            if table_exists(connection, "rename_protection_v2_attempts")
            and table_exists(connection, "rename_protection_v2_manifests")
            else "NULL AS rename_v2_policy_version, NULL AS rename_v2_state, "
            "NULL AS rename_v2_metrics_json, NULL AS rename_v2_operation_set_digest, "
            "NULL AS rename_v2_owner_secret "
        )
        rename_v2_join = (
            "LEFT JOIN rename_protection_v2_attempts AS rename_v2 USING (attempt_id) "
            "LEFT JOIN rename_protection_v2_manifests AS rename_v2_manifest USING (attempt_id) "
            if table_exists(connection, "rename_protection_v2_attempts")
            and table_exists(connection, "rename_protection_v2_manifests")
            else ""
        )
        attempt_query = (
            "SELECT attempts.plan_id, attempts.attempt_id, attempts.state, "
            "attempts.failure_classification, attempts.failure_detail, "
            "manifests.mode, manifests.selected_backup_root, "
            "manifests.canonical_destination, manifests.staging_relative_path, "
            "manifests.staging_root_dev, manifests.staging_root_ino, "
            "manifests.staging_owner_attempt_id, "
            "manifests.finalized_projection_fingerprint, "
            + rename_columns
            + rename_v2_columns
            + "FROM materialization_attempts AS attempts "
            "LEFT JOIN execution_manifests AS manifests USING (attempt_id) "
            + rename_join
            + rename_v2_join
            + "ORDER BY attempts.created_at DESC, attempts.attempt_id DESC"
        )
        attempt_rows = connection.execute(attempt_query).fetchall()
    else:
        attempt_rows = []
    latest_attempts = {
        str(attempt["plan_id"]): attempt for attempt in reversed(attempt_rows)
    }

    summaries: list[dict[str, object]] = []
    for row in rows:
        schema_version = int(row["schema_version"])
        if schema_version != PLAN_SCHEMA_VERSION:
            raise ControlPlaneError(
                "Unsupported Consolidation Plan schema version "
                f"{schema_version}: {run_path}"
            )
        status = str(row["status"])
        if status not in {PLAN_STATUS_DRAFT, PLAN_STATUS_FINALIZED}:
            raise ControlPlaneError(
                f"Unsupported Consolidation Plan status {status!r}: {run_path}"
            )
        plan_id = str(row["plan_id"])
        events = events_by_plan.get(plan_id, [])
        latest_attempt = latest_attempts.get(plan_id)
        v3_attempt = v3_attempts.get(plan_id)
        attempt_state: str | None
        recovery: dict[str, object] | None
        if v3_attempt is not None:
            failures = [
                {
                    "ordinal": int(failure["ordinal"]),
                    "operation_key": str(failure["operation_key"]),
                    "error_class": str(failure["error_class"]),
                    "errno": failure["errno"],
                    "message": str(failure["message"]),
                }
                for failure in connection.execute(
                    "SELECT ordinal, operation_key, error_class, errno, message "
                    "FROM rename_protection_v3_failures WHERE attempt_id=? "
                    "ORDER BY ordinal",
                    (v3_attempt["attempt_id"],),
                )
            ]
            pivot = connection.execute(
                "SELECT pivot_key, source_relative_path, temporary_relative_path, "
                "destination_relative_path, phase FROM rename_protection_v3_active_pivots "
                "WHERE attempt_id=?",
                (v3_attempt["attempt_id"],),
            ).fetchone()
            metrics = json.loads(str(v3_attempt["metrics_json"]))
            blocked = _v3_blocked_count(
                connection, plan_id, {int(failure["ordinal"]) for failure in failures}
            )
            attempt_state = str(v3_attempt["state"])
            materialization_status = (
                "complete"
                if attempt_state == "COMPLETE"
                else "needs-attention"
                if attempt_state in {"NEEDS_ATTENTION", "FAILED"}
                else "in-progress"
            )
            recovery = {
                "attempt_id": str(v3_attempt["attempt_id"]),
                "attempt_state": attempt_state,
                "executor_kind": "rename-protection",
                "executor_version": 3,
                "high_watermark": int(v3_attempt["high_watermark"]),
                "metrics": metrics,
                "attention": failures,
                "blocked_dependencies": blocked,
                "active_pivot": (
                    {
                        "pivot_key": str(pivot["pivot_key"]),
                        "source_relative_path": str(pivot["source_relative_path"]),
                        "temporary_relative_path": str(
                            pivot["temporary_relative_path"]
                        ),
                        "destination_relative_path": str(
                            pivot["destination_relative_path"]
                        ),
                        "phase": str(pivot["phase"]),
                    }
                    if pivot is not None
                    else None
                ),
                "resume_candidate": attempt_state != "COMPLETE",
                "resume_strategy": (
                    "reconstruct deterministic schedule; reconcile sparse failures and active pivot"
                    if attempt_state != "COMPLETE"
                    else None
                ),
            }
        else:
            attempt_state = (
                str(latest_attempt["state"]) if latest_attempt is not None else None
            )
            if attempt_state == "FAILED" or EVENT_MATERIALIZATION_FAILED in events:
                materialization_status = "needs-attention"
            elif (
                attempt_state == "COMPLETE" or EVENT_MATERIALIZATION_COMPLETE in events
            ):
                materialization_status = "complete"
            elif events:
                materialization_status = "in-progress"
            else:
                materialization_status = "not-started"
            recovery = (
                _attempt_recovery_details(connection, latest_attempt)
                if latest_attempt is not None
                else None
            )
        summaries.append(
            {
                "created_at": str(row["created_at"]),
                "finalized_at": row["finalized_at"],
                "intended_destination": str(row["intended_destination"]),
                "materialization_status": materialization_status,
                "plan_id": plan_id,
                "recovery": recovery,
                "recovery_available": materialization_status == "in-progress"
                or bool(recovery and recovery["resume_candidate"]),
                "status": status,
            }
        )
    return summaries


def discover_analysis_runs(
    run_workspace_root: Path, *, legacy_output_root: bool
) -> dict[str, object]:
    """Discover Analysis Runs beneath one resolved Run Workspace Root."""
    resolved_root = run_workspace_root.expanduser().resolve(strict=False)
    root_payload: dict[str, object] = {"run_workspace_root": str(resolved_root)}
    if legacy_output_root:
        root_payload["run_output_root"] = str(resolved_root)
    if not resolved_root.exists():
        return {**root_payload, "runs": []}
    if not resolved_root.is_dir():
        raise ControlPlaneError(
            f"Run Workspace Root is not a directory: {resolved_root}"
        )

    runs_directory = resolved_root / "runs"
    if not runs_directory.exists():
        return {**root_payload, "runs": []}
    if runs_directory.is_symlink() or not runs_directory.is_dir():
        raise ControlPlaneError(
            f"Run Workspace Root contains an unsafe runs directory: {runs_directory}"
        )

    summaries: list[dict[str, object]] = []
    for candidate in sorted(runs_directory.iterdir(), key=lambda path: path.name):
        if candidate.is_symlink() or not candidate.is_dir():
            raise ControlPlaneError(
                f"Run Workspace Root contains an unsafe Analysis Run entry: {candidate}"
            )
        summaries.append(_run_summary(candidate.resolve(strict=True)))
    run_ids = [str(summary["run_id"]) for summary in summaries]
    if len(run_ids) != len(set(run_ids)):
        raise ControlPlaneError(
            f"Run Workspace Root contains ambiguous duplicate run identities: {resolved_root}"
        )
    summaries.sort(key=lambda run: str(run["analysis_run"]))
    return {**root_payload, "runs": summaries}


def inspect_analysis_run(analysis_run: Path) -> dict[str, object]:
    """Report persisted recovery state for one explicitly identified Analysis Run."""
    try:
        run_path = analysis_run.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise ControlPlaneError(
            f"Analysis Run does not exist: {analysis_run}"
        ) from error
    if not run_path.is_dir():
        raise ControlPlaneError(f"Analysis Run is not a directory: {run_path}")
    try:
        completion = read_completion_record(run_path)
    except WorkspaceCleanupError as error:
        raise ControlPlaneError(str(error)) from error
    if completion is not None:
        status = {**cast(dict[str, object], completion["status"])}
        status["analysis_run"] = str(run_path)
        status["run_workspace_root"] = str(run_path.parent.parent)
        return status
    database_path = _safe_database_path(run_path)

    with read_only_transaction(database_path) as connection:
        row = _validated_run_row(connection, run_path)
        run_id = str(row["run_id"])
        inventory_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM inventory_entries WHERE run_id = ? "
                "AND entry_kind = 'regular-file' AND read_outcome = 'successful'",
                (run_id,),
            ).fetchone()[0]
        )
        skipped_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM skipped_entry_findings WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        plans = _plan_summaries(connection, run_id, run_path)
        workflow_progress = (
            workflow_progress_payload(connection)
            if table_exists(connection, "workflow_progress")
            else {"active": [], "recent": []}
        )
        execution_intent: dict[str, object] | None = None
        if table_exists(connection, "analysis_run_execution_intent"):
            intent = connection.execute(
                "SELECT * FROM analysis_run_execution_intent WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if intent is not None:
                intent_columns = {
                    str(column["name"])
                    for column in connection.execute(
                        "PRAGMA table_info(analysis_run_execution_intent)"
                    )
                }
                execution_intent = {
                    "execution_mode": str(intent["execution_mode"]),
                    "executor_kind": str(intent["executor_kind"]),
                    "executor_version": (
                        int(intent["executor_version"])
                        if "executor_version" in intent_columns
                        else 1
                    ),
                    "admission_version": int(intent["admission_version"]),
                    "admitted_at": str(intent["admitted_at"]),
                }
        return {
            "analysis_run": str(run_path),
            "checkpoint_relative_path": row["checkpoint_relative_path"],
            "completed_at": row["completed_at"],
            "hash_checkpoint_relative_path": row["hash_checkpoint_relative_path"],
            "inventory_count": inventory_count,
            "execution_intent": execution_intent,
            "plans": plans,
            "recovery_available": (
                row["status"] != "complete" and row["writer_lease"] is None
            ),
            "run_id": run_id,
            "run_workspace_root": str(run_path.parent.parent),
            "schema_version": int(row["schema_version"]),
            "selected_backup_root": str(row["selected_backup_root"]),
            "skipped_entry_finding_count": skipped_count,
            "snapshot_id": str(row["snapshot_id"]),
            "started_at": str(row["started_at"]),
            "status": str(row["status"]),
            "writer_active": row["writer_lease"] is not None,
            "workflow_progress": workflow_progress,
        }
