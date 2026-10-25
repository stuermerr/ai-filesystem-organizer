"""Reproducible RenameProtection benchmarks on newly created disposable data.

The harness never discovers external drives. On a hard-link filesystem,
``--force-rename`` updates only its newly created benchmark run so the fallback
adapter can be measured even though production admission correctly prefers
HardLinkProtection.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import resource
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any


def _cli(*arguments: str) -> dict[str, Any]:
    from filesystem_organizer.__main__ import main

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        result = main(list(arguments))
    if result:
        raise RuntimeError(f"CLI failed: {arguments}")
    return json.loads(stdout.getvalue())


def _write_corpus(root: Path, profile: str, files: int, size: int) -> int:
    source = root / "corpus"
    total = 0
    for index in range(files):
        if profile == "deep":
            parent = source.joinpath(*(f"d{level}-{index % 3}" for level in range(8)))
        elif profile == "fanout":
            parent = source / f"directory-{index:06d}"
        else:
            parent = source / f"bucket-{index % 32:02d}"
        parent.mkdir(parents=True, exist_ok=True)
        seed = 0 if profile == "duplicate-heavy" else index
        block = seed.to_bytes(8, "little") + b"x" * max(0, min(size, 1024**2) - 8)
        path = parent / f"file-{index:08d}.bin"
        with path.open("wb") as output:
            remaining = size
            while remaining:
                chunk = block[:remaining]
                output.write(chunk)
                remaining -= len(chunk)
        total += size
    return total


def benchmark(args: argparse.Namespace) -> dict[str, object]:
    from filesystem_organizer import materialization
    from filesystem_organizer.materialization import materialize_consolidation_plan

    with tempfile.TemporaryDirectory(
        prefix="fso-rename-benchmark-", dir=args.root
    ) as temporary:
        work = Path(temporary)
        selected = work / "selected"
        selected.mkdir()
        generated_bytes = _write_corpus(
            selected, args.profile, args.files, args.bytes_per_file
        )
        scan = _cli(
            "scan",
            str(selected),
            "--output-root",
            str(work / "output"),
            "--in-place",
        )
        run_path = Path(str(scan["analysis_run"]))
        plan_id = str(_cli("plan", str(run_path))["plan_id"])
        layout_path = work / "layout.json"
        _cli(
            "plan-layout-export",
            str(run_path),
            plan_id,
            "--output",
            str(layout_path),
        )
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        layout["rules"] = [
            {
                "selector": {"subtree": "corpus"},
                "action": {"place_under": "organized"},
            }
        ]
        if args.profile in {"exclusions", "swap-cycle"}:
            with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
                entries = connection.execute(
                    "SELECT baseline_path, entry_id FROM plan_baseline_entries "
                    "WHERE plan_id=? AND entry_kind='file' ORDER BY baseline_path",
                    (plan_id,),
                ).fetchall()
            if args.profile == "exclusions":
                layout["entry_exceptions"] = [
                    {"entry_id": entry_id, "action": {"exclude": True}}
                    for index, (_path, entry_id) in enumerate(entries)
                    if index % 2
                ]
            else:
                if len(entries) < 3:
                    raise ValueError("swap-cycle requires at least three files")
                cycle = entries[:3]
                layout["entry_exceptions"] = [
                    {
                        "entry_id": cycle[index][1],
                        "action": {"place_at": cycle[(index + 1) % 3][0]},
                    }
                    for index in range(3)
                ]
        layout_path.write_text(json.dumps(layout), encoding="utf-8")
        apply_arguments = ["plan-layout-apply"]
        if args.profile == "exclusions":
            apply_arguments.append("--acknowledge-exclusions")
        _cli(*apply_arguments, str(run_path), plan_id, str(layout_path))
        _cli("plan-finalize", str(run_path), "--plan-id", plan_id)
        if args.force_rename:
            with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
                connection.execute(
                    "UPDATE analysis_run_execution_intent SET "
                    "executor_kind='rename-protection', executor_version=2"
                )

        before = resource.getrusage(resource.RUSAGE_SELF)
        started_cpu = time.process_time()
        started = time.perf_counter()
        crash_points = {
            "interrupted-protection": "rename-protection-v2-after-stage-rename",
            "interrupted-publication": "rename-protection-v2-after-publish-rename",
            "recovery-large-prefix": "rename-protection-v2-after-staging-commit",
        }
        crashed = False
        crash_point = crash_points.get(args.profile)
        if crash_point is not None:
            os.environ[materialization.CRASH_POINT_ENVIRONMENT] = crash_point
            try:
                materialize_consolidation_plan(run_path, plan_id, in_place=True)
            except materialization._CrashSimulation:
                crashed = True
            finally:
                os.environ.pop(materialization.CRASH_POINT_ENVIRONMENT, None)
            if not crashed:
                raise RuntimeError(
                    f"benchmark crash point was not reached: {crash_point}"
                )
        recovery_started = time.perf_counter()
        result = materialize_consolidation_plan(run_path, plan_id, in_place=True)
        recovery_elapsed = time.perf_counter() - recovery_started
        elapsed = time.perf_counter() - started
        cpu = time.process_time() - started_cpu
        after = resource.getrusage(resource.RUSAGE_SELF)
        telemetry = dict(result["telemetry"])
        return {
            "profile": args.profile,
            "files": args.files,
            "generated_payload_bytes": generated_bytes,
            "elapsed_seconds": elapsed,
            "cpu_seconds": cpu,
            "peak_rss_kib": max(before.ru_maxrss, after.ru_maxrss),
            "executor_kind": result.get("executor_kind"),
            "executor_version": result.get("executor_version"),
            "injected_crash_point": crash_point,
            "recovery_elapsed_seconds": recovery_elapsed if crashed else None,
            "telemetry": telemetry,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=(
            "many-small",
            "medium",
            "large",
            "duplicate-heavy",
            "deep",
            "fanout",
            "exclusions",
            "swap-cycle",
            "interrupted-protection",
            "interrupted-publication",
            "recovery-large-prefix",
        ),
        default="many-small",
    )
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--bytes-per-file", type=int, default=4096)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--force-rename", action="store_true")
    print(json.dumps(benchmark(parser.parse_args()), sort_keys=True))


if __name__ == "__main__":
    main()
