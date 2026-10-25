from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

import pytest

from filesystem_organizer import linux_filesystem


def test_effective_mount_uses_longest_match_and_effective_mount_options() -> None:
    lines = [
        "1 0 8:2 / / rw,relatime - ext4 /dev/root rw",
        "2 1 8:1 / /media/user/USB ro,nodev - vfat /dev/sda1 rw,flush",
    ]

    mount = linux_filesystem._effective_mount_from_lines(
        Path("/media/user/USB/backups"), lines
    )

    assert mount.root == Path("/media/user/USB")
    assert mount.filesystem_type == "vfat"
    assert mount.source == "/dev/sda1"
    assert mount.device == "8:1"
    assert mount.read_only is True


def test_nonblocking_lock_refuses_a_second_holder(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "run.lock"
    with (
        linux_filesystem.nonblocking_lock(lock),
        pytest.raises(linux_filesystem.LinuxFilesystemError, match="already holds"),
        linux_filesystem.nonblocking_lock(lock),
    ):
        pass


def test_destination_lock_path_is_stable_and_parent_scoped(tmp_path: Path) -> None:
    destination = tmp_path / "materialized" / "result"
    lock = linux_filesystem.destination_lock_path(destination)

    assert lock.parent == destination.parent
    assert lock.name.startswith(".filesystem-organizer-lock-")
    assert lock == linux_filesystem.destination_lock_path(destination)


def test_clone_unavailable_removes_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"payload")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(fcntl, "ioctl", unavailable)

    assert (
        linux_filesystem.try_native_clone(source, destination)
        is linux_filesystem.CloneResult.UNSUPPORTED_SCOPE
    )
    assert not destination.exists()


def test_sync_filesystem_uses_injected_linux_system(tmp_path: Path) -> None:
    calls: list[int] = []

    class FakeSystem:
        def syncfs(self, descriptor: int) -> None:
            calls.append(descriptor)

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            raise AssertionError("not used")

        def rename_plain(self, source: bytes, destination: bytes) -> None:
            raise AssertionError("not used")

    linux_filesystem.sync_filesystem(tmp_path, system=FakeSystem())

    assert calls


def test_hard_link_probe_leaves_no_artifacts(tmp_path: Path) -> None:
    linux_filesystem.probe_hard_link_support(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_hard_link_probe_classifies_only_capability_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(*_args: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(os, "link", unsupported)

    with pytest.raises(linux_filesystem.HardLinkUnsupportedError):
        linux_filesystem.probe_hard_link_support(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_hard_link_probe_preserves_operational_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*_args: object) -> None:
        raise OSError(errno.EIO, "writeback failed")

    monkeypatch.setattr(os, "link", failed)

    with pytest.raises(linux_filesystem.LinuxFilesystemError) as raised:
        linux_filesystem.probe_hard_link_support(tmp_path)

    assert not isinstance(raised.value, linux_filesystem.HardLinkUnsupportedError)
    assert list(tmp_path.iterdir()) == []


def test_rename_protection_probe_proves_no_replace_and_cleans_up(
    tmp_path: Path,
) -> None:
    linux_filesystem.probe_rename_protection_support(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_strict_no_replace_rename_preserves_an_existing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")

    with pytest.raises(linux_filesystem.LinuxFilesystemError, match="already exists"):
        linux_filesystem.rename_no_replace(source, destination)

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"


def test_raw_no_replace_rename_uses_one_syscall_without_metadata_observation(
    tmp_path: Path,
) -> None:
    """The v3 ordinary-PLACE seam is one raw renameat2 invocation."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"payload")
    calls: list[tuple[bytes, bytes]] = []

    class RawRenameSystem:
        def rename_no_replace(self, raw_source: bytes, raw_destination: bytes) -> None:
            calls.append((raw_source, raw_destination))
            os.rename(raw_source, raw_destination)

    linux_filesystem.rename_no_replace_raw(
        source, destination, system=RawRenameSystem()
    )

    assert calls == [(os.fsencode(source), os.fsencode(destination))]
    assert not source.exists()
    assert destination.read_bytes() == b"payload"


def test_raw_no_replace_rename_preserves_the_syscall_errno(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"payload")

    class FailingRawRenameSystem:
        def rename_no_replace(self, raw_source: bytes, raw_destination: bytes) -> None:
            raise OSError(errno.EIO, "injected writeback failure")

    with pytest.raises(OSError) as raised:
        linux_filesystem.rename_no_replace_raw(
            source, destination, system=FailingRawRenameSystem()
        )

    assert raised.value.errno == errno.EIO
    assert source.read_bytes() == b"payload"
    assert not destination.exists()


def test_destination_probe_exercises_no_replace_and_cleans_up(tmp_path: Path) -> None:
    capabilities = linux_filesystem.probe_destination_capabilities(tmp_path)

    assert capabilities.writable
    assert capabilities.supports_atomic_no_replace
    assert list(tmp_path.iterdir()) == []


def test_destination_probe_detects_unsupported_no_replace_and_plain_rename(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class UnsupportedNoReplace:
        def syncfs(self, descriptor: int) -> None:
            raise AssertionError("not used")

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            calls.append("no-replace")
            raise OSError(errno.EINVAL, "unsupported rename flag")

        def rename_plain(self, source: bytes, destination: bytes) -> None:
            calls.append("plain")
            Path(source.decode()).rename(Path(destination.decode()))

    capabilities = linux_filesystem.probe_destination_capabilities(
        tmp_path, system=UnsupportedNoReplace()
    )

    assert capabilities.writable
    assert not capabilities.supports_atomic_no_replace
    assert calls == ["no-replace", "plain"]
    assert list(tmp_path.iterdir()) == []


def test_publish_uses_exact_no_replace_without_plain_fallback(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    calls: list[str] = []

    class ExactNoReplace:
        def syncfs(self, descriptor: int) -> None:
            raise AssertionError("not used")

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            calls.append("no-replace")
            Path(source.decode()).rename(Path(destination.decode()))

        def rename_plain(self, source: bytes, destination: bytes) -> None:
            raise AssertionError("plain rename must not be used")

    method = linux_filesystem.publish_directory(
        staging, destination, system=ExactNoReplace()
    )

    assert method is linux_filesystem.PublicationMethod.ATOMIC_NO_REPLACE
    assert calls == ["no-replace"]
    assert destination.is_dir()


def test_publish_falls_back_for_unsupported_no_replace(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    (staging / "payload").write_bytes(b"complete")
    calls: list[str] = []

    class FuseStyleSystem:
        def syncfs(self, descriptor: int) -> None:
            raise AssertionError("not used")

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            calls.append("no-replace")
            raise OSError(errno.EINVAL, "unsupported rename flag")

        def rename_plain(self, source: bytes, destination: bytes) -> None:
            calls.append("plain")
            Path(source.decode()).rename(Path(destination.decode()))

    method = linux_filesystem.publish_directory(
        staging, destination, system=FuseStyleSystem()
    )

    assert method is linux_filesystem.PublicationMethod.LOCKED_RENAME_FALLBACK
    assert calls == ["no-replace", "plain"]
    assert (destination / "payload").read_bytes() == b"complete"


def test_fallback_never_renames_over_an_existing_destination(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    destination.mkdir()
    marker = destination / "foreign"
    marker.write_bytes(b"must survive")

    class UnsupportedNoReplace:
        def syncfs(self, descriptor: int) -> None:
            raise AssertionError("not used")

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            raise OSError(errno.EINVAL, "unsupported rename flag")

        def rename_plain(self, source: bytes, destination: bytes) -> None:
            raise AssertionError("plain rename must not be attempted")

    with pytest.raises(linux_filesystem.LinuxFilesystemError, match="already exists"):
        linux_filesystem.publish_directory(
            staging, destination, system=UnsupportedNoReplace()
        )

    assert marker.read_bytes() == b"must survive"
    assert staging.is_dir()


def test_fallback_unsupported_error_is_not_misreported_as_no_replace_capability(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()

    class BrokenPlainRename:
        def syncfs(self, descriptor: int) -> None:
            raise AssertionError("not used")

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            raise OSError(errno.EINVAL, "unsupported rename flag")

        def rename_plain(self, source: bytes, destination: bytes) -> None:
            raise OSError(errno.EINVAL, "plain rename failed")

    with pytest.raises(linux_filesystem.LockedRenameFallbackError):
        linux_filesystem.publish_directory(
            staging, destination, system=BrokenPlainRename()
        )

    assert staging.is_dir()
    assert not destination.exists()


def test_publish_refuses_existing_destination_with_injected_system(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()

    class ExistingDestination:
        def syncfs(self, descriptor: int) -> None:
            raise AssertionError("not used")

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            raise FileExistsError(errno.EEXIST, "exists")

        def rename_plain(self, source: bytes, destination: bytes) -> None:
            raise AssertionError("not used")

    with pytest.raises(linux_filesystem.LinuxFilesystemError, match="already exists"):
        linux_filesystem.publish_directory(
            staging, destination, system=ExistingDestination()
        )


def test_clone_cache_only_remembers_conclusive_unsupported_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = [tmp_path / f"source-{index}" for index in range(3)]
    for source in sources:
        source.write_bytes(b"payload")
    calls = 0
    failure = errno.EOPNOTSUPP

    def unsupported(*_args: object) -> None:
        nonlocal calls
        calls += 1
        raise OSError(failure, "unsupported")

    monkeypatch.setattr(fcntl, "ioctl", unsupported)
    cache = linux_filesystem.CloneCapabilities()
    for index in range(2):
        cache.clone(sources[0], tmp_path / f"output-{index}")
    assert calls == 2  # EOPNOTSUPP can describe file-specific restrictions.
    cache.clone(sources[1], tmp_path / "output-2")
    cache.clone(sources[2], tmp_path / "output-3")
    assert calls == 3  # Two distinct files establish device-pair evidence.

    cache = linux_filesystem.CloneCapabilities()
    failure = errno.ENOTTY
    for index in range(4, 7):
        cache.clone(sources[index - 4], tmp_path / f"output-{index}")
    assert calls == 4
    assert set(tmp_path.iterdir()) == set(sources)
