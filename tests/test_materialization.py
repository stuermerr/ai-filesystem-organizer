from __future__ import annotations

import errno
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from filesystem_organizer.materialization import materialize_consolidation_plan
from tests.test_cli import run_cli, tree_digest


def _scan_plan_finalize(
    tmp_path: Path, *, in_place: bool = False
) -> tuple[Path, str, Path, Path]:
    """Scan the example fixture, draft a plan, and finalize it.

    Returns (run_path, plan_id, output_root, selected_root).
    """
    from tests.example_fixture import copy_prepared_source

    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(output_root),
        *(["--in-place"] if in_place else []),
    )
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    run_path = Path(scan_result["analysis_run"])

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    finalize = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalize.returncode == 0, finalize.stderr

    return run_path, plan_id, output_root, selected_root


def _select_rename_protection(run_path: Path) -> None:
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE analysis_run_execution_intent SET executor_kind='rename-protection', "
            "executor_version=1"
        )


def _select_rename_protection_v3(run_path: Path) -> None:
    """Select the newly admitted executor in a disposable public workflow."""
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE analysis_run_execution_intent SET executor_kind='rename-protection', "
            "executor_version=3"
        )


def _rename_swap_plan(tmp_path: Path) -> tuple[Path, str, Path]:
    selected_root = tmp_path / "swap-root"
    (selected_root / "folder").mkdir(parents=True)
    (selected_root / "folder" / "a.txt").write_bytes(b"A")
    (selected_root / "folder" / "b.txt").write_bytes(b"B")
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(tmp_path / "swap-output"),
        "--in-place",
    )
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "swap-layout.json"
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        entry_ids = dict(
            connection.execute(
                "SELECT baseline_path, entry_id FROM plan_baseline_entries "
                "WHERE plan_id=? AND entry_kind='file'",
                (plan_id,),
            )
        )
    layout = json.loads(layout_path.read_text())
    layout["entry_exceptions"] = [
        {
            "entry_id": entry_ids["folder/a.txt"],
            "action": {"place_at": "folder/b.txt"},
        },
        {
            "entry_id": entry_ids["folder/b.txt"],
            "action": {"place_at": "folder/a.txt"},
        },
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    _select_rename_protection(run_path)
    return run_path, plan_id, selected_root


def _rename_metadata_plan(
    tmp_path: Path,
    *,
    source: str = "unique.txt",
    destination: str = "folder/moved.txt",
) -> tuple[Path, str, Path]:
    selected_root = tmp_path / "metadata-root"
    source_path = selected_root / source
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"unique-size payload")
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(tmp_path / "metadata-output"),
        "--in-place",
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "metadata-layout.json"
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        entry_id = connection.execute(
            "SELECT entry_id FROM plan_baseline_entries WHERE plan_id=? "
            "AND baseline_path=?",
            (plan_id, source),
        ).fetchone()[0]
    layout = json.loads(layout_path.read_text())
    if source == "unique.txt":
        layout["directories"] = ["folder"]
    layout["entry_exceptions"] = [
        {"entry_id": entry_id, "action": {"place_at": destination}}
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    _select_rename_protection(run_path)
    return run_path, plan_id, selected_root


def _scan_simple_plan_finalize(
    tmp_path: Path, *, extra_files: int = 0
) -> tuple[Path, str, Path, Path]:
    """Create a non-structural plan for operation-level materialization tests."""
    selected_root = tmp_path / "selected-backup-root"
    control = selected_root / "Control"
    control.mkdir(parents=True)
    (control / "a-first.txt").write_bytes(b"published before later operations")
    for index in range(extra_files):
        (control / f"extra-{index:03d}.txt").write_bytes(
            f"extra staged file {index}".encode()
        )
    (control / "meeting-notes.txt").write_bytes(b"meeting notes")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(output_root),
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    finalize = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalize.returncode == 0, finalize.stderr
    return run_path, plan_id, output_root, selected_root


def _structural_plan(tmp_path: Path) -> tuple[Path, str, Path]:
    selected_root = tmp_path / "backup"
    for name in ("first", "second"):
        directory = selected_root / name
        directory.mkdir(parents=True)
        (directory / "same.txt").write_bytes(b"same structural content")
        (directory / "empty").mkdir()
        for region in ("shared-region-one", "shared-region-two"):
            (directory / region).mkdir()
            (directory / region / "anchor.txt").write_text(region)
    link = selected_root / "first" / "unverified-link"
    link.symlink_to("same.txt")
    (selected_root / "second" / "unverified-link").symlink_to("same.txt")
    output_root = tmp_path / "output"
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(output_root),
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    return run_path, plan_id, selected_root


@pytest.mark.parametrize(
    "mutation",
    [
        "added",
        "removed",
        "same_size_content_changed",
        "type_changed",
        "empty_directory_changed",
        "newly_unprocessable",
        "formerly_unprocessable_changed",
        "directory_newly_unprocessable",
    ],
)
def test_structural_snapshot_revalidation_refuses_live_evidence_drift_before_destination_creation(
    tmp_path: Path, mutation: str
) -> None:
    run_path, plan_id, selected_root = _structural_plan(tmp_path)
    first = selected_root / "first"
    restore_permissions: tuple[Path, int] | None = None
    if mutation == "added":
        (first / "added.txt").write_bytes(b"new")
    elif mutation == "removed":
        (first / "same.txt").unlink()
    elif mutation == "same_size_content_changed":
        (first / "same.txt").write_bytes(b"X" * len(b"same structural content"))
    elif mutation == "type_changed":
        (first / "same.txt").unlink()
        (first / "same.txt").mkdir()
    elif mutation == "empty_directory_changed":
        (first / "empty" / "new.txt").write_bytes(b"not empty")
    elif mutation == "newly_unprocessable":
        (first / "same.txt").unlink()
        (first / "same.txt").symlink_to("missing-target")
    elif mutation == "formerly_unprocessable_changed":
        (first / "unverified-link").unlink()
        (first / "unverified-link").symlink_to("different-target")
    else:
        unreadable = first / "empty"
        restore_permissions = (unreadable, unreadable.stat().st_mode)
        unreadable.chmod(0)

    destination = tmp_path / "must-not-exist"
    try:
        materialize = run_cli(
            tmp_path,
            "materialize",
            str(run_path),
            plan_id,
            "--destination",
            str(destination),
            "--yes",
        )
    finally:
        if restore_permissions is not None:
            path, mode = restore_permissions
            path.chmod(mode)

    assert materialize.returncode == 1
    assert "Structural Snapshot Revalidation failed" in materialize.stderr
    assert not destination.exists()


def test_materialization_revalidates_structural_evidence_once_after_plan_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root = _structural_plan(tmp_path)
    destination = tmp_path / "must-not-exist"
    original = materialization._revalidate_structural_snapshot
    calls = 0

    def mutate_before_post_approval_check(*args: object) -> set[str]:
        nonlocal calls
        calls += 1
        (selected_root / "first" / "same.txt").write_bytes(
            b"X" * len(b"same structural content")
        )
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(
        materialization,
        "_revalidate_structural_snapshot",
        mutate_before_post_approval_check,
    )
    with pytest.raises(
        materialization.MaterializationError,
        match="Structural Snapshot Revalidation failed",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert calls == 1
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        events = connection.execute(
            "SELECT event FROM materialization_events WHERE plan_id = ? ORDER BY seq",
            (plan_id,),
        ).fetchall()
    assert events == []
    assert not destination.exists()


def test_interrupted_normal_materialization_preserves_owned_partial_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _selected_root = _structural_plan(tmp_path)
    original = materialization._revalidate_structural_snapshot
    calls = 0

    def count_structural_revalidations(*args: object) -> set[str]:
        nonlocal calls
        calls += 1
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(
        materialization,
        "_revalidate_structural_snapshot",
        count_structural_revalidations,
    )
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, "postwritten")
    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    resumed = materialization.materialize_consolidation_plan(run_path, plan_id)

    assert resumed["status"] == "materialized"
    assert calls == 2


def test_resumed_materialization_refuses_structural_drift(tmp_path: Path) -> None:
    run_path, plan_id, selected_root = _structural_plan(tmp_path)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="admission")
    assert crashed.returncode != 0
    (selected_root / "first" / "same.txt").write_bytes(b"changed structural content")

    resumed = _materialize_cli(tmp_path, run_path, plan_id)

    assert resumed.returncode == 1
    assert "Structural Snapshot Revalidation failed" in resumed.stderr


def test_materialization_journal_indexes_operation_event_lookup(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    materialize_consolidation_plan(run_path, plan_id)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        index_columns = {
            tuple(row[2] for row in connection.execute(f"PRAGMA index_info({name})"))
            for _seq, name, _unique, _origin, _partial in connection.execute(
                "PRAGMA index_list(materialization_events)"
            )
        }
    assert ("plan_id", "operation_index", "seq") in index_columns


def test_preflight_counts_multi_root_identity_component_as_one_structural_union(
    tmp_path: Path,
) -> None:
    from filesystem_organizer import materialization

    selected_root = tmp_path / "backup"
    for name in ("alpha", "beta", "gamma", "delta"):
        directory = selected_root / name
        directory.mkdir(parents=True)
        (directory / "same.txt").write_bytes(b"same")
        for region in ("shared-region-one", "shared-region-two"):
            (directory / region).mkdir()
            (directory / region / "anchor.txt").write_text(region)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr

    preflight = materialization.preflight_materialization(run_path, plan_id)

    assert preflight.structural_union_count == 1


def test_materialize_rejects_draft_plan(tmp_path: Path) -> None:
    from tests.example_fixture import copy_prepared_source

    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 1
    assert "not finalized" in materialize.stderr


def test_materialize_refuses_an_active_run_workspace_lock(tmp_path: Path) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import nonblocking_lock

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )

    with (
        nonblocking_lock(run_path / ".filesystem-organizer.lock"),
        pytest.raises(materialization.MaterializationError, match="already holds"),
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id)


def test_normal_partial_destination_is_sibling_of_final_path(tmp_path: Path) -> None:
    from filesystem_organizer import materialization

    destination = tmp_path / "materialized" / "result"

    assert materialization._partial_destination(destination, "plan-1") == (
        destination.parent / ".result.partial-plan-1"
    )


def test_materialize_refuses_an_active_destination_lock(tmp_path: Path) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import (
        destination_lock_path,
        nonblocking_lock,
    )

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    preflight = materialization.preflight_materialization_for_execution(
        run_path, plan_id
    )

    with (
        nonblocking_lock(destination_lock_path(Path(preflight.destination))),
        pytest.raises(materialization.MaterializationError, match="already holds"),
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id)


def test_plan_finalization_refuses_an_active_run_workspace_lock(tmp_path: Path) -> None:
    from filesystem_organizer.consolidation_plan import (
        ConsolidationPlanError,
        finalize_consolidation_plan,
    )
    from filesystem_organizer.linux_filesystem import nonblocking_lock

    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    with (
        nonblocking_lock(run_path / ".filesystem-organizer.lock"),
        pytest.raises(ConsolidationPlanError, match="already holds"),
    ):
        finalize_consolidation_plan(run_path, plan_id)


def test_materialize_finalizes_draft_with_exact_revision(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    materialized = run_cli(
        tmp_path, "materialize", str(run_path), plan_id, "--revision", "0", "--yes"
    )

    assert materialized.returncode == 0, materialized.stderr
    assert json.loads(materialized.stdout)["status"] == "materialized"


def test_materialize_refuses_stale_draft_revision(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    materialized = run_cli(
        tmp_path, "materialize", str(run_path), plan_id, "--revision", "1", "--yes"
    )

    assert materialized.returncode == 1
    assert "Layout Revision" in materialized.stderr


def test_materialize_requires_yes_or_tty_for_headless_approval(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id)

    assert materialize.returncode == 1
    assert "requires --yes" in materialize.stderr


@pytest.mark.parametrize("occupied", [False, True])
def test_materialize_rejects_existing_destination(
    tmp_path: Path, occupied: bool
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    destination = tmp_path / "destination"
    destination.mkdir()
    if occupied:
        (destination / "stray.txt").write_bytes(b"already here")

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--destination",
        str(destination),
        "--yes",
    )

    assert materialize.returncode == 1
    assert "already exists" in materialize.stderr


def test_materialize_rejects_destination_inside_selected_backup_root(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)

    unsafe_destination = selected_root / "materialized-here"

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--destination",
        str(unsafe_destination),
        "--yes",
    )

    assert materialize.returncode == 1
    assert "outside the Selected Backup Root" in materialize.stderr
    assert not unsafe_destination.exists()


def test_preflight_allows_cross_filesystem_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)
    monkeypatch.setattr(
        materialization, "_source_same_filesystem", lambda *_args: False
    )

    assert (
        materialization.preflight_materialization(run_path, plan_id).plan_id == plan_id
    )


def test_materialize_rejects_insufficient_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda path: types.SimpleNamespace(total=0, used=0, free=0),
    )

    with pytest.raises(
        materialization.MaterializationError, match="Insufficient free space"
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id)


def test_materialize_rejects_changed_source_before_publication(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )

    changed_file = selected_root / "Control" / "meeting-notes.txt"
    original_bytes = changed_file.read_bytes()
    changed_file.write_bytes(b"x" * len(original_bytes))

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 1
    assert "metadata" in materialize.stderr


def test_materialize_rejects_unsupported_content_identity_algorithm(
    tmp_path: Path,
) -> None:
    import sqlite3

    from filesystem_organizer import analysis_run

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)
    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE plan_operations SET algorithm = 'MD5' "
            "WHERE plan_id = ? AND evidence_kind = 'content-identity'",
            (plan_id,),
        )
        connection.commit()

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 1
    assert "unsupported content-identity algorithm" in materialize.stderr


def test_materialize_rejects_destination_that_is_a_regular_file(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    destination = tmp_path / "not-a-directory"
    destination.write_bytes(b"occupied")

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--destination",
        str(destination),
        "--yes",
    )

    assert materialize.returncode == 1
    assert "destination already exists" in materialize.stderr
    assert destination.read_bytes() == b"occupied"


def test_materialize_happy_path_custom_destination(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)
    custom_destination = tmp_path / "custom-materialized"
    output_paths = _operation_output_paths(run_path, plan_id)

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--destination",
        str(custom_destination),
        "--yes",
    )
    assert materialize.returncode == 0, materialize.stderr
    result = json.loads(materialize.stdout)
    assert "Staging Materialized Consolidation:" in materialize.stderr
    assert result["status"] == "materialized"
    assert result["operation_count"] == 28
    assert Path(result["destination"]) == custom_destination.resolve()

    assert not (custom_destination / materialization_staging_name()).exists()
    for relative_path in output_paths:
        published = custom_destination / relative_path
        source = selected_root / relative_path
        assert published.is_file(), f"missing published output: {relative_path}"
        assert published.read_bytes() == source.read_bytes()

    assert tree_digest(selected_root) == before


def test_materialize_happy_path_black_box_cli(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)
    output_paths = _operation_output_paths(run_path, plan_id)

    plan_report = run_cli(tmp_path, "plan-report", str(run_path), "--plan-id", plan_id)
    assert plan_report.returncode == 0, plan_report.stderr

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialize.returncode == 0, materialize.stderr
    result = json.loads(materialize.stdout)
    assert result["plan_id"] == plan_id
    assert result["status"] == "materialized"
    assert result["operation_count"] == 28

    destination = Path(result["destination"])
    assert destination.is_dir()
    assert not (destination / materialization_staging_name()).exists()

    for relative_path in output_paths:
        published = destination / relative_path
        source = selected_root / relative_path
        assert published.is_file(), f"missing published output: {relative_path}"
        assert published.read_bytes() == source.read_bytes()

    assert tree_digest(selected_root) == before

    repeat = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert repeat.returncode == 1
    assert "missing analysis.sqlite3" in repeat.stderr
    assert not (destination / materialization_staging_name()).exists()
    for relative_path in output_paths:
        assert (destination / relative_path).is_file()


def test_materialize_in_place_applies_finalized_projection_after_explicit_acknowledgement(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )

    refused = run_cli(
        tmp_path, "materialize", str(run_path), plan_id, "--in-place", "--yes"
    )
    assert refused.returncode == 1
    assert "--acknowledge-destructive" in refused.stderr

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--in-place",
        "--acknowledge-destructive",
    )

    assert materialize.returncode == 0, materialize.stderr
    result = json.loads(materialize.stdout)
    assert "Preparing in-place materialization..." in materialize.stderr
    assert "Protecting originals:" in materialize.stderr
    assert "Publishing files:" in materialize.stderr
    assert result["status"] == "materialized-in-place"
    assert Path(result["destination"]) == selected_root
    assert not any(selected_root.glob(".filesystem-organizer-staging-*"))
    for relative_path in _operation_output_paths(run_path, plan_id):
        assert (selected_root / relative_path).is_file(), relative_path

    repeated = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--in-place",
        "--acknowledge-destructive",
    )
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["status"] == "materialized-in-place"


def test_rename_protection_materializes_without_writing_payload_bytes(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    _select_rename_protection(run_path)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["executor_kind"] == "rename-protection"
    assert result["executor_version"] == 2
    telemetry = cast(dict[str, object], result["telemetry"])
    assert telemetry["copied_bytes"] == 0
    assert telemetry["staged_files"]
    assert not any(selected_root.glob(".filesystem-organizer-staging-*"))
    for relative_path in _operation_output_paths(run_path, plan_id):
        assert (selected_root / relative_path).is_file(), relative_path
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        manifest = connection.execute(
            "SELECT executor_kind, executor_version, operation_set_digest, owner_secret "
            "FROM rename_protection_v2_manifests"
        ).fetchone()
        actions = {
            row[0]
            for row in connection.execute(
                "SELECT action FROM rename_protection_v2_operations"
            )
        }
    assert manifest is not None
    assert manifest[:2] == ("rename-protection", 2)
    assert manifest[2] and manifest[3]
    assert {"PLACE", "DROP_EXACT_DUPLICATE", "REMOVE_EMPTY_SOURCE_DIRECTORY"} <= actions
    assert "RETAIN_SKIPPED" not in actions


def test_rename_protection_recovers_a_rename_before_its_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    _select_rename_protection(run_path)
    monkeypatch.setenv(
        materialization.CRASH_POINT_ENVIRONMENT,
        "rename-protection-after-stage-rename",
    )

    with pytest.raises(materialization._CrashSimulation):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert any(selected_root.glob(".filesystem-organizer-staging-v2-*"))
    status = run_cli(tmp_path, "status", str(run_path))
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["execution_intent"]["executor_kind"] == "rename-protection"
    recovery = status_payload["plans"][0]["recovery"]
    assert recovery["executor_kind"] == "rename-protection"
    assert recovery["staging_ownership"] == "proven"
    assert recovery["resume_candidate"] is True
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    resumed = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert resumed["status"] == "materialized-in-place"
    assert not any(selected_root.glob(".filesystem-organizer-staging-*"))


def test_rename_protection_breaks_path_swaps_through_complete_evacuation(
    tmp_path: Path,
) -> None:
    run_path, plan_id, selected_root = _rename_swap_plan(tmp_path)

    materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert (selected_root / "folder" / "a.txt").read_bytes() == b"B"
    assert (selected_root / "folder" / "b.txt").read_bytes() == b"A"


def test_rename_protection_evacuates_duplicate_at_different_planned_output(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "duplicate-output-root"
    folder = selected_root / "folder"
    folder.mkdir(parents=True)
    duplicate = b"duplicate payload"
    replacement = b"different replacement"
    (folder / "a.txt").write_bytes(duplicate)
    (folder / "b.txt").write_bytes(duplicate)
    (folder / "c.txt").write_bytes(replacement)
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(tmp_path / "duplicate-output-workspace"),
        "--in-place",
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "duplicate-output-layout.json"
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        entry_ids = dict(
            connection.execute(
                "SELECT baseline_path, entry_id FROM plan_baseline_entries "
                "WHERE plan_id=? AND entry_kind='file'",
                (plan_id,),
            )
        )
        duplicate_entry_id = connection.execute(
            "SELECT entry_id FROM plan_baseline_entries WHERE plan_id=? "
            "AND baseline_path IN ('folder/a.txt', 'folder/b.txt')",
            (plan_id,),
        ).fetchone()[0]
    layout = json.loads(layout_path.read_text())
    layout["entry_exceptions"] = [
        {
            "entry_id": duplicate_entry_id,
            "action": {"place_at": "folder/a.txt"},
        },
        {
            "entry_id": entry_ids["folder/c.txt"],
            "action": {"place_at": "folder/b.txt"},
        },
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    _select_rename_protection(run_path)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert (folder / "a.txt").read_bytes() == duplicate
    assert (folder / "b.txt").read_bytes() == replacement
    assert not (folder / "c.txt").exists()


def test_rename_protection_never_overwrites_a_new_destination_occupant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root = _rename_swap_plan(tmp_path)
    monkeypatch.setenv(
        materialization.CRASH_POINT_ENVIRONMENT,
        "rename-protection-v2-after-staging-commit",
    )
    with pytest.raises(materialization._CrashSimulation):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)
    (selected_root / "folder" / "a.txt").write_bytes(b"foreign")

    with pytest.raises(
        materialization.MaterializationError,
        match="needs attention",
    ):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert (selected_root / "folder" / "a.txt").read_bytes() == b"foreign"


def test_legacy_in_place_preflight_persists_compatibility_execution_intent(
    tmp_path: Path,
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute("DELETE FROM analysis_run_execution_intent")

    preflight = materialization.preflight_materialization(
        run_path, plan_id, in_place=True
    )

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        persisted = connection.execute(
            "SELECT execution_mode, executor_kind, executor_version "
            "FROM analysis_run_execution_intent"
        ).fetchone()
    assert persisted == (
        "in-place",
        preflight.executor_kind,
        preflight.executor_version,
    )


def test_in_place_preflight_preserves_a_persisted_rename_protection_v2_intent(
    tmp_path: Path,
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    _select_rename_protection(run_path)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE analysis_run_execution_intent SET executor_version=2"
        )

    preflight = materialization.preflight_materialization(
        run_path, plan_id, in_place=True
    )

    assert preflight.executor_kind == "rename-protection"
    assert preflight.executor_version == 2


def test_legacy_preflight_adds_missing_executor_version_column(
    tmp_path: Path,
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute("DROP TABLE analysis_run_execution_intent")
        connection.execute(
            "CREATE TABLE analysis_run_execution_intent ("
            "run_id TEXT PRIMARY KEY, execution_mode TEXT NOT NULL, "
            "executor_kind TEXT NOT NULL, admission_version INTEGER NOT NULL, "
            "admitted_at TEXT NOT NULL)"
        )

    preflight = materialization.preflight_materialization(
        run_path, plan_id, in_place=True
    )

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(analysis_run_execution_intent)"
            )
        }
        persisted = connection.execute(
            "SELECT executor_kind, executor_version FROM analysis_run_execution_intent"
        ).fetchone()
    assert "executor_version" in columns
    assert persisted == (preflight.executor_kind, 1)


def test_rename_protection_recovers_and_removes_opaque_skipped_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    selected_root = tmp_path / "selected"
    skipped = selected_root / "opaque"
    skipped.mkdir(parents=True)
    (skipped / "nested.txt").write_bytes(b"uninterpreted")
    outside = selected_root / "outside.txt"
    outside.write_bytes(b"must survive")
    (skipped / "outside-link").symlink_to(outside)
    skipped.chmod(0)
    try:
        scan = run_cli(
            tmp_path,
            "scan",
            str(selected_root),
            "--output-root",
            str(tmp_path / "output"),
            "--in-place",
        )
        assert scan.returncode == 0, scan.stderr
        run_path = Path(json.loads(scan.stdout)["analysis_run"])
        plan = run_cli(tmp_path, "plan", str(run_path))
        plan_id = str(json.loads(plan.stdout)["plan_id"])
        layout_path = tmp_path / "opaque-layout.json"
        exported = run_cli(
            tmp_path,
            "plan-layout-export",
            str(run_path),
            plan_id,
            "--output",
            str(layout_path),
        )
        assert exported.returncode == 0, exported.stderr
        layout = json.loads(layout_path.read_text())
        layout["skipped_actions"] = [{"path": "opaque", "action": "exclude"}]
        layout_path.write_text(json.dumps(layout))
        applied = run_cli(
            tmp_path,
            "plan-layout-apply",
            "--acknowledge-exclusions",
            str(run_path),
            plan_id,
            str(layout_path),
        )
        assert applied.returncode == 0, applied.stderr
        finalized = run_cli(
            tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
        )
        assert finalized.returncode == 0, finalized.stderr
        _select_rename_protection(run_path)

        monkeypatch.setenv(
            materialization.CRASH_POINT_ENVIRONMENT,
            "rename-protection-after-stage-rename",
        )
        with pytest.raises(materialization._CrashSimulation):
            materialize_consolidation_plan(run_path, plan_id, in_place=True)
        monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

        result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

        assert result["status"] == "materialized-in-place"
        assert not skipped.exists()
        assert outside.read_bytes() == b"must survive"
        assert not any(selected_root.glob(".filesystem-organizer-staging-*"))
    finally:
        if skipped.exists():
            skipped.chmod(0o700)


@pytest.mark.parametrize(
    "crash_point",
    [
        "rename-protection-v2-after-stage-rename",
        "rename-protection-v2-after-staging-sync",
        "rename-protection-v2-after-staging-commit",
        "rename-protection-v2-after-publish-rename",
        "rename-protection-v2-after-publication-sync",
        "rename-protection-v2-after-publication-commit",
        "rename-protection-v2-after-cleanup-sync",
        "rename-protection-v2-after-cleanup-commit",
    ],
)
def test_rename_protection_recovers_every_phase_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str
) -> None:
    from filesystem_organizer import materialization

    if crash_point == "rename-protection-v2-after-publish-rename":
        run_path, plan_id, selected_root = _rename_swap_plan(tmp_path)
    else:
        run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
            tmp_path, in_place=True
        )
        _select_rename_protection(run_path)
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, crash_point)
    with pytest.raises(materialization._CrashSimulation):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert not any(selected_root.glob(".filesystem-organizer-staging-*"))


def test_rename_protection_accepts_published_planned_output_as_recreated_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root = _rename_swap_plan(tmp_path)
    monkeypatch.setenv(
        materialization.CRASH_POINT_ENVIRONMENT,
        "rename-protection-v2-after-publish-rename",
    )
    with pytest.raises(materialization._CrashSimulation):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert (selected_root / "folder" / "a.txt").read_bytes() == b"B"
    assert (selected_root / "folder" / "b.txt").read_bytes() == b"A"


def test_rename_protection_refuses_mutated_owner_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization, rename_protection_v2

    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    _select_rename_protection(run_path)
    monkeypatch.setenv(
        materialization.CRASH_POINT_ENVIRONMENT,
        "rename-protection-after-stage-rename",
    )
    with pytest.raises(materialization._CrashSimulation):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)
    owner = (
        next(selected_root.glob(".filesystem-organizer-staging-v2-*"))
        / rename_protection_v2.OWNER_FILE
    )
    owner.write_text('{"foreign":true}', encoding="utf-8")

    with pytest.raises(materialization.MaterializationError, match="ownership|foreign"):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)


def test_rename_protection_refuses_unexplained_staging_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    _select_rename_protection(run_path)
    monkeypatch.setenv(
        materialization.CRASH_POINT_ENVIRONMENT,
        "rename-protection-after-stage-rename",
    )
    with pytest.raises(materialization._CrashSimulation):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)
    staging = next(selected_root.glob(".filesystem-organizer-staging-v2-*"))
    (staging / "foreign-entry").write_bytes(b"foreign")

    with pytest.raises(
        materialization.MaterializationError, match="unexplained payload"
    ):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)


def test_in_place_materializes_a_relocated_structural_union_without_self_induced_drift(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("snapshot-2023", "snapshot-2025"):
        directory = selected_root / name / "Home" / "Desktop"
        directory.mkdir(parents=True)
        (directory / "same.txt").write_bytes(b"same structural content")
    (selected_root / "snapshot-2025" / "Home" / "Desktop" / "newer.txt").write_bytes(
        b"only in newer snapshot"
    )
    (
        selected_root / "snapshot-2025" / "Home" / "Desktop" / "unverified-link"
    ).symlink_to("same.txt")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(output_root),
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
    layout = json.loads(layout_path.read_text())
    layout["rules"] = [
        {"selector": {"subtree": "snapshot-2025"}, "action": {"exclude": True}},
        {
            "selector": {"subtree": "snapshot-2025/Home"},
            "action": {"place_under": "Home"},
        },
    ]
    layout["skipped_actions"] = [
        {
            "path": "snapshot-2025/Home/Desktop/unverified-link",
            "action": "exclude",
        }
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path,
        "plan-layout-apply",
        "--acknowledge-exclusions",
        str(run_path),
        plan_id,
        str(layout_path),
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--in-place",
        "--acknowledge-destructive",
    )

    assert materialize.returncode == 0, materialize.stderr
    assert json.loads(materialize.stdout)["status"] == "materialized-in-place"


def test_in_place_preflight_refuses_an_unowned_staging_directory(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    (selected_root / f".filesystem-organizer-staging-{plan_id}").mkdir()

    preflight = run_cli(
        tmp_path, "materialize-preflight", str(run_path), plan_id, "--in-place"
    )

    assert preflight.returncode == 1
    assert "unexpected occupant" in preflight.stderr


def test_public_in_place_preflight_is_payload_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    monkeypatch.setattr(
        materialization,
        "digest_file",
        lambda *_args, **_kwargs: pytest.fail(
            "public preflight must not read source payloads"
        ),
    )

    preflight = materialization.preflight_materialization(
        run_path, plan_id, in_place=True
    )
    assert preflight.mode == "in-place"


def test_in_place_resumes_after_durable_protection_before_destructive_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, "postprotect")

    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)
    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert resumed["status"] == "materialized-in-place"
    assert not any(selected_root.glob(".filesystem-organizer-staging-*"))
    assert sorted(
        int(index)
        for event, index, _detail in _journal_events(run_path, plan_id)
        if event == "FILE_COMPLETED" and index is not None
    ) == _operation_indexes(run_path, plan_id)


def test_in_place_completion_follows_output_and_staging_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    monkeypatch.setattr(materialization, "OUTCOME_BATCH_SIZE", 1)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    events = [event for event, _index, _detail in _journal_events(run_path, plan_id)]
    assert "IN_PLACE_DUPLICATE_CLEANUP_CHECKPOINT" in events
    assert (
        events.index("IN_PLACE_OUTPUT_DURABLE")
        < events.index("IN_PLACE_STAGING_REMOVED")
        < events.index("MATERIALIZATION_COMPLETE")
    )
    telemetry = cast(dict[str, object], result["telemetry"])
    assert telemetry["version"] == 2
    assert cast(int, telemetry["checkpoints"]) >= 4
    assert telemetry["phase"] == "complete"


@pytest.mark.parametrize(
    "crash_point",
    [
        "in-place-duplicate-cleanup-before-sync",
        "in-place-duplicate-cleanup-after-sync",
        "in-place-duplicate-cleanup-after-commit",
    ],
)
def test_in_place_duplicate_cleanup_resumes_at_each_checkpoint_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    monkeypatch.setattr(materialization, "OUTCOME_BATCH_SIZE", 1)
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, crash_point)

    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)
    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )
    assert resumed["status"] == "materialized-in-place"


def test_in_place_resumes_after_durable_staging_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    monkeypatch.setenv(
        materialization.CRASH_POINT_ENVIRONMENT, "in-place-staging-removed"
    )

    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert not any(selected_root.glob(".filesystem-organizer-staging-*"))
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)
    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )
    assert resumed["status"] == "materialized-in-place"


def test_in_place_staging_removed_recovery_refuses_same_size_output_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    monkeypatch.setenv(
        materialization.CRASH_POINT_ENVIRONMENT, "in-place-staging-removed"
    )
    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        relative_path, byte_size = connection.execute(
            "SELECT output_relative_path, expected_byte_size FROM final_plan_entries "
            "WHERE plan_id=? AND entry_kind='file' AND digest IS NOT NULL "
            "ORDER BY entry_index LIMIT 1",
            (plan_id,),
        ).fetchone()
    output = selected_root / str(relative_path)
    observed = output.stat()
    output.write_bytes(b"x" * int(byte_size))
    os.utime(output, ns=(observed.st_atime_ns, observed.st_mtime_ns))
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    with pytest.raises(
        materialization.MaterializationError,
        match="content no longer matches",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)


def test_in_place_fresh_execution_hashes_each_inode_at_most_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.content_identity import (
        digest_file as original_digest_file,
    )

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )
    calls: dict[tuple[int, int], int] = {}

    def observed_digest(path: Path) -> tuple[int, str]:
        observed = path.stat()
        key = (observed.st_dev, observed.st_ino)
        calls[key] = calls.get(key, 0) + 1
        return original_digest_file(path)

    monkeypatch.setattr(materialization, "digest_file", observed_digest)

    materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert calls
    assert max(calls.values()) == 1


def test_in_place_cleanup_never_walks_the_complete_selected_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )

    monkeypatch.setattr(
        Path,
        "rglob",
        lambda *_args, **_kwargs: pytest.fail(
            "in-place cleanup must use affected ancestors, not a full-tree walk"
        ),
    )

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)
    assert result["status"] == "materialized-in-place"


def test_in_place_cleanup_preserves_layout_owned_empty_directories(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "selected"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    output_root = tmp_path / "output"
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(output_root),
        "--in-place",
    )
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "empty-directory-layout.json"
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    layout = json.loads(layout_path.read_text())
    layout["directories"] = ["Empty"]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr

    materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert (selected_root / "Empty").is_dir()


def test_materialize_preserves_source_mode_and_modification_time(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    source = selected_root / "Control" / "meeting-notes.txt"
    source.chmod(0o640)
    expected_mtime_ns = source.stat().st_mtime_ns

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 0, materialize.stderr
    destination = Path(json.loads(materialize.stdout)["destination"])
    published = destination / "Control" / "meeting-notes.txt"
    assert stat.S_IMODE(published.stat().st_mode) == 0o640
    assert published.stat().st_mtime_ns == expected_mtime_ns


def test_materialize_records_one_compact_outcome_per_file(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)
    operation_indexes = _operation_indexes(run_path, plan_id)

    materialize_consolidation_plan(run_path, plan_id)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        evidence_indexes = [
            int(row[0])
            for row in connection.execute(
                "SELECT entry_index FROM execution_file_evidence WHERE plan_id = ? "
                "ORDER BY entry_index",
                (plan_id,),
            )
        ]
    assert evidence_indexes == operation_indexes
    assert _journal_events(run_path, plan_id) == []


def test_materialize_persists_a_source_preserving_execution_manifest(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    materialize_consolidation_plan(run_path, plan_id)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        manifest = connection.execute(
            "SELECT manifest_version, mode, run_id, canonical_destination "
            "FROM execution_manifests WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        entry_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM execution_manifest_entries AS entry "
                "JOIN execution_manifests AS manifest USING (attempt_id) "
                "WHERE manifest.plan_id = ?",
                (plan_id,),
            ).fetchone()[0]
        )
    assert manifest is not None
    assert manifest[0] == 3
    assert manifest[1] == "source-preserving"
    assert entry_count > 0


def test_normal_materialization_records_native_clone_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import CloneResult

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )

    def clone(source: Path, destination: Path) -> CloneResult:
        os.link(source, destination)
        return CloneResult.CLONED

    monkeypatch.setattr(materialization, "try_native_clone", clone)
    result = materialization.materialize_consolidation_plan(run_path, plan_id)

    assert result["status"] == "materialized"
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        evidence = connection.execute(
            "SELECT transfer_kind, algorithm, digest FROM execution_file_evidence "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        attempt = connection.execute(
            "SELECT cloned_file_count, streamed_file_count FROM materialization_attempts "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    assert evidence and all(row == ("native-clone", None, None) for row in evidence)
    assert attempt == (len(evidence), 0)


def test_normal_materialization_falls_back_to_streamed_copy_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import CloneResult

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )

    monkeypatch.setattr(
        materialization,
        "try_native_clone",
        lambda _source, _destination: CloneResult.UNAVAILABLE,
    )

    result = materialization.materialize_consolidation_plan(run_path, plan_id)

    assert result["status"] == "materialized"
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        evidence = connection.execute(
            "SELECT transfer_kind, algorithm, digest FROM execution_file_evidence "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        attempt = connection.execute(
            "SELECT cloned_file_count, streamed_file_count FROM materialization_attempts "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    assert evidence
    assert all(
        transfer_kind == "streamed" and algorithm == "BLAKE3-256" and digest
        for transfer_kind, algorithm, digest in evidence
    )
    assert attempt == (0, len(evidence))


def test_materialize_interactive_confirmation_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from filesystem_organizer.__main__ import main

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    exit_code = main(["materialize", str(run_path), plan_id])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Plan ID: {plan_id}" in captured.out
    assert "Operation count:" in captured.out
    assert "Total bytes:" in captured.out
    assert "Free space at destination:" in captured.out
    result = json.loads(captured.out.splitlines()[-1])
    assert result["status"] == "materialized"


def test_materialize_interactive_confirmation_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from filesystem_organizer.__main__ import main

    run_path, plan_id, output_root, _selected_root = _scan_plan_finalize(tmp_path)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")

    exit_code = main(["materialize", str(run_path), plan_id])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not approved" in captured.err
    assert not (output_root / "materialized").exists()


@pytest.mark.parametrize("extra_arguments", [[], ["--acknowledge-destructive"]])
def test_in_place_interactive_confirmation_requires_exact_selected_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra_arguments: list[str],
) -> None:
    from filesystem_organizer.__main__ import main

    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(
        tmp_path, in_place=True
    )

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    responses = iter(["yes", str(selected_root)])
    prompts: list[str] = []

    def respond(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", respond)

    arguments = ["materialize", str(run_path), plan_id, "--in-place", *extra_arguments]
    refused = main(arguments)
    refused_output = capsys.readouterr()

    exit_code = main(arguments)

    captured = capsys.readouterr()
    assert refused == 1
    assert "not approved" in refused_output.err
    assert exit_code == 0, captured.err
    assert all(str(selected_root) in prompt for prompt in prompts)
    assert (
        json.loads(captured.out.splitlines()[-1])["status"] == "materialized-in-place"
    )


def materialization_staging_name() -> str:
    from filesystem_organizer.materialization import STAGING_DIRECTORY_NAME

    return STAGING_DIRECTORY_NAME


def _operation_output_paths(run_path: Path, plan_id: str) -> list[str]:
    import sqlite3

    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT output_relative_path FROM final_plan_entries WHERE plan_id = ? "
            "AND entry_kind = 'file' ORDER BY entry_index",
            (plan_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _materialize_cli(
    tmp_path: Path,
    run_path: Path,
    plan_id: str,
    *,
    crash_point: str | None = None,
    destination: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = ["materialize", str(run_path), plan_id]
    if destination is not None:
        arguments += ["--destination", str(destination)]
    arguments += ["--yes"]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    if crash_point is not None:
        environment["FSO_MATERIALIZATION_CRASH_POINT"] = crash_point
    return subprocess.run(
        [sys.executable, "-m", "filesystem_organizer", *arguments],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _journal_events(
    run_path: Path, plan_id: str
) -> list[tuple[str, int | None, str | None]]:
    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT event, operation_index, detail FROM materialization_events "
            "WHERE plan_id = ? ORDER BY seq",
            (plan_id,),
        ).fetchall()
    return [(str(row[0]), row[1], row[2]) for row in rows]


def _operation_indexes(run_path: Path, plan_id: str) -> list[int]:
    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT entry_index FROM final_plan_entries WHERE plan_id = ? "
            "AND entry_kind = 'file' ORDER BY entry_index",
            (plan_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def _intended_destination(run_path: Path, plan_id: str) -> str:
    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT intended_destination FROM consolidation_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    return str(row[0])


def _reconciliation_details(run_path: Path, plan_id: str) -> list[str]:
    return [
        str(detail)
        for event, _index, detail in _journal_events(run_path, plan_id)
        if event == "RECONCILIATION" and detail is not None
    ]


def _finish_and_assert(
    tmp_path: Path,
    run_path: Path,
    plan_id: str,
    selected_root: Path,
) -> Path:
    """Resume an interrupted materialization and assert idempotent completion."""
    resume = _materialize_cli(tmp_path, run_path, plan_id)
    assert resume.returncode == 0, resume.stderr
    result = json.loads(resume.stdout)
    assert result["status"] == "materialized"
    destination = Path(result["destination"])
    assert not (destination / materialization_staging_name()).exists()
    for relative_path in _operation_output_paths(run_path, plan_id):
        published = destination / relative_path
        source = selected_root / relative_path
        assert published.is_file(), f"missing published output: {relative_path}"
        assert published.read_bytes() == source.read_bytes()
    events = _journal_events(run_path, plan_id)
    assert any(event == "MATERIALIZATION_COMPLETE" for event, _i, _d in events)
    completed_indexes = sorted(
        int(index)
        for event, index, _d in events
        if event == "FILE_COMPLETED" and index is not None
    )
    assert completed_indexes == sorted(_operation_indexes(run_path, plan_id))
    return destination


def test_materialize_crash_at_admission_resumes(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="admission")
    assert crashed.returncode != 0
    destination = Path(_intended_destination(run_path, plan_id))
    assert not destination.exists()
    resumed = _materialize_cli(tmp_path, run_path, plan_id)
    assert resumed.returncode == 0, resumed.stderr
    assert tree_digest(selected_root) == before


def test_materialize_crash_during_copy_preserves_an_unpublished_partial_tree(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="copying")
    assert crashed.returncode != 0

    destination = Path(_intended_destination(run_path, plan_id))
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    assert partial.exists()
    assert not destination.exists()
    resumed = _materialize_cli(tmp_path, run_path, plan_id)
    assert resumed.returncode == 0, resumed.stderr


def _interrupt_after_one_completed_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, Path, Path, list[str]]:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import CloneResult

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path, extra_files=2
    )
    destination = tmp_path / "resumed-result"
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    output_paths = _operation_output_paths(run_path, plan_id)
    original_copy = materialization._copy_to_temporary
    calls = 0

    def interrupt_second_copy(
        source: Path, temporary: Path, row: sqlite3.Row
    ) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            temporary.write_bytes(source.read_bytes()[:3])
            raise materialization._CrashSimulation("test interruption")
        return original_copy(source, temporary, row)

    monkeypatch.setattr(
        materialization,
        "try_native_clone",
        lambda _source, _destination: CloneResult.UNAVAILABLE,
    )
    monkeypatch.setattr(materialization, "OUTCOME_BATCH_SIZE", 1)
    monkeypatch.setattr(materialization, "_copy_to_temporary", interrupt_second_copy)
    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)
    monkeypatch.setattr(materialization, "_copy_to_temporary", original_copy)
    return run_path, plan_id, destination, partial, output_paths


def test_interrupted_staging_resumes_completed_files_and_recopies_current_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.control_plane import inspect_analysis_run

    run_path, plan_id, destination, partial, output_paths = (
        _interrupt_after_one_completed_staged_file(tmp_path, monkeypatch)
    )
    completed = partial / output_paths[0]
    completed_identity = completed.stat().st_ino
    incomplete = partial / f".partial-{_operation_indexes(run_path, plan_id)[1]}"
    assert completed.is_file()
    assert len(incomplete.read_bytes()) == 3
    uncheckpointed = partial / output_paths[2]
    uncheckpointed.parent.mkdir(parents=True, exist_ok=True)
    uncheckpointed.write_bytes(b"untrusted completed suffix")
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM execution_file_evidence WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()[0]
            == 1
        )

    status = inspect_analysis_run(run_path)
    plans = cast(list[dict[str, object]], status["plans"])
    recovery = cast(
        dict[str, object],
        next(plan for plan in plans if plan["plan_id"] == plan_id)["recovery"],
    )
    assert recovery["attempt_state"] == "STAGING_STARTED"
    assert recovery["staging_ownership"] == "proven"
    assert recovery["resume_candidate"] is True
    assert (
        recovery["resume_strategy"]
        == "reuse-durable-completed-files-and-recopy-incomplete"
    )

    recopied: list[str] = []
    original_copy = materialization._copy_to_temporary

    def track_copy(source: Path, temporary: Path, row: sqlite3.Row) -> tuple[int, str]:
        recopied.append(str(row["output_relative_path"]))
        return original_copy(source, temporary, row)

    monkeypatch.setattr(materialization, "_copy_to_temporary", track_copy)
    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )

    assert resumed["status"] == "materialized"
    assert recopied == output_paths[1:]
    assert (destination / output_paths[0]).stat().st_ino == completed_identity
    assert not partial.exists()


def test_interrupted_staging_rebinds_changed_inode_after_full_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.control_plane import inspect_analysis_run

    run_path, plan_id, destination, partial, output_paths = (
        _interrupt_after_one_completed_staged_file(tmp_path, monkeypatch)
    )
    observed = partial.stat()
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_manifests SET staging_root_ino = ? WHERE plan_id = ?",
            (observed.st_ino + 1, plan_id),
        )
        connection.commit()

    status = inspect_analysis_run(run_path)
    plans = cast(list[dict[str, object]], status["plans"])
    recovery = cast(
        dict[str, object],
        next(plan for plan in plans if plan["plan_id"] == plan_id)["recovery"],
    )
    assert recovery["staging_ownership"] == "revalidation-required"
    assert recovery["staging_identity"] == "changed-on-same-device"
    assert recovery["staging_identity_revalidation_required"] is True
    assert recovery["resume_candidate"] is True

    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )

    assert resumed["status"] == "materialized"
    assert not partial.exists()
    assert all((destination / path).is_file() for path in output_paths)
    published = destination.stat()
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        identity = connection.execute(
            "SELECT staging_root_dev, staging_root_ino FROM execution_manifests "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    assert identity == (published.st_dev, published.st_ino)


def test_interrupted_staging_inode_rebind_refuses_changed_owner_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.control_plane import inspect_analysis_run

    run_path, plan_id, destination, partial, _output_paths = (
        _interrupt_after_one_completed_staged_file(tmp_path, monkeypatch)
    )
    observed = partial.stat()
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_manifests SET staging_root_ino = ? WHERE plan_id = ?",
            (observed.st_ino + 1, plan_id),
        )
        connection.commit()
    (partial / materialization.PARTIAL_OWNER_FILE).write_text(
        "different owner\n", encoding="utf-8"
    )
    status = inspect_analysis_run(run_path)
    plans = cast(list[dict[str, object]], status["plans"])
    recovery = cast(
        dict[str, object],
        next(plan for plan in plans if plan["plan_id"] == plan_id)["recovery"],
    )
    assert recovery["staging_ownership"] == "not-proven"
    assert recovery["resume_candidate"] is False

    with pytest.raises(
        materialization.MaterializationError,
        match="partial staging ownership is ambiguous",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert partial.exists()
    assert not destination.exists()


def test_interrupted_staging_inode_rebind_refuses_changed_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, destination, partial, _output_paths = (
        _interrupt_after_one_completed_staged_file(tmp_path, monkeypatch)
    )
    observed = partial.stat()
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_manifests SET staging_root_dev = ?, staging_root_ino = ? "
            "WHERE plan_id = ?",
            (observed.st_dev + 1, observed.st_ino + 1, plan_id),
        )
        connection.commit()

    with pytest.raises(
        materialization.MaterializationError,
        match="partial staging ownership is ambiguous",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert partial.exists()
    assert not destination.exists()


def test_interrupted_staging_refuses_changed_completed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, destination, partial, output_paths = (
        _interrupt_after_one_completed_staged_file(tmp_path, monkeypatch)
    )
    (partial / output_paths[0]).write_bytes(b"changed staged content")
    observed = partial.stat()
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_manifests SET staging_root_ino = ? WHERE plan_id = ?",
            (observed.st_ino + 1, plan_id),
        )
        connection.commit()

    with pytest.raises(
        materialization.MaterializationError,
        match="completed staged file does not match",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)
    assert partial.exists()
    assert not destination.exists()


def test_interrupted_staging_refuses_ambiguous_extra_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, destination, partial, _output_paths = (
        _interrupt_after_one_completed_staged_file(tmp_path, monkeypatch)
    )
    (partial / "unexpected.txt").write_bytes(b"ambiguous")

    with pytest.raises(
        materialization.MaterializationError,
        match="staging structure is ambiguous",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)
    assert partial.exists()
    assert not destination.exists()


def test_materialize_crash_after_staging_written_never_publishes_a_partial_tree(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="postwritten")
    assert crashed.returncode != 0

    destination = Path(_intended_destination(run_path, plan_id))
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    assert partial.exists()
    assert not destination.exists()


def test_materialize_crash_with_durable_staging_owner_resumes_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    destination = tmp_path / "published-result"
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, "staging-durable")

    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert (partial / materialization.PARTIAL_OWNER_FILE).is_file()
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    def refuse_recopy(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("durable staged payload must not be recopied")

    monkeypatch.setattr(materialization, "_copy_to_temporary", refuse_recopy)
    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )

    assert resumed["status"] == "materialized"
    assert destination.is_dir()
    assert not (destination / materialization.PARTIAL_OWNER_FILE).exists()


def test_recovery_digest_refuses_path_replaced_during_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.content_identity import (
        stable_read as original_stable_read,
    )

    path = tmp_path / "staged.txt"
    path.write_bytes(b"original")

    def replace_after_read(
        read_path: Path,
        before: os.stat_result,
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> tuple[int, bool]:
        result = original_stable_read(read_path, before, on_chunk)
        path.unlink()
        path.write_bytes(b"replaced")
        return result

    monkeypatch.setattr(materialization, "stable_read", replace_after_read)

    with pytest.raises(
        materialization.MaterializationError,
        match="changed while being verified",
    ):
        materialization._stable_recovery_digest(path)


def test_staging_durability_is_checkpointed_in_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import (
        FilesystemSync,
    )

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path, extra_files=3
    )
    destination = tmp_path / "batched-result"
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    partial_sync_count = 0

    original_sync = FilesystemSync.sync

    def count_sync(self: FilesystemSync, path: Path) -> None:
        nonlocal partial_sync_count
        if path == partial:
            partial_sync_count += 1
        original_sync(self, path)

    monkeypatch.setattr(materialization, "OUTCOME_BATCH_SIZE", 2)
    monkeypatch.setattr(
        materialization, "STAGING_CHECKPOINT_BYTE_SIZE", 1024 * 1024 * 1024
    )
    monkeypatch.setattr(FilesystemSync, "sync", count_sync)

    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )

    assert result["operation_count"] == 5
    assert partial_sync_count == 5  # Three batches plus two phase boundaries.


def test_read_only_publication_failure_preserves_and_reuses_durable_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.control_plane import inspect_analysis_run
    from filesystem_organizer.linux_filesystem import (
        LinuxFilesystemError,
        publish_directory,
    )

    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    destination = tmp_path / "published-result"
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    original_publish = publish_directory

    def fail_read_only(_staging: Path, _destination: Path) -> None:
        try:
            raise OSError(errno.EROFS, os.strerror(errno.EROFS))
        except OSError as error:
            raise LinuxFilesystemError(
                "atomic no-replace publication failed"
            ) from error

    monkeypatch.setattr(materialization, "publish_directory", fail_read_only)
    with pytest.raises(materialization.MaterializationError, match="read-only"):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert partial.is_dir()
    status = inspect_analysis_run(run_path)
    plans = cast(list[dict[str, object]], status["plans"])
    plan_status = next(plan for plan in plans if plan["plan_id"] == plan_id)
    recovery = cast(dict[str, object], plan_status["recovery"])
    assert plan_status["materialization_status"] == "needs-attention"
    assert plan_status["recovery_available"] is True
    assert plan_status["recovery"] == {
        "attempt_id": recovery["attempt_id"],
        "attempt_state": "FAILED",
        "failure_classification": "retryable-environment",
        "failure_detail": (
            "destination filesystem became read-only during atomic publication "
            "(EROFS: Read-only file system)"
        ),
        "staging_path": str(partial),
        "staging_present": True,
        "staging_ownership": "proven",
        "staging_identity": "matches",
        "staging_identity_revalidation_required": False,
        "resume_candidate": True,
        "resume_strategy": "reuse-after-source-and-staging-integrity-revalidation",
    }

    monkeypatch.setattr(materialization, "publish_directory", original_publish)

    def refuse_recopy(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("durable staged payload must not be recopied")

    monkeypatch.setattr(materialization, "_copy_to_temporary", refuse_recopy)
    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )

    assert resumed["status"] == "materialized"
    assert not partial.exists()
    assert tree_digest(destination) == tree_digest(selected_root)


def test_unsupported_publication_reuses_durable_staging_with_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import linux_filesystem, materialization
    from filesystem_organizer.linux_filesystem import LinuxFilesystemError

    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    destination = tmp_path / "published-result"
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"

    def fail_like_previous_version(_staging: Path, _destination: Path) -> None:
        try:
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        except OSError as error:
            raise LinuxFilesystemError(
                "atomic no-replace publication failed"
            ) from error

    monkeypatch.setattr(
        materialization,
        "publish_directory",
        fail_like_previous_version,
    )
    with pytest.raises(
        materialization.MaterializationError, match="unsupported-capability"
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert partial.is_dir()
    assert not destination.exists()
    observed = partial.stat()
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_manifests SET staging_root_ino = ? WHERE plan_id = ?",
            (observed.st_ino + 1, plan_id),
        )
        connection.commit()
    from filesystem_organizer.control_plane import inspect_analysis_run

    status = inspect_analysis_run(run_path)
    plans = cast(list[dict[str, object]], status["plans"])
    plan_status = next(plan for plan in plans if plan["plan_id"] == plan_id)
    recovery = cast(dict[str, object], plan_status["recovery"])
    assert recovery["failure_classification"] == "unsupported-capability"
    assert recovery["resume_candidate"] is True
    assert recovery["staging_ownership"] == "revalidation-required"
    assert recovery["staging_identity"] == "changed-on-same-device"

    class FuseStyleSystem:
        def syncfs(self, descriptor: int) -> None:
            raise AssertionError("not used")

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            raise OSError(errno.EINVAL, "unsupported rename flag")

        def rename_plain(self, source: bytes, destination: bytes) -> None:
            Path(os.fsdecode(source)).rename(Path(os.fsdecode(destination)))

    def publish_with_fallback(staging: Path, final: Path) -> None:
        with (
            pytest.raises(LinuxFilesystemError, match="already holds"),
            linux_filesystem.nonblocking_lock(
                linux_filesystem.destination_lock_path(final)
            ),
        ):
            pass
        method = linux_filesystem.publish_directory(
            staging, final, system=FuseStyleSystem()
        )
        assert method is linux_filesystem.PublicationMethod.LOCKED_RENAME_FALLBACK

    monkeypatch.setattr(
        materialization,
        "publish_directory",
        publish_with_fallback,
    )

    def refuse_recopy(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("durable staged payload must not be recopied")

    monkeypatch.setattr(materialization, "_copy_to_temporary", refuse_recopy)
    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )

    assert resumed["status"] == "materialized"
    assert not partial.exists()
    assert tree_digest(destination) == tree_digest(selected_root)


def test_fallback_failure_is_not_classified_as_no_replace_capability() -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import LockedRenameFallbackError

    try:
        try:
            raise OSError(errno.EINVAL, "plain rename failed")
        except OSError as operating_system_error:
            raise LockedRenameFallbackError(
                "locked rename fallback publication failed"
            ) from operating_system_error
    except LockedRenameFallbackError as error:
        classification, detail = materialization._publication_failure(error)

    assert classification == "io-integrity-manual-attention"
    assert (
        detail == "locked rename fallback publication failed (EINVAL: Invalid argument)"
    )


def test_interrupted_fallback_publication_reconciles_without_recopy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import linux_filesystem, materialization

    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    destination = tmp_path / "published-result"
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"

    class InterruptedPublication(BaseException):
        pass

    class FuseStyleSystem:
        def syncfs(self, descriptor: int) -> None:
            raise AssertionError("not used")

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            raise OSError(errno.EINVAL, "unsupported rename flag")

        def rename_plain(self, source: bytes, destination: bytes) -> None:
            Path(os.fsdecode(source)).rename(Path(os.fsdecode(destination)))

    def publish_then_interrupt(staging: Path, final: Path) -> None:
        method = linux_filesystem.publish_directory(
            staging, final, system=FuseStyleSystem()
        )
        assert method is linux_filesystem.PublicationMethod.LOCKED_RENAME_FALLBACK
        raise InterruptedPublication

    with monkeypatch.context() as patch:
        patch.setattr(
            materialization,
            "publish_directory",
            publish_then_interrupt,
        )
        with pytest.raises(InterruptedPublication):
            materialization.materialize_consolidation_plan(
                run_path, plan_id, destination
            )

    assert destination.is_dir()
    assert not partial.exists()

    def refuse_recopy(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("published payload must not be recopied")

    monkeypatch.setattr(materialization, "_copy_to_temporary", refuse_recopy)
    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )

    assert resumed["status"] == "materialized"
    assert tree_digest(destination) == tree_digest(selected_root)


def test_durable_staging_reuse_refuses_changed_staged_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import LinuxFilesystemError

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    destination = tmp_path / "published-result"
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"

    def fail_read_only(_staging: Path, _destination: Path) -> None:
        try:
            raise OSError(errno.EROFS, os.strerror(errno.EROFS))
        except OSError as error:
            raise LinuxFilesystemError(
                "atomic no-replace publication failed"
            ) from error

    monkeypatch.setattr(materialization, "publish_directory", fail_read_only)
    with pytest.raises(materialization.MaterializationError):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)
    staged_file = partial / "Control" / "meeting-notes.txt"
    staged_file.write_bytes(b"changed staged bytes")

    with pytest.raises(
        materialization.MaterializationError,
        match="durable staging bytes do not match",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)
    assert partial.exists()
    assert not destination.exists()


def test_materialize_completion_records_compact_outcomes(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    materialize_consolidation_plan(run_path, plan_id)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        attempt = connection.execute(
            "SELECT state, planned_file_count, cloned_file_count, streamed_file_count "
            "FROM materialization_attempts WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    assert attempt is not None
    assert attempt[0] == "COMPLETE"
    assert int(attempt[1]) == int(attempt[2]) + int(attempt[3])


def _crash_after_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, Path, Path]:
    """Leave a STAGING_DURABLE Attempt after rename and before PUBLISHED."""
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import publish_directory

    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    destination = tmp_path / "published-result"

    class InterruptedPublication(BaseException):
        pass

    def publish_then_interrupt(staging: Path, final: Path) -> None:
        publish_directory(staging, final)
        raise InterruptedPublication

    with monkeypatch.context() as patch:
        patch.setattr(materialization, "publish_directory", publish_then_interrupt)
        with pytest.raises(InterruptedPublication):
            materialization.materialize_consolidation_plan(
                run_path, plan_id, destination
            )
    return run_path, plan_id, selected_root, destination


def test_materialize_reconciles_published_staging_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root, destination = _crash_after_atomic_publication(
        tmp_path, monkeypatch
    )
    source_before = tree_digest(selected_root)
    identity = destination.stat()

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT attempts.state, manifests.staging_root_dev, "
            "manifests.staging_root_ino FROM materialization_attempts AS attempts "
            "JOIN execution_manifests AS manifests USING (attempt_id) "
            "WHERE attempts.plan_id = ?",
            (plan_id,),
        ).fetchone() == ("STAGING_DURABLE", identity.st_dev, identity.st_ino)

    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )

    assert resumed["status"] == "materialized"
    assert tree_digest(selected_root) == source_before
    assert tree_digest(destination) == source_before
    assert (destination.stat().st_dev, destination.stat().st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT state FROM materialization_attempts WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == ("COMPLETE",)


def test_materialize_reconciles_published_attempt_after_published_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, "published")
    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    resumed = materialization.materialize_consolidation_plan(run_path, plan_id)

    assert resumed["status"] == "materialized"
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT state FROM materialization_attempts WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == ("COMPLETE",)


def test_materialize_published_recovery_fsync_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root, destination = _crash_after_atomic_publication(
        tmp_path, monkeypatch
    )
    source_before = tree_digest(selected_root)
    identity = destination.stat()
    original_fsync = materialization._fsync_directory

    def fail_final_parent(path: Path) -> None:
        if path == destination.parent:
            raise OSError("injected parent sync failure")
        original_fsync(path)

    monkeypatch.setattr(materialization, "_fsync_directory", fail_final_parent)
    with pytest.raises(materialization.MaterializationError, match="needs-attention"):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert tree_digest(selected_root) == source_before
    assert tree_digest(destination) == source_before
    assert (destination.stat().st_dev, destination.stat().st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT state, failure_classification FROM materialization_attempts "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == ("FAILED", "io-integrity-manual-attention")


def test_materialize_published_recovery_refuses_different_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root, destination = _crash_after_atomic_publication(
        tmp_path, monkeypatch
    )
    source_before = tree_digest(selected_root)
    replacement = tmp_path / "moved-published-result"
    destination.rename(replacement)
    destination.mkdir()

    with pytest.raises(
        materialization.MaterializationError, match="recovery-ambiguous"
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert tree_digest(selected_root) == source_before
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert tree_digest(replacement) == source_before


@pytest.mark.parametrize("damage", ["missing", "mismatched"])
def test_materialize_published_recovery_refuses_damaged_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root, destination = _crash_after_atomic_publication(
        tmp_path, monkeypatch
    )
    source_before = tree_digest(selected_root)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        if damage == "missing":
            connection.execute(
                "DELETE FROM execution_manifests WHERE plan_id = ?", (plan_id,)
            )
        else:
            connection.execute(
                "UPDATE execution_manifests SET canonical_destination = ? WHERE plan_id = ?",
                (str(tmp_path / "different-destination"), plan_id),
            )
        connection.commit()

    with pytest.raises(
        materialization.MaterializationError, match="recovery-ambiguous"
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert tree_digest(selected_root) == source_before
    assert tree_digest(destination) == source_before


def test_materialize_source_drift_records_mismatch_and_latches_plan(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    before = tree_digest(selected_root)

    changed_file = selected_root / "Control" / "meeting-notes.txt"
    original_bytes = changed_file.read_bytes()
    original_stat = changed_file.stat()
    changed_file.write_bytes(b"x" * len(original_bytes))

    failed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert failed.returncode == 1
    assert "metadata" in failed.stderr

    destination = Path(_intended_destination(run_path, plan_id))
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    assert partial.exists()
    assert not destination.exists()

    changed_file.write_bytes(original_bytes)
    os.utime(changed_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert tree_digest(selected_root) == before

    resumed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert resumed.returncode == 0, resumed.stderr


def test_materialize_missing_source_records_mismatch_and_stops(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )

    missing_file = selected_root / "Control" / "meeting-notes.txt"
    missing_file.unlink()

    failed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert failed.returncode == 1
    assert "source file is missing" in failed.stderr

    destination = Path(_intended_destination(run_path, plan_id))
    assert not destination.exists()


def test_materialize_never_overwrites_unexpected_existing_final_path(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="admission")
    assert crashed.returncode != 0

    destination = Path(_intended_destination(run_path, plan_id))
    conflicting_path = destination / _operation_output_paths(run_path, plan_id)[0]
    conflicting_path.parent.mkdir(parents=True, exist_ok=True)
    conflicting_path.write_bytes(b"unexpected occupant")

    resumed = _materialize_cli(tmp_path, run_path, plan_id)
    assert resumed.returncode == 1
    assert "destination already exists" in resumed.stderr
    assert conflicting_path.read_bytes() == b"unexpected occupant"
    assert tree_digest(selected_root) == before


def test_materialize_missing_final_after_publication_stops_without_recreating(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)
    output_paths = _operation_output_paths(run_path, plan_id)

    first = materialize_consolidation_plan(run_path, plan_id)
    destination = Path(str(first["destination"]))

    removed_path = destination / output_paths[-1]
    assert removed_path.is_file()
    removed_path.unlink()

    resumed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert resumed.returncode == 1
    assert "destination already exists" in resumed.stderr
    assert not removed_path.exists()
    assert tree_digest(selected_root) == before


def test_materialize_corrupted_final_records_conflict_and_latches_plan(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)
    relative_paths = _operation_output_paths(run_path, plan_id)

    first = materialize_consolidation_plan(run_path, plan_id)
    destination = Path(str(first["destination"]))

    corrupted_path = destination / relative_paths[-1]
    corrupted_path.write_bytes(b"corrupted after publication")

    resumed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert resumed.returncode == 1
    assert "destination already exists" in resumed.stderr
    assert corrupted_path.read_bytes() == b"corrupted after publication"
    assert tree_digest(selected_root) == before

    refused = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert refused.returncode == 1
    assert "destination already exists" in refused.stderr


def test_materialize_recovery_rejects_same_size_changed_final(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)
    output_paths = _operation_output_paths(run_path, plan_id)
    first = materialize_consolidation_plan(run_path, plan_id)
    destination = Path(str(first["destination"]))
    changed = destination / output_paths[0]
    changed.write_bytes(b"x" * changed.stat().st_size)

    resumed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert resumed.returncode == 1
    assert "destination already exists" in resumed.stderr


def test_streamed_materialization_has_no_per_file_durability_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import CloneResult

    run_path, plan_id, _, _ = _scan_simple_plan_finalize(tmp_path, extra_files=3)
    original_fsync = os.fsync
    barriers: list[str] = []

    def record_barrier(descriptor: int) -> None:
        barriers.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_barrier)
    monkeypatch.setattr(os, "fdatasync", record_barrier)
    monkeypatch.setattr(
        materialization, "try_native_clone", lambda *_: CloneResult.UNAVAILABLE
    )
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, tmp_path / "result"
    )
    assert result["status"] == "materialized"
    assert not any(Path(path).name.startswith(".partial-") for path in barriers)


@pytest.mark.parametrize(
    "boundary",
    [
        "checkpoint-before-sync",
        "checkpoint-after-sync",
        "checkpoint-before-commit",
        "checkpoint-after-commit",
    ],
)
def test_checkpoint_interruption_preserves_only_confirmed_work(
    tmp_path: Path, boundary: str
) -> None:
    run_path, plan_id, _, selected_root = _scan_simple_plan_finalize(tmp_path)
    before = tree_digest(selected_root)
    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point=boundary)
    assert crashed.returncode != 0
    resumed = _materialize_cli(tmp_path, run_path, plan_id)
    assert resumed.returncode == 0, resumed.stderr
    assert tree_digest(selected_root) == before


@pytest.mark.parametrize("side", ["source", "staged"])
def test_recovery_refuses_same_size_restored_mtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, destination, partial, output_paths = (
        _interrupt_after_one_completed_staged_file(tmp_path, monkeypatch)
    )
    path = partial / output_paths[0]
    if side == "source":
        path = tmp_path / "selected-backup-root" / "Control" / "a-first.txt"
    before = path.stat()
    path.write_bytes(b"X" * before.st_size)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(materialization.MaterializationError, match="does not match"):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)
    assert not destination.exists()


def test_resume_reports_actual_verification_reads_separately_from_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, destination, partial, output_paths = (
        _interrupt_after_one_completed_staged_file(tmp_path, monkeypatch)
    )
    expected_verified = (partial / output_paths[0]).stat().st_size
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )
    metrics = cast(dict[str, object], result["telemetry"])
    assert metrics["recovery_source_bytes"] == expected_verified
    assert metrics["recovery_staged_bytes"] == expected_verified
    assert metrics["reused_files"] == 1
    assert metrics["copied_files"] == 3
    assert metrics["durable_files"] == 4


def test_completed_recovery_reports_reused_durable_work(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _, _ = _scan_simple_plan_finalize(tmp_path)
    destination = tmp_path / "completed-result"
    crashed = _materialize_cli(
        tmp_path, run_path, plan_id, destination=destination, crash_point="published"
    )
    assert crashed.returncode != 0
    recovered = materialize_consolidation_plan(run_path, plan_id, destination)

    metrics = cast(dict[str, object], recovered["telemetry"])
    assert metrics["phase"] == "complete"
    assert metrics["reused_files"] == recovered["operation_count"]
    assert metrics["durable_files"] == recovered["operation_count"]
    assert metrics["durable_bytes"] == recovered["total_bytes"]
    assert float(cast(float, metrics["durable_publication_seconds"])) > 0


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC])
def test_checkpoint_writeback_failure_never_confirms_or_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    from filesystem_organizer import linux_filesystem, materialization

    run_path, plan_id, _, selected_root = _scan_simple_plan_finalize(tmp_path)
    destination = tmp_path / "failed-result"
    original = tree_digest(selected_root)

    def fail_sync(_self: object, _descriptor: int) -> None:
        raise OSError(code, "injected writeback failure")

    with monkeypatch.context() as patch:
        patch.setattr(linux_filesystem._CtypesLinuxSystem, "syncfs", fail_sync)
        with pytest.raises(materialization.MaterializationError, match="synchronize"):
            materialization.materialize_consolidation_plan(
                run_path, plan_id, destination
            )
    assert not destination.exists()
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_file_evidence"
            ).fetchone()[0]
            == 0
        )
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )
    assert result["status"] == "materialized"
    assert tree_digest(selected_root) == original


@pytest.mark.parametrize(
    "limit", ["STAGING_CHECKPOINT_SECONDS", "STAGING_CHECKPOINT_BYTE_SIZE"]
)
def test_checkpoint_time_and_byte_limits_apply_before_file_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _, _ = _scan_simple_plan_finalize(tmp_path, extra_files=3)
    monkeypatch.setattr(materialization, limit, 0)
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, tmp_path / "result"
    )
    metrics = cast(dict[str, object], result["telemetry"])
    assert metrics["checkpoints"] == 5
    assert metrics["durable_files"] == 5


def test_checkpoint_policy_adapts_elapsed_limit_with_bounded_overhead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from filesystem_organizer import materialization

    now = 4.9
    monkeypatch.setattr(time, "monotonic", lambda: now)
    policy = materialization._CheckpointPolicy(started=0.0)
    assert not policy.due(1, 1)
    now = 5.0
    assert policy.due(1, 1)

    policy.confirmed(0.1)
    assert policy.interval == 2.0
    policy.confirmed(10.0)
    assert policy.interval == 30.0

    low_cost = materialization._CheckpointPolicy(started=now)
    low_cost.confirmed(0.001)
    assert low_cost.interval == 1.0


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC])
def test_copy_io_failure_never_confirms_or_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import CloneResult

    run_path, plan_id, _, _ = _scan_simple_plan_finalize(tmp_path)
    destination = tmp_path / "failed-copy-result"
    monkeypatch.setattr(
        materialization, "try_native_clone", lambda *_: CloneResult.UNAVAILABLE
    )

    def fail_copy(*_args: object) -> tuple[int, str]:
        raise OSError(code, "injected copy failure")

    monkeypatch.setattr(materialization, "_copy_to_temporary", fail_copy)
    with pytest.raises(
        materialization.MaterializationError, match="streamed copy failed"
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)
    assert not destination.exists()
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_file_evidence"
            ).fetchone()[0]
            == 0
        )


def test_previous_manifest_version_is_explicitly_refused_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, destination, partial, _ = (
        _interrupt_after_one_completed_staged_file(tmp_path, monkeypatch)
    )
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE execution_manifests SET manifest_version = manifest_version - 1 "
            "WHERE plan_id = ?",
            (plan_id,),
        )

    with pytest.raises(
        materialization.MaterializationError, match="Execution manifest does not match"
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)
    assert partial.exists()
    assert not destination.exists()


def test_v3_public_in_place_move_uses_one_raw_rename_without_legacy_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new RenameProtection admission takes the near-raw public path."""
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import rename_no_replace_raw

    run_path, plan_id, selected_root = _rename_metadata_plan(tmp_path)
    _select_rename_protection_v3(run_path)
    calls: list[tuple[Path, Path]] = []

    def counted_rename(source: Path, destination: Path) -> None:
        calls.append((source, destination))
        rename_no_replace_raw(source, destination)

    monkeypatch.setattr(materialization, "rename_no_replace_raw", counted_rename)
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert result["status"] == "materialized-in-place"
    assert calls == [(selected_root / "unique.txt", selected_root / "folder/moved.txt")]
    assert not (selected_root / "unique.txt").exists()
    assert (selected_root / "folder/moved.txt").read_bytes() == b"unique-size payload"
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM rename_protection_v3_failures"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='rename_protection_v2_operations'"
            ).fetchone()[0]
            == 0
        )


def test_v3_creates_missing_destination_ancestor_chain(tmp_path: Path) -> None:
    """Nested destination parents need not themselves be planned directories."""
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root = _rename_metadata_plan(
        tmp_path, destination="folder/intermediate/moved.txt"
    )
    _select_rename_protection_v3(run_path)

    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert result["status"] == "materialized-in-place"
    assert not (selected_root / "unique.txt").exists()
    assert (
        selected_root / "folder/intermediate/moved.txt"
    ).read_bytes() == b"unique-size payload"


def test_v3_parent_preparation_refuses_a_conflicting_file(tmp_path: Path) -> None:
    """An existing non-directory ancestor remains a hard conflict."""
    from filesystem_organizer import materialization

    root = tmp_path / "root"
    root.mkdir()
    (root / "folder").write_text("not a directory")

    with pytest.raises(FileExistsError):
        materialization._v3_prepare_parents(
            root,
            [
                materialization._V3Operation(
                    0, "PLACE", "source.txt", "folder/intermediate/moved.txt"
                )
            ],
        )


def test_v3_swap_cycle_uses_one_exceptional_pivot_and_n_plus_one_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A true namespace cycle is the sole case that receives a temporary name."""
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import rename_no_replace_raw

    run_path, plan_id, selected_root = _rename_swap_plan(tmp_path)
    _select_rename_protection_v3(run_path)
    calls: list[tuple[Path, Path]] = []

    def counted_rename(source: Path, destination: Path) -> None:
        calls.append((source, destination))
        rename_no_replace_raw(source, destination)

    monkeypatch.setattr(materialization, "rename_no_replace_raw", counted_rename)
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert result["status"] == "materialized-in-place"
    assert (selected_root / "folder/a.txt").read_bytes() == b"B"
    assert (selected_root / "folder/b.txt").read_bytes() == b"A"
    assert len(calls) == 3
    assert any(".filesystem-organizer-v3-pivot-" in str(dst) for _, dst in calls)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM rename_protection_v3_active_pivots"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM rename_protection_v3_failures"
            ).fetchone()[0]
            == 0
        )


def test_v3_resume_adopts_an_uncheckpointed_successful_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interruption after the raw rename does not turn success into failure."""
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import rename_no_replace_raw

    run_path, plan_id, selected_root = _rename_metadata_plan(tmp_path)
    _select_rename_protection_v3(run_path)
    calls = 0

    def interrupt_after_rename(source: Path, destination: Path) -> None:
        nonlocal calls
        rename_no_replace_raw(source, destination)
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(
        materialization, "rename_no_replace_raw", interrupt_after_rename
    )
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert resumed["status"] == "materialized-in-place"
    assert calls == 1
    assert (
        selected_root / "folder" / "moved.txt"
    ).read_bytes() == b"unique-size payload"
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        progress = connection.execute(
            "SELECT high_watermark, state FROM rename_protection_v3_attempts"
        ).fetchone()
    assert progress == (0, "COMPLETE")


def test_v3_status_and_resume_recover_a_crashed_owned_cycle_pivot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public status seam exposes sparse pivot recovery without staging."""
    from filesystem_organizer import materialization
    from filesystem_organizer.control_plane import inspect_analysis_run
    from filesystem_organizer.linux_filesystem import rename_no_replace_raw

    run_path, plan_id, selected_root = _rename_swap_plan(tmp_path)
    _select_rename_protection_v3(run_path)

    def crash_after_owned_pivot(source: Path, destination: Path) -> None:
        rename_no_replace_raw(source, destination)
        if ".filesystem-organizer-v3-pivot-" in destination.name:
            raise RuntimeError("simulated pivot interruption")

    monkeypatch.setattr(
        materialization, "rename_no_replace_raw", crash_after_owned_pivot
    )
    with pytest.raises(RuntimeError, match="simulated pivot interruption"):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    status = inspect_analysis_run(run_path)
    plan_status = next(
        plan
        for plan in cast(list[dict[str, object]], status["plans"])
        if plan["plan_id"] == plan_id
    )
    recovery = cast(dict[str, object], plan_status["recovery"])
    pivot = cast(dict[str, object], recovery["active_pivot"])
    assert plan_status["materialization_status"] == "in-progress"
    assert recovery["executor_version"] == 3
    assert recovery["resume_candidate"] is True
    assert pivot["phase"] == "INTENT"

    monkeypatch.setattr(materialization, "rename_no_replace_raw", rename_no_replace_raw)
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert result["status"] == "materialized-in-place"
    assert (selected_root / "folder/a.txt").read_bytes() == b"B"
    assert (selected_root / "folder/b.txt").read_bytes() == b"A"


def test_v3_uses_a_pivot_only_for_a_payload_trapped_in_replaced_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory-to-file replacement pivots only when direct draining cannot work."""
    selected_root = tmp_path / "trapped-root"
    (selected_root / "outer" / "folder").mkdir(parents=True)
    (selected_root / "outer" / "folder" / "payload.txt").write_bytes(b"trapped")
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(tmp_path / "trapped-output"),
        "--in-place",
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan_id = str(
        json.loads(run_cli(tmp_path, "plan", str(run_path)).stdout)["plan_id"]
    )
    layout_path = tmp_path / "trapped-layout.json"
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        entry_id = connection.execute(
            "SELECT entry_id FROM plan_baseline_entries WHERE plan_id=? "
            "AND baseline_path='outer/folder/payload.txt'",
            (plan_id,),
        ).fetchone()[0]
    layout = json.loads(layout_path.read_text())
    layout["entry_exceptions"] = [
        {"entry_id": entry_id, "action": {"place_at": "outer/folder"}}
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    _select_rename_protection_v3(run_path)

    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import rename_no_replace_raw

    def crash_after_pivot(source: Path, destination: Path) -> None:
        rename_no_replace_raw(source, destination)
        if ".filesystem-organizer-v3-pivot-" in destination.name:
            raise RuntimeError("simulated trapped pivot interruption")

    monkeypatch.setattr(materialization, "rename_no_replace_raw", crash_after_pivot)
    with pytest.raises(RuntimeError, match="simulated trapped pivot interruption"):
        materialize_consolidation_plan(run_path, plan_id, in_place=True)
    monkeypatch.setattr(materialization, "rename_no_replace_raw", rename_no_replace_raw)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert (selected_root / "outer" / "folder").read_bytes() == b"trapped"


def test_v3_directly_deletes_approved_exact_duplicate_without_payload_read(
    tmp_path: Path,
) -> None:
    """The public v3 execution path deletes Plan-proven duplicate occurrences."""
    from tests.test_rename_protection_v2 import _finalized_relocation

    run_path, plan_id, selected_root = _finalized_relocation(tmp_path, duplicate=True)
    _select_rename_protection_v3(run_path)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert len(list((selected_root / "organized").glob("*.bin"))) == 1
    assert len(list((selected_root / "source").glob("*.bin"))) == 0
    assert not (selected_root / "source").exists()
    metrics = cast(dict[str, object], result["executor_metrics"])
    assert cast(int, metrics["unlink_count"]) == 1
    assert cast(int, metrics["rmdir_count"]) >= 1
    assert cast(int, metrics["metadata_observation_count"]) == 0


def test_v3_vacates_exact_duplicate_at_planned_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate occupying a moved canonical file's output must vacate first."""
    from filesystem_organizer import materialization

    selected_root = tmp_path / "selected"
    source = selected_root / "source"
    source.mkdir(parents=True)
    older = source / "older.bin"
    newer = source / "newer.bin"
    older.write_bytes(b"same payload" * 4096)
    newer.write_bytes(b"same payload" * 4096)
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(tmp_path / "workspace"),
        "--in-place",
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "duplicate-destination-layout.json"
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        entry_id = connection.execute(
            "SELECT entry_id FROM plan_baseline_entries WHERE plan_id=? "
            "AND baseline_path='source/newer.bin'",
            (plan_id,),
        ).fetchone()[0]
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["entry_exceptions"] = [
        {"entry_id": entry_id, "action": {"place_at": "source/older.bin"}}
    ]
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    _select_rename_protection_v3(run_path)

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        operations = materialization._v3_operations(connection, plan_id)
    schedule = {
        (operation.action, operation.source, operation.destination): operation.ordinal
        for operation in operations
    }
    assert ("PLACE", "source/newer.bin", "source/older.bin") in schedule
    assert ("UNLINK", "source/older.bin", None) in schedule

    monkeypatch.setattr(
        materialization,
        "_v3_proven_duplicate_destination_occupants",
        lambda _connection, _plan_id: {},
    )
    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert older.read_bytes() == b"same payload" * 4096
    assert not newer.exists()


def test_v3_removes_explicitly_excluded_skipped_directory(tmp_path: Path) -> None:
    """A finalized skipped-directory exclusion executes as directory removal."""
    selected_root = tmp_path / "selected"
    skipped = selected_root / "corpus" / "skipped-directory"
    skipped.mkdir(parents=True)
    (selected_root / "corpus" / "keep.bin").write_bytes(b"keep")
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(tmp_path / "output"),
        "--in-place",
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        run_id = connection.execute("SELECT run_id FROM analysis_runs").fetchone()[0]
        connection.execute(
            "UPDATE inventory_entries SET read_outcome='unreadable' "
            "WHERE run_id=? AND relative_path='corpus/skipped-directory'",
            (run_id,),
        )
        connection.execute(
            "UPDATE directory_evidence SET read_outcome='unreadable' "
            "WHERE run_id=? AND relative_path='corpus/skipped-directory'",
            (run_id,),
        )
        connection.execute(
            "INSERT INTO skipped_entry_findings VALUES "
            "(?, 'corpus/skipped-directory', 'unreadable')",
            (run_id,),
        )
    planned = run_cli(tmp_path, "plan", str(run_path))
    assert planned.returncode == 0, planned.stderr
    plan_id = str(json.loads(planned.stdout)["plan_id"])
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
    layout = json.loads(layout_path.read_text())
    layout["skipped_actions"] = [
        {"path": "corpus/skipped-directory", "action": "exclude"}
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path,
        "plan-layout-apply",
        "--acknowledge-exclusions",
        str(run_path),
        plan_id,
        str(layout_path),
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    _select_rename_protection_v3(run_path)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert not skipped.exists()
    assert (selected_root / "corpus" / "keep.bin").read_bytes() == b"keep"


def test_v3_self_placement_completes_without_a_rename_syscall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-path Placement retains its schedule ordinal but is already satisfied."""
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root = _rename_metadata_plan(
        tmp_path, source="folder/unique.txt", destination="folder/unique.txt"
    )
    _select_rename_protection_v3(run_path)

    def reject_rename(_source: Path, _destination: Path) -> None:
        raise AssertionError("self-placement attempted a rename syscall")

    monkeypatch.setattr(materialization, "rename_no_replace_raw", reject_rename)
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert result["status"] == "materialized-in-place"
    assert (selected_root / "folder/unique.txt").read_bytes() == b"unique-size payload"
    metrics = cast(dict[str, object], result["executor_metrics"])
    assert metrics["rename_count"] == 1
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT high_watermark, state FROM rename_protection_v3_attempts"
        ).fetchone() == (0, "COMPLETE")


def test_v3_rmdir_prunes_only_empty_unplanned_source_descendants(
    tmp_path: Path,
) -> None:
    """A planned parent RMDIR may exceptionally prune empty descendant dirs."""
    from tests.test_rename_protection_v2 import _finalized_relocation

    run_path, plan_id, selected_root = _finalized_relocation(
        tmp_path, nested_empty_directory=True
    )
    _select_rename_protection_v3(run_path)

    result = materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert result["status"] == "materialized-in-place"
    assert not (selected_root / "source").exists()
    assert (selected_root / "organized" / "a.bin").read_bytes() == b"a" * 4096
    assert (selected_root / "organized" / "b.bin").read_bytes() == b"b" * 4096


def test_v3_rmdir_never_removes_an_unknown_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exceptional empty-directory pruning fails closed around unknown files."""
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import rename_no_replace_raw
    from tests.test_rename_protection_v2 import _finalized_relocation

    run_path, plan_id, selected_root = _finalized_relocation(
        tmp_path, nested_empty_directory=True
    )
    _select_rename_protection_v3(run_path)
    injected = False

    def inject_unknown_file(source: Path, destination: Path) -> None:
        nonlocal injected
        rename_no_replace_raw(source, destination)
        if not injected:
            (selected_root / "source" / "empty" / "nested" / "unknown.txt").write_text(
                "must survive"
            )
            injected = True

    monkeypatch.setattr(materialization, "rename_no_replace_raw", inject_unknown_file)

    with pytest.raises(
        materialization.MaterializationError,
        match="RenameProtection v3 needs attention",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    assert (
        selected_root / "source" / "empty" / "nested" / "unknown.txt"
    ).read_text() == "must survive"


def test_v3_resume_skips_checkpointed_success_and_retries_sparse_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NEEDS_ATTENTION resume does not replay the successful prefix."""
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import rename_no_replace_raw
    from tests.test_rename_protection_v2 import _finalized_relocation

    run_path, plan_id, selected_root = _finalized_relocation(tmp_path)
    _select_rename_protection_v3(run_path)

    def fail_second_placement(source: Path, destination: Path) -> None:
        if source.name == "b.bin":
            raise OSError(errno.EIO, "injected sparse failure")
        rename_no_replace_raw(source, destination)

    monkeypatch.setattr(materialization, "rename_no_replace_raw", fail_second_placement)
    with pytest.raises(
        materialization.MaterializationError,
        match="RenameProtection v3 needs attention",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    resumed_sources: list[Path] = []

    def count_resumed_rename(source: Path, destination: Path) -> None:
        resumed_sources.append(source)
        rename_no_replace_raw(source, destination)

    monkeypatch.setattr(materialization, "rename_no_replace_raw", count_resumed_rename)
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert result["status"] == "materialized-in-place"
    assert resumed_sources == [selected_root / "source" / "b.bin"]
    assert not (selected_root / "source").exists()
