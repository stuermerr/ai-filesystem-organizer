from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from filesystem_organizer import materialization
from scripts import qualify_vfat_rename_protection as qualifier


def _prepare_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    selected = tmp_path / "selected"
    workspace = tmp_path / "workspace"
    selected.mkdir()
    workspace.mkdir()
    real_cli = qualifier._cli

    def report_v3_admission(*arguments: str) -> dict[str, Any]:
        result = real_cli(*arguments)
        if arguments[0] == "scan":
            result = {
                **result,
                "executor_kind": "rename-protection",
                "executor_version": 3,
            }
        return result

    monkeypatch.setattr(qualifier, "_cli", report_v3_admission)
    run_path, plan_id = qualifier._prepare(selected, workspace)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE analysis_run_execution_intent "
            "SET executor_kind='rename-protection', executor_version=3"
        )
    return selected, run_path, plan_id


def test_fixture_schedules_vacator_reuse_and_swap_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real qualifier Plan contains both exceptional v3 graph shapes."""
    _, run_path, plan_id = _prepare_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        operations = materialization._v3_operations(connection, plan_id)

    scheduled = {
        (operation.action, operation.source, operation.destination): operation.ordinal
        for operation in operations
    }
    target_unlink = scheduled[("UNLINK", "corpus/reuse-target.bin", None)]
    source_place = scheduled[
        ("PLACE", "corpus/reuse-source.bin", "corpus/reuse-target.bin")
    ]
    vacators = {
        operation.source: operation.ordinal
        for operation in operations
        if operation.source is not None
    }
    destination = operations[source_place].destination
    assert destination is not None
    assert vacators[destination] == target_unlink
    assert {
        (operation.source, operation.destination)
        for operation in operations
        if operation.action == "PLACE"
        and operation.source is not None
        and operation.source.startswith("corpus/swap-")
    } == {
        ("corpus/swap-a.bin", "corpus/swap-b.bin"),
        ("corpus/swap-b.bin", "corpus/swap-c.bin"),
        ("corpus/swap-c.bin", "corpus/swap-a.bin"),
    }


@pytest.mark.parametrize(
    "crash_point",
    [
        "rename-protection-v3-after-vacator-receipt",
        "rename-protection-v3-after-pivot-vacate",
    ],
)
def test_fixture_reaches_exceptional_crash_points_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str
) -> None:
    selected, run_path, plan_id = _prepare_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, crash_point)
    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(
            run_path, plan_id, in_place=True
        )
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert result["status"] == "materialized-in-place"
    qualifier._assert_result(selected)
