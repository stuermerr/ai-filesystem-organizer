from __future__ import annotations

import sqlite3

import pytest


def test_v3_attempt_binding_is_plan_only_and_idempotent() -> None:
    from filesystem_organizer.rename_protection_v3 import (
        AttemptBinding,
        RenameProtectionV3Error,
        bind_attempt,
        ensure_schema,
        load_attempt_binding,
    )

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    binding = AttemptBinding(
        attempt_id="attempt-1",
        plan_id="plan-1",
        plan_schema_version=9,
        layout_revision=3,
        finalized_projection_fingerprint="frozen-plan-fingerprint",
    )

    bind_attempt(connection, binding)
    bind_attempt(connection, binding)

    assert load_attempt_binding(connection, "attempt-1") == binding
    assert {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'rename_protection_v3_%'"
        )
    } == {
        "rename_protection_v3_attempts",
        "rename_protection_v3_failures",
        "rename_protection_v3_active_pivots",
        "rename_protection_v3_vacated",
    }
    with pytest.raises(RenameProtectionV3Error, match="contradicts"):
        bind_attempt(
            connection,
            AttemptBinding(
                attempt_id="attempt-1",
                plan_id="plan-1",
                plan_schema_version=9,
                layout_revision=4,
                finalized_projection_fingerprint="frozen-plan-fingerprint",
            ),
        )


def test_v3_checkpoint_only_persists_coarse_high_watermark_and_metrics() -> None:
    from filesystem_organizer.rename_protection_v3 import (
        AttemptBinding,
        bind_attempt,
        checkpoint,
        ensure_schema,
        load_attempt_progress,
    )

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    bind_attempt(
        connection,
        AttemptBinding("attempt-1", "plan-1", 9, 3, "frozen-plan-fingerprint"),
    )

    checkpoint(
        connection,
        "attempt-1",
        high_watermark=50_000,
        metrics={"rename_count": 49_000, "unlink_count": 1_000},
    )

    assert load_attempt_progress(connection, "attempt-1") == (
        50_000,
        {"rename_count": 49_000, "unlink_count": 1_000},
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM rename_protection_v3_failures"
    ).fetchone()[0] == 0


def test_v3_persists_only_actual_failures_and_active_pivots() -> None:
    from filesystem_organizer.rename_protection_v3 import (
        ActivePivot,
        AttemptBinding,
        Failure,
        bind_attempt,
        clear_active_pivot,
        ensure_schema,
        load_active_pivot,
        load_failures,
        record_failure,
        resolve_failure,
        set_active_pivot,
    )

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    bind_attempt(
        connection,
        AttemptBinding("attempt-1", "plan-1", 9, 3, "frozen-plan-fingerprint"),
    )
    failure = Failure(
        ordinal=7,
        operation_key="PLACE:source/a->organized/a",
        error_class="os-error",
        errno=5,
        message="Input/output error",
    )

    record_failure(connection, "attempt-1", failure)
    set_active_pivot(
        connection,
        "attempt-1",
        ActivePivot(
            pivot_key="cycle:7",
            source_relative_path="source/a",
            temporary_relative_path=".pivot/a",
            destination_relative_path="organized/a",
            phase="PIVOTED",
        ),
    )

    assert load_failures(connection, "attempt-1") == [failure]
    assert load_active_pivot(connection, "attempt-1") is not None
    resolve_failure(connection, "attempt-1", failure.ordinal)
    clear_active_pivot(connection, "attempt-1")
    assert load_failures(connection, "attempt-1") == []
    assert load_active_pivot(connection, "attempt-1") is None
