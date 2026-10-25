"""Public materialization regressions for the five release correctness fixes."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from filesystem_organizer import materialization
from filesystem_organizer.control_plane import inspect_analysis_run
from filesystem_organizer.linux_filesystem import rename_no_replace_raw
from tests.test_cli import run_cli
from tests.test_materialization import _select_rename_protection_v3
from tests.test_rename_protection_v2 import (
    _finalized_relocation,
    _leave_payload_empty_v1_attempt,
)


class Interrupted(BaseException):
    pass


def _plan(
    tmp_path: Path,
    files: dict[str, bytes],
    placements: dict[str, str],
    *,
    directories: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> tuple[Path, str, Path]:
    root = tmp_path / "selected"
    root.mkdir()
    for index, (name, content) in enumerate(files.items()):
        path = root / "folder" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, ns=(1_700_000_000_000_000_000 + index * 10**9,) * 2)
    scan = run_cli(
        tmp_path,
        "scan",
        str(root),
        "--output-root",
        str(tmp_path / "output"),
        "--in-place",
    )
    assert scan.returncode == 0, scan.stderr
    run = Path(json.loads(scan.stdout)["analysis_run"])
    planned = run_cli(tmp_path, "plan", str(run))
    assert planned.returncode == 0, planned.stderr
    plan_id = str(json.loads(planned.stdout)["plan_id"])
    layout_path = tmp_path / "layout.json"
    exported = run_cli(
        tmp_path, "plan-layout-export", str(run), plan_id, "--output", str(layout_path)
    )
    assert exported.returncode == 0, exported.stderr
    layout = json.loads(layout_path.read_text())
    with sqlite3.connect(run / "analysis.sqlite3") as connection:
        ids = dict(
            connection.execute(
                "SELECT baseline_path, entry_id FROM plan_baseline_entries WHERE plan_id=?",
                (plan_id,),
            )
        )
    layout["entry_exceptions"] = [
        {
            "entry_id": ids["folder/" + source],
            "action": {"place_at": "folder/" + destination},
        }
        for source, destination in placements.items()
    ] + [
        {"entry_id": ids["folder/" + source], "action": {"exclude": True}}
        for source in exclude
    ]
    layout["directories"] = ["folder/" + name for name in directories]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path,
        "plan-layout-apply",
        str(run),
        plan_id,
        str(layout_path),
        *(
            ["--acknowledge-exclusions", "--acknowledge-content-empty"]
            if exclude
            else []
        ),
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    _select_rename_protection_v3(run)
    return run, plan_id, root / "folder"


def _execute(run: Path, plan_id: str) -> dict[str, object]:
    return materialization.materialize_consolidation_plan(run, plan_id, in_place=True)


def _crash(monkeypatch: pytest.MonkeyPatch, point: str, occurrence: int = 1) -> None:
    seen = 0

    def crash(observed: str) -> None:
        nonlocal seen
        if observed == point:
            seen += 1
            if seen == occurrence:
                raise Interrupted(point)

    monkeypatch.setattr(materialization, "_maybe_crash", crash)


@pytest.mark.parametrize("duplicate", [True, False])
def test_interrupted_pathname_reuse_preserves_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duplicate: bool
) -> None:
    files = {"a": b"X", "b": b"Y", "c": b"Y"} if duplicate else {"a": b"X", "b": b"Y"}
    placements = {"a": "b", "c": "c"} if duplicate else {"a": "b", "b": "c"}
    run, plan_id, root = _plan(tmp_path, files, placements)
    # Both paths perform two PLACE operations, including the self Placement.
    _crash(monkeypatch, "rename-protection-v3-after-place", 2)
    with pytest.raises(Interrupted):
        _execute(run, plan_id)
    monkeypatch.setattr(materialization, "_maybe_crash", lambda _: None)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert (root / "b").read_bytes() == b"X"
    assert (root / "c").read_bytes() == b"Y"
    assert not (root / "a").exists()


@pytest.mark.parametrize(
    "crash_point", [None, "rename-protection-v3-after-pivot-publish"]
)
def test_multiple_trapped_pivots_finish_all_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str | None
) -> None:
    run, plan_id, root = _plan(
        tmp_path, {"a/file": b"A", "b/file": b"B"}, {"a/file": "a", "b/file": "b"}
    )
    if crash_point:
        _crash(monkeypatch, crash_point)
        with pytest.raises(Interrupted):
            _execute(run, plan_id)
        assert (
            cast(list[dict[str, object]], inspect_analysis_run(run)["plans"])[0][
                "recovery_available"
            ]
            is True
        )
        monkeypatch.setattr(materialization, "_maybe_crash", lambda _: None)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert (root / "a").read_bytes() == b"A"
    assert (root / "b").read_bytes() == b"B"
    assert (
        cast(list[dict[str, object]], inspect_analysis_run(run)["plans"])[0][
            "recovery_available"
        ]
        is False
    )


@pytest.mark.parametrize("interrupt", [False, True])
def test_trapped_pivot_releases_nested_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    run, plan_id, root = _plan(tmp_path, {"a/inner/file": b"A"}, {"a/inner/file": "a"})
    if interrupt:
        _crash(monkeypatch, "rename-protection-v3-after-pivot-publish")
        with pytest.raises(Interrupted):
            _execute(run, plan_id)
        monkeypatch.setattr(materialization, "_maybe_crash", lambda _: None)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert (root / "a").read_bytes() == b"A"


@pytest.mark.parametrize("count", [3, 4])
@pytest.mark.parametrize(
    "point,occurrence",
    [
        (None, 1),
        ("rename-protection-v3-after-pivot", 1),
        ("rename-protection-v3-after-pivot-vacate", 1),
        ("rename-protection-v3-after-pivot-vacate", 2),
        ("rename-protection-v3-after-pivot-publish", 1),
        ("rename-protection-v3-after-pivot-receipt", 1),
        ("rename-protection-v3-after-pivot-phase", 1),
        ("rename-protection-v3-after-pivot-phase", -1),
    ],
)
def test_n_member_cycle_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    point: str | None,
    occurrence: int,
) -> None:
    names = [chr(ord("a") + i) for i in range(count)]
    files = {name: name.encode() for name in names}
    placements = {name: names[(i + 1) % count] for i, name in enumerate(names)}
    run, plan_id, root = _plan(tmp_path, files, placements)
    if point:
        _crash(monkeypatch, point, count if occurrence == -1 else occurrence)
        with pytest.raises(Interrupted):
            _execute(run, plan_id)
        monkeypatch.setattr(materialization, "_maybe_crash", lambda _: None)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert {p.name: p.read_bytes() for p in root.iterdir()} == {
        destination: files[source] for source, destination in placements.items()
    }
    assert (
        cast(list[dict[str, object]], inspect_analysis_run(run)["plans"])[0][
            "recovery_available"
        ]
        is False
    )


@pytest.mark.parametrize(
    "directory,only_directories", [("Empty", False), ("outer/nested/Empty", True)]
)
@pytest.mark.parametrize("interrupt", [False, True])
def test_explicit_empty_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
    only_directories: bool,
    interrupt: bool,
) -> None:
    run, plan_id, root = _plan(
        tmp_path,
        {"a": b"A"},
        {},
        directories=(directory,),
        exclude=("a",) if only_directories else (),
    )
    if interrupt:
        _crash(monkeypatch, "rename-protection-v3-after-directories")
        with pytest.raises(Interrupted):
            _execute(run, plan_id)
        monkeypatch.setattr(materialization, "_maybe_crash", lambda _: None)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert (root / directory).is_dir()
    if only_directories:
        assert not (root / "a").exists()


@pytest.mark.parametrize(
    "point",
    [
        None,
        "rename-protection-v1-supersession-intent",
        "rename-protection-v1-supersession-removed",
        "rename-protection-v1-supersession-owner-removed",
        "rename-protection-v2-after-publish-rename",
    ],
)
def test_v1_supersession_repeated_and_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str | None
) -> None:
    run, plan_id, root = _finalized_relocation(tmp_path)
    old_attempt = _leave_payload_empty_v1_attempt(run, plan_id)
    if point:
        _crash(monkeypatch, point)
        with pytest.raises(Interrupted):
            _execute(run, plan_id)
        monkeypatch.setattr(materialization, "_maybe_crash", lambda _: None)
    _execute(run, plan_id)
    _execute(run, plan_id)
    assert (root / "organized/a.bin").read_bytes() == b"a" * 4096
    assert (root / "organized/b.bin").read_bytes() == b"b" * 4096
    with sqlite3.connect(run / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT state FROM rename_protection_v2_attempts WHERE attempt_id=?",
            (old_attempt,),
        ).fetchone() == ("SUPERSEDED",)
        assert connection.execute(
            "SELECT COUNT(*) FROM rename_protection_manifests WHERE attempt_id=?",
            (old_attempt,),
        ).fetchone() == (1,)


def test_structural_empty_directory_relocated_by_v3(tmp_path: Path) -> None:
    from tests.test_consolidation_plan import _write_shared_regions

    root = tmp_path / "selected"
    for name in ("first", "second"):
        tree = root / name
        (tree / "empty").mkdir(parents=True)
        _write_shared_regions(tree)
    scan = run_cli(
        tmp_path,
        "scan",
        str(root),
        "--output-root",
        str(tmp_path / "output"),
        "--in-place",
    )
    assert scan.returncode == 0, scan.stderr
    run = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "layout.json"
    exported = run_cli(
        tmp_path, "plan-layout-export", str(run), plan_id, "--output", str(layout_path)
    )
    assert exported.returncode == 0, exported.stderr
    with sqlite3.connect(run / "analysis.sqlite3") as connection:
        directory = str(
            connection.execute(
                "SELECT baseline_path FROM plan_baseline_entries WHERE plan_id=? AND entry_kind='directory' AND baseline_path LIKE '%/empty'",
                (plan_id,),
            ).fetchone()[0]
        )
    layout = json.loads(layout_path.read_text())
    layout["rules"] = [
        {
            "selector": {"subtree": directory.split("/")[0]},
            "action": {"place_under": "organized"},
        }
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    _select_rename_protection_v3(run)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert (root / "organized/empty").is_dir()


class RecordingProgress:
    def __init__(self) -> None:
        self.total: int | None = None
        self.advances: list[int] = []
        self.closed = False

    def start(
        self, description: str, total: int | None = None, unit: str = "operations"
    ) -> None:
        assert "v3" in description
        self.total = total

    def advance(self, amount: int = 1) -> None:
        self.advances.append(amount)

    def complete(self) -> None:
        self.closed = True


def test_interactive_progress_failure_resume_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, plan_id, root = _plan(
        tmp_path, {"a": b"A", "b": b"B"}, {"a": "out/a", "b": "out/b"}
    )
    raw = rename_no_replace_raw

    def fail_second(source: Path, destination: Path) -> None:
        if source.name == "b":
            raise PermissionError("injected permission error")
        raw(source, destination)

    monkeypatch.setattr(materialization, "rename_no_replace_raw", fail_second)
    first = RecordingProgress()
    with pytest.raises(materialization.MaterializationError, match="needs attention"):
        materialization.materialize_consolidation_plan(
            run, plan_id, in_place=True, progress=first
        )
    assert first.closed
    assert 0 < sum(first.advances) < cast(int, first.total)
    monkeypatch.setattr(materialization, "rename_no_replace_raw", raw)
    resumed = RecordingProgress()
    materialization.materialize_consolidation_plan(
        run, plan_id, in_place=True, progress=resumed
    )
    assert resumed.closed
    assert resumed.advances[0] > 0
    assert sum(resumed.advances) == resumed.total
    assert (root / "out/a").read_bytes() == b"A"
    assert (root / "out/b").read_bytes() == b"B"


def test_interactive_progress_is_batched(tmp_path: Path) -> None:
    files = {f"file-{index:04}": str(index).encode() for index in range(600)}
    run, plan_id, _root = _plan(
        tmp_path, files, {name: "out/" + name for name in files}
    )
    progress = RecordingProgress()
    materialization.materialize_consolidation_plan(
        run, plan_id, in_place=True, progress=progress
    )
    assert progress.closed
    assert sum(progress.advances) == progress.total
    assert len(progress.advances) < 30
    with sqlite3.connect(run / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rename_protection_v3_vacated"
        ).fetchone() == (0,)


def test_pivot_failure_never_completes_until_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, plan_id, root = _plan(
        tmp_path, {"a/file": b"A", "b/file": b"B"}, {"a/file": "a", "b/file": "b"}
    )

    def fail_second_pivot(source: Path, destination: Path) -> None:
        if source == root / "b/file":
            raise PermissionError("injected pivot failure")
        rename_no_replace_raw(source, destination)

    monkeypatch.setattr(materialization, "rename_no_replace_raw", fail_second_pivot)
    progress = RecordingProgress()
    with pytest.raises(materialization.MaterializationError, match="needs attention"):
        materialization.materialize_consolidation_plan(
            run, plan_id, in_place=True, progress=progress
        )
    assert progress.closed
    status = cast(list[dict[str, object]], inspect_analysis_run(run)["plans"])[0]
    assert status["materialization_status"] == "needs-attention"
    assert status["recovery_available"] is True
    assert (root / "b/file").read_bytes() == b"B"
    monkeypatch.setattr(materialization, "rename_no_replace_raw", rename_no_replace_raw)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert (root / "a").read_bytes() == b"A"
    assert (root / "b").read_bytes() == b"B"
    assert (
        cast(list[dict[str, object]], inspect_analysis_run(run)["plans"])[0][
            "materialization_status"
        ]
        == "complete"
    )


@pytest.mark.parametrize("duplicate", [False, True])
def test_resume_after_vacator_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duplicate: bool
) -> None:
    files = {"a": b"X", "b": b"Y", "c": b"Y"} if duplicate else {"a": b"X", "b": b"Y"}
    placements = {"a": "b", "c": "c"} if duplicate else {"a": "b", "b": "c"}
    run, plan_id, root = _plan(tmp_path, files, placements)
    _crash(monkeypatch, "rename-protection-v3-after-vacator-receipt")
    progress = RecordingProgress()
    with pytest.raises(Interrupted):
        materialization.materialize_consolidation_plan(
            run, plan_id, in_place=True, progress=progress
        )
    assert progress.closed
    monkeypatch.setattr(materialization, "_maybe_crash", lambda _: None)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert (root / "b").read_bytes() == b"X"
    assert (root / "c").read_bytes() == b"Y"


@pytest.mark.parametrize("interrupt", [False, True])
def test_explicit_directory_reuses_vacated_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    run, plan_id, root = _plan(
        tmp_path, {"a": b"A"}, {"a": "out"}, directories=("a/Empty",)
    )
    if interrupt:
        _crash(monkeypatch, "rename-protection-v3-after-directories")
        with pytest.raises(Interrupted):
            _execute(run, plan_id)
        monkeypatch.setattr(materialization, "_maybe_crash", lambda _: None)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert (root / "out").read_bytes() == b"A"
    assert (root / "a/Empty").is_dir()


def test_normal_place_keeps_raw_fast_path_with_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {"a": b"A", "b": b"BB", "c": b"CCC"}
    run, plan_id, root = _plan(tmp_path, files, {name: "out/" + name for name in files})
    payload_paths = {root / name for name in files} | {
        root / "out" / name for name in files
    }
    real_stat = Path.stat
    real_lstat = Path.lstat
    real_open = os.open
    calls: list[tuple[Path, Path]] = []

    def guarded_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        assert path not in payload_paths, "ordinary PLACE observed payload metadata"
        return real_stat(path, follow_symlinks=follow_symlinks)

    def guarded_lstat(path: Path) -> os.stat_result:
        assert path not in payload_paths, "ordinary PLACE observed payload metadata"
        return real_lstat(path)

    def guarded_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        assert Path(os.fsdecode(path)) not in payload_paths, (
            "ordinary PLACE opened payload"
        )
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def counted_rename(source: Path, destination: Path) -> None:
        calls.append((source, destination))
        rename_no_replace_raw(source, destination)

    def refuse_receipt(*_args: object, **_kwargs: object) -> None:
        pytest.fail("ordinary independent PLACE persisted an exceptional receipt")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", guarded_stat)
        patch.setattr(Path, "lstat", guarded_lstat)
        patch.setattr(os, "open", guarded_open)
        patch.setattr(materialization, "rename_no_replace_raw", counted_rename)
        patch.setattr(materialization, "record_rename_v3_vacated", refuse_receipt)
        result = materialization.materialize_consolidation_plan(
            run, plan_id, in_place=True, progress=RecordingProgress()
        )
    assert result["status"] == "materialized-in-place"
    assert calls == [(root / name, root / "out" / name) for name in files]
    assert {path.name: path.read_bytes() for path in (root / "out").iterdir()} == files


def test_cycle_receipt_failure_resumes_without_partial_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer.rename_protection_v3 import record_vacated

    run, plan_id, root = _plan(
        tmp_path, {"a": b"A", "b": b"B", "c": b"C"}, {"a": "b", "b": "c", "c": "a"}
    )

    def fail_receipt_batch(
        connection: sqlite3.Connection,
        attempt_id: str,
        ordinals: list[int],
        *,
        finish_pivot: bool = False,
    ) -> None:
        assert finish_pivot
        connection.execute(
            "CREATE TEMP TRIGGER fail_receipt BEFORE INSERT ON rename_protection_v3_vacated WHEN NEW.ordinal=1 BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"
        )
        record_vacated(connection, attempt_id, ordinals, finish_pivot=finish_pivot)

    monkeypatch.setattr(materialization, "record_rename_v3_vacated", fail_receipt_batch)
    with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
        _execute(run, plan_id)
    monkeypatch.setattr(materialization, "record_rename_v3_vacated", record_vacated)
    assert _execute(run, plan_id)["status"] == "materialized-in-place"
    assert {path.name: path.read_bytes() for path in root.iterdir()} == {
        "a": b"C",
        "b": b"A",
        "c": b"B",
    }
