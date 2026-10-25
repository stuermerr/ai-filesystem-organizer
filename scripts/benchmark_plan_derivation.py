"""Benchmark Plan derivation on a generated mirrored-directory workload."""

from __future__ import annotations

import argparse
import inspect
import json
import statistics
import tempfile
import time
from pathlib import Path

from filesystem_organizer.analysis_run import create_analysis_run
from filesystem_organizer.consolidation_plan import create_consolidation_plan


def _seed(root: Path, entry_count: int) -> Path:
    selected = root / "selected"
    per_tree = (entry_count + 1) // 2
    pair_count = max(2, min(1_000, per_tree))
    for index in range(per_tree):
        relative = Path(f"pair-{index % pair_count:06d}") / f"file-{index:09d}.bin"
        for tree in ("first", "second"):
            target = selected / tree / "archive" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(index), encoding="ascii")
    result = create_analysis_run(selected, root / "output")
    return Path(str(result["analysis_run"]))


def run_case(entry_count: int, repetitions: int) -> dict[str, object]:
    samples: list[dict[str, float | int]] = []
    accepts_metrics = (
        "performance_metrics" in inspect.signature(create_consolidation_plan).parameters
    )
    for _ in range(repetitions):
        with tempfile.TemporaryDirectory(prefix="fso-plan-benchmark-") as temporary:
            run_path = _seed(Path(temporary), entry_count)
            metrics: dict[str, int] = {}
            started = time.perf_counter()
            if accepts_metrics:
                result = create_consolidation_plan(
                    run_path, performance_metrics=metrics
                )
            else:
                result = create_consolidation_plan(run_path)
            elapsed = time.perf_counter() - started
            sample: dict[str, float | int] = {
                "plan_seconds": elapsed,
                "output_entries": int(result["output_entry_count"]),
            }
            sample.update(metrics)
            samples.append(sample)
    fields = sorted(set.intersection(*(set(sample) for sample in samples)))
    return {
        "entry_count": entry_count,
        "repetitions": repetitions,
        "median": {
            field: statistics.median(float(sample[field]) for sample in samples)
            for field in fields
        },
        "samples": samples,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sizes", type=int, nargs="+", default=[10_000, 100_000, 500_000]
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args(arguments)
    if options.repetitions < 1 or any(size < 2 for size in options.sizes):
        parser.error("sizes must be at least two and repetitions must be positive")
    result = {
        "schema_version": 1,
        "workload": "two mirrored directory trees of small regular files",
        "cases": [run_case(size, options.repetitions) for size in options.sizes],
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if options.output is not None:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
