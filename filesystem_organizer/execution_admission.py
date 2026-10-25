"""Execution-mode admission shared by scan and materialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .linux_filesystem import (
    HardLinkUnsupportedError,
    LinuxFilesystemError,
    probe_hard_link_support,
    probe_rename_protection_support,
)

EXECUTION_ADMISSION_VERSION = 1
EXECUTION_MODE_SOURCE_PRESERVING = "source-preserving"
EXECUTION_MODE_IN_PLACE = "in-place"
EXECUTOR_SOURCE_PRESERVING = "source-preserving"
EXECUTOR_HARD_LINK_PROTECTION = "hard-link-protection"
EXECUTOR_RENAME_PROTECTION = "rename-protection"
EXECUTOR_VERSION = 1
# New RenameProtection admissions use the near-raw v3 executor.  The
# persisted version, rather than a fresh admission, selects the executor for
# an existing attempt.
RENAME_PROTECTION_VERSION = 3


class ExecutionAdmissionError(Exception):
    """A selected executor cannot safely run in the requested environment."""


@dataclass(frozen=True)
class ExecutionIntent:
    execution_mode: str
    executor_kind: str
    executor_version: int
    admission_version: int
    admitted_at: str


def admit_execution(selected_root: Path, *, in_place: bool) -> ExecutionIntent:
    """Select one executor and exercise its environment-specific capability."""
    if in_place:
        try:
            probe_hard_link_support(selected_root)
        except HardLinkUnsupportedError:
            try:
                probe_rename_protection_support(selected_root)
            except (LinuxFilesystemError, OSError) as error:
                raise ExecutionAdmissionError(
                    "Selected filesystem supports neither HardLinkProtection nor "
                    "the complete RenameProtection capability set."
                ) from error
            executor = EXECUTOR_RENAME_PROTECTION
        except (LinuxFilesystemError, OSError) as error:
            raise ExecutionAdmissionError(str(error)) from error
        else:
            executor = EXECUTOR_HARD_LINK_PROTECTION
        mode = EXECUTION_MODE_IN_PLACE
    else:
        mode = EXECUTION_MODE_SOURCE_PRESERVING
        executor = EXECUTOR_SOURCE_PRESERVING
    return ExecutionIntent(
        execution_mode=mode,
        executor_kind=executor,
        executor_version=(
            RENAME_PROTECTION_VERSION
            if executor == EXECUTOR_RENAME_PROTECTION
            else EXECUTOR_VERSION
        ),
        admission_version=EXECUTION_ADMISSION_VERSION,
        admitted_at=datetime.now(UTC).isoformat(),
    )
