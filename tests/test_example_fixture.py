from __future__ import annotations

import stat
from pathlib import Path

from tests.example_fixture import (
    SOURCE_ROOT,
    copy_prepared_source,
    load_fixture_manifest,
)

EXPECTED_SOURCE_FILES = {
    "LAPTOP OLD - DO NOT DELETE/Desktop/CV final FINAL 2.docx",
    "LAPTOP OLD - DO NOT DELETE/Desktop/New Folder (12)/passport scan.pdf",
    "LAPTOP OLD - DO NOT DELETE/Desktop/Tax 2023 FINAL.pdf",
    "LAPTOP OLD - DO NOT DELETE/Pictures/Camera/IMG_1042.JPG",
    "LAPTOP OLD - DO NOT DELETE/Pictures/Camera/IMG_1043.JPG",
    "LAPTOP OLD - DO NOT DELETE/Pictures/Camera/IMG_1044.JPG",
    "LAPTOP OLD - DO NOT DELETE/Pictures/Summer Holiday/2021-07-14 beach.jpg",
    "LAPTOP OLD - DO NOT DELETE/Pictures/Summer Holiday/2021-07-15 beach.jpg",
    "LAPTOP OLD - DO NOT DELETE/Pictures/phone backup old/IMG_20190504_182244.jpg",
    "LAPTOP OLD - DO NOT DELETE/Pictures/phone backup old/IMG_20190504_182245.jpg",
    "LAPTOP OLD - DO NOT DELETE/Recipes/banana-bread.txt",
    "PHONE BACKUP DO NOT FORMAT/DCIM/Camera/IMG_1042.JPG",
    "PHONE BACKUP DO NOT FORMAT/DCIM/Camera/IMG_2201.JPG",
    "PHONE BACKUP DO NOT FORMAT/DCIM/Screenshots/Screenshot_2020-04-02.png",
    "PHONE BACKUP DO NOT FORMAT/DCIM/Screenshots/Screenshot_2020-04-03.png",
    "PHONE BACKUP DO NOT FORMAT/Videos/VID_20191231_235959.mp4",
    "PHONE BACKUP DO NOT FORMAT/Videos/VID_20210715_183011.mp4",
    "Random Stuff - SORT LATER (2019)/CV current.docx",
    "Random Stuff - SORT LATER (2019)/Family Photos/2020-08-03 mountains.jpg",
    "Random Stuff - SORT LATER (2019)/Family Photos/Christmas/IMG_20171224_183412.JPG",
    "Random Stuff - SORT LATER (2019)/Family Photos/grandma-birthday.jpg",
    "Random Stuff - SORT LATER (2019)/Old iPhone photos maybe/IMG_0001.JPG",
    "Random Stuff - SORT LATER (2019)/Old iPhone photos maybe/IMG_0002.JPG",
    "Random Stuff - SORT LATER (2019)/Old iPhone photos maybe/IMG_0003.JPG",
    "Random Stuff - SORT LATER (2019)/random/notes.txt",
    "USB BACKUP NEWEST I THINK/DCIM copy FINAL/2021-07-14 beach copy.jpg",
    "USB BACKUP NEWEST I THINK/DCIM copy FINAL/2022-12-24 family.jpg",
    "USB BACKUP NEWEST I THINK/DCIM copy FINAL/IMG_1042 copy.JPG",
    "USB BACKUP NEWEST I THINK/DCIM copy FINAL/IMG_1043.JPG",
    "USB BACKUP NEWEST I THINK/WhatsApp Images/IMG-20200612-WA0012.jpg",
    "USB BACKUP NEWEST I THINK/WhatsApp Images/IMG-20200612-WA0013.jpg",
    "USB BACKUP NEWEST I THINK/best newest recipes/banana-bread.txt",
    "USB BACKUP NEWEST I THINK/best newest recipes/pasta.txt",
    "USB BACKUP NEWEST I THINK/important/Tax 2023 FINAL copy.pdf",
    "USB BACKUP NEWEST I THINK/important/passport scan copy.pdf",
}

EXPECTED_DUPLICATE_GROUPS = (
    {
        "LAPTOP OLD - DO NOT DELETE/Desktop/Tax 2023 FINAL.pdf",
        "USB BACKUP NEWEST I THINK/important/Tax 2023 FINAL copy.pdf",
    },
    {
        "LAPTOP OLD - DO NOT DELETE/Desktop/New Folder (12)/passport scan.pdf",
        "USB BACKUP NEWEST I THINK/important/passport scan copy.pdf",
    },
    {
        "LAPTOP OLD - DO NOT DELETE/Pictures/Camera/IMG_1042.JPG",
        "PHONE BACKUP DO NOT FORMAT/DCIM/Camera/IMG_1042.JPG",
        "USB BACKUP NEWEST I THINK/DCIM copy FINAL/IMG_1042 copy.JPG",
    },
    {
        "LAPTOP OLD - DO NOT DELETE/Pictures/Camera/IMG_1043.JPG",
        "USB BACKUP NEWEST I THINK/DCIM copy FINAL/IMG_1043.JPG",
    },
    {
        "LAPTOP OLD - DO NOT DELETE/Pictures/Summer Holiday/2021-07-14 beach.jpg",
        "USB BACKUP NEWEST I THINK/DCIM copy FINAL/2021-07-14 beach copy.jpg",
    },
    {
        "LAPTOP OLD - DO NOT DELETE/Recipes/banana-bread.txt",
        "USB BACKUP NEWEST I THINK/best newest recipes/banana-bread.txt",
    },
)

NON_CANONICAL_DUPLICATE_PATHS = {
    "PHONE BACKUP DO NOT FORMAT/DCIM/Camera/IMG_1042.JPG",
    "USB BACKUP NEWEST I THINK/DCIM copy FINAL/2021-07-14 beach copy.jpg",
    "USB BACKUP NEWEST I THINK/DCIM copy FINAL/IMG_1042 copy.JPG",
    "USB BACKUP NEWEST I THINK/DCIM copy FINAL/IMG_1043.JPG",
    "USB BACKUP NEWEST I THINK/best newest recipes/banana-bread.txt",
    "USB BACKUP NEWEST I THINK/important/Tax 2023 FINAL copy.pdf",
    "USB BACKUP NEWEST I THINK/important/passport scan copy.pdf",
}

# Compatibility name used by end-to-end tests for entries omitted from the
# deterministic baseline Plan.
OMITTED_SOURCE_PATHS = NON_CANONICAL_DUPLICATE_PATHS


def relative_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_example_source_matches_readme_before_tree() -> None:
    assert relative_files(SOURCE_ROOT) == EXPECTED_SOURCE_FILES
    assert not any(path.is_symlink() for path in SOURCE_ROOT.rglob("*"))


def test_prepared_source_reconstructs_all_declared_metadata(tmp_path: Path) -> None:
    prepared = copy_prepared_source(tmp_path / "selected-backup-root")
    manifest = load_fixture_manifest()

    assert set(manifest["source_modified_time_ns"]) == EXPECTED_SOURCE_FILES
    assert manifest["unreadable_source_paths"] == []
    for relative_path, expected_modified_time_ns in manifest[
        "source_modified_time_ns"
    ].items():
        source = prepared / relative_path
        assert source.stat().st_mtime_ns == expected_modified_time_ns
        assert source.stat().st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def test_synthetic_contents_encode_only_the_intended_exact_duplicates() -> None:
    paths_by_content: dict[bytes, set[str]] = {}
    for relative_path in EXPECTED_SOURCE_FILES:
        content = (SOURCE_ROOT / relative_path).read_bytes()
        paths_by_content.setdefault(content, set()).add(relative_path)

    actual_duplicate_groups = {
        frozenset(paths) for paths in paths_by_content.values() if len(paths) > 1
    }
    assert actual_duplicate_groups == {
        frozenset(paths) for paths in EXPECTED_DUPLICATE_GROUPS
    }

    old_cv = SOURCE_ROOT / "LAPTOP OLD - DO NOT DELETE/Desktop/CV final FINAL 2.docx"
    current_cv = SOURCE_ROOT / "Random Stuff - SORT LATER (2019)/CV current.docx"
    assert old_cv.read_bytes() != current_cv.read_bytes()
