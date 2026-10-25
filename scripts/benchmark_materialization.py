"""Compare real CLI materialization/recovery on disposable, identical datasets.

Use --checkout twice to rotate baseline/candidate order over >=3 repetitions.
No mounts, global cache dropping, or existing collections are modified.
"""

from __future__ import annotations

import argparse
import contextlib
import cProfile
import fcntl
import hashlib
import io
import json
import os
import platform
import pstats
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

PROFILES = {
    "small": (10000, 4096),
    "medium": (2000, 65536),
    "large": (16, 64 * 1024**2),
    "mixed": (1024, 4096),
}


def cli(*args: str) -> dict[str, Any]:
    from filesystem_organizer.__main__ import main

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        code = main(list(args))
    if code:
        raise RuntimeError(f"CLI failed: {args}")
    return json.loads(stdout.getvalue()) if stdout.getvalue().startswith("{") else {}


def digest_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.file_digest(
            p.open("rb"), "sha256"
        ).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def checkout_revision(checkout: Path) -> str:
    resolved = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if resolved.returncode:
        return "unavailable"
    revision = resolved.stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(checkout), "diff", "--quiet"], check=False
    ).returncode
    return f"{revision}{'+dirty' if dirty else ''}"


def mount_options(path: Path) -> str:
    result = subprocess.run(
        ["findmnt", "--noheadings", "--output", "OPTIONS", "--target", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def worker(args: argparse.Namespace) -> dict[str, Any]:
    sys.path.insert(0, str(args.worker.resolve()))
    from filesystem_organizer import linux_filesystem as linux
    from filesystem_organizer import materialization as mat
    from filesystem_organizer.content_identity import stable_read

    count, size = PROFILES[args.profile]
    count = args.files or count
    size = args.bytes_per_file or size
    with tempfile.TemporaryDirectory(
        prefix="fso-benchmark-", dir=args.root
    ) as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        for index in range(count):
            folder = source / (
                f"d{index % 16}/nested/deep" if args.profile == "mixed" else "files"
            )
            folder.mkdir(parents=True, exist_ok=True)
            file_size = size if args.profile != "mixed" or index % 32 else size * 256
            block = index.to_bytes(8, "little") + b"x" * (min(file_size, 1024**2) - 8)
            with (folder / f"{index:08}.bin").open("wb") as output:
                left = file_size
                while left:
                    chunk = block[:left]
                    output.write(chunk)
                    left -= len(chunk)
        before = digest_tree(source)
        scan = cli("scan", str(source), "--output-root", str(root / "runs"))
        run = scan["analysis_run"]
        plan = cli("plan", run)["plan_id"]
        cli("plan-finalize", run, "--plan-id", plan)
        destination = root / "result"
        command = ("materialize", run, plan, "--destination", str(destination), "--yes")
        prefix_scenarios = {
            "small-prefix",
            "large-prefix",
            "tampered-staged",
            "changed-source",
            "previous-version",
        }
        if args.scenario in prefix_scenarios:
            prefix = max(
                1, int(count * (0.05 if args.scenario == "small-prefix" else 0.75))
            )
            original_checkpoint = mat._checkpoint_staged_files
            confirmed = 0

            def interrupt(*values: Any, **kwargs: Any) -> None:
                nonlocal confirmed
                original_checkpoint(*values, **kwargs)
                confirmed += len(values[3])
                if confirmed >= prefix:
                    raise mat._CrashSimulation("benchmark confirmed prefix")

            with (
                patch.object(
                    mat, "OUTCOME_BATCH_SIZE", min(prefix, mat.OUTCOME_BATCH_SIZE)
                ),
                patch.object(mat, "_checkpoint_staged_files", interrupt),
            ):
                try:
                    cli(*command)
                except mat._CrashSimulation:
                    pass
        elif args.scenario == "unconfirmed":
            with (
                patch.object(mat, "OUTCOME_BATCH_SIZE", count + 1),
                patch.dict(os.environ, {mat.CRASH_POINT_ENVIRONMENT: "postwritten"}),
            ):
                try:
                    cli(*command)
                except mat._CrashSimulation:
                    pass

        expected_refusal = args.scenario in {
            "tampered-staged",
            "changed-source",
            "previous-version",
        }
        if args.scenario == "tampered-staged":
            partial = next(root.glob(".result.partial-*"))
            staged = next(
                path
                for path in partial.rglob("*")
                if path.is_file()
                and path.name != mat.PARTIAL_OWNER_FILE
                and not path.name.startswith(".partial-")
            )
            observed = staged.stat()
            staged.write_bytes(b"X" * observed.st_size)
            os.utime(staged, ns=(observed.st_atime_ns, observed.st_mtime_ns))
        elif args.scenario == "changed-source":
            changed = next(path for path in source.rglob("*") if path.is_file())
            observed = changed.stat()
            changed.write_bytes(b"X" * observed.st_size)
            os.utime(changed, ns=(observed.st_atime_ns, observed.st_mtime_ns))
        elif args.scenario == "previous-version":
            with sqlite3.connect(Path(run) / "analysis.sqlite3") as connection:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(
                    "UPDATE execution_manifests SET manifest_version = manifest_version - 1 "
                    "WHERE plan_id = ?",
                    (plan,),
                )

        counters: dict[str, int | float] = {
            "copy_read_bytes": 0,
            "recovery_source_bytes": 0,
            "recovery_staged_bytes": 0,
            "sync_count": 0,
            "sync_seconds": 0.0,
            "clone_attempts": 0,
        }
        recovery_side = ""
        original_digest = mat._stable_recovery_digest
        original_stable = stable_read
        original_read = os.read
        original_ioctl = fcntl.ioctl

        def recovery(path: Path, *values: Any, **kwargs: Any) -> Any:
            nonlocal recovery_side
            recovery_side = (
                "recovery_source_bytes"
                if path.is_relative_to(source)
                else "recovery_staged_bytes"
            )
            try:
                return original_digest(path, *values, **kwargs)
            finally:
                recovery_side = ""

        def stable(path: Path, before: Any, *, on_chunk: Any = None) -> Any:
            def observed(chunk: bytes) -> None:
                if recovery_side:
                    counters[recovery_side] += len(chunk)
                if on_chunk:
                    on_chunk(chunk)

            return original_stable(path, before, on_chunk=observed)

        def read(fd: int, length: int) -> bytes:
            data = original_read(fd, length)
            counters["copy_read_bytes"] += len(data)
            return data

        def ioctl(*values: Any, **kwargs: Any) -> Any:
            if values[1] == linux._FICLONE:
                counters["clone_attempts"] += 1
            return original_ioctl(*values, **kwargs)

        def timed(function: Any) -> Any:
            def call(*values: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                counters["sync_count"] += 1
                try:
                    return function(*values, **kwargs)
                finally:
                    counters["sync_seconds"] += time.perf_counter() - start

            return call

        profiler = cProfile.Profile()
        with contextlib.ExitStack() as stack:
            for owner, name, replacement in [
                (mat, "_stable_recovery_digest", recovery),
                (mat, "stable_read", stable),
                (os, "read", read),
                (fcntl, "ioctl", ioctl),
                (os, "fsync", timed(os.fsync)),
                (os, "fdatasync", timed(os.fdatasync)),
                (
                    linux._CtypesLinuxSystem,
                    "syncfs",
                    timed(linux._CtypesLinuxSystem.syncfs),
                ),
            ]:
                stack.enter_context(patch.object(owner, name, replacement))
            start = time.perf_counter()
            cpu = time.process_time()
            profiler.enable()
            refused = False
            try:
                result = cli(*command)
            except RuntimeError:
                if not expected_refusal:
                    raise
                refused = True
                result = {}
            profiler.disable()
            elapsed = time.perf_counter() - start
            cpu = time.process_time() - cpu
        stats = pstats.Stats(profiler)
        raw_stats = cast(
            dict[tuple[str, int, str], tuple[int, int, float, float, object]],
            stats.stats,  # type: ignore[attr-defined]
        )
        sqlite_seconds = sum(
            value[2] for key, value in raw_stats.items() if "sqlite3" in key[2]
        )
        if not expected_refusal:
            assert digest_tree(source) == before, "source changed"
            assert digest_tree(destination) == before, "output differs"
        else:
            assert refused and not destination.exists(), "unsafe refusal scenario"
        output_bytes = sum(
            p.stat().st_size for p in destination.rglob("*") if p.is_file()
        )
        return {
            "elapsed_seconds": elapsed,
            "cpu_seconds": cpu,
            "files_per_second": count / elapsed,
            "bytes_per_second": output_bytes / elapsed,
            "output_bytes": output_bytes,
            "sqlite_seconds": sqlite_seconds,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "filesystem": linux.effective_filesystem_type(root),
            "mount_options": mount_options(root),
            "kernel": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unavailable",
            "revision": checkout_revision(args.worker),
            "expected_refusal": expected_refusal,
            "refused": refused,
            "telemetry": result.get("telemetry"),
            "durable_publication_seconds": cast(
                dict[str, Any], result.get("telemetry") or {}
            ).get("durable_publication_seconds", 0.0),
            **counters,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, action="append")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--profile", choices=PROFILES, default="small")
    parser.add_argument(
        "--scenario",
        choices=[
            "fresh",
            "small-prefix",
            "large-prefix",
            "tampered-staged",
            "changed-source",
            "unconfirmed",
            "previous-version",
        ],
        default="fresh",
    )
    parser.add_argument("--files", type=int)
    parser.add_argument("--bytes-per-file", type=int)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--storage-label",
        default="unspecified; warm cache; physical reads not measured",
    )
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args)))
        return
    if args.repetitions < 3:
        parser.error("at least three repetitions are required")
    checkouts = args.checkout or [Path(__file__).resolve().parents[1]]
    records = []
    for repetition in range(args.repetitions):
        for checkout in checkouts if repetition % 2 == 0 else list(reversed(checkouts)):
            command = [
                sys.executable,
                __file__,
                "--worker",
                str(checkout.resolve()),
                "--root",
                str(args.root),
                "--profile",
                args.profile,
                "--scenario",
                args.scenario,
            ]
            if args.files:
                command += ["--files", str(args.files)]
            if args.bytes_per_file:
                command += ["--bytes-per-file", str(args.bytes_per_file)]
            record = json.loads(subprocess.check_output(command, text=True))
            record.update(checkout=str(checkout), repetition=repetition)
            records.append(record)
    summary = {}
    for checkout in checkouts:
        rows = [r for r in records if r["checkout"] == str(checkout)]
        summary[str(checkout)] = {
            key: {
                "median": statistics.median(r[key] for r in rows),
                "min": min(r[key] for r in rows),
                "max": max(r[key] for r in rows),
            }
            for key in rows[0]
            if isinstance(rows[0][key], (int, float))
            and not isinstance(rows[0][key], bool)
            and key != "repetition"
        }
    print(
        json.dumps(
            {
                "profile": args.profile,
                "scenario": args.scenario,
                "storage_label": args.storage_label,
                "logical_io_only": True,
                "profiled": True,
                "summary": summary,
                "runs": records,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
