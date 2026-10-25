from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from filesystem_organizer.linux_filesystem import (
    destination_lock_path,
    nonblocking_lock,
)
from filesystem_organizer.materialization import materialize_consolidation_plan
from filesystem_organizer.workspace_cleanup import COMPLETION_RECORD_NAME
from tests.test_cli import run_cli, tree_digest
from tests.test_materialization import _scan_simple_plan_finalize


def test_complete_source_preserving_run_is_automatically_cleaned(
    tmp_path: Path,
) -> None:
    run_path, plan_id, output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )

    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialized.returncode == 0, materialized.stderr
    payload = json.loads(materialized.stdout)
    destination = Path(payload["destination"])
    assert payload["workspace_cleanup"] == "cleaned"
    assert destination.is_dir()
    assert not (run_path / "analysis.sqlite3").exists()
    assert (run_path / COMPLETION_RECORD_NAME).is_file()
    assert (run_path / ".filesystem-organizer.lock").is_file()
    assert destination_lock_path(destination).is_file()

    status = run_cli(tmp_path, "status", str(run_path))
    runs = run_cli(tmp_path, "runs", str(output_root))
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["plans"][0]["materialization_status"] == "complete"
    assert runs.returncode == 0, runs.stderr
    assert json.loads(runs.stdout)["runs"][0]["run_id"] == payload["run_id"]


@pytest.mark.parametrize("attempt_state", ["ADMITTED", "FAILED"])
def test_incomplete_or_failed_run_state_is_preserved(
    tmp_path: Path, attempt_state: str
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    materialize_consolidation_plan(run_path, plan_id)
    database = run_path / "analysis.sqlite3"
    with sqlite3.connect(database) as connection:
        if attempt_state == "ADMITTED":
            connection.execute(
                "UPDATE materialization_attempts SET state = 'ADMITTED', "
                "completed_at = NULL WHERE plan_id = ?",
                (plan_id,),
            )
        else:
            connection.execute(
                "UPDATE materialization_attempts SET state = 'FAILED', "
                "failure_classification = 'internal-failure', "
                "failure_detail = 'test failure' WHERE plan_id = ?",
                (plan_id,),
            )

    cleanup = run_cli(tmp_path, "cleanup-completed", str(run_path))

    assert cleanup.returncode == 0, cleanup.stderr
    assert json.loads(cleanup.stdout)["cleanup_status"] == "preserved"
    assert database.is_file()
    assert not (run_path / COMPLETION_RECORD_NAME).exists()


def test_old_complete_run_cleanup_does_not_touch_final_output(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    result = materialize_consolidation_plan(run_path, plan_id)
    destination = Path(str(result["destination"]))
    before = tree_digest(destination)

    cleanup = run_cli(tmp_path, "cleanup-completed", str(run_path))

    assert cleanup.returncode == 0, cleanup.stderr
    assert json.loads(cleanup.stdout)["cleanup_status"] == "cleaned"
    assert tree_digest(destination) == before
    assert destination_lock_path(destination).is_file()


def test_active_run_lock_prevents_old_run_cleanup(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    materialize_consolidation_plan(run_path, plan_id)

    with nonblocking_lock(run_path / ".filesystem-organizer.lock"):
        cleanup = run_cli(tmp_path, "cleanup-completed", str(run_path))

    assert cleanup.returncode == 1
    assert "already holds Linux filesystem lock" in cleanup.stderr
    assert (run_path / "analysis.sqlite3").is_file()
    assert not (run_path / COMPLETION_RECORD_NAME).exists()


def test_completed_run_cleanup_is_idempotent(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    materialize_consolidation_plan(run_path, plan_id)

    first = run_cli(tmp_path, "cleanup-completed", str(run_path))
    second = run_cli(tmp_path, "cleanup-completed", str(run_path))

    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["cleanup_status"] == "cleaned"
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["cleanup_status"] == "already-clean"
    assert not (run_path / "analysis.sqlite3").exists()
    assert (run_path / ".filesystem-organizer.lock").is_file()
