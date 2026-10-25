from __future__ import annotations

import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Self

from tqdm import tqdm


class ProgressReporter(Protocol):
    """Report sequential process phases without coupling work to a terminal UI."""

    def start(
        self, description: str, total: int | None = None, unit: str = "operations"
    ) -> None: ...

    def advance(self, amount: int = 1) -> None: ...

    def complete(self) -> None: ...


class NullProgressReporter:
    """Default reporter for library callers that do not need terminal output."""

    def start(
        self, description: str, total: int | None = None, unit: str = "operations"
    ) -> None:
        pass

    def advance(self, amount: int = 1) -> None:
        pass

    def complete(self) -> None:
        pass


class TqdmProgressReporter:
    """Render sequential CLI phases on stderr while leaving stdout machine-readable."""

    def __init__(self) -> None:
        self._bar: tqdm[Any] | None = None
        self._description: str | None = None
        self._materialization_phase: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(
        self, description: str, total: int | None = None, unit: str = "operations"
    ) -> None:
        self.complete()
        self._description = description
        if total is None:
            print(f"{description}...", file=sys.stderr, flush=True)
            return
        self._bar = tqdm(total=total, desc=description, unit=unit)

    def advance(self, amount: int = 1) -> None:
        if self._bar is not None:
            self._bar.update(amount)

    def complete(self) -> None:
        had_bar = self._bar is not None
        if self._bar is not None:
            self._bar.close()
            self._bar = None
        if self._description is not None and not had_bar:
            print(f"{self._description} done", file=sys.stderr, flush=True)
            self._description = None
        self._description = None

    def materialization_status(self, metrics: dict[str, object]) -> None:
        phase = str(metrics["phase"])
        if self._bar is not None:
            self._bar.set_postfix(
                phase=phase,
                copied_bytes=metrics["copied_bytes"],
                durable_files=metrics["durable_files"],
                durable_bytes=metrics["durable_bytes"],
                verified_bytes=int(metrics["recovery_source_bytes"])  # type: ignore[call-overload]
                + int(metrics["recovery_staged_bytes"]),  # type: ignore[call-overload]
                mib_s=round(float(metrics["copied_bytes_per_second"]) / 1024**2, 2),  # type: ignore[arg-type]
                elapsed_s=round(float(metrics["elapsed_seconds"]), 1),  # type: ignore[arg-type]
                checkpoints=metrics["checkpoints"],
                sync_s=round(float(metrics["checkpoint_sync_seconds"]), 2),  # type: ignore[arg-type]
                database_s=round(float(metrics["checkpoint_database_seconds"]), 2),  # type: ignore[arg-type]
                refresh=False,
            )
        elif phase != self._materialization_phase:
            elapsed = float(metrics["elapsed_seconds"])  # type: ignore[arg-type]
            print(
                f"Materialization phase: {phase} ({elapsed:.1f}s elapsed)",
                file=sys.stderr,
                flush=True,
            )
        self._materialization_phase = phase

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None
        self._description = None


@dataclass
class MaterializationTelemetry:
    """Versioned, invocation-local counters; bytes count observed logical I/O."""

    version: int = 2
    phase: str = "admission/revalidation"
    copied_files: int = 0
    copied_bytes: int = 0
    reused_files: int = 0
    durable_files: int = 0
    durable_bytes: int = 0
    recovery_source_bytes: int = 0
    recovery_staged_bytes: int = 0
    checkpoints: int = 0
    checkpoint_sync_seconds: float = 0.0
    checkpoint_database_seconds: float = 0.0
    clone_attempts: int = 0
    elapsed_seconds: float = 0.0
    copied_bytes_per_second: float = 0.0
    durable_publication_seconds: float | None = None
    revalidated_files: int = 0
    revalidated_bytes: int = 0
    protected_files: int = 0
    protected_bytes: int = 0
    published_files: int = 0
    published_bytes: int = 0
    duplicate_cleaned_files: int = 0
    duplicate_cleaned_bytes: int = 0
    exclusion_cleaned_files: int = 0
    exclusion_cleaned_bytes: int = 0
    executor_kind: str = "source-preserving"
    executor_version: int = 1
    payload_bytes_written: int = 0
    rename_count: int = 0
    staged_files: int = 0
    staged_bytes: int = 0
    deleted_files: int = 0
    deleted_bytes: int = 0
    payload_read_files: int = 0
    payload_read_bytes: int = 0
    payload_read_exceptional_promotion_bytes: int = 0
    retries: int = 0
    reconciliation_count: int = 0
    alternate_promotions: int = 0
    resume_skipped_operations: int = 0
    _started: float = field(default_factory=time.monotonic, repr=False)

    def snapshot(self) -> dict[str, object]:
        self.elapsed_seconds = time.monotonic() - self._started
        self.copied_bytes_per_second = (
            self.copied_bytes / self.elapsed_seconds if self.elapsed_seconds else 0.0
        )
        result = asdict(self)
        result.pop("_started")
        return result

    def published(self) -> None:
        self.durable_publication_seconds = time.monotonic() - self._started

    def report(self, reporter: ProgressReporter, phase: str) -> None:
        self.phase = phase
        update = getattr(reporter, "materialization_status", None)
        if update is not None:
            update(self.snapshot())
