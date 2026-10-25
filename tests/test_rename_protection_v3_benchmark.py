from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "benchmark_rename_protection_v3.py"


def test_v3_benchmark_reports_same_runtime_raw_baseline_and_dispersion(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--files",
            "1",
            "--repetitions",
            "5",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["benchmark"] == "rename-protection-v3-same-runtime-raw-no-sync"
    assert result["repetitions"] == 5
    assert len(result["raw_no_sync"]["runs"]) == 5
    assert len(result["v3"]["runs"]) == 5
    assert result["raw_no_sync"]["median"]["syncfs_count"] == 0
    assert result["v3"]["median"]["syncfs_count"] == 0
    assert result["v3"]["median"]["payload_read_bytes"] == 0
    assert "plan_load_schedule_seconds" in result["v3"]["median"]
    assert "execution_seconds_dispersion_seconds" in result["v3"]["median"]


def test_v3_benchmark_refuses_nondisposable_container(tmp_path: Path) -> None:
    protected = tmp_path / "NON-DISPOSABLE-BACKUP"
    protected.mkdir()
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(protected), "--files", "1"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "non-disposable backup" in completed.stderr
