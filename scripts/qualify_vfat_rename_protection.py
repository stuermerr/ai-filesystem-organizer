"""Qualify RenameProtection on an explicitly mounted disposable VFAT root."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, cast


def _cli(*arguments: str) -> dict[str, Any]:
    from filesystem_organizer.__main__ import main

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        code = main(list(arguments))
    if code:
        raise RuntimeError(f"CLI failed: {arguments}")
    return cast(dict[str, Any], json.loads(stdout.getvalue()))


def _prepare(
    selected: Path,
    workspace: Path,
    *,
    high_operation_count: int = 0,
) -> tuple[Path, str]:
    corpus = selected / "corpus"
    (corpus / "deep" / "nested").mkdir(parents=True)
    (corpus / "one.bin").write_bytes(b"one" * 4096)
    (corpus / "duplicate-a.bin").write_bytes(b"duplicate" * 4096)
    (corpus / "duplicate-b.bin").write_bytes(b"duplicate" * 4096)
    (corpus / "deep" / "nested" / "large.bin").write_bytes(b"L" * 1024**2)
    (corpus / "excluded.bin").write_bytes(b"excluded" * 2048)
    (corpus / "reuse-source.bin").write_bytes(b"reuse-source" * 4096)
    (corpus / "reuse-target.bin").write_bytes(b"reuse-target" * 4096)
    (corpus / "swap-a.bin").write_bytes(b"A" * 4096)
    (corpus / "swap-b.bin").write_bytes(b"B" * 4096)
    (corpus / "swap-c.bin").write_bytes(b"C" * 4096)
    (corpus / "type-tree").mkdir()
    (corpus / "type-tree" / "leaf.bin").write_bytes(b"type-conflict")
    (corpus / "skipped-directory").mkdir()
    for index in range(high_operation_count):
        (corpus / "many" / f"file-{index:06d}.bin").parent.mkdir(exist_ok=True)
        (corpus / "many" / f"file-{index:06d}.bin").write_bytes(
            index.to_bytes(8, "little")
        )
    scan = _cli(
        "scan",
        str(selected),
        "--output-root",
        str(workspace),
        "--in-place",
    )
    if scan["executor_kind"] != "rename-protection" or scan["executor_version"] != 3:
        raise RuntimeError(f"VFAT did not select RenameProtection: {scan}")
    run_path = Path(str(scan["analysis_run"]))
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
    plan_id = str(_cli("plan", str(run_path))["plan_id"])
    layout_path = workspace / "vfat-layout.json"
    _cli(
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        entry_ids = dict(
            connection.execute(
                "SELECT baseline_path, entry_id FROM plan_baseline_entries "
                "WHERE plan_id=? AND entry_kind='file'",
                (plan_id,),
            )
        )
    layout["rules"] = [
        {
            "selector": {"subtree": "corpus"},
            "action": {"place_under": "qualified"},
        }
    ]
    layout["directories"] = ["qualified/empty"]
    layout["entry_exceptions"] = [
        {
            "entry_id": entry_ids["corpus/excluded.bin"],
            "action": {"exclude": True},
        },
        {
            "entry_id": entry_ids["corpus/reuse-target.bin"],
            "action": {"exclude": True},
        },
        {
            "entry_id": entry_ids["corpus/reuse-source.bin"],
            "action": {"place_at": "corpus/reuse-target.bin"},
        },
        {
            "entry_id": entry_ids["corpus/swap-a.bin"],
            "action": {"place_at": "corpus/swap-b.bin"},
        },
        {
            "entry_id": entry_ids["corpus/swap-b.bin"],
            "action": {"place_at": "corpus/swap-c.bin"},
        },
        {
            "entry_id": entry_ids["corpus/swap-c.bin"],
            "action": {"place_at": "corpus/swap-a.bin"},
        },
        {
            "entry_id": entry_ids["corpus/type-tree/leaf.bin"],
            "action": {"place_at": "corpus/type-tree"},
        },
    ]
    layout["skipped_actions"] = [
        {"path": "corpus/skipped-directory", "action": "exclude"}
    ]
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    _cli(
        "plan-layout-apply",
        "--acknowledge-exclusions",
        str(run_path),
        plan_id,
        str(layout_path),
    )
    _cli("plan-finalize", str(run_path), "--plan-id", plan_id)
    return run_path, plan_id


def _assert_result(selected: Path) -> None:
    expected = {
        "qualified/one.bin": b"one" * 4096,
        "qualified/deep/nested/large.bin": b"L" * 1024**2,
    }
    for relative, payload in expected.items():
        if (selected / relative).read_bytes() != payload:
            raise RuntimeError(f"unexpected VFAT output: {relative}")
    duplicates = list((selected / "qualified").glob("duplicate-*.bin"))
    if len(duplicates) != 1 or duplicates[0].read_bytes() != b"duplicate" * 4096:
        raise RuntimeError("VFAT exact-duplicate consolidation is incorrect")
    if not (selected / "qualified" / "empty").is_dir():
        raise RuntimeError("layout-owned VFAT directory is missing")
    reuse_target = selected / "corpus" / "reuse-target.bin"
    if reuse_target.read_bytes() != b"reuse-source" * 4096:
        raise RuntimeError("VFAT pathname-reuse payload is incorrect")
    if (selected / "corpus" / "reuse-source.bin").exists():
        raise RuntimeError("VFAT pathname-reuse source was retained twice")
    expected_swaps = {
        "swap-a.bin": b"C" * 4096,
        "swap-b.bin": b"A" * 4096,
        "swap-c.bin": b"B" * 4096,
    }
    for name, payload in expected_swaps.items():
        if (selected / "corpus" / name).read_bytes() != payload:
            raise RuntimeError(f"VFAT cycle output is incorrect: {name}")
    if (selected / "corpus" / "excluded.bin").exists():
        raise RuntimeError("VFAT exclusion was not removed")
    if (selected / "corpus" / "skipped-directory").exists():
        raise RuntimeError("VFAT skipped exclusion was not removed")
    if (selected / "corpus" / "type-tree").read_bytes() != b"type-conflict":
        raise RuntimeError("VFAT ancestry/type-conflict output is incorrect")
    if any(selected.glob(".filesystem-organizer-staging-*")):
        raise RuntimeError("VFAT staging remains after completion")


def full_case(
    mount: Path,
    crash_point: str | None,
    *,
    high_operation_count: int = 0,
) -> dict[str, object]:
    from filesystem_organizer import materialization

    with (
        tempfile.TemporaryDirectory(
            prefix="fso-vfat-selected-", dir=mount
        ) as selected_dir,
        tempfile.TemporaryDirectory(prefix="fso-vfat-workspace-") as workspace_dir,
    ):
        selected = Path(selected_dir)
        workspace = Path(workspace_dir)
        run_path, plan_id = _prepare(
            selected,
            workspace,
            high_operation_count=high_operation_count,
        )
        crashed = False
        if crash_point is not None:
            os.environ[materialization.CRASH_POINT_ENVIRONMENT] = crash_point
            try:
                materialization.materialize_consolidation_plan(
                    run_path, plan_id, in_place=True
                )
            except materialization._CrashSimulation:
                crashed = True
            finally:
                os.environ.pop(materialization.CRASH_POINT_ENVIRONMENT, None)
            if not crashed:
                raise RuntimeError(f"crash point was not reached: {crash_point}")
        result = materialization.materialize_consolidation_plan(
            run_path, plan_id, in_place=True
        )
        telemetry = cast(dict[str, object], result["telemetry"])
        if result["executor_version"] != 3:
            raise RuntimeError(f"VFAT did not execute RenameProtection v3: {result}")
        if telemetry["payload_read_bytes"] != 0:
            raise RuntimeError(f"normal VFAT run read payload bytes: {telemetry}")
        metrics = cast(dict[str, object], result["executor_metrics"])
        if metrics["metadata_observation_count"] != 0 or metrics["syncfs_count"] != 0:
            raise RuntimeError(f"normal VFAT run used v3 metadata/syncfs: {metrics}")
        _assert_result(selected)
        return {
            "case": crash_point or "fresh",
            "crashed": crashed,
            "executor_kind": result["executor_kind"],
            "executor_version": result["executor_version"],
            "telemetry": telemetry,
        }


def prepare_remount(mount: Path, state_path: Path) -> dict[str, object]:
    from filesystem_organizer import materialization

    selected = mount / "fso-vfat-remount-selected"
    workspace = Path(tempfile.mkdtemp(prefix="fso-vfat-remount-workspace-"))
    if selected.exists():
        raise RuntimeError(f"refusing existing remount qualification root: {selected}")
    selected.mkdir()
    run_path, plan_id = _prepare(selected, workspace)
    os.environ[materialization.CRASH_POINT_ENVIRONMENT] = (
        "rename-protection-v3-after-place"
    )
    try:
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)
    except materialization._CrashSimulation:
        pass
    else:
        raise RuntimeError("remount preparation crash point was not reached")
    finally:
        os.environ.pop(materialization.CRASH_POINT_ENVIRONMENT, None)
    state = {
        "selected": str(selected),
        "workspace": str(workspace),
        "run_path": str(run_path),
        "plan_id": plan_id,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return {"case": "remount-prepared", **state}


def resume_remount(state_path: Path) -> dict[str, object]:
    from filesystem_organizer.materialization import materialize_consolidation_plan

    state = json.loads(state_path.read_text(encoding="utf-8"))
    selected = Path(state["selected"])
    result = materialize_consolidation_plan(
        Path(state["run_path"]), str(state["plan_id"]), in_place=True
    )
    _assert_result(selected)
    shutil.rmtree(selected)
    shutil.rmtree(Path(state["workspace"]))
    state_path.unlink()
    return {
        "case": "remount-resumed",
        "executor_kind": result["executor_kind"],
        "executor_version": result["executor_version"],
        "telemetry": result["telemetry"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mount", type=Path)
    parser.add_argument("--crash-point")
    parser.add_argument("--prepare-remount", type=Path)
    parser.add_argument("--resume-remount", type=Path)
    parser.add_argument("--high-operation-count", type=int, default=0)
    args = parser.parse_args()
    mount = args.mount.resolve(strict=True)
    if any(part.casefold() == "non-disposable-backup" for part in mount.parts):
        raise RuntimeError("refusing a non-disposable backup path")
    from filesystem_organizer.linux_filesystem import effective_filesystem_type

    if effective_filesystem_type(mount).casefold() not in {"vfat", "msdos", "fat"}:
        raise RuntimeError(
            "qualification requires an explicitly provided writable VFAT mount"
        )
    if args.prepare_remount:
        result = prepare_remount(mount, args.prepare_remount)
    elif args.resume_remount:
        result = resume_remount(args.resume_remount)
    else:
        result = full_case(
            mount,
            args.crash_point,
            high_operation_count=args.high_operation_count,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
