"""One immutable, range-indexed inventory view for Plan derivation."""

from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class InventoryProjectionRecord:
    relative_path: str
    entry_kind: str
    read_outcome: str
    modified_ns: int | None
    algorithm: str | None
    algorithm_version: int | None
    byte_size: int | None
    digest: str | None
    identity_outcome: str | None


class InventoryProjectionIndex:
    """Sorted inventory plus O(log n + descendants) subtree ranges."""

    def __init__(
        self,
        records: list[InventoryProjectionRecord],
        metrics: dict[str, int] | None = None,
    ) -> None:
        self.records = tuple(records)
        self.paths = tuple(record.relative_path for record in records)
        self.metrics = metrics

    @classmethod
    def load(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
        metrics: dict[str, int] | None = None,
        progress_update: Callable[[int], None] | None = None,
    ) -> InventoryProjectionIndex:
        cursor = connection.execute(
            "SELECT inventory.relative_path, inventory.entry_kind, inventory.read_outcome, "
            "inventory.modified_ns, identity.algorithm, identity.algorithm_version, "
            "identity.byte_size, identity.digest, identity.read_outcome AS identity_outcome "
            "FROM inventory_entries AS inventory LEFT JOIN content_identities AS identity "
            "ON identity.run_id = inventory.run_id "
            "AND identity.relative_path = inventory.relative_path "
            "WHERE inventory.run_id = ? ORDER BY inventory.relative_path",
            (run_id,),
        )
        rows: list[sqlite3.Row] = []
        while batch := cursor.fetchmany(1_000):
            rows.extend(batch)
            if progress_update is not None:
                progress_update(len(rows))
        if metrics is not None:
            metrics["global_inventory_loads"] = (
                metrics.get("global_inventory_loads", 0) + 1
            )
            metrics["global_inventory_rows_loaded"] = len(rows)
            metrics.setdefault("descendant_range_lookups", 0)
            metrics.setdefault("descendant_visits", 0)
        return cls(
            [
                InventoryProjectionRecord(
                    relative_path=str(row["relative_path"]),
                    entry_kind=str(row["entry_kind"]),
                    read_outcome=str(row["read_outcome"]),
                    modified_ns=(
                        None if row["modified_ns"] is None else int(row["modified_ns"])
                    ),
                    algorithm=(
                        None if row["algorithm"] is None else str(row["algorithm"])
                    ),
                    algorithm_version=(
                        None
                        if row["algorithm_version"] is None
                        else int(row["algorithm_version"])
                    ),
                    byte_size=(
                        None if row["byte_size"] is None else int(row["byte_size"])
                    ),
                    digest=None if row["digest"] is None else str(row["digest"]),
                    identity_outcome=(
                        None
                        if row["identity_outcome"] is None
                        else str(row["identity_outcome"])
                    ),
                )
                for row in rows
            ],
            metrics,
        )

    def descendants(self, root: str) -> tuple[InventoryProjectionRecord, ...]:
        prefix = "" if root == "." else f"{root}/"
        start = 0 if root == "." else bisect_left(self.paths, prefix)
        end = (
            len(self.paths)
            if root == "."
            else bisect_left(self.paths, f"{prefix}\U0010ffff")
        )
        result = self.records[start:end]
        if self.metrics is not None:
            self.metrics["descendant_range_lookups"] += 1
            self.metrics["descendant_visits"] += len(result)
        return result
