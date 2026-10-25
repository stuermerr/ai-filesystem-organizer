"""RenameProtection v1: forward-only in-place execution without payload copies."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

from blake3 import blake3

from .content_identity import digest_file
from .linux_filesystem import FilesystemSync, LinuxFilesystemError, rename_no_replace
from .progress import MaterializationTelemetry, ProgressReporter
from .run_workspace import journal_append
from .workflow_progress import WorkflowProgress

EXECUTOR_KIND = "rename-protection"
EXECUTOR_VERSION = 1
MANIFEST_VERSION = 1
OWNER_VERSION = 1
OWNER_FILE = ".filesystem-organizer-rename-owner.json"
CHECKPOINT_FILES = 128
CHECKPOINT_BYTES = 1024 * 1024 * 1024
CHECKPOINT_MAX_SECONDS = 30.0

PLACE = "PLACE"
DROP_EXACT_DUPLICATE = "DROP_EXACT_DUPLICATE"
DROP_EXCLUDED = "DROP_EXCLUDED"
DROP_SKIPPED_EXCLUSION = "DROP_SKIPPED_EXCLUSION"
RETAIN_SKIPPED = "RETAIN_SKIPPED"
ENSURE_DIRECTORY = "ENSURE_DIRECTORY"
REMOVE_EMPTY_SOURCE_DIRECTORY = "REMOVE_EMPTY_SOURCE_DIRECTORY"

EVENT_MANIFEST_DURABLE = "RENAME_MANIFEST_DURABLE"
EVENT_PROTECTION_DURABLE = "RENAME_PROTECTION_DURABLE"
EVENT_PUBLICATION_DURABLE = "RENAME_PUBLICATION_DURABLE"
EVENT_OUTPUT_DURABLE = "RENAME_OUTPUT_DURABLE"
EVENT_QUARANTINE_REMOVED = "RENAME_QUARANTINE_REMOVED"
EVENT_STAGING_TOMBSTONED = "RENAME_STAGING_TOMBSTONED"
EVENT_STAGING_REMOVED = "RENAME_STAGING_REMOVED"
EVENT_COMPLETE = "MATERIALIZATION_COMPLETE"


class RenameProtectionError(Exception):
    """RenameProtection cannot safely reconcile or continue execution."""


@dataclass(frozen=True)
class RenameContext:
    attempt_id: str
    plan_id: str
    run_id: str
    revision: int
    fingerprint: str
    selected_root: Path
    staging_root: Path


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
    attempt_digest: str | None = None
    verified_observation: tuple[int, int, int, int] | None = None


@dataclass
class _AdaptiveCheckpoint:
    started: float
    interval: float = 5.0
    sync_cost: float | None = None

    def due(self, files: int, byte_count: int) -> bool:
        return (
            files >= CHECKPOINT_FILES
            or byte_count >= CHECKPOINT_BYTES
            or time.monotonic() - self.started
            >= min(self.interval, CHECKPOINT_MAX_SECONDS)
        )

    def confirmed(self, sync_seconds: float) -> None:
        self.sync_cost = (
            sync_seconds
            if self.sync_cost is None
            else 0.75 * self.sync_cost + 0.25 * sync_seconds
        )
        self.interval = min(CHECKPOINT_MAX_SECONDS, max(1.0, 20 * self.sync_cost))
        self.started = time.monotonic()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rename_protection_manifests (
  attempt_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  manifest_version INTEGER NOT NULL CHECK (manifest_version = 1),
  executor_kind TEXT NOT NULL CHECK (executor_kind = 'rename-protection'),
  executor_version INTEGER NOT NULL CHECK (executor_version = 1),
  layout_revision INTEGER NOT NULL,
  finalized_projection_fingerprint TEXT NOT NULL,
  operation_set_digest TEXT NOT NULL,
  staging_relative_path TEXT NOT NULL,
  owner_secret TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (attempt_id, plan_id)
    REFERENCES materialization_attempts(attempt_id, plan_id)
);
CREATE TABLE IF NOT EXISTS rename_protection_operations (
  attempt_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN (
    'PLACE', 'DROP_EXACT_DUPLICATE', 'DROP_EXCLUDED',
    'DROP_SKIPPED_EXCLUSION', 'RETAIN_SKIPPED', 'ENSURE_DIRECTORY',
    'REMOVE_EMPTY_SOURCE_DIRECTORY'
  )),
  source_relative_path TEXT,
  output_relative_path TEXT,
  expected_type TEXT NOT NULL,
  expected_byte_size INTEGER,
  expected_mtime_ns INTEGER,
  algorithm TEXT,
  algorithm_version INTEGER,
  digest TEXT,
  staging_relative_path TEXT,
  attempt_digest TEXT,
  state TEXT NOT NULL DEFAULT 'PENDING',
  PRIMARY KEY (attempt_id, operation_id),
  FOREIGN KEY (attempt_id) REFERENCES rename_protection_manifests(attempt_id)
);
CREATE INDEX IF NOT EXISTS rename_protection_operation_source
  ON rename_protection_operations(attempt_id, source_relative_path);
CREATE TABLE IF NOT EXISTS rename_protection_proofs (
  attempt_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  proof_version INTEGER NOT NULL CHECK (proof_version = 1),
  digest TEXT NOT NULL,
  observed_dev INTEGER NOT NULL,
  observed_ino INTEGER NOT NULL,
  observed_size INTEGER NOT NULL,
  observed_mtime_ns INTEGER NOT NULL,
  proven_at TEXT NOT NULL,
  PRIMARY KEY (attempt_id, operation_id),
  FOREIGN KEY (attempt_id, operation_id)
    REFERENCES rename_protection_operations(attempt_id, operation_id)
);
"""


def staging_name(plan_id: str) -> str:
    return f".filesystem-organizer-staging-v1-{plan_id}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _operation_id(context: RenameContext, payload: dict[str, object]) -> str:
    bound = {
        "run_id": context.run_id,
        "plan_id": context.plan_id,
        "revision": context.revision,
        "fingerprint": context.fingerprint,
        "executor_kind": EXECUTOR_KIND,
        "executor_version": EXECUTOR_VERSION,
        **payload,
    }
    return blake3(_canonical_json(bound).encode()).hexdigest()


def _staged_path(action: str, operation_id: str) -> str | None:
    namespaces = {
        PLACE: "placements",
        DROP_EXACT_DUPLICATE: "quarantine/duplicates",
        DROP_EXCLUDED: "quarantine/exclusions",
        DROP_SKIPPED_EXCLUSION: "quarantine/skipped-exclusions",
    }
    namespace = namespaces.get(action)
    if namespace is None:
        return None
    return f"{namespace}/{operation_id[:2]}/{operation_id}"


def _make_operation(
    context: RenameContext,
    *,
    action: str,
    source: str | None,
    output: str | None,
    expected_type: str,
    size: int | None = None,
    mtime_ns: int | None = None,
    algorithm: str | None = None,
    algorithm_version: int | None = None,
    digest: str | None = None,
) -> RenameOperation:
    payload: dict[str, object] = {
        "action": action,
        "source": source,
        "output": output,
        "expected_type": expected_type,
        "size": size,
        "mtime_ns": mtime_ns,
        "algorithm": algorithm,
        "algorithm_version": algorithm_version,
        "digest": digest,
    }
    operation_id = _operation_id(context, payload)
    return RenameOperation(
        operation_id,
        action,
        source,
        output,
        expected_type,
        size,
        mtime_ns,
        algorithm,
        algorithm_version,
        digest,
        _staged_path(action, operation_id),
    )


def derive_operations(
    connection: sqlite3.Connection, context: RenameContext
) -> list[RenameOperation]:
    entries = connection.execute(
        "SELECT entry_kind, source_relative_path, output_relative_path, "
        "expected_byte_size, algorithm, algorithm_version, digest, "
        "evidence_mtime_ns FROM final_plan_entries WHERE plan_id=? "
        "ORDER BY entry_index",
        (context.plan_id,),
    ).fetchall()
    inventory = {
        str(row["relative_path"]): row
        for row in connection.execute(
            "SELECT relative_path, entry_kind, observed_byte_size, modified_ns, "
            "read_outcome FROM inventory_entries WHERE run_id=?",
            (context.run_id,),
        )
    }
    operations: list[RenameOperation] = []
    owned_sources: set[str] = set()
    retained_identities: set[tuple[int, str]] = set()
    planned_output_identities: dict[str, tuple[int, str] | None] = {}
    for row in entries:
        output = str(row["output_relative_path"])
        if row["entry_kind"] == "directory":
            operations.append(
                _make_operation(
                    context,
                    action=ENSURE_DIRECTORY,
                    source=(
                        str(row["source_relative_path"])
                        if row["source_relative_path"] is not None
                        else None
                    ),
                    output=output,
                    expected_type="directory",
                    mtime_ns=row["evidence_mtime_ns"],
                )
            )
            continue
        source = str(row["source_relative_path"])
        owned_sources.add(source)
        if row["digest"] is not None:
            planned_identity = (
                int(row["expected_byte_size"]),
                str(row["digest"]),
            )
            retained_identities.add(planned_identity)
        else:
            planned_identity = None
        planned_output_identities[output] = planned_identity
        operations.append(
            _make_operation(
                context,
                action=PLACE,
                source=source,
                output=output,
                expected_type="regular-file",
                size=int(row["expected_byte_size"]),
                mtime_ns=row["evidence_mtime_ns"],
                algorithm=row["algorithm"],
                algorithm_version=row["algorithm_version"],
                digest=row["digest"],
            )
        )

    excluded_identities: set[tuple[int, str]] = set()
    for row in connection.execute(
        "SELECT source_relative_path, expected_byte_size, digest "
        "FROM final_plan_exclusions WHERE plan_id=? ORDER BY source_relative_path",
        (context.plan_id,),
    ):
        source = str(row["source_relative_path"])
        owned_sources.add(source)
        if row["digest"] is not None:
            excluded_identities.add(
                (int(row["expected_byte_size"]), str(row["digest"]))
            )
        observation = inventory[source]
        operations.append(
            _make_operation(
                context,
                action=DROP_EXCLUDED,
                source=source,
                output=None,
                expected_type="regular-file",
                size=int(row["expected_byte_size"]),
                mtime_ns=observation["modified_ns"],
                algorithm="blake3" if row["digest"] is not None else None,
                algorithm_version=1 if row["digest"] is not None else None,
                digest=row["digest"],
            )
        )

    identities = connection.execute(
        "SELECT identity.relative_path, identity.byte_size, identity.digest, "
        "inventory.modified_ns FROM content_identities AS identity "
        "JOIN inventory_entries AS inventory ON inventory.run_id=identity.run_id "
        "AND inventory.relative_path=identity.relative_path "
        "WHERE identity.run_id=? AND identity.read_outcome='successful' "
        "ORDER BY identity.relative_path",
        (context.run_id,),
    ).fetchall()
    for row in identities:
        source = str(row["relative_path"])
        identity = (int(row["byte_size"]), str(row["digest"]))
        action: str | None = None
        if identity in excluded_identities and source not in owned_sources:
            action = DROP_EXCLUDED
        elif (
            identity in retained_identities
            and source not in owned_sources
            and planned_output_identities.get(source) != identity
        ):
            action = DROP_EXACT_DUPLICATE
        if action is None:
            continue
        owned_sources.add(source)
        operations.append(
            _make_operation(
                context,
                action=action,
                source=source,
                output=None,
                expected_type="regular-file",
                size=identity[0],
                mtime_ns=int(row["modified_ns"]),
                algorithm="blake3",
                algorithm_version=1,
                digest=identity[1],
            )
        )

    for row in connection.execute(
        "SELECT actions.relative_path, actions.action, inventory.entry_kind, "
        "inventory.modified_ns FROM final_plan_skipped_actions AS actions "
        "JOIN consolidation_plans AS plans USING(plan_id) "
        "JOIN inventory_entries AS inventory ON inventory.run_id=plans.run_id "
        "AND inventory.relative_path=actions.relative_path "
        "WHERE actions.plan_id=? ORDER BY actions.relative_path",
        (context.plan_id,),
    ):
        source = str(row["relative_path"])
        action = (
            DROP_SKIPPED_EXCLUSION if row["action"] == "exclude" else RETAIN_SKIPPED
        )
        if action == DROP_SKIPPED_EXCLUSION:
            owned_sources.add(source)
        operations.append(
            _make_operation(
                context,
                action=action,
                source=source,
                output=None,
                expected_type=str(row["entry_kind"]),
                mtime_ns=row["modified_ns"],
            )
        )

    affected_directories: set[str] = set()
    for source in owned_sources:
        parent = Path(source).parent
        while parent != Path("."):
            affected_directories.add(parent.as_posix())
            parent = parent.parent
    explicit_directories = {
        str(operation.output_relative_path)
        for operation in operations
        if operation.action == ENSURE_DIRECTORY
    }
    for relative in sorted(
        affected_directories - explicit_directories,
        key=lambda value: (-len(Path(value).parts), value),
    ):
        operations.append(
            _make_operation(
                context,
                action=REMOVE_EMPTY_SOURCE_DIRECTORY,
                source=relative,
                output=None,
                expected_type="directory",
            )
        )
    return sorted(operations, key=lambda operation: operation.operation_id)


def _operation_set_digest(operations: list[RenameOperation]) -> str:
    return blake3(
        _canonical_json(
            [
                {
                    key: value
                    for key, value in asdict(operation).items()
                    if key not in {"attempt_digest", "verified_observation"}
                }
                for operation in operations
            ]
        ).encode()
    ).hexdigest()


def ensure_manifest(
    connection: sqlite3.Connection, context: RenameContext
) -> tuple[list[RenameOperation], str, str]:
    connection.executescript(_SCHEMA)
    operations = derive_operations(connection, context)
    operation_digest = _operation_set_digest(operations)
    row = connection.execute(
        "SELECT * FROM rename_protection_manifests WHERE attempt_id=?",
        (context.attempt_id,),
    ).fetchone()
    if row is None:
        owner_secret = secrets.token_hex(32)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "INSERT INTO rename_protection_manifests VALUES "
                "(?,?,1,?,1,?,?,?,?,?,datetime('now'))",
                (
                    context.attempt_id,
                    context.plan_id,
                    EXECUTOR_KIND,
                    context.revision,
                    context.fingerprint,
                    operation_digest,
                    context.staging_root.name,
                    owner_secret,
                ),
            )
            connection.executemany(
                "INSERT INTO rename_protection_operations "
                "(attempt_id, operation_id, action, source_relative_path, "
                "output_relative_path, expected_type, expected_byte_size, "
                "expected_mtime_ns, algorithm, algorithm_version, digest, "
                "staging_relative_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        context.attempt_id,
                        operation.operation_id,
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
    if (
        int(row["manifest_version"]) != MANIFEST_VERSION
        or row["executor_kind"] != EXECUTOR_KIND
        or int(row["executor_version"]) != EXECUTOR_VERSION
        or int(row["layout_revision"]) != context.revision
        or row["finalized_projection_fingerprint"] != context.fingerprint
        or row["operation_set_digest"] != operation_digest
        or row["staging_relative_path"] != context.staging_root.name
    ):
        raise RenameProtectionError(
            "RenameProtection manifest does not match the finalized plan"
        )
    attempt_digests = {
        str(operation_row["operation_id"]): operation_row["attempt_digest"]
        for operation_row in connection.execute(
            "SELECT operation_id, attempt_digest FROM rename_protection_operations "
            "WHERE attempt_id=?",
            (context.attempt_id,),
        )
    }
    operations = [
        replace(
            operation,
            attempt_digest=(
                str(attempt_digests[operation.operation_id])
                if attempt_digests.get(operation.operation_id) is not None
                else None
            ),
        )
        for operation in operations
    ]
    return operations, operation_digest, str(row["owner_secret"])


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
    synchronizer: FilesystemSync,
    crash: Callable[[str], None],
) -> None:
    expected = _owner_payload(context, operation_digest, owner_secret)
    owner_path = context.staging_root / OWNER_FILE
    pending_root = context.staging_root.with_name(
        f"{context.staging_root.name}.owner-pending-{owner_secret[:16]}"
    )
    if context.staging_root.exists():
        if _exists(pending_root):
            raise RenameProtectionError(
                "RenameProtection has ambiguous owner publication state"
            )
        try:
            observed = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RenameProtectionError(
                "RenameProtection staging ownership is missing or ambiguous"
            ) from error
        if observed != expected:
            raise RenameProtectionError("RenameProtection staging ownership is foreign")
        allowed_files = {OWNER_FILE}
        allowed_directories: set[str] = set()
        opaque_directories: set[str] = set()
        allowed_symlinks: set[str] = set()
        for operation in operations:
            if operation.staging_relative_path is None:
                continue
            relative = Path(operation.staging_relative_path)
            relative_name = relative.as_posix()
            if operation.expected_type == "directory":
                opaque_directories.add(relative_name)
            elif operation.expected_type == "symbolic-link":
                allowed_symlinks.add(relative_name)
            else:
                allowed_files.add(relative_name)
            allowed_directories.update(
                parent.as_posix() for parent in relative.parents if parent != Path(".")
            )
        for root, directory_names, file_names in os.walk(
            context.staging_root, followlinks=False
        ):
            root_path = Path(root)
            for name in list(directory_names):
                path = root_path / name
                relative_name = path.relative_to(context.staging_root).as_posix()
                if relative_name in opaque_directories:
                    if path.is_symlink() or not path.is_dir():
                        raise RenameProtectionError(
                            "RenameProtection staged directory type changed"
                        )
                    directory_names.remove(name)
                elif relative_name in allowed_symlinks:
                    if not path.is_symlink():
                        raise RenameProtectionError(
                            "RenameProtection staged symbolic link type changed"
                        )
                    directory_names.remove(name)
                elif path.is_symlink() or relative_name not in allowed_directories:
                    raise RenameProtectionError(
                        "RenameProtection staging contains unexplained content"
                    )
            for name in file_names:
                path = root_path / name
                relative_name = path.relative_to(context.staging_root).as_posix()
                if relative_name in allowed_symlinks:
                    if not path.is_symlink():
                        raise RenameProtectionError(
                            "RenameProtection staged symbolic link type changed"
                        )
                elif relative_name not in allowed_files:
                    raise RenameProtectionError(
                        "RenameProtection staging contains unexplained content"
                    )
        return
    pending_owner = pending_root / OWNER_FILE
    partial_owner = pending_root / f"{OWNER_FILE}.partial"
    if _exists(pending_root):
        if pending_root.is_symlink() or not pending_root.is_dir():
            raise RenameProtectionError(
                "RenameProtection pending owner root is foreign"
            )
        with os.scandir(pending_root) as iterator:
            entries = {entry.name for entry in iterator}
        if not entries <= {OWNER_FILE, partial_owner.name}:
            raise RenameProtectionError(
                "RenameProtection pending owner root has unexplained content"
            )
        if _exists(pending_owner):
            try:
                observed = json.loads(pending_owner.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise RenameProtectionError(
                    "RenameProtection pending owner evidence is ambiguous"
                ) from error
            if observed != expected or _exists(partial_owner):
                raise RenameProtectionError(
                    "RenameProtection pending owner evidence is foreign"
                )
        elif _exists(partial_owner):
            observed_partial = partial_owner.lstat()
            if not stat.S_ISREG(observed_partial.st_mode):
                raise RenameProtectionError(
                    "RenameProtection pending owner evidence is foreign"
                )
            partial_owner.unlink()
    else:
        pending_root.mkdir(mode=0o700)
        crash("rename-protection-after-owner-directory-create")
    if not _exists(pending_owner):
        descriptor = os.open(
            partial_owner,
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
        crash("rename-protection-after-owner-file-write")
        rename_no_replace(partial_owner, pending_owner)
        crash("rename-protection-after-owner-file-rename")
        synchronizer.sync(context.selected_root)
        crash("rename-protection-after-owner-file-sync")
    rename_no_replace(pending_root, context.staging_root)
    crash("rename-protection-after-owner-directory-rename")
    synchronizer.sync(context.selected_root)


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _has_event(connection: sqlite3.Connection, plan_id: str, event: str) -> bool:
    return bool(
        connection.execute(
            "SELECT COUNT(*) FROM materialization_events WHERE plan_id=? AND event=?",
            (plan_id, event),
        ).fetchone()[0]
    )


def _verify_regular(
    path: Path,
    operation: RenameOperation,
    *,
    require_digest: bool,
    telemetry: MaterializationTelemetry,
) -> str:
    try:
        observed = path.lstat()
    except OSError as error:
        raise RenameProtectionError(f"cannot observe required path: {path}") from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_size != operation.expected_byte_size
        or (
            operation.expected_mtime_ns is not None
            and observed.st_mtime_ns != operation.expected_mtime_ns
        )
    ):
        raise RenameProtectionError(f"current source evidence does not match: {path}")
    observed_identity = (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
    )
    cached_digest = operation.attempt_digest or operation.digest
    if (
        operation.verified_observation == observed_identity
        and cached_digest is not None
    ):
        return cached_digest
    if (
        operation.digest is None
        and operation.attempt_digest is None
        and not require_digest
    ):
        return ""
    bytes_read = 0

    def _count(chunk: bytes) -> None:
        nonlocal bytes_read
        bytes_read += len(chunk)

    try:
        size, digest = digest_file(path, on_chunk=_count)
    except OSError as error:
        telemetry.revalidated_bytes += bytes_read
        error_name = os.strerror(error.errno) if error.errno is not None else str(error)
        raise RenameProtectionError(
            "cannot read required payload: "
            f"{path} (operation {operation.operation_id}, action {operation.action}, "
            f"errno {error.errno}: {error_name}, byte offset {bytes_read})"
        ) from error
    telemetry.revalidated_files += 1
    telemetry.revalidated_bytes += size
    if operation.digest is not None and digest != operation.digest:
        raise RenameProtectionError(f"current content identity does not match: {path}")
    if operation.attempt_digest is not None and digest != operation.attempt_digest:
        raise RenameProtectionError(
            f"attempt-local execution identity does not match: {path}"
        )
    return digest


def _establish_current_proof(
    connection: sqlite3.Connection,
    context: RenameContext,
    operations: list[RenameOperation],
    telemetry: MaterializationTelemetry,
    progress: ProgressReporter,
    persisted_progress: WorkflowProgress,
    crash: Callable[[str], None],
) -> list[RenameOperation]:
    """Read and checkpoint destructive payload proof before namespace mutation."""
    if _has_event(connection, context.plan_id, EVENT_PROTECTION_DURABLE):
        return operations
    proof_actions = {PLACE, DROP_EXACT_DUPLICATE, DROP_EXCLUDED}
    proof_total = sum(operation.action in proof_actions for operation in operations)
    telemetry.report(progress, "PROOF")
    progress.start("Proving in-place source payloads", proof_total, "files")
    persisted_progress.phase("proof", total_count=proof_total, unit="files")
    proof_cursor = iter(
        connection.execute(
            "SELECT * FROM rename_protection_proofs WHERE attempt_id=? "
            "ORDER BY operation_id",
            (context.attempt_id,),
        )
    )
    next_proof = next(proof_cursor, None)
    proven: list[RenameOperation] = []
    pending_digests: list[tuple[str, str]] = []
    pending_proofs: list[tuple[object, ...]] = []
    checkpoint = _AdaptiveCheckpoint(time.monotonic())
    completed = 0
    batch_bytes = 0

    def commit_proofs() -> None:
        nonlocal pending_digests, pending_proofs, batch_bytes
        if not pending_proofs:
            return
        has_attempt_digests = bool(pending_digests)
        connection.executemany(
            "INSERT INTO rename_protection_proofs VALUES(?,?,1,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(attempt_id, operation_id) DO UPDATE SET "
            "digest=excluded.digest, observed_dev=excluded.observed_dev, "
            "observed_ino=excluded.observed_ino, observed_size=excluded.observed_size, "
            "observed_mtime_ns=excluded.observed_mtime_ns, proven_at=excluded.proven_at",
            pending_proofs,
        )
        connection.executemany(
            "UPDATE rename_protection_operations SET attempt_digest=? "
            "WHERE attempt_id=? AND operation_id=?",
            [
                (digest_value, context.attempt_id, operation_id)
                for digest_value, operation_id in pending_digests
            ],
        )
        connection.commit()
        persisted_progress.phase(
            "proof",
            completed_count=completed,
            total_count=proof_total,
            unit="files",
            metrics=telemetry.snapshot(),
        )
        if has_attempt_digests:
            crash("rename-protection-after-attempt-digest-commit")
        crash("rename-protection-after-proof-batch-commit")
        pending_digests = []
        pending_proofs = []
        batch_bytes = 0
        checkpoint.confirmed(0.0)

    try:
        for operation in operations:
            while (
                next_proof is not None
                and str(next_proof["operation_id"]) < operation.operation_id
            ):
                next_proof = next(proof_cursor, None)
            if operation.action not in proof_actions:
                proven.append(operation)
                continue
            assert operation.source_relative_path is not None
            source = context.selected_root / operation.source_relative_path
            if not _exists(source):
                # Recovery will prove the staged or final path against the durable
                # attempt digest/content identity below.
                proven.append(operation)
                completed += 1
                progress.advance()
                continue
            observed = source.lstat()
            observed_identity = (
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
            )
            proof = (
                next_proof
                if next_proof is not None
                and str(next_proof["operation_id"]) == operation.operation_id
                else None
            )
            if proof is not None:
                next_proof = next(proof_cursor, None)
            expected_digest = operation.attempt_digest or operation.digest
            if proof is not None:
                persisted_identity = (
                    int(proof["observed_dev"]),
                    int(proof["observed_ino"]),
                    int(proof["observed_size"]),
                    int(proof["observed_mtime_ns"]),
                )
                if persisted_identity != observed_identity or (
                    expected_digest is not None and proof["digest"] != expected_digest
                ):
                    raise RenameProtectionError(
                        f"persisted proof source changed: {source} "
                        f"(operation {operation.operation_id})"
                    )
                digest = str(proof["digest"])
                telemetry.reused_files += 1
            else:
                before = observed_identity
                digest = _verify_regular(
                    source, operation, require_digest=True, telemetry=telemetry
                )
                observed = source.lstat()
                observed_identity = (
                    observed.st_dev,
                    observed.st_ino,
                    observed.st_size,
                    observed.st_mtime_ns,
                )
                if observed_identity != before:
                    raise RenameProtectionError(
                        f"source changed while establishing proof: {source} "
                        f"(operation {operation.operation_id})"
                    )
                pending_proofs.append(
                    (
                        context.attempt_id,
                        operation.operation_id,
                        digest,
                        *observed_identity,
                    )
                )
                batch_bytes += observed.st_size
                if operation.digest is None and operation.attempt_digest is None:
                    pending_digests.append((digest, operation.operation_id))
            proven.append(
                replace(
                    operation,
                    attempt_digest=operation.attempt_digest or digest,
                    verified_observation=observed_identity,
                )
            )
            completed += 1
            progress.advance()
            if checkpoint.due(len(pending_proofs), batch_bytes):
                commit_proofs()
        commit_proofs()
        persisted_progress.update(completed, metrics=telemetry.snapshot())
    finally:
        progress.complete()
    return proven


def _source_is_proven_planned_output(
    source: Path,
    selected_root: Path,
    planned_outputs: dict[str, RenameOperation],
    telemetry: MaterializationTelemetry,
) -> bool:
    relative = source.relative_to(selected_root).as_posix()
    candidate = planned_outputs.get(relative)
    if candidate is None:
        return False
    _verify_regular(source, candidate, require_digest=True, telemetry=telemetry)
    return True


def _verify_skipped(
    path: Path, operation: RenameOperation, *, verify_metadata: bool = True
) -> None:
    try:
        observed = path.lstat()
    except OSError as error:
        raise RenameProtectionError(f"skipped entry is missing: {path}") from error
    expected = operation.expected_type
    if expected == "symbolic-link":
        matches = stat.S_ISLNK(observed.st_mode)
    elif expected == "directory":
        matches = stat.S_ISDIR(observed.st_mode)
    elif expected == "special-entry":
        matches = not stat.S_ISREG(observed.st_mode) and not stat.S_ISDIR(
            observed.st_mode
        )
    else:
        matches = True
    if not matches or (
        verify_metadata
        and operation.expected_mtime_ns is not None
        and observed.st_mtime_ns != operation.expected_mtime_ns
    ):
        raise RenameProtectionError(f"skipped entry evidence changed: {path}")


def _checkpoint_states(
    connection: sqlite3.Connection,
    context: RenameContext,
    updates: list[tuple[str, str | None, str]],
) -> None:
    if not updates:
        return
    connection.execute("BEGIN IMMEDIATE")
    connection.executemany(
        "UPDATE rename_protection_operations SET state=?, attempt_digest=COALESCE(?, attempt_digest) "
        "WHERE attempt_id=? AND operation_id=?",
        [
            (state, digest, context.attempt_id, operation_id)
            for operation_id, digest, state in updates
        ],
    )
    connection.commit()


def _stage_destructive_operations(
    connection: sqlite3.Connection,
    context: RenameContext,
    operations: list[RenameOperation],
    synchronizer: FilesystemSync,
    telemetry: MaterializationTelemetry,
    progress: ProgressReporter,
    persisted_progress: WorkflowProgress,
    crash: Callable[[str], None],
) -> None:
    destructive = [
        operation
        for operation in operations
        if operation.action
        in {PLACE, DROP_EXACT_DUPLICATE, DROP_EXCLUDED, DROP_SKIPPED_EXCLUSION}
    ]
    planned_outputs = {
        str(operation.output_relative_path): operation
        for operation in operations
        if operation.action == PLACE and operation.output_relative_path is not None
    }
    telemetry.report(progress, "STAGING")
    progress.start("RenameProtection staging", len(destructive), unit="entries")
    persisted_progress.phase("staging", total_count=len(destructive), unit="entries")
    updates: list[tuple[str, str | None, str]] = []
    checkpoint_files = 0
    checkpoint_bytes = 0
    checkpoint = _AdaptiveCheckpoint(time.monotonic())
    first_rename_pending = True
    protection_durable = _has_event(
        connection, context.plan_id, EVENT_PROTECTION_DURABLE
    )
    output_durable = _has_event(connection, context.plan_id, EVENT_OUTPUT_DURABLE)
    for completed, operation in enumerate(destructive, start=1):
        assert operation.source_relative_path is not None
        source = context.selected_root / operation.source_relative_path
        staged = context.staging_root / str(operation.staging_relative_path)
        final = (
            context.selected_root / str(operation.output_relative_path)
            if operation.output_relative_path is not None
            else None
        )
        same_final = final is not None and source == final
        source_exists = _exists(source)
        staged_exists = _exists(staged)
        final_exists = final is not None and _exists(final)
        if protection_durable and not same_final and source_exists:
            # Publication may recreate an original source path only as another
            # manifest Placement. Prove that exact output before ignoring the
            # name for this operation; every other recreation is foreign.
            if not _source_is_proven_planned_output(
                source, context.selected_root, planned_outputs, telemetry
            ):
                raise RenameProtectionError(
                    f"unexplained source recreation: {operation.source_relative_path}"
                )
            source_exists = False
        if same_final:
            _verify_regular(
                source, operation, require_digest=False, telemetry=telemetry
            )
            updates.append((operation.operation_id, None, "ALREADY_FINAL"))
        elif source_exists and staged_exists:
            raise RenameProtectionError(
                f"ambiguous double-presence for {operation.source_relative_path}"
            )
        elif staged_exists:
            if operation.action == DROP_SKIPPED_EXCLUSION:
                _verify_skipped(staged, operation, verify_metadata=not output_durable)
                digest = ""
            else:
                digest = _verify_regular(
                    staged, operation, require_digest=True, telemetry=telemetry
                )
                telemetry.recovery_staged_bytes += int(
                    operation.expected_byte_size or 0
                )
            updates.append((operation.operation_id, digest or None, "STAGED"))
        elif not source_exists:
            if final_exists and operation.action == PLACE and final is not None:
                digest = _verify_regular(
                    final, operation, require_digest=True, telemetry=telemetry
                )
                telemetry.recovery_source_bytes += int(
                    operation.expected_byte_size or 0
                )
                updates.append((operation.operation_id, digest or None, "PUBLISHED"))
            elif operation.action != PLACE and _has_event(
                connection, context.plan_id, EVENT_OUTPUT_DURABLE
            ):
                updates.append((operation.operation_id, None, "CLEANED"))
            else:
                raise RenameProtectionError(
                    f"source, staging, and final are missing: {operation.source_relative_path}"
                )
        else:
            if operation.action == DROP_SKIPPED_EXCLUSION:
                _verify_skipped(source, operation)
                if operation.expected_type == "directory":
                    observed_mode = source.lstat().st_mode
                    os.chmod(
                        source,
                        observed_mode | stat.S_IRWXU,
                        follow_symlinks=False,
                    )
                digest = None
            else:
                digest = _verify_regular(
                    source, operation, require_digest=True, telemetry=telemetry
                )
            staged.parent.mkdir(parents=True, exist_ok=True)
            if first_rename_pending:
                crash("rename-protection-before-first-stage-rename")
                first_rename_pending = False
            try:
                rename_no_replace(source, staged)
            except LinuxFilesystemError as error:
                raise RenameProtectionError(str(error)) from error
            crash("rename-protection-after-stage-rename")
            updates.append((operation.operation_id, digest or None, "STAGED"))
            telemetry.protected_files += 1
            telemetry.protected_bytes += int(operation.expected_byte_size or 0)
            telemetry.staged_files += 1
            telemetry.staged_bytes += int(operation.expected_byte_size or 0)
            telemetry.rename_count += 1
        checkpoint_files += 1
        checkpoint_bytes += int(operation.expected_byte_size or 0)
        progress.advance()
        persisted_progress.update(completed)
        if checkpoint.due(checkpoint_files, checkpoint_bytes):
            started = time.monotonic()
            synchronizer.sync(context.selected_root)
            crash("rename-protection-after-protection-batch-sync")
            sync_seconds = time.monotonic() - started
            telemetry.checkpoint_sync_seconds += sync_seconds
            telemetry.checkpoints += 1
            started = time.monotonic()
            _checkpoint_states(connection, context, updates)
            crash("rename-protection-after-protection-batch-commit")
            telemetry.checkpoint_database_seconds += time.monotonic() - started
            checkpoint.confirmed(sync_seconds)
            updates = []
            checkpoint_files = 0
            checkpoint_bytes = 0
    crash("rename-protection-before-protection-sync")
    started = time.monotonic()
    synchronizer.sync(context.selected_root)
    telemetry.checkpoint_sync_seconds += time.monotonic() - started
    telemetry.checkpoints += 1
    crash("rename-protection-after-protection-sync")
    started = time.monotonic()
    _checkpoint_states(connection, context, updates)
    journal_append(
        connection,
        context.plan_id,
        EVENT_PROTECTION_DURABLE,
        detail=f"{len(destructive)} destructive operations reconciled",
    )
    telemetry.report(progress, "PROTECTION_DURABLE")
    connection.execute(
        "UPDATE materialization_attempts SET state='STAGING_DURABLE', "
        "updated_at=datetime('now') WHERE attempt_id=?",
        (context.attempt_id,),
    )
    connection.commit()
    telemetry.checkpoint_database_seconds += time.monotonic() - started
    crash("rename-protection-after-protection-commit")
    progress.complete()


def _remove_empty_sources(
    context: RenameContext, operations: list[RenameOperation]
) -> None:
    for operation in operations:
        if operation.action != REMOVE_EMPTY_SOURCE_DIRECTORY:
            continue
        assert operation.source_relative_path is not None
        directory = context.selected_root / operation.source_relative_path
        try:
            directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # A retained/unmanaged entry keeps this directory meaningful. A later
            # output type check will refuse if the occupied path conflicts.
            pass


def _ensure_output_directories(
    context: RenameContext, operations: list[RenameOperation]
) -> None:
    directories = sorted(
        (operation for operation in operations if operation.action == ENSURE_DIRECTORY),
        key=lambda operation: len(Path(str(operation.output_relative_path)).parts),
    )
    for operation in directories:
        output = context.selected_root / str(operation.output_relative_path)
        if _exists(output):
            if output.is_symlink() or not output.is_dir():
                raise RenameProtectionError(f"output directory is occupied: {output}")
        else:
            output.mkdir(parents=True, exist_ok=False)


def _publish(
    connection: sqlite3.Connection,
    context: RenameContext,
    operations: list[RenameOperation],
    synchronizer: FilesystemSync,
    telemetry: MaterializationTelemetry,
    progress: ProgressReporter,
    persisted_progress: WorkflowProgress,
    crash: Callable[[str], None],
) -> None:
    _remove_empty_sources(context, operations)
    _ensure_output_directories(context, operations)
    placements = [operation for operation in operations if operation.action == PLACE]
    telemetry.report(progress, "PUBLISHING")
    progress.start("RenameProtection publishing", len(placements), unit="files")
    persisted_progress.phase("publishing", total_count=len(placements), unit="files")
    updates: list[tuple[str, str | None, str]] = []
    checkpoint_files = 0
    checkpoint_bytes = 0
    checkpoint = _AdaptiveCheckpoint(time.monotonic())
    for completed, operation in enumerate(placements, start=1):
        assert operation.source_relative_path is not None
        assert operation.output_relative_path is not None
        source = context.selected_root / operation.source_relative_path
        staged = context.staging_root / str(operation.staging_relative_path)
        final = context.selected_root / operation.output_relative_path
        same_final = source == final
        staged_exists = _exists(staged)
        final_exists = _exists(final)
        if same_final:
            _verify_regular(final, operation, require_digest=False, telemetry=telemetry)
        elif staged_exists and final_exists:
            # The only supported double-presence case: both names prove the
            # exact finalized Placement identity. The staged occurrence stays
            # owned until OUTPUT_DURABLE and is removed with staging.
            staged_digest = _verify_regular(
                staged, operation, require_digest=True, telemetry=telemetry
            )
            final_digest = _verify_regular(
                final, operation, require_digest=True, telemetry=telemetry
            )
            if staged_digest != final_digest:
                raise RenameProtectionError(
                    f"ambiguous staged and final identities: {final}"
                )
            telemetry.recovery_staged_bytes += int(operation.expected_byte_size or 0)
            telemetry.recovery_source_bytes += int(operation.expected_byte_size or 0)
        elif final_exists:
            _verify_regular(final, operation, require_digest=True, telemetry=telemetry)
            telemetry.recovery_source_bytes += int(operation.expected_byte_size or 0)
        elif staged_exists:
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                rename_no_replace(staged, final)
            except LinuxFilesystemError as error:
                raise RenameProtectionError(str(error)) from error
            crash("rename-protection-after-publish-rename")
            telemetry.rename_count += 1
        else:
            raise RenameProtectionError(
                f"staged and final placement are both missing: {final}"
            )
        updates.append((operation.operation_id, None, "PUBLISHED"))
        telemetry.published_files += 1
        telemetry.published_bytes += int(operation.expected_byte_size or 0)
        checkpoint_files += 1
        checkpoint_bytes += int(operation.expected_byte_size or 0)
        progress.advance()
        persisted_progress.update(completed)
        if checkpoint.due(checkpoint_files, checkpoint_bytes):
            started = time.monotonic()
            synchronizer.sync(context.selected_root)
            crash("rename-protection-after-publication-batch-sync")
            sync_seconds = time.monotonic() - started
            telemetry.checkpoint_sync_seconds += sync_seconds
            telemetry.checkpoints += 1
            started = time.monotonic()
            _checkpoint_states(connection, context, updates)
            crash("rename-protection-after-publication-batch-commit")
            telemetry.checkpoint_database_seconds += time.monotonic() - started
            checkpoint.confirmed(sync_seconds)
            updates = []
            checkpoint_files = 0
            checkpoint_bytes = 0
    crash("rename-protection-before-publication-sync")
    started = time.monotonic()
    synchronizer.sync(context.selected_root)
    telemetry.checkpoint_sync_seconds += time.monotonic() - started
    telemetry.checkpoints += 1
    crash("rename-protection-after-publication-sync")
    started = time.monotonic()
    _checkpoint_states(connection, context, updates)
    journal_append(
        connection,
        context.plan_id,
        EVENT_PUBLICATION_DURABLE,
        detail=f"{len(placements)} placements reconciled",
    )
    telemetry.checkpoint_database_seconds += time.monotonic() - started
    crash("rename-protection-after-publication-commit")
    progress.complete()


def _verify_output(
    context: RenameContext,
    operations: list[RenameOperation],
    telemetry: MaterializationTelemetry,
) -> None:
    for operation in operations:
        if operation.action == PLACE:
            final = context.selected_root / str(operation.output_relative_path)
            _verify_regular(final, operation, require_digest=False, telemetry=telemetry)
        elif operation.action == ENSURE_DIRECTORY:
            final = context.selected_root / str(operation.output_relative_path)
            if final.is_symlink() or not final.is_dir():
                raise RenameProtectionError(
                    f"planned output directory is missing: {final}"
                )
        elif operation.action == RETAIN_SKIPPED:
            retained = context.selected_root / str(operation.source_relative_path)
            _verify_skipped(retained, operation)


def _remove_entry_at(
    parent_descriptor: int,
    name: str,
    crash: Callable[[str], None] | None = None,
) -> None:
    """Remove an entry relative to an owned directory descriptor, no-follow."""
    observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        os.unlink(name, dir_fd=parent_descriptor)
        if crash is not None:
            crash("rename-protection-during-staging-removal")
        return
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    except PermissionError:
        os.chmod(
            name,
            observed.st_mode | stat.S_IRWXU,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    try:
        for child_name in os.listdir(descriptor):
            _remove_entry_at(descriptor, child_name, crash)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)
    if crash is not None:
        crash("rename-protection-during-staging-removal")


def _remove_no_follow(path: Path) -> None:
    """Remove one owned entry recursively without following symbolic links."""
    parent_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        _remove_entry_at(parent_descriptor, path.name)
    finally:
        os.close(parent_descriptor)


def _cleanup_quarantine(
    connection: sqlite3.Connection,
    context: RenameContext,
    operations: list[RenameOperation],
    synchronizer: FilesystemSync,
    telemetry: MaterializationTelemetry,
    crash: Callable[[str], None],
) -> None:
    for operation in operations:
        if operation.action not in {
            DROP_EXACT_DUPLICATE,
            DROP_EXCLUDED,
            DROP_SKIPPED_EXCLUSION,
        }:
            continue
        staged = context.staging_root / str(operation.staging_relative_path)
        if not _exists(staged):
            continue
        try:
            _remove_no_follow(staged)
        except OSError as error:
            raise RenameProtectionError(
                f"cannot remove owned quarantine entry safely: {staged}"
            ) from error
        if operation.action == DROP_EXACT_DUPLICATE:
            telemetry.duplicate_cleaned_files += 1
            telemetry.duplicate_cleaned_bytes += int(operation.expected_byte_size or 0)
        else:
            telemetry.exclusion_cleaned_files += 1
            telemetry.exclusion_cleaned_bytes += int(operation.expected_byte_size or 0)
        telemetry.deleted_files += 1
        telemetry.deleted_bytes += int(operation.expected_byte_size or 0)
        crash("rename-protection-after-quarantine-delete")
    synchronizer.sync(context.selected_root)
    telemetry.checkpoints += 1
    journal_append(
        connection,
        context.plan_id,
        EVENT_QUARANTINE_REMOVED,
        detail="authorized quarantine cleanup is durable",
    )


def _staging_tombstone(context: RenameContext, owner_secret: str) -> Path:
    return context.staging_root.with_name(
        f"{context.staging_root.name}.removing-{context.attempt_id}-{owner_secret[:16]}"
    )


def _read_owner_at(descriptor: int) -> dict[str, object]:
    owner_descriptor = os.open(
        OWNER_FILE,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=descriptor,
    )
    try:
        with os.fdopen(owner_descriptor, "r", encoding="utf-8") as owner:
            observed = json.load(owner)
            if not isinstance(observed, dict):
                raise RenameProtectionError(
                    "RenameProtection removal ownership is ambiguous"
                )
            return cast(dict[str, object], observed)
    except (OSError, ValueError) as error:
        raise RenameProtectionError(
            "RenameProtection removal ownership is ambiguous"
        ) from error


def _remove_staging_forward(
    connection: sqlite3.Connection,
    context: RenameContext,
    operation_digest: str,
    owner_secret: str,
    synchronizer: FilesystemSync,
    telemetry: MaterializationTelemetry,
    crash: Callable[[str], None],
) -> None:
    tombstone = _staging_tombstone(context, owner_secret)
    if _exists(context.staging_root):
        if _exists(tombstone):
            raise RenameProtectionError("RenameProtection staging removal is ambiguous")
        rename_no_replace(context.staging_root, tombstone)
        crash("rename-protection-after-staging-tombstone-rename")
        synchronizer.sync(context.selected_root)
        telemetry.checkpoints += 1
        journal_append(
            connection,
            context.plan_id,
            EVENT_STAGING_TOMBSTONED,
            detail=tombstone.name,
        )
        crash("rename-protection-after-staging-tombstone-commit")
    if _exists(tombstone):
        descriptor = os.open(
            tombstone,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            entries = set(os.listdir(descriptor))
            if OWNER_FILE in entries:
                if _read_owner_at(descriptor) != _owner_payload(
                    context, operation_digest, owner_secret
                ):
                    raise RenameProtectionError(
                        "RenameProtection removal ownership is foreign"
                    )
                for name in sorted(entries - {OWNER_FILE}):
                    _remove_entry_at(descriptor, name, crash)
                os.unlink(OWNER_FILE, dir_fd=descriptor)
                crash("rename-protection-after-removal-owner-delete")
            elif entries:
                raise RenameProtectionError(
                    "RenameProtection removal lost owner before payload cleanup"
                )
        finally:
            os.close(descriptor)
        tombstone.rmdir()
        crash("rename-protection-after-staging-tombstone-delete")
    synchronizer.sync(context.selected_root)
    telemetry.checkpoints += 1
    if not _has_event(connection, context.plan_id, EVENT_STAGING_REMOVED):
        journal_append(
            connection,
            context.plan_id,
            EVENT_STAGING_REMOVED,
            detail="owned RenameProtection staging removal is durable",
        )


def _complete(
    connection: sqlite3.Connection,
    context: RenameContext,
    telemetry: MaterializationTelemetry,
    persisted_progress: WorkflowProgress,
) -> None:
    if not _has_event(connection, context.plan_id, EVENT_COMPLETE):
        journal_append(
            connection,
            context.plan_id,
            EVENT_COMPLETE,
            detail="RenameProtection execution completed",
        )
    connection.execute(
        "UPDATE materialization_attempts SET state='COMPLETE', "
        "updated_at=datetime('now'), completed_at=COALESCE(completed_at, datetime('now')), "
        "streamed_file_count=0, streamed_byte_count=0 WHERE attempt_id=?",
        (context.attempt_id,),
    )
    connection.commit()
    telemetry.phase = "COMPLETE"
    persisted_progress.complete(metrics=telemetry.snapshot())


def execute(
    connection: sqlite3.Connection,
    context: RenameContext,
    progress: ProgressReporter,
    telemetry: MaterializationTelemetry,
    persisted_progress: WorkflowProgress,
    crash: Callable[[str], None],
) -> None:
    synchronizer = FilesystemSync(context.selected_root)
    try:
        operations, operation_digest, owner_secret = ensure_manifest(
            connection, context
        )
        tombstone = _staging_tombstone(context, owner_secret)
        output_durable = _has_event(connection, context.plan_id, EVENT_OUTPUT_DURABLE)
        quarantine_removed = _has_event(
            connection, context.plan_id, EVENT_QUARANTINE_REMOVED
        )
        teardown_started = (
            _exists(tombstone)
            or _has_event(connection, context.plan_id, EVENT_STAGING_TOMBSTONED)
            or _has_event(connection, context.plan_id, EVENT_STAGING_REMOVED)
            or (
                output_durable
                and quarantine_removed
                and not _exists(context.staging_root)
            )
        )
        if teardown_started:
            if not output_durable or not quarantine_removed:
                raise RenameProtectionError(
                    "RenameProtection staging teardown lacks durable prerequisites"
                )
            _verify_output(context, operations, telemetry)
            telemetry.report(progress, "STAGING_REMOVAL")
            _remove_staging_forward(
                connection,
                context,
                operation_digest,
                owner_secret,
                synchronizer,
                telemetry,
                crash,
            )
            crash("rename-protection-staging-removed")
            _complete(connection, context, telemetry, persisted_progress)
            return
        _ensure_owner(
            context,
            operations,
            operation_digest,
            owner_secret,
            synchronizer,
            crash,
        )
        operations = _establish_current_proof(
            connection,
            context,
            operations,
            telemetry,
            progress,
            persisted_progress,
            crash,
        )
        _stage_destructive_operations(
            connection,
            context,
            operations,
            synchronizer,
            telemetry,
            progress,
            persisted_progress,
            crash,
        )
        _publish(
            connection,
            context,
            operations,
            synchronizer,
            telemetry,
            progress,
            persisted_progress,
            crash,
        )
        persisted_progress.phase(
            "output-durable", total_count=len(operations), unit="operations"
        )
        telemetry.report(progress, "OUTPUT_DURABLE")
        _verify_output(context, operations, telemetry)
        synchronizer.sync(context.selected_root)
        telemetry.checkpoints += 1
        journal_append(
            connection,
            context.plan_id,
            EVENT_OUTPUT_DURABLE,
            detail="complete finalized output and retentions verified",
        )
        connection.execute(
            "UPDATE materialization_attempts SET state='PUBLISHED', "
            "updated_at=datetime('now') WHERE attempt_id=?",
            (context.attempt_id,),
        )
        connection.commit()
        telemetry.published()
        crash("rename-protection-output-durable")
        persisted_progress.phase("quarantine-cleanup", unit="entries")
        telemetry.report(progress, "QUARANTINE_CLEANUP")
        _cleanup_quarantine(
            connection, context, operations, synchronizer, telemetry, crash
        )
        telemetry.report(progress, "STAGING_REMOVAL")
        _remove_staging_forward(
            connection,
            context,
            operation_digest,
            owner_secret,
            synchronizer,
            telemetry,
            crash,
        )
        crash("rename-protection-staging-removed")
        _complete(connection, context, telemetry, persisted_progress)
    finally:
        synchronizer.close()
