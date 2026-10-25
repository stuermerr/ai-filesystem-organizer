from __future__ import annotations

from pathlib import Path

import pytest

from filesystem_organizer import scan_admission
from filesystem_organizer.analysis_run import AnalysisRunError
from filesystem_organizer.run_workspace import RunWorkspaceError, WorkspaceCapabilities
from filesystem_organizer.scan_admission import admit_scan_environment


def test_scan_admission_checks_destination_without_reading_source_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.bin").write_bytes(b"payload")
    destination = tmp_path / "result"

    def fail_payload_read(*_args: object, **_kwargs: object) -> object:
        pytest.fail("scan admission must not read source payloads")

    monkeypatch.setattr(Path, "read_bytes", fail_payload_read)
    result = admit_scan_environment(source, tmp_path / "runs", destination)

    assert result.destination == str(destination)
    assert result.publication_method in {"atomic-no-replace", "locked-rename-fallback"}
    assert not destination.exists()
    assert not (tmp_path / "runs").exists()


def test_scan_admission_refuses_existing_destination_before_scan(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "result"
    destination.mkdir()

    with pytest.raises(AnalysisRunError, match="destination already exists"):
        admit_scan_environment(source, tmp_path / "runs", destination)


def test_scan_admission_refuses_equal_normalized_output_root_and_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    output_root = tmp_path / "output"

    with pytest.raises(AnalysisRunError, match="must not equal"):
        admit_scan_environment(source, output_root, output_root / ".")

    assert not output_root.exists()


def test_workspace_and_destination_admissions_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    workspace = tmp_path / "workspace"
    workspace_checks: list[Path] = []
    destination_checks: list[Path] = []

    def admit_workspace(path: Path, _source: Path) -> WorkspaceCapabilities:
        workspace_checks.append(path)
        return WorkspaceCapabilities(
            root=str(path),
            filesystem_type="ext4",
            file_size_bits=63,
            maximum_file_size=(1 << 63) - 1,
            free_bytes=10_000_000_000,
            writable_directory_creation=True,
            sqlite_wal=True,
        )

    def refuse_destination(parent: Path, _label: str) -> tuple[int, str]:
        destination_checks.append(parent)
        raise AnalysisRunError("destination-only refusal")

    monkeypatch.setattr(scan_admission, "admit_workspace_root", admit_workspace)
    monkeypatch.setattr(scan_admission, "_admit_parent", refuse_destination)

    with pytest.raises(AnalysisRunError, match="destination-only refusal"):
        admit_scan_environment(
            source,
            tmp_path / "output",
            tmp_path / "destination",
            workspace,
        )

    assert workspace_checks == [workspace.resolve()]
    assert len(destination_checks) == 1


def test_workspace_refusal_precedes_independent_destination_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()

    def refuse_workspace(_path: Path, _source: Path) -> WorkspaceCapabilities:
        raise RunWorkspaceError("workspace-only refusal")

    monkeypatch.setattr(scan_admission, "admit_workspace_root", refuse_workspace)
    monkeypatch.setattr(
        scan_admission,
        "_admit_parent",
        lambda *_args: pytest.fail("destination probe must remain independent"),
    )

    with pytest.raises(AnalysisRunError, match="workspace-only refusal"):
        admit_scan_environment(
            source,
            tmp_path / "output",
            tmp_path / "destination",
            tmp_path / "workspace",
        )
