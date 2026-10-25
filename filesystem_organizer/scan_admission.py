"""Cheap, payload-free admission checks before a potentially long scan."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .analysis_run import AnalysisRunError, _resolved_directory
from .execution_admission import ExecutionAdmissionError, admit_execution
from .linux_filesystem import LinuxFilesystemError, probe_destination_capabilities
from .run_workspace import (
    RunWorkspaceError,
    WorkspaceCapabilities,
    admit_workspace_root,
    nearest_existing_directory,
    require_outside_selected_root,
)


@dataclass(frozen=True)
class ScanAdmission:
    selected_backup_root: str
    run_output_root: str
    run_workspace_root: str
    destination: str | None
    output_filesystem_device: int
    workspace_filesystem_type: str
    workspace_file_size_bits: int
    workspace_max_file_size: int | None
    workspace_free_bytes: int
    workspace_writable_directory_creation: bool
    workspace_sqlite_wal: bool
    destination_filesystem_device: int | None
    publication_method: str | None
    execution_mode: str
    executor_kind: str
    executor_version: int
    execution_admission_version: int
    execution_admitted_at: str


def _admit_parent(parent: Path, label: str) -> tuple[int, str]:
    try:
        capabilities = probe_destination_capabilities(parent)
        device = parent.stat().st_dev
    except (LinuxFilesystemError, OSError) as error:
        raise AnalysisRunError(
            f"{label} lacks required write or publication capability: {parent}"
        ) from error
    method = (
        "atomic-no-replace"
        if capabilities.supports_atomic_no_replace
        else "locked-rename-fallback"
    )
    return device, method


def admit_scan_environment(
    selected_backup_root: Path,
    output_root: Path,
    destination: Path | None = None,
    workspace_root: Path | None = None,
    *,
    in_place: bool = False,
) -> ScanAdmission:
    """Refuse conclusive environmental blockers without reading file payloads.

    The destination is optional because a normal scan does not yet have a Plan.
    A caller with a user-selected destination should supply it so an existing
    conflicting entry and the actual publication volume are checked up front.
    """
    selected_root = _resolved_directory(selected_backup_root, "Selected Backup Root")
    if in_place and destination is not None:
        raise AnalysisRunError("--in-place does not accept --destination")
    try:
        execution = admit_execution(selected_root, in_place=in_place)
    except ExecutionAdmissionError as error:
        raise AnalysisRunError(str(error)) from error
    resolved_output_root = output_root.expanduser().resolve(strict=False)
    resolved_workspace_root = (
        workspace_root.expanduser().resolve(strict=False)
        if workspace_root is not None
        else resolved_output_root
    )
    resolved_destination = (
        destination.expanduser().resolve(strict=False)
        if destination is not None
        else None
    )
    if resolved_destination == resolved_output_root:
        raise AnalysisRunError(
            "Run Output Root must not equal the exact Materialized Consolidation "
            f"destination: {resolved_output_root}"
        )
    require_outside_selected_root(
        resolved_output_root, "Run Output Root", selected_root
    )
    try:
        workspace: WorkspaceCapabilities = admit_workspace_root(
            resolved_workspace_root, selected_root
        )
        output_parent = nearest_existing_directory(
            resolved_output_root, "Run Output Root"
        )
        output_device = output_parent.stat().st_dev
    except RunWorkspaceError as error:
        raise AnalysisRunError(str(error)) from error

    destination_device: int | None = None
    publication_method: str | None = None
    if resolved_destination is not None:
        if resolved_destination == resolved_workspace_root:
            raise AnalysisRunError(
                "Run Workspace Root must not equal the exact Materialized "
                f"Consolidation destination: {resolved_workspace_root}"
            )
        require_outside_selected_root(
            resolved_destination, "Materialized Consolidation", selected_root
        )
        try:
            resolved_destination.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise AnalysisRunError(
                f"cannot verify Materialized Consolidation destination: {resolved_destination}"
            ) from error
        else:
            raise AnalysisRunError(
                f"Materialized Consolidation destination already exists: {resolved_destination}"
            )
        try:
            destination_parent = nearest_existing_directory(
                resolved_destination.parent, "Materialized Consolidation destination"
            )
        except RunWorkspaceError as error:
            raise AnalysisRunError(str(error)) from error
        destination_device, publication_method = _admit_parent(
            destination_parent, "Materialized Consolidation destination"
        )
    elif not in_place:
        try:
            destination_parent = nearest_existing_directory(
                resolved_output_root / "materialized",
                "default Materialized Consolidation destination",
            )
        except RunWorkspaceError as error:
            raise AnalysisRunError(str(error)) from error
        destination_device, publication_method = _admit_parent(
            destination_parent, "default Materialized Consolidation destination"
        )

    return ScanAdmission(
        selected_backup_root=str(selected_root),
        run_output_root=str(resolved_output_root),
        run_workspace_root=str(resolved_workspace_root),
        destination=str(resolved_destination) if resolved_destination else None,
        output_filesystem_device=output_device,
        workspace_filesystem_type=workspace.filesystem_type,
        workspace_file_size_bits=workspace.file_size_bits,
        workspace_max_file_size=workspace.maximum_file_size,
        workspace_free_bytes=workspace.free_bytes,
        workspace_writable_directory_creation=workspace.writable_directory_creation,
        workspace_sqlite_wal=workspace.sqlite_wal,
        destination_filesystem_device=destination_device,
        publication_method=publication_method,
        execution_mode=execution.execution_mode,
        executor_kind=execution.executor_kind,
        executor_version=execution.executor_version,
        execution_admission_version=execution.admission_version,
        execution_admitted_at=execution.admitted_at,
    )


def scan_admission_payload(
    selected_backup_root: Path,
    output_root: Path,
    destination: Path | None = None,
    workspace_root: Path | None = None,
    *,
    in_place: bool = False,
) -> dict[str, object]:
    return asdict(
        admit_scan_environment(
            selected_backup_root,
            output_root,
            destination,
            workspace_root,
            in_place=in_place,
        )
    )
