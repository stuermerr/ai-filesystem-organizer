from __future__ import annotations

import os
import shutil
import sqlite3
from collections import namedtuple
from pathlib import Path
from typing import cast

import pytest

from filesystem_organizer import analysis_run, run_workspace, structural_relationships
from filesystem_organizer.analysis_run import (
    AnalysisRunError,
    create_analysis_run,
    resume_analysis_run,
)
from filesystem_organizer.consolidation_plan import create_consolidation_plan
from filesystem_organizer.content_identity import (
    ContentIdentity,
    FileObservation,
    hash_regular_file,
)
from filesystem_organizer.run_workspace import WorkspaceCapabilities
from filesystem_organizer.structural_relationships import StructuralEvidenceProjection


def _workspace_capabilities(
    root: Path, *, maximum: int, free: int
) -> WorkspaceCapabilities:
    return WorkspaceCapabilities(
        root=str(root.resolve()),
        filesystem_type="vfat" if maximum == (1 << 32) - 1 else "ext4",
        file_size_bits=maximum.bit_length(),
        maximum_file_size=maximum,
        free_bytes=free,
        writable_directory_creation=True,
        sqlite_wal=True,
    )


def test_small_projected_run_is_admitted_on_four_gib_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "small.txt").write_text("small")
    output_root = tmp_path / "output"
    maximum = (1 << 32) - 1
    monkeypatch.setattr(
        analysis_run,
        "admit_workspace_root",
        lambda workspace, _selected: _workspace_capabilities(
            workspace, maximum=maximum, free=20_000_000_000
        ),
    )

    result = create_analysis_run(selected_root, output_root)

    admission = cast(dict[str, object], result["workspace_admission"])
    assert admission["workspace_filesystem_type"] == "vfat"
    assert cast(int, admission["required_single_file_capacity"]) < maximum


def test_intenso_scale_projection_is_refused_before_hashing_from_one_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    output_root = tmp_path / "output"
    maximum = (1 << 32) - 1
    collected = 0
    entries = [
        analysis_run.InventoryEntry(
            ".", "directory", None, 1, "successful", is_empty=False
        ),
        analysis_run.InventoryEntry("one.bin", "regular-file", 1, 1, "successful"),
        analysis_run.InventoryEntry("two.bin", "regular-file", 1, 1, "successful"),
    ]

    def collect_once(_root: Path) -> list[analysis_run.InventoryEntry]:
        nonlocal collected
        collected += 1
        return entries

    projected_rows = 6_906_865
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: usage(40_000_000_000, 10_000_000_000, 30_000_000_000),
    )
    monkeypatch.setattr(analysis_run, "_collect_entries", collect_once)
    monkeypatch.setattr(
        analysis_run,
        "project_structural_evidence",
        lambda _entries: StructuralEvidenceProjection(
            row_count=projected_rows,
            indexed_text_bytes=3_500_000_000,
            estimated_storage_bytes=projected_rows * 1000,
        ),
    )
    monkeypatch.setattr(
        analysis_run,
        "admit_workspace_root",
        lambda workspace, _selected: _workspace_capabilities(
            workspace, maximum=maximum, free=30_000_000_000
        ),
    )
    monkeypatch.setattr(
        analysis_run,
        "hash_regular_file",
        lambda *_args, **_kwargs: pytest.fail("workspace refusal must precede hashing"),
    )

    with pytest.raises(AnalysisRunError, match=r"6,906,865|6906865") as refusal:
        create_analysis_run(selected_root, output_root)

    message = str(refusal.value)
    assert "workspace filesystem vfat" in message
    assert "maximum single-file size 4294967295" in message
    assert "estimated database size" in message
    assert "available free space 30000000000" in message
    assert "--workspace-root" in message
    assert collected == 1
    assert not output_root.exists()


def test_sufficient_workspace_capacity_allows_hashing_to_begin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.bin").write_bytes(b"same")
    (selected_root / "two.bin").write_bytes(b"same")
    workspace_root = tmp_path / "workspace"
    hashed: list[str] = []
    original_hash = hash_regular_file
    monkeypatch.setattr(
        analysis_run,
        "admit_workspace_root",
        lambda workspace, _selected: _workspace_capabilities(
            workspace, maximum=(1 << 63) - 1, free=30_000_000_000
        ),
    )

    def record_hash(*args: object, **kwargs: object) -> ContentIdentity:
        hashed.append(str(args[1]))
        return original_hash(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(analysis_run, "hash_regular_file", record_hash)

    create_analysis_run(
        selected_root, tmp_path / "output", workspace_root=workspace_root
    )

    assert hashed == ["one.bin", "two.bin"]


def test_projected_rows_match_persisted_ancestor_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    (selected_root / "a" / "b").mkdir(parents=True)
    (selected_root / "a" / "b" / "file.txt").write_text("one")
    original_collect = analysis_run._collect_entries
    collections = 0

    def collect_once(root: Path) -> list[analysis_run.InventoryEntry]:
        nonlocal collections
        collections += 1
        return original_collect(root)

    monkeypatch.setattr(analysis_run, "_collect_entries", collect_once)
    result = create_analysis_run(selected_root, tmp_path / "output")
    run_path = Path(str(result["analysis_run"]))
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        persisted = int(
            connection.execute(
                "SELECT COUNT(*) FROM structural_shared_descendants"
            ).fetchone()[0]
        )

    admission = cast(dict[str, object], result["workspace_admission"])
    assert admission["projected_structural_rows"] == 6
    assert persisted == 6
    assert collections == 1


def test_scan_leaves_unique_files_as_metadata_plan_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "small.txt").write_bytes(b"one")
    (selected_root / "large.txt").write_bytes(b"two-two")
    hashed: list[str] = []
    def record_hash(
        path: Path, relative_path: str, observation: FileObservation
    ) -> ContentIdentity:
        hashed.append(relative_path)
        return hash_regular_file(path, relative_path, observation)

    monkeypatch.setattr(
        analysis_run, "hash_regular_file", record_hash, raising=False
    )
    result = create_analysis_run(selected_root, tmp_path / "output")
    run_path = Path(str(result["analysis_run"]))

    assert hashed == []
    plan = create_consolidation_plan(run_path)
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        identities = connection.execute("SELECT relative_path FROM content_identities").fetchall()
        evidence = connection.execute(
            "SELECT source_relative_path, evidence_kind, algorithm, digest "
            "FROM plan_operations WHERE plan_id = ? ORDER BY source_relative_path",
            (plan["plan_id"],),
        ).fetchall()
    assert identities == []
    assert evidence == [
        ("large.txt", "metadata-observation", None, None),
        ("small.txt", "metadata-observation", None, None),
    ]


def test_metadata_admission_reuses_inventory_and_refuses_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "first.bin").write_bytes(b"12345")
    (selected_root / "second.bin").write_bytes(b"12345")
    destination = tmp_path / "result"
    original_collect = analysis_run._collect_entries
    collection_count = 0

    def counted_collect(root: Path) -> list[analysis_run.InventoryEntry]:
        nonlocal collection_count
        collection_count += 1
        return original_collect(root)

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(analysis_run, "_collect_entries", counted_collect)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(10, 9, 1))
    monkeypatch.setattr(
        analysis_run,
        "hash_regular_file",
        lambda *_args, **_kwargs: pytest.fail("metadata refusal must precede hashing"),
    )

    with pytest.raises(AnalysisRunError, match="insufficient free space"):
        create_analysis_run(
            selected_root, tmp_path / "output", destination=destination
        )

    assert collection_count == 1
    assert not (tmp_path / "output").exists()


def test_default_destination_metadata_admission_precedes_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "large.bin").write_bytes(b"12345")
    output_root = tmp_path / "output"
    run_id = "11111111-1111-1111-1111-111111111111"
    expected_destination = output_root / "materialized" / run_id
    observed_destinations: list[Path] = []
    original_collect = analysis_run._collect_entries
    original_admission = analysis_run._admit_metadata_inventory
    collection_count = 0

    def counted_collect(root: Path) -> list[analysis_run.InventoryEntry]:
        nonlocal collection_count
        collection_count += 1
        return original_collect(root)

    def recorded_admission(
        entries: list[analysis_run.InventoryEntry], destination: Path
    ) -> analysis_run.MetadataAdmission:
        observed_destinations.append(destination)
        return original_admission(entries, destination)

    monkeypatch.setattr(analysis_run, "_collect_entries", counted_collect)
    monkeypatch.setattr(analysis_run, "_admit_metadata_inventory", recorded_admission)
    monkeypatch.setattr(
        analysis_run, "_destination_max_file_size", lambda _path: ("vfat", 4)
    )
    monkeypatch.setattr(
        analysis_run,
        "hash_regular_file",
        lambda *_args, **_kwargs: pytest.fail("metadata refusal must precede hashing"),
    )

    with pytest.raises(AnalysisRunError, match="maximum supported file size"):
        create_analysis_run(selected_root, output_root, run_id=run_id)

    assert observed_destinations == [expected_destination.resolve()]
    assert collection_count == 1
    assert expected_destination != output_root
    assert not output_root.exists()


def test_default_destination_capacity_admission_precedes_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "first.bin").write_bytes(b"12345")
    (selected_root / "second.bin").write_bytes(b"12345")
    output_root = tmp_path / "output"
    run_id = "22222222-2222-2222-2222-222222222222"
    usage = namedtuple("usage", "total used free")

    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(10, 9, 1))
    monkeypatch.setattr(
        analysis_run,
        "hash_regular_file",
        lambda *_args, **_kwargs: pytest.fail("metadata refusal must precede hashing"),
    )

    with pytest.raises(AnalysisRunError, match="insufficient free space"):
        create_analysis_run(selected_root, output_root, run_id=run_id)

    assert not output_root.exists()


def test_metadata_admission_refuses_known_destination_file_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "large.bin").write_bytes(b"12345")
    monkeypatch.setattr(
        analysis_run, "_destination_max_file_size", lambda _path: ("vfat", 4)
    )
    monkeypatch.setattr(
        analysis_run,
        "hash_regular_file",
        lambda *_args, **_kwargs: pytest.fail("metadata refusal must precede hashing"),
    )

    with pytest.raises(AnalysisRunError, match="maximum supported file size"):
        create_analysis_run(
            selected_root, tmp_path / "output", destination=tmp_path / "result"
        )


def test_metadata_admission_reports_inaccessible_entries_for_plan_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    observed = [
        analysis_run.InventoryEntry(
            ".", "directory", None, 1, "successful", is_empty=False
        ),
        analysis_run.InventoryEntry(
            "blocked", "unknown", None, None, "unreadable", "unreadable"
        ),
    ]
    monkeypatch.setattr(analysis_run, "_collect_entries", lambda _root: observed)

    result = create_analysis_run(
        selected_root, tmp_path / "output", destination=tmp_path / "result"
    )

    admission = cast(dict[str, object], result["metadata_admission"])
    assert admission["inaccessible_entry_count"] == 1
    assert isinstance(admission["destination_name_max"], int)
    assert admission["destination_name_max"] > 0
    assert isinstance(admission["destination_path_max"], int)
    assert admission["destination_path_max"] > 0


def test_metadata_admission_reports_destination_name_and_path_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "result"
    entries = [
        analysis_run.InventoryEntry(
            "long-name.txt", "regular-file", 1, 1, "successful"
        )
    ]
    monkeypatch.setattr(
        analysis_run, "_destination_max_file_size", lambda _path: ("testfs", 100)
    )

    def path_limit(_path: Path, name: str) -> int:
        return 4 if name == "PC_NAME_MAX" else 8

    monkeypatch.setattr(os, "pathconf", path_limit)
    admission = analysis_run._admit_metadata_inventory(entries, destination)

    assert admission.destination_name_incompatibility_count == 1
    assert admission.destination_path_incompatibility_count == 1


def test_scan_proves_empty_duplicate_group_without_reading_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "first.empty").touch()
    (selected_root / "second.empty").touch()
    monkeypatch.setattr(
        analysis_run,
        "hash_regular_file",
        lambda *_args, **_kwargs: pytest.fail("empty files must not be read"),
    )

    result = create_analysis_run(selected_root, tmp_path / "output")
    with sqlite3.connect(Path(str(result["analysis_run"])) / analysis_run.DATABASE_NAME) as connection:
        identities = connection.execute(
            "SELECT relative_path, byte_size, read_outcome FROM content_identities "
            "ORDER BY relative_path"
        ).fetchall()
    assert identities == [
        ("first.empty", 0, "successful"),
        ("second.empty", 0, "successful"),
    ]


def test_scan_refuses_non_wal_run_output_root_and_removes_incomplete_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    output_root = tmp_path / "output"
    non_wal_connection = sqlite3.connect(":memory:", isolation_level=None)
    non_wal_connection.row_factory = sqlite3.Row
    monkeypatch.setattr(
        run_workspace,
        "open_read_write",
        lambda _database: non_wal_connection,
    )

    with pytest.raises(AnalysisRunError, match="failed WAL admission"):
        create_analysis_run(selected_root, output_root)

    assert not output_root.exists()


def test_resume_reuses_persisted_structural_schema_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    for name in ("first", "second"):
        directory = selected_root / name
        directory.mkdir()
        (directory / "same.txt").write_bytes(b"same")
    output_root = tmp_path / "output"
    original_analyze = getattr(  # noqa: B009 - private orchestration seam
        analysis_run, "analyze_structural_relationships"
    )

    def interrupted_analyze(*args: object) -> None:
        original_analyze(*args)
        raise RuntimeError("simulated interruption after structural persistence")

    monkeypatch.setattr(
        analysis_run, "analyze_structural_relationships", interrupted_analyze
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(selected_root, output_root)

    monkeypatch.setattr(
        analysis_run, "analyze_structural_relationships", original_analyze
    )
    run_path = next((output_root / "runs").iterdir())
    resumed = resume_analysis_run(run_path)

    assert resumed["status"] == "complete"


def test_structural_candidate_evidence_commits_in_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    for index in range(501):
        (selected_root / f"directory-{index:03}").mkdir()

    def interrupt_after_first_candidate_batch(name: str) -> None:
        if name == "candidate-batch":
            raise RuntimeError("simulated interruption after first candidate batch")

    monkeypatch.setattr(
        structural_relationships, "_checkpoint", interrupt_after_first_candidate_batch
    )
    output_root = tmp_path / "output"
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(selected_root, output_root)

    run_path = next((output_root / "runs").iterdir())
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        persisted_rows = connection.execute(
            "SELECT COUNT(*) FROM directory_fingerprints"
        ).fetchone()[0]
    assert persisted_rows == 500

    resumed_checkpoints: list[str] = []
    monkeypatch.setattr(
        structural_relationships, "_checkpoint", resumed_checkpoints.append
    )
    resumed = resume_analysis_run(run_path)

    assert resumed["status"] == "complete"
    assert resumed_checkpoints.count("candidate-batch") == 1
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM directory_fingerprints"
            ).fetchone()[0]
            == 502
        )


def test_structural_evidence_batch_commits_before_exceeding_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConnectionProbe:
        def __init__(self) -> None:
            self.commits = 0

        def execute(
            self, _statement: str, _parameters: tuple[object, ...] = ()
        ) -> None:
            pass

        def commit(self) -> None:
            self.commits += 1

    checkpoints: list[str] = []
    monkeypatch.setattr(structural_relationships, "_checkpoint", checkpoints.append)
    connection = ConnectionProbe()
    batch = structural_relationships._StructuralEvidenceBatch(
        cast(sqlite3.Connection, connection), "candidate-batch"
    )
    largest_row = "x" * structural_relationships._STRUCTURAL_BATCH_MAX_PAYLOAD_BYTES

    batch.write("INSERT", (largest_row,))
    batch.write("INSERT", ("one",))
    batch.commit()

    assert connection.commits == 2
    assert checkpoints == ["candidate-batch", "candidate-batch"]


def test_resume_completes_conflicts_split_across_structural_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    for root_name in ("first", "second"):
        directory = selected_root / root_name
        directory.mkdir(parents=True)
        for index in range(502):
            (directory / f"anchor-{index:03}.txt").write_bytes(b"same")
        for region in ("shared-region-one", "shared-region-two"):
            (directory / region).mkdir()
            (directory / region / "anchor.txt").write_bytes(b"same")
        for index in range(501):
            (directory / f"conflict-{index:03}.txt").write_text(root_name)

    relationship_batches = 0

    def interrupt_after_first_relationship_batch(name: str) -> None:
        nonlocal relationship_batches
        if name == "relationship-batch":
            relationship_batches += 1
            if relationship_batches == 1:
                raise RuntimeError("simulated interruption after relationship batch")

    monkeypatch.setattr(
        structural_relationships,
        "_checkpoint",
        interrupt_after_first_relationship_batch,
    )
    output_root = tmp_path / "output"
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(selected_root, output_root)

    run_path = next((output_root / "runs").iterdir())
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM directory_relationships"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM relationship_conflicts"
            ).fetchone()[0]
            == 499
        )

    resumed_checkpoints: list[str] = []
    monkeypatch.setattr(
        structural_relationships, "_checkpoint", resumed_checkpoints.append
    )
    resume_analysis_run(run_path)

    assert resumed_checkpoints.count("relationship-batch") == 1
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM relationship_conflicts"
            ).fetchone()[0]
            == 501
        )


@pytest.mark.parametrize(
    "checkpoint", ["tree-index-batch", "candidate-batch", "relationship-batch"]
)
def test_resume_preserves_committed_structural_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    def prepare(root: Path) -> None:
        for name in ("first", "second", "third"):
            directory = root / name
            directory.mkdir(parents=True)
            (directory / "same.txt").write_bytes(b"same")
            (directory / "also-same.txt").write_bytes(b"also same")
            for region in ("shared-region-one", "shared-region-two"):
                (directory / region).mkdir()
                (directory / region / "anchor.txt").write_bytes(b"same")
            (directory / "conflict.txt").write_bytes(name.encode("utf-8"))
        (root / "first" / "unverified-link").symlink_to("same.txt")

    interrupted_source = tmp_path / "interrupted-source"
    uninterrupted_source = tmp_path / "uninterrupted-source"
    prepare(interrupted_source)
    prepare(uninterrupted_source)

    def interrupt_after_commit(name: str) -> None:
        if name == checkpoint:
            raise RuntimeError(f"simulated interruption after {checkpoint}")

    monkeypatch.setattr(structural_relationships, "_checkpoint", interrupt_after_commit)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(interrupted_source, tmp_path / "interrupted-output")
    monkeypatch.setattr(structural_relationships, "_checkpoint", lambda _name: None)

    interrupted_run = next((tmp_path / "interrupted-output" / "runs").iterdir())
    resume_analysis_run(interrupted_run)
    uninterrupted = create_analysis_run(
        uninterrupted_source, tmp_path / "uninterrupted-output"
    )

    def graph_rows(run_path: Path) -> tuple[list[tuple[object, ...]], ...]:
        with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
            return tuple(
                connection.execute(query).fetchall()
                for query in (
                    (
                        "SELECT root_relative_path, fingerprint_version, algorithm, digest "
                        "FROM directory_fingerprints ORDER BY root_relative_path"
                    ),
                    (
                        "SELECT component_id, root_relative_path "
                        "FROM actionable_exact_component_members "
                        "ORDER BY component_id, root_relative_path"
                    ),
                    (
                        "SELECT root_relative_path, descendant_relative_path, entry_kind, identity "
                        "FROM structural_shared_descendants "
                        "ORDER BY root_relative_path, descendant_relative_path"
                    ),
                    (
                        "SELECT left_root, right_root, classification, canonical_root, qualifications "
                        "FROM directory_relationships ORDER BY left_root, right_root"
                    ),
                    (
                        "SELECT left_root, right_root, descendant_relative_path, left_evidence, right_evidence "
                        "FROM relationship_conflicts ORDER BY left_root, right_root, descendant_relative_path"
                    ),
                )
            )

    assert graph_rows(interrupted_run) == graph_rows(
        Path(str(uninterrupted["analysis_run"]))
    )


def test_recompute_candidates_matches_fresh_derivation_after_candidate_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    for name in ("first", "second", "third"):
        directory = source / name
        directory.mkdir(parents=True)
        (directory / "same.txt").write_bytes(b"same")
        (directory / "conflict.txt").write_bytes(name.encode("utf-8"))
    output_root = tmp_path / "output"

    def interrupt_after_candidates(name: str) -> None:
        if name == "candidate-phase":
            raise RuntimeError("simulated interruption after candidate phase")

    monkeypatch.setattr(
        structural_relationships, "_checkpoint", interrupt_after_candidates
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(source, output_root)
    monkeypatch.setattr(structural_relationships, "_checkpoint", lambda _name: None)

    run_path = next((output_root / "runs").iterdir())
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        run_id = str(
            connection.execute("SELECT run_id FROM structural_analysis").fetchone()[0]
        )
        phase = str(
            connection.execute(
                "SELECT phase FROM structural_analysis WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )
        assert phase == "relationships"
        recomputed = structural_relationships._recompute_candidates(connection, run_id)
        fresh = structural_relationships._derive_candidates(
            connection, run_id, source.name
        )
        assert recomputed == fresh
        stored_count = int(
            connection.execute(
                "SELECT candidate_count FROM structural_analysis WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        assert len(recomputed) == stored_count
