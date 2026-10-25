from pathlib import Path

import pytest

from filesystem_organizer import execution_admission
from filesystem_organizer.linux_filesystem import (
    HardLinkUnsupportedError,
    LinuxFilesystemError,
)


def test_in_place_admission_prefers_hard_link_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        execution_admission,
        "probe_hard_link_support",
        lambda _root: calls.append("hard-link"),
    )
    monkeypatch.setattr(
        execution_admission,
        "probe_rename_protection_support",
        lambda _root: calls.append("rename"),
    )

    intent = execution_admission.admit_execution(tmp_path, in_place=True)

    assert intent.executor_kind == "hard-link-protection"
    assert intent.executor_version == 1
    assert calls == ["hard-link"]


def test_in_place_admission_falls_back_to_rename_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def reject_hard_links(_root: Path) -> None:
        calls.append("hard-link")
        raise HardLinkUnsupportedError("unsupported")

    monkeypatch.setattr(
        execution_admission, "probe_hard_link_support", reject_hard_links
    )
    monkeypatch.setattr(
        execution_admission,
        "probe_rename_protection_support",
        lambda _root: calls.append("rename"),
    )

    intent = execution_admission.admit_execution(tmp_path, in_place=True)

    assert intent.executor_kind == "rename-protection"
    assert intent.executor_version == 3
    assert calls == ["hard-link", "rename"]


def test_in_place_admission_does_not_mask_hard_link_operational_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fail_hard_link_probe(_root: Path) -> None:
        calls.append("hard-link")
        raise LinuxFilesystemError("hard-link probe failed with EIO")

    monkeypatch.setattr(
        execution_admission, "probe_hard_link_support", fail_hard_link_probe
    )
    monkeypatch.setattr(
        execution_admission,
        "probe_rename_protection_support",
        lambda _root: calls.append("rename"),
    )

    with pytest.raises(execution_admission.ExecutionAdmissionError, match="EIO"):
        execution_admission.admit_execution(tmp_path, in_place=True)

    assert calls == ["hard-link"]
