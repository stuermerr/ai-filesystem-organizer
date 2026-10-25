"""Read-only source/mount inspection and explicit mount-root preparation."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .linux_filesystem import (
    LinuxFilesystemError,
    inspect_effective_mount,
    rename_no_replace,
)

ORIGINAL_SOURCE_DIRECTORY = "original-source-dir"
RESULT_DIRECTORY = "result-dir"


class SourceProbeError(Exception):
    """Source inspection or mount-root preparation failed safely."""


@dataclass(frozen=True)
class SourceProbe:
    resolved_source: str
    source_root_readable: bool
    source_root_traversable: bool
    source_writable: bool
    effective_mount_root: str
    selected_source_is_mount_root: bool
    filesystem_type: str
    mount_read_only: bool
    filesystem_device: int
    mount_device: str
    mount_source: str
    suggested_sibling_output_root: str | None


def derive_sibling_output_root(source: Path, *, is_mount_root: bool) -> Path | None:
    """Derive the normal sibling only when it remains inside the source mount."""
    if is_mount_root:
        return None
    name = source.name
    if name.endswith("_source"):
        result_name = f"{name[:-7]}_results"
    elif name.endswith("-source"):
        result_name = f"{name[:-7]}-results"
    else:
        result_name = f"{name}-result"
    return source.with_name(result_name)


def inspect_source(path: Path) -> SourceProbe:
    """Inspect source access and effective mount facts without writing."""
    try:
        source = path.expanduser().resolve(strict=True)
        if not source.is_dir():
            raise SourceProbeError(f"Selected Backup Root is not a directory: {source}")
        mount = inspect_effective_mount(source)
        source_metadata = source.stat()
    except SourceProbeError:
        raise
    except (LinuxFilesystemError, OSError) as error:
        raise SourceProbeError(
            f"cannot inspect Selected Backup Root: {path}"
        ) from error

    try:
        with os.scandir(source):
            readable = True
    except OSError:
        readable = False
    traversable = os.access(source, os.X_OK, effective_ids=True)
    writable = not mount.read_only and os.access(source, os.W_OK, effective_ids=True)
    is_mount_root = source == mount.root
    sibling = derive_sibling_output_root(source, is_mount_root=is_mount_root)
    return SourceProbe(
        resolved_source=str(source),
        source_root_readable=readable,
        source_root_traversable=traversable,
        source_writable=writable,
        effective_mount_root=str(mount.root),
        selected_source_is_mount_root=is_mount_root,
        filesystem_type=mount.filesystem_type,
        mount_read_only=mount.read_only,
        filesystem_device=source_metadata.st_dev,
        mount_device=mount.device,
        mount_source=mount.source,
        suggested_sibling_output_root=str(sibling) if sibling is not None else None,
    )


def source_probe_payload(path: Path) -> dict[str, object]:
    return asdict(inspect_source(path))


def mount_root_preparation_payload(path: Path, *, execute: bool) -> dict[str, object]:
    """Preview or explicitly perform the simple same-drive preparation."""
    probe = inspect_source(path)
    source = Path(probe.resolved_source)
    if not probe.selected_source_is_mount_root:
        raise SourceProbeError(
            f"Selected Backup Root is not its effective mount root: {source}"
        )
    if not probe.source_writable:
        detail = "read-only mount" if probe.mount_read_only else "missing write access"
        raise SourceProbeError(
            f"mount-root preparation unavailable: {detail}: {source}"
        )

    original_source = source / ORIGINAL_SOURCE_DIRECTORY
    result = source / RESULT_DIRECTORY
    for candidate in (original_source, result):
        if candidate.exists() or candidate.is_symlink():
            raise SourceProbeError(f"preparation path already exists: {candidate}")

    try:
        entries = sorted(source.iterdir(), key=lambda entry: os.fsencode(entry.name))
    except OSError as error:
        raise SourceProbeError(f"cannot list mount root: {source}") from error
    for entry in entries:
        try:
            if entry.lstat().st_dev != probe.filesystem_device:
                raise SourceProbeError(
                    f"preparation refuses entry on another filesystem: {entry}"
                )
        except OSError as error:
            raise SourceProbeError(
                f"cannot inspect preparation entry: {entry}"
            ) from error

    payload: dict[str, object] = {
        "status": "preview" if not execute else "prepared",
        "mount_root": str(source),
        "original_source": str(original_source),
        "run_output_root": str(result),
        "final_result": str(result / "materialized" / "<generated RUN ID>"),
        "top_level_entries": [entry.name for entry in entries],
        "moved_entry_count": 0,
    }
    if not execute:
        return payload

    moved = 0
    try:
        original_source.mkdir()
        result.mkdir()
        expected = {entry.name for entry in entries} | {
            ORIGINAL_SOURCE_DIRECTORY,
            RESULT_DIRECTORY,
        }
        if {entry.name for entry in source.iterdir()} != expected:
            raise SourceProbeError(
                "mount root changed during preparation; no entries moved"
            )
        for entry in entries:
            destination = original_source / entry.name
            if destination.exists() or destination.is_symlink():
                raise SourceProbeError(
                    f"unexpected preparation destination conflict: {destination}"
                )
            rename_no_replace(entry, destination)
            moved += 1
    except (OSError, SourceProbeError) as error:
        raise SourceProbeError(
            f"mount-root preparation stopped after moving {moved} entries; "
            "do not improvise further moves"
        ) from error
    payload["moved_entry_count"] = moved
    return payload
