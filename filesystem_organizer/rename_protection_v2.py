"""Metadata-first, component-wise RenameProtection v2 executor.

The executor owns manifest meaning, current-evidence policy, dependency units,
bounded retries, reconciliation, durability checkpoints, and durable outcomes.
Normal execution never opens a file payload.  A payload is read only while
proving an alternate member of a persisted Exact Duplicate Group.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import sqlite3
import stat
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Protocol

from blake3 import blake3

from .content_identity import digest_file
from .linux_filesystem import FilesystemSync, LinuxFilesystemError, rename_no_replace
from .progress import MaterializationTelemetry, ProgressReporter
from .rename_protection import (
    DROP_EXACT_DUPLICATE,
    DROP_EXCLUDED,
    DROP_SKIPPED_EXCLUSION,
    ENSURE_DIRECTORY,
    PLACE,
    REMOVE_EMPTY_SOURCE_DIRECTORY,
    RETAIN_SKIPPED,
    RenameContext,
)
from .rename_protection import (
    RenameOperation as V1Operation,
)
from .rename_protection import (
    derive_operations as derive_v1_operations,
)
from .run_workspace import journal_append
from .workflow_progress import WorkflowProgress

EXECUTOR_KIND = "rename-protection"
EXECUTOR_VERSION = 2
MANIFEST_VERSION = 2
OWNER_VERSION = 2
OWNER_FILE = ".filesystem-organizer-rename-owner.json"
MAX_ATTEMPTS = 3
CHECKPOINT_FILES = 128
CHECKPOINT_BYTES = 1024 * 1024 * 1024
CHECKPOINT_MAX_SECONDS = 30.0

PENDING = "PENDING"
STAGED_DURABLE = "STAGED_DURABLE"
PUBLISHED_DURABLE = "PUBLISHED_DURABLE"
COMPLETE = "COMPLETE"
NEEDS_ATTENTION = "NEEDS_ATTENTION"
BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"

EVENT_MANIFEST_DURABLE = "RENAME_V2_MANIFEST_DURABLE"
EVENT_COMPLETE = "MATERIALIZATION_COMPLETE"
EVENT_NEEDS_ATTENTION = "RENAME_V2_NEEDS_ATTENTION"


class RenameProtectionV2Error(Exception):
    """Global execution trust was lost and the whole invocation must stop."""


class RenameProtectionV2NeedsAttention(RenameProtectionV2Error):
    """Independent durable progress exists, but required work remains."""

    def __init__(self, outcome: RenameOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            f"RenameProtection v2 needs attention: {outcome.attention_count} "
            f"attention, {outcome.blocked_count} dependency-blocked"
        )


@dataclass(frozen=True)
class RenameOperation:
    operation_id: str
    action: str
    source_relative_path: str | None
    output_relative_path: str | None
    expected_type: str
    expected_byte_size: int | None
    expected_mtime_ns: int | None
    algorithm: str | None
    algorithm_version: int | None
    digest: str | None
    staging_relative_path: str | None
    component_id: str


@dataclass(frozen=True)
class RenameOutcome:
    state: str
    operation_counts: dict[str, int]
    attention_count: int
    blocked_count: int
    unresolved_identity_groups: int
    resume_strategy: str


@dataclass(frozen=True)
class Observation:
    location: str
    entry_type: str
    byte_size: int
    mtime_ns: int
    device: int
    inode: int


class FilesystemAdapter(Protocol):
    """The only filesystem seam used by the v2 executor."""

    def observe(self, path: Path, location: str) -> Observation | None: ...

    def rename(self, source: Path, destination: Path) -> None: ...

    def remove(self, path: Path) -> None: ...

    def sync(self) -> float: ...

    def prove(self, path: Path) -> tuple[int, str, int]: ...

    def close(self) -> None: ...


class LinuxFilesystemAdapter:
    """Production Linux/VFAT adapter; it never follows symlinks."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.synchronizer = FilesystemSync(root)

    def observe(self, path: Path, location: str) -> Observation | None:
        try:
            observed = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(observed.st_mode):
            entry_type = "symbolic-link"
        elif stat.S_ISREG(observed.st_mode):
            entry_type = "regular-file"
        elif stat.S_ISDIR(observed.st_mode):
            entry_type = "directory"
        else:
            entry_type = "special-entry"
        return Observation(
            location,
            entry_type,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_dev,
            observed.st_ino,
        )

    def rename(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        rename_no_replace(source, destination)

    def remove(self, path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            # Skipped exclusions may be opaque directories.  Removal is
            # descriptor-relative and no-follow in the v1 primitive.
            from .rename_protection import _remove_no_follow

            _remove_no_follow(path)

    def sync(self) -> float:
        started = time.monotonic()
        self.synchronizer.sync(self.root)
        return time.monotonic() - started

    def prove(self, path: Path) -> tuple[int, str, int]:
        bytes_read = 0

        def count(chunk: bytes) -> None:
            nonlocal bytes_read
            bytes_read += len(chunk)

        size, digest = digest_file(path, on_chunk=count)
        return size, digest, bytes_read

    def close(self) -> None:
        self.synchronizer.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rename_protection_v2_attempts (
  attempt_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  policy_version INTEGER NOT NULL CHECK (policy_version = 2),
  state TEXT NOT NULL CHECK (state IN (
    'ADMITTED', 'EXECUTING', 'NEEDS_ATTENTION', 'COMPLETE', 'FAILED', 'SUPERSEDED'
  )),
  supersedes_attempt_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (attempt_id, plan_id)
    REFERENCES materialization_attempts(attempt_id, plan_id)
);
CREATE TABLE IF NOT EXISTS rename_protection_v2_manifests (
  attempt_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  manifest_version INTEGER NOT NULL CHECK (manifest_version = 2),
  executor_kind TEXT NOT NULL CHECK (executor_kind = 'rename-protection'),
  executor_version INTEGER NOT NULL CHECK (executor_version = 2),
  layout_revision INTEGER NOT NULL,
  finalized_projection_fingerprint TEXT NOT NULL,
  destination TEXT NOT NULL,
  operation_set_digest TEXT NOT NULL,
  staging_relative_path TEXT NOT NULL,
  owner_secret TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (attempt_id) REFERENCES rename_protection_v2_attempts(attempt_id)
);
CREATE TABLE IF NOT EXISTS rename_protection_v2_operations (
  attempt_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  component_id TEXT NOT NULL,
  action TEXT NOT NULL,
  source_relative_path TEXT,
  output_relative_path TEXT,
  expected_type TEXT NOT NULL,
  expected_byte_size INTEGER,
  expected_mtime_ns INTEGER,
  algorithm TEXT,
  algorithm_version INTEGER,
  digest TEXT,
  staging_relative_path TEXT,
  state TEXT NOT NULL CHECK (state IN (
    'PENDING', 'STAGED_DURABLE', 'PUBLISHED_DURABLE', 'COMPLETE',
    'NEEDS_ATTENTION', 'BLOCKED_BY_DEPENDENCY'
  )),
  selected_supplier_operation_id TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (attempt_id, operation_id),
  FOREIGN KEY (attempt_id) REFERENCES rename_protection_v2_manifests(attempt_id)
);
CREATE INDEX IF NOT EXISTS rename_v2_operation_state
  ON rename_protection_v2_operations(attempt_id, state, component_id);
CREATE TABLE IF NOT EXISTS rename_protection_v2_identity_groups (
  attempt_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  algorithm TEXT NOT NULL,
  algorithm_version INTEGER NOT NULL,
  byte_size INTEGER NOT NULL,
  digest TEXT NOT NULL,
  intended_operation_id TEXT NOT NULL,
  planned_output_relative_path TEXT NOT NULL,
  selected_operation_id TEXT NOT NULL,
  candidate_order_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('PENDING', 'RETAINED_DURABLE', 'NEEDS_ATTENTION')),
  PRIMARY KEY (attempt_id, group_id),
  FOREIGN KEY (attempt_id) REFERENCES rename_protection_v2_manifests(attempt_id)
);
CREATE TABLE IF NOT EXISTS rename_protection_v2_dependencies (
  attempt_id TEXT NOT NULL,
  predecessor_operation_id TEXT NOT NULL,
  dependent_operation_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY (attempt_id, predecessor_operation_id, dependent_operation_id, reason),
  FOREIGN KEY (attempt_id) REFERENCES rename_protection_v2_manifests(attempt_id)
);
CREATE TABLE IF NOT EXISTS rename_protection_v2_attention (
  attempt_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  error_class TEXT NOT NULL,
  errno INTEGER,
  message TEXT NOT NULL,
  phase TEXT NOT NULL,
  retry_count INTEGER NOT NULL,
  locations_json TEXT NOT NULL,
  observed_metadata_json TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (attempt_id, operation_id),
  FOREIGN KEY (attempt_id, operation_id)
    REFERENCES rename_protection_v2_operations(attempt_id, operation_id)
);
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Install the additive v2 authority tables without changing v1 constraints."""
    connection.executescript(_SCHEMA)


def staging_name(plan_id: str, attempt_id: str) -> str:
    return f".filesystem-organizer-staging-v2-{plan_id}-{attempt_id}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return blake3(_canonical_json(value).encode()).hexdigest()


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parents = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parents[value]
        if parent != value:
            self.parents[value] = self.find(parent)
        return self.parents[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parents[max(left_root, right_root)] = min(left_root, right_root)


def _operation_payload(
    context: RenameContext, operation: V1Operation
) -> dict[str, object]:
    return {
        "run_id": context.run_id,
        "plan_id": context.plan_id,
        "layout_revision": context.revision,
        "fingerprint": context.fingerprint,
        "executor_kind": EXECUTOR_KIND,
        "executor_version": EXECUTOR_VERSION,
        "action": operation.action,
        "source": operation.source_relative_path,
        "output": operation.output_relative_path,
        "expected_type": operation.expected_type,
        "size": operation.expected_byte_size,
        "mtime_ns": operation.expected_mtime_ns,
        "algorithm": operation.algorithm,
        "algorithm_version": operation.algorithm_version,
        "digest": operation.digest,
    }


def derive_operations(
    connection: sqlite3.Connection, context: RenameContext
) -> tuple[list[RenameOperation], list[tuple[str, str, str]]]:
    """Compile deterministic operations and only concrete namespace dependencies."""
    raw = derive_v1_operations(connection, context)
    # V1 deliberately left a known duplicate in place when its current pathname
    # was also the planned final pathname.  V2 records every group occurrence
    # explicitly so that such an occupant is staged before the intended
    # Canonical Copy is published; no current payload read is needed.
    owned_sources = {
        operation.source_relative_path
        for operation in raw
        if operation.source_relative_path is not None
    }
    retained_identities = {
        (int(operation.expected_byte_size), str(operation.digest)): operation
        for operation in raw
        if operation.action == PLACE
        and operation.expected_byte_size is not None
        and operation.digest is not None
    }
    for row in connection.execute(
        "SELECT identity.relative_path, identity.byte_size, identity.digest, "
        "inventory.modified_ns FROM content_identities AS identity "
        "JOIN inventory_entries AS inventory ON inventory.run_id=identity.run_id "
        "AND inventory.relative_path=identity.relative_path "
        "WHERE identity.run_id=? AND identity.read_outcome='successful' "
        "ORDER BY identity.relative_path",
        (context.run_id,),
    ):
        source = str(row["relative_path"])
        identity = (int(row["byte_size"]), str(row["digest"]))
        retained = retained_identities.get(identity)
        if retained is None or source in owned_sources:
            continue
        raw.append(
            V1Operation(
                operation_id="",
                action=DROP_EXACT_DUPLICATE,
                source_relative_path=source,
                output_relative_path=None,
                expected_type="regular-file",
                expected_byte_size=identity[0],
                expected_mtime_ns=int(row["modified_ns"]),
                algorithm=retained.algorithm,
                algorithm_version=retained.algorithm_version,
                digest=identity[1],
                staging_relative_path="pending",
            )
        )
        owned_sources.add(source)
    payloads = [_operation_payload(context, operation) for operation in raw]
    operation_ids = [_digest(payload) for payload in payloads]
    union = _UnionFind(operation_ids)
    dependencies: set[tuple[str, str, str]] = set()

    by_source: dict[str, list[int]] = defaultdict(list)
    by_output: dict[str, list[int]] = defaultdict(list)
    by_identity: dict[tuple[int, str], list[int]] = defaultdict(list)
    for index, operation in enumerate(raw):
        if operation.source_relative_path is not None:
            by_source[operation.source_relative_path].append(index)
        if operation.output_relative_path is not None:
            by_output[operation.output_relative_path].append(index)
        if operation.digest is not None and operation.expected_byte_size is not None:
            by_identity[(operation.expected_byte_size, operation.digest)].append(index)

    # Identical source/final names and actual source->final occupancy couple work.
    for path in sorted(set(by_source) | set(by_output)):
        indexes = sorted(set(by_source.get(path, []) + by_output.get(path, [])))
        for left, right in pairwise(indexes):
            union.union(operation_ids[left], operation_ids[right])
            predecessor = operation_ids[left]
            dependent = operation_ids[right]
            dependencies.add((predecessor, dependent, f"namespace occupancy: {path}"))
    # An Exact Duplicate Group is a bounded retention unit.
    for indexes in by_identity.values():
        duplicate_indexes = [
            index
            for index in indexes
            if raw[index].action in {PLACE, DROP_EXACT_DUPLICATE}
        ]
        if len(duplicate_indexes) < 2:
            continue
        intended = next(
            (index for index in duplicate_indexes if raw[index].action == PLACE),
            duplicate_indexes[0],
        )
        for index in duplicate_indexes:
            union.union(operation_ids[intended], operation_ids[index])
            if index != intended:
                dependencies.add(
                    (
                        operation_ids[intended],
                        operation_ids[index],
                        "durable retained Exact Duplicate occurrence",
                    )
                )

    result: list[RenameOperation] = []
    for operation, operation_id in zip(raw, operation_ids, strict=True):
        staged = None
        if operation.staging_relative_path is not None:
            namespace = {
                PLACE: "placements",
                DROP_EXACT_DUPLICATE: "quarantine/duplicates",
                DROP_EXCLUDED: "quarantine/exclusions",
                DROP_SKIPPED_EXCLUSION: "quarantine/skipped-exclusions",
            }[operation.action]
            staged = f"{namespace}/{operation_id[:2]}/{operation_id}"
        root = union.find(operation_id)
        result.append(
            RenameOperation(
                operation_id,
                operation.action,
                operation.source_relative_path,
                operation.output_relative_path,
                operation.expected_type,
                operation.expected_byte_size,
                operation.expected_mtime_ns,
                operation.algorithm,
                operation.algorithm_version,
                operation.digest,
                staged,
                _digest({"component": root}),
            )
        )
    result.sort(key=lambda operation: operation.operation_id)
    return result, sorted(dependencies)


def _owner_payload(
    context: RenameContext, operation_digest: str, owner_secret: str
) -> dict[str, object]:
    return {
        "owner_version": OWNER_VERSION,
        "run_id": context.run_id,
        "plan_id": context.plan_id,
        "layout_revision": context.revision,
        "fingerprint": context.fingerprint,
        "executor_kind": EXECUTOR_KIND,
        "executor_version": EXECUTOR_VERSION,
        "attempt_id": context.attempt_id,
        "selected_root": str(context.selected_root),
        "operation_set_digest": operation_digest,
        "secret": owner_secret,
    }


def _ensure_owner(
    context: RenameContext,
    operations: list[RenameOperation],
    operation_digest: str,
    owner_secret: str,
    filesystem: FilesystemAdapter,
) -> None:
    expected = _owner_payload(context, operation_digest, owner_secret)
    owner_path = context.staging_root / OWNER_FILE
    if context.staging_root.exists():
        try:
            observed = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RenameProtectionV2Error(
                "RenameProtection v2 staging ownership is missing or ambiguous"
            ) from error
        if observed != expected:
            raise RenameProtectionV2Error(
                "RenameProtection v2 staging ownership is foreign"
            )
        allowed_entries = {
            str(operation.staging_relative_path)
            for operation in operations
            if operation.staging_relative_path is not None
        }
        allowed_directories = {
            parent.as_posix()
            for entry in allowed_entries
            for parent in Path(entry).parents
            if parent != Path(".")
        }
        opaque_directories = {
            str(operation.staging_relative_path)
            for operation in operations
            if operation.staging_relative_path is not None
            and operation.expected_type == "directory"
        }
        for root, directory_names, file_names in os.walk(
            context.staging_root, followlinks=False
        ):
            root_path = Path(root)
            for name in list(directory_names):
                path = root_path / name
                relative = path.relative_to(context.staging_root).as_posix()
                if path.is_symlink():
                    if relative not in allowed_entries:
                        raise RenameProtectionV2Error(
                            "RenameProtection v2 staging contains unexplained payload"
                        )
                    directory_names.remove(name)
                elif relative in opaque_directories:
                    directory_names.remove(name)
                elif relative not in allowed_directories:
                    raise RenameProtectionV2Error(
                        "RenameProtection v2 staging contains unexplained payload"
                    )
            for name in file_names:
                relative = (
                    (root_path / name).relative_to(context.staging_root).as_posix()
                )
                if relative != OWNER_FILE and relative not in allowed_entries:
                    raise RenameProtectionV2Error(
                        "RenameProtection v2 staging contains unexplained payload"
                    )
        return
    context.staging_root.mkdir(mode=0o700)
    descriptor = os.open(
        owner_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        payload = _canonical_json(expected).encode()
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        filesystem.sync()
    except OSError as error:
        raise RenameProtectionV2Error(
            "RenameProtection v2 durability barrier failed"
        ) from error


def _persist_manifest(
    connection: sqlite3.Connection, context: RenameContext
) -> tuple[list[RenameOperation], str, str]:
    ensure_schema(connection)
    operations, dependencies = derive_operations(connection, context)
    # Dependencies constrain the allowed namespace transitions, so bind them
    # into the durable manifest identity along with the operations themselves.
    operation_digest = _digest(
        {
            "operations": [asdict(operation) for operation in operations],
            "dependencies": dependencies,
        }
    )
    existing = connection.execute(
        "SELECT * FROM rename_protection_v2_manifests WHERE attempt_id=?",
        (context.attempt_id,),
    ).fetchone()
    if existing is not None:
        if (
            int(existing["manifest_version"]) != MANIFEST_VERSION
            or int(existing["executor_version"]) != EXECUTOR_VERSION
            or existing["operation_set_digest"] != operation_digest
            or existing["staging_relative_path"] != context.staging_root.name
            or existing["destination"] != str(context.selected_root)
        ):
            raise RenameProtectionV2Error(
                "RenameProtection v2 manifest contradicts the admitted context"
            )
        return operations, operation_digest, str(existing["owner_secret"])

    owner_secret = secrets.token_hex(32)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO rename_protection_v2_attempts "
            "(attempt_id, plan_id, policy_version, state, created_at, updated_at) "
            "VALUES (?,?,2,'ADMITTED',datetime('now'),datetime('now'))",
            (context.attempt_id, context.plan_id),
        )
        connection.execute(
            "INSERT INTO rename_protection_v2_manifests VALUES "
            "(?,?,2,?,2,?,?,?,?,?,?,datetime('now'))",
            (
                context.attempt_id,
                context.plan_id,
                EXECUTOR_KIND,
                context.revision,
                context.fingerprint,
                str(context.selected_root),
                operation_digest,
                context.staging_root.name,
                owner_secret,
            ),
        )
        connection.executemany(
            "INSERT INTO rename_protection_v2_operations "
            "(attempt_id, operation_id, component_id, action, source_relative_path, "
            "output_relative_path, expected_type, expected_byte_size, expected_mtime_ns, "
            "algorithm, algorithm_version, digest, staging_relative_path, state, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',datetime('now'))",
            [
                (
                    context.attempt_id,
                    operation.operation_id,
                    operation.component_id,
                    operation.action,
                    operation.source_relative_path,
                    operation.output_relative_path,
                    operation.expected_type,
                    operation.expected_byte_size,
                    operation.expected_mtime_ns,
                    operation.algorithm,
                    operation.algorithm_version,
                    operation.digest,
                    operation.staging_relative_path,
                )
                for operation in operations
            ],
        )
        connection.executemany(
            "INSERT INTO rename_protection_v2_dependencies VALUES (?,?,?,?)",
            [
                (context.attempt_id, predecessor, dependent, reason)
                for predecessor, dependent, reason in dependencies
            ],
        )
        groups: list[tuple[object, ...]] = []
        by_identity: dict[tuple[int, int, str], list[RenameOperation]] = defaultdict(
            list
        )
        for operation in operations:
            if (
                operation.action in {PLACE, DROP_EXACT_DUPLICATE}
                and operation.algorithm is not None
                and operation.algorithm_version is not None
                and operation.expected_byte_size is not None
                and operation.digest is not None
            ):
                by_identity[
                    (
                        operation.algorithm_version,
                        operation.expected_byte_size,
                        operation.digest,
                    )
                ].append(operation)
        for identity, members in sorted(by_identity.items()):
            intended = next(
                (operation for operation in members if operation.action == PLACE), None
            )
            if intended is None or len(members) < 2:
                continue
            candidates = [
                intended,
                *sorted(
                    (operation for operation in members if operation != intended),
                    key=lambda operation: (
                        str(operation.source_relative_path),
                        operation.operation_id,
                    ),
                ),
            ]
            group_id = _digest(
                {"identity": identity, "members": [m.operation_id for m in candidates]}
            )
            groups.append(
                (
                    context.attempt_id,
                    group_id,
                    str(intended.algorithm),
                    identity[0],
                    identity[1],
                    identity[2],
                    intended.operation_id,
                    intended.output_relative_path,
                    intended.operation_id,
                    _canonical_json([member.operation_id for member in candidates]),
                    "PENDING",
                )
            )
        connection.executemany(
            "INSERT INTO rename_protection_v2_identity_groups VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            groups,
        )
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    journal_append(
        connection,
        context.plan_id,
        EVENT_MANIFEST_DURABLE,
        detail=f"{len(operations)} operations; set {operation_digest}",
    )
    return operations, operation_digest, owner_secret


def _matches(observation: Observation | None, operation: RenameOperation) -> bool:
    if observation is None or observation.entry_type != operation.expected_type:
        return False
    if (
        operation.expected_byte_size is not None
        and observation.byte_size != operation.expected_byte_size
    ):
        return False
    return not (
        operation.expected_mtime_ns is not None
        and observation.mtime_ns != operation.expected_mtime_ns
    )


def _locations(
    filesystem: FilesystemAdapter, context: RenameContext, operation: RenameOperation
) -> dict[str, Observation]:
    paths: list[tuple[str, Path]] = []
    if operation.source_relative_path is not None:
        paths.append(("source", context.selected_root / operation.source_relative_path))
    if operation.staging_relative_path is not None:
        paths.append(
            ("staging", context.staging_root / operation.staging_relative_path)
        )
    if operation.output_relative_path is not None:
        output = context.selected_root / operation.output_relative_path
        if not paths or output != paths[0][1]:
            paths.append(("final", output))
    result: dict[str, Observation] = {}
    for location, path in paths:
        observation = filesystem.observe(path, location)
        if observation is not None:
            result[location] = observation
    return result


def _set_state(
    connection: sqlite3.Connection,
    context: RenameContext,
    operation_id: str,
    state: str,
    *,
    retry_count: int | None = None,
    selected_supplier: str | None = None,
) -> None:
    connection.execute(
        "UPDATE rename_protection_v2_operations SET state=?, "
        "retry_count=COALESCE(?, retry_count), "
        "selected_supplier_operation_id=COALESCE(?, selected_supplier_operation_id), "
        "updated_at=datetime('now') WHERE attempt_id=? AND operation_id=?",
        (
            state,
            retry_count,
            selected_supplier,
            context.attempt_id,
            operation_id,
        ),
    )


def _attention(
    connection: sqlite3.Connection,
    context: RenameContext,
    operation: RenameOperation,
    *,
    phase: str,
    reason: str,
    retry_count: int,
    error: BaseException | None,
    observations: dict[str, Observation],
) -> None:
    error_number = error.errno if isinstance(error, OSError) else None
    message = str(error) if error is not None else reason
    connection.execute(
        "INSERT INTO rename_protection_v2_attention VALUES "
        "(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now')) "
        "ON CONFLICT(attempt_id, operation_id) DO UPDATE SET "
        "error_class=excluded.error_class, errno=excluded.errno, "
        "message=excluded.message, phase=excluded.phase, "
        "retry_count=excluded.retry_count, locations_json=excluded.locations_json, "
        "observed_metadata_json=excluded.observed_metadata_json, "
        "reason=excluded.reason, updated_at=excluded.updated_at",
        (
            context.attempt_id,
            operation.operation_id,
            type(error).__name__ if error is not None else "EvidenceConflict",
            error_number,
            message,
            phase,
            retry_count,
            _canonical_json(sorted(observations)),
            _canonical_json(
                {
                    location: asdict(observation)
                    for location, observation in observations.items()
                }
            ),
            reason,
        ),
    )
    _set_state(
        connection,
        context,
        operation.operation_id,
        NEEDS_ATTENTION,
        retry_count=retry_count,
    )
    connection.commit()


def _rename_with_retries(
    connection: sqlite3.Connection,
    context: RenameContext,
    operation: RenameOperation,
    filesystem: FilesystemAdapter,
    source: Path,
    destination: Path,
    *,
    phase: str,
    crash: Callable[[str], None],
    telemetry: MaterializationTelemetry,
) -> bool:
    last_error: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            filesystem.rename(source, destination)
            telemetry.rename_count += 1
            telemetry.retries += attempt - 1
            crash(f"rename-protection-v2-after-{phase}-rename")
            # Preserve v1 boundary aliases for existing recovery qualification.
            crash(f"rename-protection-after-{phase}-rename")
            return True
        except (OSError, LinuxFilesystemError) as error:
            last_error = error
            telemetry.reconciliation_count += 1
            observations = _locations(filesystem, context, operation)
            if "staging" in observations and "source" not in observations:
                telemetry.retries += attempt - 1
                return True
            if (
                "final" in observations
                and "staging" not in observations
                and phase == "publish"
            ):
                telemetry.retries += attempt - 1
                return True
            deterministic = (
                isinstance(error, LinuxFilesystemError)
                and "exists" in str(error).lower()
            )
            if deterministic or attempt == MAX_ATTEMPTS:
                _attention(
                    connection,
                    context,
                    operation,
                    phase=phase,
                    reason="bounded namespace mutation failed",
                    retry_count=attempt - 1,
                    error=error,
                    observations=observations,
                )
                telemetry.retries += attempt - 1
                return False
    raise AssertionError(last_error)


def _sync_checkpoint(
    connection: sqlite3.Connection,
    context: RenameContext,
    filesystem: FilesystemAdapter,
    telemetry: MaterializationTelemetry,
    updates: list[tuple[str, str]],
    crash: Callable[[str], None],
    phase: str,
) -> None:
    try:
        sync_seconds = filesystem.sync()
    except OSError as error:
        raise RenameProtectionV2Error(
            "RenameProtection v2 durability barrier failed"
        ) from error
    telemetry.checkpoints += 1
    telemetry.checkpoint_sync_seconds += sync_seconds
    crash(f"rename-protection-v2-after-{phase}-sync")
    started = time.monotonic()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for operation_id, state in updates:
            _set_state(connection, context, operation_id, state)
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    crash(f"rename-protection-v2-after-{phase}-commit")
    telemetry.checkpoint_database_seconds += time.monotonic() - started


def _block_component(
    connection: sqlite3.Connection,
    context: RenameContext,
    component: list[RenameOperation],
    failed_ids: set[str],
) -> None:
    failed_components = {
        operation.component_id
        for operation in component
        if operation.operation_id in failed_ids
    }
    for operation in component:
        if (
            operation.operation_id in failed_ids
            or operation.component_id not in failed_components
        ):
            continue
        current = connection.execute(
            "SELECT state FROM rename_protection_v2_operations "
            "WHERE attempt_id=? AND operation_id=?",
            (context.attempt_id, operation.operation_id),
        ).fetchone()[0]
        if current in {COMPLETE, NEEDS_ATTENTION}:
            continue
        _set_state(connection, context, operation.operation_id, BLOCKED_BY_DEPENDENCY)
        predecessor = min(
            failed_id
            for failed_id in failed_ids
            if next(
                item.component_id
                for item in component
                if item.operation_id == failed_id
            )
            == operation.component_id
        )
        connection.execute(
            "INSERT OR IGNORE INTO rename_protection_v2_dependencies VALUES (?,?,?,?)",
            (
                context.attempt_id,
                predecessor,
                operation.operation_id,
                "component prerequisite needs attention",
            ),
        )
    connection.commit()


def _prove_alternate(
    connection: sqlite3.Connection,
    context: RenameContext,
    operation: RenameOperation,
    path: Path,
    filesystem: FilesystemAdapter,
    telemetry: MaterializationTelemetry,
) -> bool:
    read = 0
    last_error: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            size, digest, bytes_read = filesystem.prove(path)
            read += bytes_read
            telemetry.payload_read_files += 1
            telemetry.payload_read_bytes += bytes_read
            telemetry.payload_read_exceptional_promotion_bytes += bytes_read
            if size != operation.expected_byte_size or digest != operation.digest:
                raise ValueError("alternate does not match persisted Content Identity")
            telemetry.alternate_promotions += 1
            return True
        except (OSError, ValueError) as error:
            last_error = error
            if isinstance(error, OSError) and attempt < MAX_ATTEMPTS:
                continue
            break
    _attention(
        connection,
        context,
        operation,
        phase="exceptional-proof",
        reason="alternate Content Identity proof failed",
        retry_count=MAX_ATTEMPTS - 1 if isinstance(last_error, OSError) else 0,
        error=last_error,
        observations=_locations(filesystem, context, operation),
    )
    return False


def _process_component(
    connection: sqlite3.Connection,
    context: RenameContext,
    component: list[RenameOperation],
    directory_removals: dict[str, RenameOperation],
    filesystem: FilesystemAdapter,
    telemetry: MaterializationTelemetry,
    crash: Callable[[str], None],
) -> None:
    actionable = [
        operation
        for operation in component
        if operation.action
        in {PLACE, DROP_EXACT_DUPLICATE, DROP_EXCLUDED, DROP_SKIPPED_EXCLUSION}
    ]
    failed: set[str] = set()
    staged: list[RenameOperation] = []
    already_final: list[RenameOperation] = []
    recovered_final: list[RenameOperation] = []
    recovered_cleanup: list[RenameOperation] = []
    completed_existing: set[str] = set()
    planned_outputs = {
        str(operation.output_relative_path): operation
        for operation in component
        if operation.action == PLACE and operation.output_relative_path is not None
    }

    for operation in actionable:
        row = connection.execute(
            "SELECT state FROM rename_protection_v2_operations "
            "WHERE attempt_id=? AND operation_id=?",
            (context.attempt_id, operation.operation_id),
        ).fetchone()
        if row is not None and row[0] == COMPLETE:
            completed_existing.add(operation.operation_id)
            continue
        assert operation.source_relative_path is not None
        source = context.selected_root / operation.source_relative_path
        staged_path = context.staging_root / str(operation.staging_relative_path)
        final = (
            context.selected_root / operation.output_relative_path
            if operation.output_relative_path is not None
            else None
        )
        observations = _locations(filesystem, context, operation)
        producer = planned_outputs.get(str(operation.source_relative_path))
        if (
            "source" in observations
            and "staging" in observations
            and producer is not None
            and producer.operation_id != operation.operation_id
            and _matches(observations["source"], producer)
            and _matches(observations["staging"], operation)
        ):
            # Recovery after publication may recreate another operation's
            # original pathname.  Its own staged occurrence remains authority.
            observations.pop("source")
        if final == source and "source" in observations:
            if not _matches(observations["source"], operation):
                _attention(
                    connection,
                    context,
                    operation,
                    phase="metadata-validation",
                    reason="current source metadata does not match persisted evidence",
                    retry_count=0,
                    error=None,
                    observations=observations,
                )
                failed.add(operation.operation_id)
            else:
                already_final.append(operation)
            continue
        if (
            "staging" in observations
            and "source" not in observations
            and _matches(observations["staging"], operation)
        ):
            staged.append(operation)
            continue
        if (
            "final" in observations
            and "source" not in observations
            and operation.action == PLACE
            and _matches(observations["final"], operation)
        ):
            telemetry.resume_skipped_operations += 1
            completed_existing.add(operation.operation_id)
            recovered_final.append(operation)
            continue
        if (
            row is not None
            and row[0] == STAGED_DURABLE
            and operation.action
            in {DROP_EXACT_DUPLICATE, DROP_EXCLUDED, DROP_SKIPPED_EXCLUSION}
            and "staging" not in observations
            and (
                "source" not in observations
                or (producer is not None and _matches(observations["source"], producer))
            )
        ):
            # A crash may happen after the owned quarantine removal and its
            # filesystem barrier, before the COMPLETE database checkpoint.
            # STAGED_DURABLE establishes that the source reached owned
            # quarantine first. The original name may now be occupied by a
            # different placement that has recreated it, but the missing
            # owned entry is still filesystem-leading cleanup, not drift.
            telemetry.resume_skipped_operations += 1
            recovered_cleanup.append(operation)
            continue
        if "source" not in observations or not _matches(
            observations["source"], operation
        ):
            _attention(
                connection,
                context,
                operation,
                phase="metadata-validation",
                reason="current source metadata does not match persisted evidence",
                retry_count=0,
                error=None,
                observations=observations,
            )
            failed.add(operation.operation_id)
            continue
        if "staging" in observations:
            _attention(
                connection,
                context,
                operation,
                phase="staging",
                reason="source and owned staging are both present",
                retry_count=0,
                error=None,
                observations=observations,
            )
            failed.add(operation.operation_id)
            continue
        if (
            operation.action == DROP_SKIPPED_EXCLUSION
            and operation.expected_type == "directory"
        ):
            observed_mode = source.lstat().st_mode
            os.chmod(source, observed_mode | stat.S_IRWXU, follow_symlinks=False)
        if _rename_with_retries(
            connection,
            context,
            operation,
            filesystem,
            source,
            staged_path,
            phase="stage",
            crash=crash,
            telemetry=telemetry,
        ):
            staged.append(operation)
            telemetry.staged_files += 1
            telemetry.staged_bytes += int(operation.expected_byte_size or 0)
        else:
            failed.add(operation.operation_id)

    if staged or already_final:
        _sync_checkpoint(
            connection,
            context,
            filesystem,
            telemetry,
            [(operation.operation_id, STAGED_DURABLE) for operation in staged],
            crash,
            "staging",
        )

    placements = [
        operation
        for operation in component
        if operation.action == PLACE
        and operation.operation_id not in completed_existing
    ]
    vacated_directories: list[RenameOperation] = []
    blocked_placement_ids: set[str] = set()
    for placement in placements:
        if placement.output_relative_path is None:
            continue
        removal = directory_removals.get(str(placement.output_relative_path))
        if removal is None:
            continue
        row = connection.execute(
            "SELECT state FROM rename_protection_v2_operations "
            "WHERE attempt_id=? AND operation_id=?",
            (context.attempt_id, removal.operation_id),
        ).fetchone()
        if row is not None and row[0] == COMPLETE:
            continue
        final = context.selected_root / str(placement.output_relative_path)
        observation = filesystem.observe(final, "final")
        if observation is None or observation.entry_type != "directory":
            continue
        try:
            # This directory is an owned source ancestor whose contents have
            # already reached durable staging.  Vacating it is the narrowly
            # required namespace step for a planned file-at-directory path.
            final.rmdir()
        except OSError as error:
            _attention(
                connection,
                context,
                removal,
                phase="directory-vacate-for-publication",
                reason="planned source directory could not be vacated for publication",
                retry_count=0,
                error=error,
                observations={"final": observation},
            )
            failed.add(removal.operation_id)
            blocked_placement_ids.add(placement.operation_id)
            continue
        vacated_directories.append(removal)
    if vacated_directories:
        _sync_checkpoint(
            connection,
            context,
            filesystem,
            telemetry,
            [(operation.operation_id, COMPLETE) for operation in vacated_directories],
            crash,
            "directory-vacate",
        )
    for placement in placements:
        if placement.operation_id in blocked_placement_ids:
            continue
        if placement in already_final:
            _set_state(connection, context, placement.operation_id, PUBLISHED_DURABLE)
            connection.commit()
            continue
        supplier = placement
        supplier_path = context.staging_root / str(placement.staging_relative_path)
        if placement.operation_id in failed:
            alternates = sorted(
                (
                    operation
                    for operation in staged
                    if operation.action == DROP_EXACT_DUPLICATE
                    and operation.digest == placement.digest
                    and operation.expected_byte_size == placement.expected_byte_size
                ),
                key=lambda operation: (
                    str(operation.source_relative_path),
                    operation.operation_id,
                ),
            )
            supplier = next(
                (
                    operation
                    for operation in alternates
                    if _prove_alternate(
                        connection,
                        context,
                        operation,
                        context.staging_root / str(operation.staging_relative_path),
                        filesystem,
                        telemetry,
                    )
                ),
                placement,
            )
            if supplier == placement:
                continue
            supplier_path = context.staging_root / str(supplier.staging_relative_path)
            connection.execute(
                "UPDATE rename_protection_v2_identity_groups SET "
                "selected_operation_id=?, state='PENDING' "
                "WHERE attempt_id=? AND intended_operation_id=?",
                (supplier.operation_id, context.attempt_id, placement.operation_id),
            )
            connection.commit()
            crash("rename-protection-v2-after-role-reassignment-commit")
        final = context.selected_root / str(placement.output_relative_path)
        final_observation = filesystem.observe(final, "final")
        if final_observation is not None:
            _attention(
                connection,
                context,
                placement,
                phase="publication",
                reason="planned final path has an unexplained occupant",
                retry_count=0,
                error=FileExistsError(errno.EEXIST, "destination exists", final),
                observations=_locations(filesystem, context, placement),
            )
            failed.add(placement.operation_id)
            continue
        if _rename_with_retries(
            connection,
            context,
            placement,
            filesystem,
            supplier_path,
            final,
            phase="publish",
            crash=crash,
            telemetry=telemetry,
        ):
            # Verify the resulting namespace entry with metadata only.  The
            # normal path must never turn into a payload-proof pass.
            observed_final = filesystem.observe(final, "final")
            if not _matches(observed_final, placement):
                _attention(
                    connection,
                    context,
                    placement,
                    phase="publication-validation",
                    reason="published metadata does not match persisted evidence",
                    retry_count=0,
                    error=None,
                    observations=_locations(filesystem, context, placement),
                )
                failed.add(placement.operation_id)
                continue
            _set_state(
                connection,
                context,
                placement.operation_id,
                PUBLISHED_DURABLE,
                selected_supplier=supplier.operation_id,
            )
            telemetry.published_files += 1
            telemetry.published_bytes += int(placement.expected_byte_size or 0)
        else:
            failed.add(placement.operation_id)
    connection.commit()

    published = [
        operation
        for operation in placements
        if connection.execute(
            "SELECT state FROM rename_protection_v2_operations "
            "WHERE attempt_id=? AND operation_id=?",
            (context.attempt_id, operation.operation_id),
        ).fetchone()[0]
        == PUBLISHED_DURABLE
    ]
    durable_publications = [*published, *recovered_final]
    if durable_publications:
        _sync_checkpoint(
            connection,
            context,
            filesystem,
            telemetry,
            [
                (operation.operation_id, PUBLISHED_DURABLE)
                for operation in durable_publications
            ],
            crash,
            "publication",
        )
        for operation in durable_publications:
            connection.execute(
                "UPDATE rename_protection_v2_identity_groups SET state='RETAINED_DURABLE' "
                "WHERE attempt_id=? AND intended_operation_id=?",
                (context.attempt_id, operation.operation_id),
            )
        connection.commit()

    cleaned: list[RenameOperation] = []
    for operation in staged:
        if operation.action == PLACE:
            continue
        if operation.action == DROP_EXACT_DUPLICATE:
            retained = next(
                (
                    placement
                    for placement in component
                    if placement.action == PLACE
                    and placement.digest == operation.digest
                    and placement.expected_byte_size == operation.expected_byte_size
                ),
                None,
            )
            retained_state = (
                connection.execute(
                    "SELECT state FROM rename_protection_v2_operations "
                    "WHERE attempt_id=? AND operation_id=?",
                    (context.attempt_id, retained.operation_id),
                ).fetchone()[0]
                if retained is not None
                else None
            )
            if retained_state not in {PUBLISHED_DURABLE, COMPLETE}:
                _attention(
                    connection,
                    context,
                    operation,
                    phase="duplicate-retention",
                    reason="no Exact Duplicate occurrence is durably retained",
                    retry_count=0,
                    error=None,
                    observations=_locations(filesystem, context, operation),
                )
                failed.add(operation.operation_id)
                continue
        staged_path = context.staging_root / str(operation.staging_relative_path)
        try:
            filesystem.remove(staged_path)
        except OSError as error:
            _attention(
                connection,
                context,
                operation,
                phase="quarantine-cleanup",
                reason="owned quarantine cleanup failed",
                retry_count=0,
                error=error,
                observations=_locations(filesystem, context, operation),
            )
            failed.add(operation.operation_id)
            continue
        cleaned.append(operation)
        telemetry.deleted_files += 1
        telemetry.deleted_bytes += int(operation.expected_byte_size or 0)
        if operation.action == DROP_EXACT_DUPLICATE:
            telemetry.duplicate_cleaned_files += 1
            telemetry.duplicate_cleaned_bytes += int(operation.expected_byte_size or 0)
        else:
            telemetry.exclusion_cleaned_files += 1
            telemetry.exclusion_cleaned_bytes += int(operation.expected_byte_size or 0)
    completed = [
        *durable_publications,
        *already_final,
        *cleaned,
        *recovered_cleanup,
    ]
    if completed:
        _sync_checkpoint(
            connection,
            context,
            filesystem,
            telemetry,
            [(operation.operation_id, COMPLETE) for operation in completed],
            crash,
            "cleanup",
        )
    if failed:
        _block_component(connection, context, component, failed)


def _finish_directories(
    connection: sqlite3.Connection,
    context: RenameContext,
    operations: list[RenameOperation],
    filesystem: FilesystemAdapter,
) -> None:
    attention_sources = {
        str(row["source_relative_path"]): str(row["operation_id"])
        for row in connection.execute(
            "SELECT source_relative_path, operation_id "
            "FROM rename_protection_v2_operations "
            "WHERE attempt_id=? AND state='NEEDS_ATTENTION' "
            "AND source_relative_path IS NOT NULL",
            (context.attempt_id,),
        )
    }
    completed: list[RenameOperation] = []
    planned_file_outputs = {
        str(item.output_relative_path)
        for item in operations
        if item.action == PLACE and item.output_relative_path is not None
    }
    for operation in sorted(
        (item for item in operations if item.action == ENSURE_DIRECTORY),
        key=lambda item: len(Path(str(item.output_relative_path)).parts),
    ):
        path = context.selected_root / str(operation.output_relative_path)
        observation = filesystem.observe(path, "final")
        try:
            if observation is None:
                path.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            _attention(
                connection,
                context,
                operation,
                phase="directory-publication",
                reason="planned directory creation failed",
                retry_count=0,
                error=error,
                observations={},
            )
            continue
        if observation is not None and observation.entry_type != "directory":
            _attention(
                connection,
                context,
                operation,
                phase="directory-publication",
                reason="planned directory path is occupied",
                retry_count=0,
                error=None,
                observations={"final": observation},
            )
            continue
        completed.append(operation)
    for operation in sorted(
        (item for item in operations if item.action == REMOVE_EMPTY_SOURCE_DIRECTORY),
        key=lambda item: -len(Path(str(item.source_relative_path)).parts),
    ):
        state = connection.execute(
            "SELECT state FROM rename_protection_v2_operations "
            "WHERE attempt_id=? AND operation_id=?",
            (context.attempt_id, operation.operation_id),
        ).fetchone()
        if state is not None and state[0] == COMPLETE:
            continue
        path = context.selected_root / str(operation.source_relative_path)
        directory_relative = str(operation.source_relative_path)
        blocker = next(
            (
                operation_id
                for source, operation_id in sorted(attention_sources.items())
                if source.startswith(f"{directory_relative}/")
            ),
            None,
        )
        if blocker is not None:
            _set_state(
                connection,
                context,
                operation.operation_id,
                BLOCKED_BY_DEPENDENCY,
            )
            connection.execute(
                "INSERT OR IGNORE INTO rename_protection_v2_dependencies VALUES (?,?,?,?)",
                (
                    context.attempt_id,
                    blocker,
                    operation.operation_id,
                    "ancestor cleanup requires resolved child",
                ),
            )
            continue
        # A source ancestor may deliberately remain because a planned output
        # is published below it. There is no cleanup mutation to perform in
        # that case; attempting rmdir would merely report the expected final
        # namespace as a failure.
        if any(
            output.startswith(f"{directory_relative}/")
            for output in planned_file_outputs
        ):
            completed.append(operation)
            continue
        try:
            path.rmdir()
        except FileNotFoundError:
            completed.append(operation)
            continue
        except OSError as error:
            source_observation = filesystem.observe(path, "source")
            _attention(
                connection,
                context,
                operation,
                phase="directory-cleanup",
                reason="planned empty source directory could not be removed",
                retry_count=0,
                error=error,
                observations=(
                    {} if source_observation is None else {"source": source_observation}
                ),
            )
            attention_sources[directory_relative] = operation.operation_id
            continue
        completed.append(operation)
    for operation in (item for item in operations if item.action == RETAIN_SKIPPED):
        observation = filesystem.observe(
            context.selected_root / str(operation.source_relative_path), "source"
        )
        if not _matches(observation, operation):
            _attention(
                connection,
                context,
                operation,
                phase="result-validation",
                reason="unmanaged retained entry changed",
                retry_count=0,
                error=None,
                observations={} if observation is None else {"source": observation},
            )
        else:
            completed.append(operation)
    if completed:
        # Directory creates/removals are namespace mutations as well. Their
        # durable state follows the filesystem barrier, never leads it.
        try:
            filesystem.sync()
        except OSError as error:
            raise RenameProtectionV2Error(
                "RenameProtection v2 durability barrier failed"
            ) from error
        for operation in completed:
            _set_state(connection, context, operation.operation_id, COMPLETE)
    connection.commit()


def _counts(connection: sqlite3.Connection, attempt_id: str) -> dict[str, int]:
    counts = {
        state: 0
        for state in (
            PENDING,
            STAGED_DURABLE,
            PUBLISHED_DURABLE,
            COMPLETE,
            NEEDS_ATTENTION,
            BLOCKED_BY_DEPENDENCY,
        )
    }
    for state, count in connection.execute(
        "SELECT state, COUNT(*) FROM rename_protection_v2_operations "
        "WHERE attempt_id=? GROUP BY state",
        (attempt_id,),
    ):
        counts[str(state)] = int(count)
    return counts


def _remove_empty_staging(
    context: RenameContext, filesystem: FilesystemAdapter
) -> None:
    if not context.staging_root.exists():
        return
    for path in sorted(
        context.staging_root.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if path == context.staging_root / OWNER_FILE:
            path.unlink()
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    context.staging_root.rmdir()
    filesystem.sync()


def execute(
    connection: sqlite3.Connection,
    context: RenameContext,
    progress: ProgressReporter,
    telemetry: MaterializationTelemetry,
    persisted_progress: WorkflowProgress,
    crash: Callable[[str], None],
    *,
    filesystem: FilesystemAdapter | None = None,
) -> RenameOutcome:
    """Execute one v2 invocation and return its authoritative structured outcome."""
    own_filesystem = filesystem is None
    adapter = filesystem or LinuxFilesystemAdapter(context.selected_root)
    try:
        operations, operation_digest, owner_secret = _persist_manifest(
            connection, context
        )
        _ensure_owner(context, operations, operation_digest, owner_secret, adapter)
        connection.execute(
            "UPDATE rename_protection_v2_attempts SET state='EXECUTING', "
            "updated_at=datetime('now') WHERE attempt_id=?",
            (context.attempt_id,),
        )
        connection.commit()
        components: dict[str, list[RenameOperation]] = defaultdict(list)
        directory_removals = {
            str(operation.source_relative_path): operation
            for operation in operations
            if operation.action == REMOVE_EMPTY_SOURCE_DIRECTORY
            and operation.source_relative_path is not None
        }
        for operation in operations:
            if operation.action not in {
                ENSURE_DIRECTORY,
                REMOVE_EMPTY_SOURCE_DIRECTORY,
                RETAIN_SKIPPED,
            }:
                components[operation.component_id].append(operation)
        progress.start("RenameProtection v2 components", len(components), "components")
        persisted_progress.phase(
            "component-execution", total_count=len(components), unit="components"
        )
        ordered_components = [components[key] for key in sorted(components)]
        batches: list[list[RenameOperation]] = []
        batch: list[RenameOperation] = []
        batch_files = 0
        batch_bytes = 0
        batch_started = time.monotonic()
        for component in ordered_components:
            batch.extend(component)
            batch_files += sum(
                operation.staging_relative_path is not None for operation in component
            )
            batch_bytes += sum(
                int(operation.expected_byte_size or 0) for operation in component
            )
            if (
                batch_files >= CHECKPOINT_FILES
                or batch_bytes >= CHECKPOINT_BYTES
                or time.monotonic() - batch_started >= CHECKPOINT_MAX_SECONDS
            ):
                batches.append(batch)
                batch = []
                batch_files = 0
                batch_bytes = 0
                batch_started = time.monotonic()
        if batch:
            batches.append(batch)
        completed_components = 0
        for operation_batch in batches:
            _process_component(
                connection,
                context,
                operation_batch,
                directory_removals,
                adapter,
                telemetry,
                crash,
            )
            batch_component_count = len(
                {operation.component_id for operation in operation_batch}
            )
            completed_components += batch_component_count
            progress.advance(batch_component_count)
            persisted_progress.update(
                completed_components, metrics=telemetry.snapshot()
            )
        progress.complete()
        _finish_directories(connection, context, operations, adapter)
        counts = _counts(connection, context.attempt_id)
        attention_count = counts[NEEDS_ATTENTION]
        blocked_count = counts[BLOCKED_BY_DEPENDENCY]
        unresolved_groups = int(
            connection.execute(
                "SELECT COUNT(*) FROM rename_protection_v2_identity_groups "
                "WHERE attempt_id=? AND state!='RETAINED_DURABLE'",
                (context.attempt_id,),
            ).fetchone()[0]
        )
        if attention_count or blocked_count or counts[PENDING]:
            outcome = RenameOutcome(
                NEEDS_ATTENTION,
                counts,
                attention_count,
                blocked_count,
                unresolved_groups,
                "resume component execution; reconcile source, owned staging, and final paths",
            )
            connection.execute(
                "UPDATE rename_protection_v2_attempts SET state='NEEDS_ATTENTION', "
                "updated_at=datetime('now'), completed_at=datetime('now'), metrics_json=? "
                "WHERE attempt_id=?",
                (_canonical_json(telemetry.snapshot()), context.attempt_id),
            )
            # The legacy coarse table cannot represent recoverable partial success.
            # FAILED is therefore a compatibility projection; v2_attempts is authority.
            connection.execute(
                "UPDATE materialization_attempts SET state='FAILED', "
                "updated_at=datetime('now'), completed_at=datetime('now'), "
                "failure_classification='io-integrity-manual-attention', "
                "failure_detail=? WHERE attempt_id=?",
                (str(RenameProtectionV2NeedsAttention(outcome)), context.attempt_id),
            )
            connection.commit()
            journal_append(
                connection,
                context.plan_id,
                EVENT_NEEDS_ATTENTION,
                detail=_canonical_json(asdict(outcome)),
            )
            persisted_progress.attention(metrics=telemetry.snapshot())
            raise RenameProtectionV2NeedsAttention(outcome)

        _remove_empty_staging(context, adapter)
        outcome = RenameOutcome(
            COMPLETE,
            counts,
            0,
            0,
            0,
            "none; all required operations are complete",
        )
        connection.execute(
            "UPDATE rename_protection_v2_attempts SET state='COMPLETE', "
            "updated_at=datetime('now'), completed_at=datetime('now'), metrics_json=? "
            "WHERE attempt_id=?",
            (_canonical_json(telemetry.snapshot()), context.attempt_id),
        )
        connection.execute(
            "UPDATE materialization_attempts SET state='COMPLETE', "
            "updated_at=datetime('now'), completed_at=datetime('now'), "
            "failure_classification=NULL, failure_detail=NULL, "
            "streamed_file_count=0, streamed_byte_count=0 WHERE attempt_id=?",
            (context.attempt_id,),
        )
        connection.commit()
        journal_append(
            connection,
            context.plan_id,
            EVENT_COMPLETE,
            detail="RenameProtection v2 execution completed",
        )
        telemetry.phase = "COMPLETE"
        persisted_progress.complete(metrics=telemetry.snapshot())
        return outcome
    finally:
        if own_filesystem:
            adapter.close()
