from __future__ import annotations

import os
from pathlib import Path

import pytest

from filesystem_organizer import source_probe
from filesystem_organizer.linux_filesystem import EffectiveMount


def _mount(root: Path, *, read_only: bool) -> EffectiveMount:
    return EffectiveMount(
        root=root,
        filesystem_type="vfat",
        source="/dev/test1",
        device="8:99",
        read_only=read_only,
    )


def test_source_probe_reports_mount_root_and_read_only_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        source_probe,
        "inspect_effective_mount",
        lambda _path: _mount(tmp_path, read_only=True),
    )

    result = source_probe.inspect_source(tmp_path)

    assert result.resolved_source == str(tmp_path)
    assert result.source_root_readable is True
    assert result.source_root_traversable is True
    assert result.source_writable is False
    assert result.selected_source_is_mount_root is True
    assert result.mount_read_only is True
    assert result.filesystem_type == "vfat"
    assert result.suggested_sibling_output_root is None


def test_non_mount_root_derives_sibling_inside_effective_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "backups"
    source.mkdir()
    monkeypatch.setattr(
        source_probe,
        "inspect_effective_mount",
        lambda _path: _mount(tmp_path, read_only=False),
    )

    result = source_probe.inspect_source(source)

    assert result.selected_source_is_mount_root is False
    assert result.suggested_sibling_output_root == str(tmp_path / "backups-result")
    assert Path(result.suggested_sibling_output_root).parent == tmp_path
    assert tmp_path == Path(result.effective_mount_root)


def test_source_probe_reports_agent_access_failure_without_claiming_mount_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RefusedScan:
        def __enter__(self) -> None:
            raise PermissionError("blocked")

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        source_probe,
        "inspect_effective_mount",
        lambda _path: _mount(tmp_path, read_only=False),
    )
    monkeypatch.setattr(os, "scandir", lambda _path: RefusedScan())
    monkeypatch.setattr(os, "access", lambda *_args, **_kwargs: False)

    result = source_probe.inspect_source(tmp_path)

    assert result.source_root_readable is False
    assert result.source_root_traversable is False
    assert result.source_writable is False
    assert result.mount_read_only is False


@pytest.mark.parametrize(
    ("name", "expected"),
    (("example_source", "example_results"), ("example-source", "example-results")),
)
def test_sibling_derivation_preserves_existing_suffix_contract(
    tmp_path: Path, name: str, expected: str
) -> None:
    assert (
        source_probe.derive_sibling_output_root(tmp_path / name, is_mount_root=False)
        == tmp_path / expected
    )


def test_mount_root_preparation_requires_write_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        source_probe,
        "inspect_effective_mount",
        lambda _path: _mount(tmp_path, read_only=True),
    )

    with pytest.raises(source_probe.SourceProbeError, match="read-only mount"):
        source_probe.mount_root_preparation_payload(tmp_path, execute=False)


def test_mount_root_preparation_previews_then_moves_only_top_level_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    (first / "nested.txt").write_text("nested")
    (tmp_path / "second.txt").write_text("second")
    monkeypatch.setattr(
        source_probe,
        "inspect_effective_mount",
        lambda _path: _mount(tmp_path, read_only=False),
    )

    preview = source_probe.mount_root_preparation_payload(tmp_path, execute=False)

    assert preview["status"] == "preview"
    assert preview["top_level_entries"] == ["first", "second.txt"]
    assert first.is_dir()
    assert not (tmp_path / source_probe.ORIGINAL_SOURCE_DIRECTORY).exists()

    prepared = source_probe.mount_root_preparation_payload(tmp_path, execute=True)

    original = tmp_path / source_probe.ORIGINAL_SOURCE_DIRECTORY
    result = tmp_path / source_probe.RESULT_DIRECTORY
    assert prepared["status"] == "prepared"
    assert prepared["moved_entry_count"] == 2
    assert (original / "first" / "nested.txt").read_text() == "nested"
    assert (original / "second.txt").read_text() == "second"
    assert result.is_dir()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        source_probe.ORIGINAL_SOURCE_DIRECTORY,
        source_probe.RESULT_DIRECTORY,
    ]


def test_mount_root_preparation_refuses_existing_reserved_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / source_probe.ORIGINAL_SOURCE_DIRECTORY).mkdir()
    monkeypatch.setattr(
        source_probe,
        "inspect_effective_mount",
        lambda _path: _mount(tmp_path, read_only=False),
    )

    with pytest.raises(source_probe.SourceProbeError, match="already exists"):
        source_probe.mount_root_preparation_payload(tmp_path, execute=False)
