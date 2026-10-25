"""Sparse persistence for RenameProtection v3.

The finalized Plan remains the execution authority.  This module intentionally
stores no operation manifest, ordinary PLACE success rows, dependency graph, or
staging/quarantine state.  The v3 executor can reconstruct its deterministic
schedule from the Plan and uses this small state only for resume boundaries and
exceptional recovery. Namespace-reuse receipts protect only exceptional vacators.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

EXECUTOR_KIND = "rename-protection"
EXECUTOR_VERSION = 3


class RenameProtectionV3Error(Exception):
    """The persisted v3 state contradicts the admitted execution context."""


@dataclass(frozen=True)
class AttemptBinding:
    attempt_id: str
    plan_id: str
    plan_schema_version: int
    layout_revision: int
    finalized_projection_fingerprint: str


@dataclass(frozen=True)
class Failure:
    ordinal: int
    operation_key: str
    error_class: str
    errno: int | None
    message: str


@dataclass(frozen=True)
class ActivePivot:
    pivot_key: str
    source_relative_path: str
    temporary_relative_path: str
    destination_relative_path: str
    phase: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rename_protection_v3_attempts (
  attempt_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  plan_schema_version INTEGER NOT NULL,
  layout_revision INTEGER NOT NULL,
  finalized_projection_fingerprint TEXT NOT NULL,
  executor_kind TEXT NOT NULL CHECK (executor_kind = 'rename-protection'),
  executor_version INTEGER NOT NULL CHECK (executor_version = 3),
  state TEXT NOT NULL CHECK (state IN (
    'ADMITTED', 'EXECUTING', 'NEEDS_ATTENTION', 'COMPLETE', 'FAILED'
  )),
  high_watermark INTEGER NOT NULL DEFAULT -1 CHECK (high_watermark >= -1),
  checkpointed_at TEXT,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rename_v3_attempt_plan_lookup
  ON rename_protection_v3_attempts(plan_id, created_at, attempt_id);

CREATE TABLE IF NOT EXISTS rename_protection_v3_failures (
  attempt_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  operation_key TEXT NOT NULL,
  error_class TEXT NOT NULL,
  errno INTEGER,
  message TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (attempt_id, ordinal),
  FOREIGN KEY (attempt_id) REFERENCES rename_protection_v3_attempts(attempt_id)
);

CREATE TABLE IF NOT EXISTS rename_protection_v3_active_pivots (
  attempt_id TEXT PRIMARY KEY,
  pivot_key TEXT NOT NULL,
  source_relative_path TEXT NOT NULL,
  temporary_relative_path TEXT NOT NULL,
  destination_relative_path TEXT NOT NULL,
  phase TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (attempt_id) REFERENCES rename_protection_v3_attempts(attempt_id)
);

CREATE TABLE IF NOT EXISTS rename_protection_v3_vacated (
  attempt_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  PRIMARY KEY (attempt_id, ordinal),
  FOREIGN KEY (attempt_id) REFERENCES rename_protection_v3_attempts(attempt_id)
);
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Install only the additive, sparse v3 persistence tables."""
    connection.executescript(_SCHEMA)


def bind_attempt(connection: sqlite3.Connection, binding: AttemptBinding) -> None:
    """Persist, or verify, the immutable v3 binding to a finalized Plan."""
    ensure_schema(connection)
    existing = connection.execute(
        "SELECT attempt_id, plan_id, plan_schema_version, layout_revision, "
        "finalized_projection_fingerprint, executor_kind, executor_version "
        "FROM rename_protection_v3_attempts WHERE attempt_id=?",
        (binding.attempt_id,),
    ).fetchone()
    if existing is not None:
        observed = tuple(existing)
        expected = (
            binding.attempt_id,
            binding.plan_id,
            binding.plan_schema_version,
            binding.layout_revision,
            binding.finalized_projection_fingerprint,
            EXECUTOR_KIND,
            EXECUTOR_VERSION,
        )
        if observed != expected:
            raise RenameProtectionV3Error(
                "RenameProtection v3 attempt binding contradicts the admitted Plan"
            )
        return
    with connection:
        connection.execute(
            "INSERT INTO rename_protection_v3_attempts "
            "(attempt_id, plan_id, plan_schema_version, layout_revision, "
            "finalized_projection_fingerprint, executor_kind, executor_version, "
            "state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,'ADMITTED',datetime('now'),datetime('now'))",
            (
                binding.attempt_id,
                binding.plan_id,
                binding.plan_schema_version,
                binding.layout_revision,
                binding.finalized_projection_fingerprint,
                EXECUTOR_KIND,
                EXECUTOR_VERSION,
            ),
        )


def load_attempt_binding(
    connection: sqlite3.Connection, attempt_id: str
) -> AttemptBinding | None:
    row = connection.execute(
        "SELECT attempt_id, plan_id, plan_schema_version, layout_revision, "
        "finalized_projection_fingerprint "
        "FROM rename_protection_v3_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    return None if row is None else AttemptBinding(*tuple(row))


def checkpoint(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    high_watermark: int,
    metrics: dict[str, int | float],
) -> None:
    """Atomically advance the coarse cursor and replace coarse v3 metrics."""
    if high_watermark < -1:
        raise ValueError("high_watermark must be at least -1")
    encoded_metrics = json.dumps(metrics, sort_keys=True, separators=(",", ":"))
    with connection:
        cursor = connection.execute(
            "UPDATE rename_protection_v3_attempts SET "
            "high_watermark=?, metrics_json=?, checkpointed_at=datetime('now'), "
            "updated_at=datetime('now') "
            "WHERE attempt_id=? AND high_watermark <= ?",
            (high_watermark, encoded_metrics, attempt_id, high_watermark),
        )
        if cursor.rowcount != 1:
            raise RenameProtectionV3Error(
                "RenameProtection v3 checkpoint is unknown or moves backward"
            )


def load_attempt_progress(
    connection: sqlite3.Connection, attempt_id: str
) -> tuple[int, dict[str, int | float]] | None:
    row = connection.execute(
        "SELECT high_watermark, metrics_json FROM rename_protection_v3_attempts "
        "WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), json.loads(str(row[1]))


def record_failure(
    connection: sqlite3.Connection, attempt_id: str, failure: Failure
) -> None:
    """Upsert one real syscall/reconciliation failure; no derived blocks are stored."""
    with connection:
        connection.execute(
            "INSERT INTO rename_protection_v3_failures "
            "(attempt_id, ordinal, operation_key, error_class, errno, message, "
            "observed_at, updated_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'),datetime('now')) "
            "ON CONFLICT(attempt_id, ordinal) DO UPDATE SET "
            "operation_key=excluded.operation_key, error_class=excluded.error_class, "
            "errno=excluded.errno, message=excluded.message, updated_at=datetime('now')",
            (
                attempt_id,
                failure.ordinal,
                failure.operation_key,
                failure.error_class,
                failure.errno,
                failure.message,
            ),
        )


def resolve_failure(
    connection: sqlite3.Connection, attempt_id: str, ordinal: int
) -> None:
    """Remove an actual failure once its operation has genuinely succeeded."""
    with connection:
        connection.execute(
            "DELETE FROM rename_protection_v3_failures WHERE attempt_id=? AND ordinal=?",
            (attempt_id, ordinal),
        )


def load_failures(connection: sqlite3.Connection, attempt_id: str) -> list[Failure]:
    rows = connection.execute(
        "SELECT ordinal, operation_key, error_class, errno, message "
        "FROM rename_protection_v3_failures WHERE attempt_id=? ORDER BY ordinal",
        (attempt_id,),
    ).fetchall()
    return [Failure(*tuple(row)) for row in rows]


def set_active_pivot(
    connection: sqlite3.Connection, attempt_id: str, pivot: ActivePivot
) -> None:
    """Persist the sole pathname of a rare, currently active cycle pivot."""
    with connection:
        connection.execute(
            "INSERT INTO rename_protection_v3_active_pivots "
            "(attempt_id, pivot_key, source_relative_path, temporary_relative_path, "
            "destination_relative_path, phase, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'),datetime('now')) "
            "ON CONFLICT(attempt_id) DO UPDATE SET pivot_key=excluded.pivot_key, "
            "source_relative_path=excluded.source_relative_path, "
            "temporary_relative_path=excluded.temporary_relative_path, "
            "destination_relative_path=excluded.destination_relative_path, "
            "phase=excluded.phase, updated_at=datetime('now')",
            (
                attempt_id,
                pivot.pivot_key,
                pivot.source_relative_path,
                pivot.temporary_relative_path,
                pivot.destination_relative_path,
                pivot.phase,
            ),
        )


def clear_active_pivot(connection: sqlite3.Connection, attempt_id: str) -> None:
    """Clear the exceptional pivot state once the cycle transition is complete."""
    with connection:
        connection.execute(
            "DELETE FROM rename_protection_v3_active_pivots WHERE attempt_id=?",
            (attempt_id,),
        )


def load_active_pivot(
    connection: sqlite3.Connection, attempt_id: str
) -> ActivePivot | None:
    row = connection.execute(
        "SELECT pivot_key, source_relative_path, temporary_relative_path, "
        "destination_relative_path, phase "
        "FROM rename_protection_v3_active_pivots WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    return None if row is None else ActivePivot(*tuple(row))


def load_vacated(connection: sqlite3.Connection, attempt_id: str) -> set[int]:
    """Exceptional receipts prevent replay after a source pathname is reused."""
    return {
        int(row[0])
        for row in connection.execute(
            "SELECT ordinal FROM rename_protection_v3_vacated WHERE attempt_id=?",
            (attempt_id,),
        )
    }


def record_vacated(
    connection: sqlite3.Connection,
    attempt_id: str,
    ordinals: list[int],
    *,
    finish_pivot: bool = False,
) -> None:
    """Commit before releasing reuse; retire a completed pivot atomically."""
    # Run Workspace connections use autocommit: a connection context manager
    # alone would commit each receipt separately and could strand a pivot.
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            "INSERT OR IGNORE INTO rename_protection_v3_vacated VALUES (?,?)",
            [(attempt_id, ordinal) for ordinal in ordinals],
        )
        connection.executemany(
            "DELETE FROM rename_protection_v3_failures WHERE attempt_id=? AND ordinal=?",
            [(attempt_id, ordinal) for ordinal in ordinals],
        )
        if finish_pivot:
            connection.execute(
                "DELETE FROM rename_protection_v3_active_pivots WHERE attempt_id=?",
                (attempt_id,),
            )
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
