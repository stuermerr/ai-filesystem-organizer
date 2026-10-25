"""Observational, non-authorizing progress for long-running workflows."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime

PROGRESS_SCHEMA_VERSION = 1


class _Unset:
    pass


_UNSET = _Unset()


def ensure_workflow_progress_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflow_progress (
          operation_id TEXT PRIMARY KEY,
          progress_version INTEGER NOT NULL CHECK (progress_version = 1),
          operation_kind TEXT NOT NULL CHECK (operation_kind IN (
            'plan-derivation', 'layout-apply', 'materialization')),
          run_id TEXT NOT NULL,
          plan_id TEXT,
          attempt_id TEXT,
          phase TEXT NOT NULL,
          completed_count INTEGER NOT NULL CHECK (completed_count >= 0),
          total_count INTEGER CHECK (total_count IS NULL OR total_count >= 0),
          unit TEXT NOT NULL,
          state TEXT NOT NULL CHECK (state IN (
            'active', 'complete', 'failed', 'needs-attention')),
          metrics_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS workflow_progress_status_lookup
          ON workflow_progress(run_id, state, updated_at, operation_id);
        CREATE INDEX IF NOT EXISTS workflow_progress_plan_lookup
          ON workflow_progress(plan_id, updated_at, operation_id);
        """
    )
    table_sql = str(
        connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='workflow_progress'"
        ).fetchone()[0]
    )
    if "needs-attention" not in table_sql:
        connection.executescript(
            """
            CREATE TABLE workflow_progress_v2 (
              operation_id TEXT PRIMARY KEY,
              progress_version INTEGER NOT NULL CHECK (progress_version = 1),
              operation_kind TEXT NOT NULL CHECK (operation_kind IN (
                'plan-derivation', 'layout-apply', 'materialization')),
              run_id TEXT NOT NULL,
              plan_id TEXT,
              attempt_id TEXT,
              phase TEXT NOT NULL,
              completed_count INTEGER NOT NULL CHECK (completed_count >= 0),
              total_count INTEGER CHECK (total_count IS NULL OR total_count >= 0),
              unit TEXT NOT NULL,
              state TEXT NOT NULL CHECK (state IN (
                'active', 'complete', 'failed', 'needs-attention')),
              metrics_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO workflow_progress_v2 SELECT * FROM workflow_progress;
            DROP TABLE workflow_progress;
            ALTER TABLE workflow_progress_v2 RENAME TO workflow_progress;
            CREATE INDEX workflow_progress_status_lookup
              ON workflow_progress(run_id, state, updated_at, operation_id);
            CREATE INDEX workflow_progress_plan_lookup
              ON workflow_progress(plan_id, updated_at, operation_id);
            """
        )


class WorkflowProgress:
    """Persist phase transitions and throttled counters outside authority state."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        operation_kind: str,
        run_id: str,
        *,
        plan_id: str | None = None,
        attempt_id: str | None = None,
        phase: str = "preparing",
        total_count: int | None = None,
        unit: str = "entries",
    ) -> None:
        ensure_workflow_progress_schema(connection)
        self.connection = connection
        self.operation_id = str(uuid.uuid4())
        self.operation_kind = operation_kind
        self.run_id = run_id
        self.plan_id = plan_id
        self.attempt_id = attempt_id
        self.phase_name = phase
        self.completed_count = 0
        self.total_count = total_count
        self.unit = unit
        self.metrics: dict[str, object] = {}
        self._last_persisted_count = 0
        self._last_persisted_at = 0.0
        self._persist("active")

    def _persist(self, state: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO workflow_progress VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(operation_id) DO UPDATE SET plan_id=excluded.plan_id, "
            "attempt_id=excluded.attempt_id, phase=excluded.phase, "
            "completed_count=excluded.completed_count, total_count=excluded.total_count, "
            "unit=excluded.unit, state=excluded.state, metrics_json=excluded.metrics_json, "
            "updated_at=excluded.updated_at",
            (
                self.operation_id,
                PROGRESS_SCHEMA_VERSION,
                self.operation_kind,
                self.run_id,
                self.plan_id,
                self.attempt_id,
                self.phase_name,
                self.completed_count,
                self.total_count,
                self.unit,
                state,
                json.dumps(self.metrics, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )
        self.connection.commit()
        self._last_persisted_count = self.completed_count
        self._last_persisted_at = time.monotonic()

    def phase(
        self,
        phase: str,
        *,
        completed_count: int = 0,
        total_count: int | None | _Unset = _UNSET,
        unit: str | None = None,
        metrics: dict[str, object] | None = None,
    ) -> None:
        self.phase_name = phase
        self.completed_count = completed_count
        if not isinstance(total_count, _Unset):
            self.total_count = None if total_count is None else int(total_count)
        if unit is not None:
            self.unit = unit
        if metrics is not None:
            self.metrics = dict(metrics)
        self._persist("active")

    def update(
        self, completed_count: int, *, metrics: dict[str, object] | None = None
    ) -> None:
        self.completed_count = completed_count
        if metrics is not None:
            self.metrics = dict(metrics)
        count_delta = self.completed_count - self._last_persisted_count
        if count_delta >= 256 or time.monotonic() - self._last_persisted_at >= 2.0:
            self._persist("active")

    def complete(self, *, metrics: dict[str, object] | None = None) -> None:
        if self.total_count is not None:
            self.completed_count = self.total_count
        if metrics is not None:
            self.metrics = dict(metrics)
        self.phase_name = "complete"
        self._persist("complete")

    def fail(self, detail: str) -> None:
        self.metrics = {**self.metrics, "failure": detail}
        self.phase_name = "failed"
        self._persist("failed")

    def attention(self, *, metrics: dict[str, object] | None = None) -> None:
        if metrics is not None:
            self.metrics = dict(metrics)
        self.phase_name = "needs-attention"
        self._persist("needs-attention")


def workflow_progress_payload(connection: sqlite3.Connection) -> dict[str, object]:
    rows = connection.execute(
        "SELECT * FROM workflow_progress ORDER BY updated_at DESC, operation_id DESC"
    ).fetchall()

    def payload(row: sqlite3.Row) -> dict[str, object]:
        total = None if row["total_count"] is None else int(row["total_count"])
        completed = int(row["completed_count"])
        return {
            "operation_id": str(row["operation_id"]),
            "version": int(row["progress_version"]),
            "operation_kind": str(row["operation_kind"]),
            "run_id": str(row["run_id"]),
            "plan_id": row["plan_id"],
            "attempt_id": row["attempt_id"],
            "phase": str(row["phase"]),
            "completed_count": completed,
            "total_count": total,
            "unit": str(row["unit"]),
            "state": str(row["state"]),
            "metrics": json.loads(str(row["metrics_json"])),
            "updated_at": str(row["updated_at"]),
            "percentage": (
                None if total in (None, 0) else round(completed * 100 / total, 2)
            ),
        }

    active = [payload(row) for row in rows if row["state"] == "active"]
    recent = [payload(row) for row in rows if row["state"] != "active"][:1]
    return {"active": active, "recent": recent}
