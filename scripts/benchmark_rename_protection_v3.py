"""Compare v3 with the same-runtime raw no-sync namespace lower bound.

Every fixture is created below ``--root`` by this process.  ``--root`` is a
container for disposable data, never a Selected Backup Root.  The raw case
uses the production raw ``renameat2(RENAME_NOREPLACE)`` primitive and the same
Python interpreter as v3; it deliberately performs no final ``syncfs``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, cast


def _refuse_nondisposable_path(path: Path) -> None:
    if any(part.casefold() == "non-disposable-backup" for part in path.parts):
        raise ValueError("refusing a non-disposable backup path")


def _cli(*arguments: str) -> dict[str, Any]:
    from filesystem_organizer.__main__ import main

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        if main(list(arguments)):
            raise RuntimeError(f"CLI failed: {arguments}")
    return cast(dict[str, Any], json.loads(stdout.getvalue()))


def _relative(index: int, layout: str) -> Path:
    if layout == "flat":
        return Path(f"file-{index:08d}.bin")
    if layout == "fanout":
        return Path(f"directory-{index:08d}") / "payload.bin"
    return Path(*(f"level-{level}-{index % 3}" for level in range(8))) / (
        f"file-{index:08d}.bin"
    )


def _write_source(root: Path, files: int, layout: str) -> list[Path]:
    source = root / "corpus"
    relatives: list[Path] = []
    for index in range(files):
        relative = _relative(index, layout)
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(index.to_bytes(8, "little"))
        relatives.append(relative)
    return relatives


def _raw_case(root: Path, files: int, layout: str) -> dict[str, float | int]:
    from filesystem_organizer.linux_filesystem import rename_no_replace_raw

    relatives = _write_source(root, files, layout)
    source = root / "corpus"
    destination = root / "organized"
    # Parent preparation belongs to the primitive lower bound, but no sync or
    # metadata validation is part of it.
    for relative in relatives:
        (destination / relative).parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    for relative in relatives:
        rename_no_replace_raw(source / relative, destination / relative)
    elapsed = time.perf_counter() - started
    return {
        "execution_seconds": elapsed,
        "placements_per_second": files / elapsed if elapsed else 0.0,
        "total_mutations_per_second": files / elapsed if elapsed else 0.0,
        "rename_count": files,
        "unlink_count": 0,
        "mkdir_count": len({relative.parent for relative in relatives}),
        "metadata_observation_count": 0,
        "sqlite_commit_count": 0,
        "wal_bytes": 0,
        "pivot_count": 0,
        "staging_count": 0,
        "syncfs_count": 0,
    }


def _v3_case(root: Path, files: int, layout: str) -> dict[str, float | int]:
    from filesystem_organizer import materialization

    selected = root / "selected"
    _write_source(selected, files, layout)
    scan = _cli(
        "scan", str(selected), "--output-root", str(root / "output"), "--in-place"
    )
    run_path = Path(str(scan["analysis_run"]))
    plan_id = str(_cli("plan", str(run_path))["plan_id"])
    layout_path = root / "layout.json"
    _cli("plan-layout-export", str(run_path), plan_id, "--output", str(layout_path))
    plan_layout = json.loads(layout_path.read_text(encoding="utf-8"))
    plan_layout["rules"] = [
        {"selector": {"subtree": "corpus"}, "action": {"place_under": "organized"}}
    ]
    layout_path.write_text(json.dumps(plan_layout), encoding="utf-8")
    _cli("plan-layout-apply", str(run_path), plan_id, str(layout_path))
    _cli("plan-finalize", str(run_path), "--plan-id", plan_id)
    # The fixture's run is newly created and disposable.  This forces the
    # fallback executor on filesystems that normally select hard links.
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE analysis_run_execution_intent SET "
            "executor_kind='rename-protection', executor_version=3"
        )

    # Measure the authoritative Plan load and deterministic in-memory schedule
    # probe separately.  The following materialization repeats that work as it
    # must in production; its wall time is the execution measurement.
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        load_started = time.perf_counter()
        operations = materialization._v3_operations(connection, plan_id)
        vacator = {
            operation.source: operation.ordinal
            for operation in operations
            if operation.source
        }
        pending = {operation.ordinal: 0 for operation in operations}
        for operation in operations:
            if (
                operation.destination in vacator
                and vacator[operation.destination] != operation.ordinal
            ):
                pending[operation.ordinal] += 1
        plan_load_schedule_seconds = time.perf_counter() - load_started

    started = time.perf_counter()
    result = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )
    execution_seconds = time.perf_counter() - started
    if result["executor_version"] != 3:
        raise RuntimeError(f"benchmark did not execute v3: {result}")
    metrics = cast(dict[str, object], result["executor_metrics"])
    telemetry = cast(dict[str, object], result["telemetry"])
    return {
        "execution_seconds": execution_seconds,
        "plan_load_schedule_seconds": plan_load_schedule_seconds,
        "placements_per_second": files / execution_seconds if execution_seconds else 0.0,
        "total_mutations_per_second": (
            (int(metrics["rename_count"]) + int(metrics["unlink_count"]))
            / execution_seconds
            if execution_seconds
            else 0.0
        ),
        "rename_count": int(metrics["rename_count"]),
        "unlink_count": int(metrics["unlink_count"]),
        "mkdir_count": int(metrics["mkdir_count"]),
        "metadata_observation_count": int(metrics["metadata_observation_count"]),
        "sqlite_commit_count": int(metrics["sqlite_checkpoint_count"]),
        "wal_bytes": 0,
        "pivot_count": 0,
        "staging_count": 0,
        "syncfs_count": int(metrics["syncfs_count"]),
        "payload_read_bytes": int(telemetry["payload_read_bytes"]),
    }


def _summary(runs: list[dict[str, float | int]]) -> dict[str, float | int]:
    keys = runs[0].keys()
    summary: dict[str, float | int] = {}
    for key in keys:
        values = [float(run[key]) for run in runs]
        summary[key] = statistics.median(values)
        if key.endswith("_seconds"):
            summary[f"{key}_dispersion_seconds"] = statistics.pstdev(values)
    return summary


def benchmark(args: argparse.Namespace) -> dict[str, object]:
    if args.repetitions < 5:
        raise ValueError("at least five repetitions are required")
    container = (args.root or Path(tempfile.gettempdir())).resolve(strict=True)
    _refuse_nondisposable_path(container)
    if not container.is_dir():
        raise ValueError("--root must be an existing disposable-data container")
    raw_runs: list[dict[str, float | int]] = []
    v3_runs: list[dict[str, float | int]] = []
    for _ in range(args.repetitions):
        with tempfile.TemporaryDirectory(
            prefix="fso-v3-raw-", dir=container
        ) as raw_dir:
            raw_runs.append(_raw_case(Path(raw_dir), args.files, args.layout))
        with tempfile.TemporaryDirectory(
            prefix="fso-v3-executor-", dir=container
        ) as v3_dir:
            v3_runs.append(_v3_case(Path(v3_dir), args.files, args.layout))
    raw = _summary(raw_runs)
    v3 = _summary(v3_runs)
    overhead = (
        (float(v3["execution_seconds"]) / float(raw["execution_seconds"])) - 1
        if float(raw["execution_seconds"])
        else 0.0
    )
    return {
        "benchmark": "rename-protection-v3-same-runtime-raw-no-sync",
        "repetitions": args.repetitions,
        "files": args.files,
        "layout": args.layout,
        "raw_no_sync": {"runs": raw_runs, "median": raw},
        "v3": {"runs": v3_runs, "median": v3},
        "median_execution_overhead_fraction": overhead,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--files", type=int, default=10_000)
    parser.add_argument("--layout", choices=("flat", "fanout", "deep"), default="flat")
    parser.add_argument("--repetitions", type=int, default=5)
    print(json.dumps(benchmark(parser.parse_args()), sort_keys=True))


if __name__ == "__main__":
    main()
