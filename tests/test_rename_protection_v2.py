from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from filesystem_organizer.materialization import (
    MaterializationError,
    materialize_consolidation_plan,
)
from tests.test_cli import run_cli


def _finalized_relocation(
    tmp_path: Path,
    *,
    duplicate: bool = False,
    exclude_a: bool = False,
    file_over_source_directory: bool = False,
    nested_empty_directory: bool = False,
) -> tuple[Path, str, Path]:
    selected = tmp_path / "selected"
    (selected / "source").mkdir(parents=True)
    (selected / "source" / "a.bin").write_bytes(b"a" * 4096)
    (selected / "source" / "b.bin").write_bytes((b"a" if duplicate else b"b") * 4096)
    if file_over_source_directory:
        (selected / "source" / "type-tree").mkdir()
        (selected / "source" / "type-tree" / "leaf.bin").write_bytes(b"type-conflict")
    if nested_empty_directory:
        (selected / "source" / "empty" / "nested").mkdir(parents=True)
    output = tmp_path / "workspace"
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected),
        "--output-root",
        str(output),
        "--in-place",
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "layout.json"
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["rules"] = [
        {
            "selector": {"subtree": "source"},
            "action": {"place_under": "organized"},
        }
    ]
    entry_exceptions = []
    if exclude_a:
        with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
            entry_id = connection.execute(
                "SELECT entry_id FROM plan_baseline_entries "
                "WHERE plan_id=? AND baseline_path='source/a.bin'",
                (plan_id,),
            ).fetchone()[0]
        entry_exceptions.append({"entry_id": entry_id, "action": {"exclude": True}})
    if file_over_source_directory:
        with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
            entry_id = connection.execute(
                "SELECT entry_id FROM plan_baseline_entries "
                "WHERE plan_id=? AND baseline_path='source/type-tree/leaf.bin'",
                (plan_id,),
            ).fetchone()[0]
        entry_exceptions.append(
            {
                "entry_id": entry_id,
                "action": {"place_at": "source/type-tree"},
            }
        )
    if entry_exceptions:
        layout["entry_exceptions"] = entry_exceptions
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    applied = run_cli(
        tmp_path,
        "plan-layout-apply",
        *(["--acknowledge-exclusions"] if exclude_a else []),
        str(run_path),
        plan_id,
        str(layout_path),
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE analysis_run_execution_intent SET "
            "executor_kind='rename-protection', executor_version=2"
        )
    return run_path, plan_id, selected


def test_v2_normal_placement_never_reads_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import rename_protection_v2

    run_path, plan_id, selected = _finalized_relocation(tmp_path)

    def reject_payload_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("normal RenameProtection v2 opened payload")

    monkeypatch.setattr(rename_protection_v2, "digest_file", reject_payload_read)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert result["executor_version"] == 2
    assert (selected / "organized" / "a.bin").read_bytes() == b"a" * 4096
    assert (selected / "organized" / "b.bin").read_bytes() == b"b" * 4096
    telemetry = cast(dict[str, object], result["telemetry"])
    assert telemetry["payload_read_files"] == 0
    assert telemetry["payload_read_bytes"] == 0
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT manifest_version, executor_version "
            "FROM rename_protection_v2_manifests"
        ).fetchone() == (2, 2)
        assert set(
            connection.execute(
                "SELECT state FROM rename_protection_v2_operations"
            ).fetchall()
        ) == {("COMPLETE",)}


def test_v2_metadata_drift_is_local_and_preserves_the_object(tmp_path: Path) -> None:
    run_path, plan_id, selected = _finalized_relocation(tmp_path)
    drifted = selected / "source" / "a.bin"
    drifted.write_bytes(b"changed")
    os.utime(drifted, None)

    with pytest.raises(MaterializationError, match="needs attention"):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert drifted.read_bytes() == b"changed"
    assert (selected / "organized" / "b.bin").read_bytes() == b"b" * 4096
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        attention = connection.execute(
            "SELECT operations.source_relative_path, attention.phase, "
            "attention.retry_count, attention.reason "
            "FROM rename_protection_v2_attention AS attention "
            "JOIN rename_protection_v2_operations AS operations "
            "USING(attempt_id, operation_id)"
        ).fetchone()
        states = dict(
            connection.execute(
                "SELECT source_relative_path, state "
                "FROM rename_protection_v2_operations "
                "WHERE action='PLACE'"
            )
        )
        attempt_state = connection.execute(
            "SELECT state FROM rename_protection_v2_attempts"
        ).fetchone()[0]
        progress_state = connection.execute(
            "SELECT state FROM workflow_progress ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()[0]
    assert attention is not None
    assert attention["source_relative_path"] == "source/a.bin"
    assert attention["phase"] == "metadata-validation"
    assert attention["retry_count"] == 0
    assert "metadata" in attention["reason"]
    assert states == {"source/a.bin": "NEEDS_ATTENTION", "source/b.bin": "COMPLETE"}
    assert attempt_state == "NEEDS_ATTENTION"
    assert progress_state == "needs-attention"
    status = run_cli(tmp_path, "status", str(run_path))
    assert status.returncode == 0, status.stderr
    recovery = json.loads(status.stdout)["plans"][0]["recovery"]
    assert recovery["attempt_state"] == "NEEDS_ATTENTION"
    assert recovery["operation_counts"] == {
        "BLOCKED_BY_DEPENDENCY": 1,
        "COMPLETE": 1,
        "NEEDS_ATTENTION": 1,
    }
    assert recovery["attention"][0]["phase"] == "metadata-validation"
    assert recovery["resume_candidate"] is True
    assert recovery["blocked_dependencies"] == 1


def test_v2_unchanged_exact_duplicate_group_reads_zero_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import rename_protection_v2

    run_path, plan_id, selected = _finalized_relocation(tmp_path, duplicate=True)

    def reject_payload_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("normal duplicate handling opened payload")

    monkeypatch.setattr(rename_protection_v2, "digest_file", reject_payload_read)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    organized = list((selected / "organized").glob("*.bin"))
    assert len(organized) == 1
    assert organized[0].read_bytes() == b"a" * 4096
    assert cast(dict[str, object], result["telemetry"])["payload_read_bytes"] == 0
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        operations = connection.execute(
            "SELECT action, expected_byte_size, algorithm, algorithm_version, digest "
            "FROM rename_protection_v2_operations"
        ).fetchall()
        assert connection.execute(
            "SELECT state FROM rename_protection_v2_identity_groups"
        ).fetchone() == ("RETAINED_DURABLE",), operations


def test_v2_vacates_empty_source_directory_before_file_publication(
    tmp_path: Path,
) -> None:
    run_path, plan_id, selected = _finalized_relocation(
        tmp_path, file_over_source_directory=True
    )

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert (selected / "source" / "type-tree").read_bytes() == b"type-conflict"


def test_v2_rename_eio_retries_twice_then_continues_unrelated_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import rename_protection_v2

    run_path, plan_id, selected = _finalized_relocation(tmp_path)
    original = rename_protection_v2.LinuxFilesystemAdapter.rename
    failed_source = selected / "source" / "a.bin"
    attempts = 0

    def fail_one_source(
        adapter: rename_protection_v2.LinuxFilesystemAdapter,
        source: Path,
        destination: Path,
    ) -> None:
        nonlocal attempts
        if source == failed_source:
            attempts += 1
            raise OSError(5, "simulated EIO", source)
        original(adapter, source, destination)

    monkeypatch.setattr(
        rename_protection_v2.LinuxFilesystemAdapter, "rename", fail_one_source
    )

    with pytest.raises(MaterializationError, match="needs attention"):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert attempts == 3
    assert failed_source.read_bytes() == b"a" * 4096
    assert (selected / "organized" / "b.bin").read_bytes() == b"b" * 4096
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        attention = connection.execute(
            "SELECT errno, phase, retry_count FROM rename_protection_v2_attention"
        ).fetchone()
    assert attention == (5, "stage", 2)


def test_v2_proves_only_promoted_duplicate_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import rename_protection_v2

    run_path, plan_id, selected = _finalized_relocation(tmp_path, duplicate=True)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        intended_relative = str(
            connection.execute(
                "SELECT source_relative_path FROM final_plan_entries "
                "WHERE plan_id=? AND entry_kind='file'",
                (plan_id,),
            ).fetchone()[0]
        )
    intended = selected / intended_relative
    original_rename = rename_protection_v2.LinuxFilesystemAdapter.rename
    from filesystem_organizer.content_identity import digest_file as original_digest

    read_paths: list[Path] = []

    def fail_intended(
        adapter: rename_protection_v2.LinuxFilesystemAdapter,
        source: Path,
        destination: Path,
    ) -> None:
        if source == intended:
            raise OSError(5, "simulated EIO", source)
        original_rename(adapter, source, destination)

    def record_digest(path: Path, *, on_chunk: object = None) -> tuple[int, str]:
        read_paths.append(path)
        return original_digest(path, on_chunk=on_chunk)  # type: ignore[arg-type]

    monkeypatch.setattr(
        rename_protection_v2.LinuxFilesystemAdapter, "rename", fail_intended
    )
    monkeypatch.setattr(rename_protection_v2, "digest_file", record_digest)

    with pytest.raises(MaterializationError, match="needs attention"):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert intended.exists()
    assert len(read_paths) == 1
    assert ".filesystem-organizer-staging-v2-" in str(read_paths[0])
    organized = list((selected / "organized").glob("*.bin"))
    assert len(organized) == 1
    assert organized[0].read_bytes() == b"a" * 4096
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        selected_supplier, group_state = connection.execute(
            "SELECT selected_operation_id, state "
            "FROM rename_protection_v2_identity_groups"
        ).fetchone()
        intended_operation = connection.execute(
            "SELECT intended_operation_id FROM rename_protection_v2_identity_groups"
        ).fetchone()[0]
    assert selected_supplier != intended_operation
    assert group_state == "RETAINED_DURABLE"


def test_v2_recovers_rename_that_led_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected = _finalized_relocation(tmp_path)
    monkeypatch.setenv(
        materialization.CRASH_POINT_ENVIRONMENT,
        "rename-protection-v2-after-stage-rename",
    )
    with pytest.raises(materialization._CrashSimulation):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    assert any(selected.glob(".filesystem-organizer-staging-v2-*"))
    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert not any(selected.glob(".filesystem-organizer-staging-v2-*"))
    assert (selected / "organized" / "a.bin").read_bytes() == b"a" * 4096
    assert (
        cast(dict[str, object], result["telemetry"])["resume_skipped_operations"] == 0
    )


def test_v2_failed_durability_barrier_is_a_global_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import rename_protection_v2

    run_path, plan_id, _selected = _finalized_relocation(tmp_path)

    def fail_sync(_adapter: object) -> float:
        raise OSError(5, "simulated syncfs EIO")

    monkeypatch.setattr(rename_protection_v2.LinuxFilesystemAdapter, "sync", fail_sync)
    with pytest.raises(MaterializationError, match="durability barrier failed"):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        v2_state = connection.execute(
            "SELECT state FROM rename_protection_v2_attempts"
        ).fetchone()[0]
        legacy_state = connection.execute(
            "SELECT state FROM materialization_attempts"
        ).fetchone()[0]
        progress_state = connection.execute(
            "SELECT state FROM workflow_progress ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()[0]
    assert (v2_state, legacy_state, progress_state) == ("FAILED", "FAILED", "failed")


def test_v2_exclusion_is_metadata_only_and_drift_is_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import rename_protection_v2

    run_path, plan_id, selected = _finalized_relocation(tmp_path, exclude_a=True)
    excluded = selected / "source" / "a.bin"
    excluded.write_bytes(b"drifted exclusion")

    def reject_payload_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("exclusion policy attempted payload hashing")

    monkeypatch.setattr(rename_protection_v2, "digest_file", reject_payload_read)
    with pytest.raises(MaterializationError, match="needs attention"):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert excluded.read_bytes() == b"drifted exclusion"
    assert (selected / "organized" / "b.bin").read_bytes() == b"b" * 4096
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        action, state = connection.execute(
            "SELECT action, state FROM rename_protection_v2_operations "
            "WHERE source_relative_path='source/a.bin'"
        ).fetchone()
        metrics = json.loads(
            connection.execute(
                "SELECT metrics_json FROM rename_protection_v2_attempts "
                "WHERE state='NEEDS_ATTENTION'"
            ).fetchone()[0]
        )
    assert (action, state) == ("DROP_EXCLUDED", "NEEDS_ATTENTION")
    assert metrics["payload_read_bytes"] == 0


def _leave_payload_empty_v1_attempt(run_path: Path, plan_id: str) -> str:
    from filesystem_organizer import materialization, rename_protection
    from filesystem_organizer.progress import (
        MaterializationTelemetry,
        NullProgressReporter,
    )
    from filesystem_organizer.run_workspace import (
        ensure_journal_schema,
        open_read_write,
    )
    from filesystem_organizer.workflow_progress import WorkflowProgress

    preflight = materialization.preflight_materialization_for_execution(
        run_path, plan_id, in_place=True
    )
    connection = open_read_write(run_path / "analysis.sqlite3")
    ensure_journal_schema(connection)
    materialization._ensure_execution_manifest(
        connection, preflight, rename_protection.staging_name(plan_id)
    )
    attempt = connection.execute(
        "SELECT attempts.attempt_id, projection.revision, projection.fingerprint "
        "FROM materialization_attempts AS attempts "
        "JOIN final_plan_projection AS projection USING(plan_id) "
        "WHERE attempts.plan_id=?",
        (plan_id,),
    ).fetchone()
    attempt_id = str(attempt["attempt_id"])
    progress = WorkflowProgress(
        connection,
        "materialization",
        preflight.run_id,
        plan_id=plan_id,
        attempt_id=attempt_id,
    )
    context = rename_protection.RenameContext(
        attempt_id,
        plan_id,
        preflight.run_id,
        int(attempt["revision"]),
        str(attempt["fingerprint"]),
        Path(preflight.selected_backup_root),
        Path(preflight.selected_backup_root) / rename_protection.staging_name(plan_id),
    )

    class StopAfterProof(BaseException):
        pass

    def crash(point: str) -> None:
        if point == "rename-protection-after-proof-batch-commit":
            raise StopAfterProof

    try:
        with pytest.raises(StopAfterProof):
            rename_protection.execute(
                connection,
                context,
                NullProgressReporter(),
                MaterializationTelemetry(),
                progress,
                crash,
            )
    finally:
        connection.close()
    return attempt_id


def test_v2_safely_supersedes_payload_empty_v1_attempt(tmp_path: Path) -> None:
    run_path, plan_id, selected = _finalized_relocation(tmp_path)
    old_attempt = _leave_payload_empty_v1_attempt(run_path, plan_id)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["executor_version"] == 2
    assert (selected / "organized" / "a.bin").read_bytes() == b"a" * 4096
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        attempts = connection.execute(
            "SELECT attempt_id, state FROM rename_protection_v2_attempts "
            "ORDER BY created_at, attempt_id"
        ).fetchall()
    assert (old_attempt, "SUPERSEDED") in attempts
    assert any(state == "COMPLETE" for _attempt_id, state in attempts)


def test_v2_refuses_v1_supersession_with_unexpected_staged_payload(
    tmp_path: Path,
) -> None:
    run_path, plan_id, selected = _finalized_relocation(tmp_path)
    _leave_payload_empty_v1_attempt(run_path, plan_id)
    v1_staging = selected / f".filesystem-organizer-staging-v1-{plan_id}"
    (v1_staging / "unexpected.bin").write_bytes(b"preserve me")

    with pytest.raises(MaterializationError, match="unexpected staged payload"):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert (v1_staging / "unexpected.bin").read_bytes() == b"preserve me"
