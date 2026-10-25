"""Small Linux syscall seam used by materialization orchestration.

The module deliberately exposes typed outcomes instead of leaking errno policy
through workflow code.  Its ``LinuxSystem`` dependency is replaceable in tests.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

_FICLONE = 0x4004_9409
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class LinuxFilesystemError(Exception):
    """A Linux filesystem operation could not establish its stated guarantee."""


class HardLinkUnsupportedError(LinuxFilesystemError):
    """The filesystem specifically reports that hard links are unsupported."""


def _unescape_mount_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def effective_filesystem_type(path: Path) -> str:
    """Return the effective filesystem type from Linux's longest mount match."""
    return inspect_effective_mount(path).filesystem_type


class LockedRenameFallbackError(LinuxFilesystemError):
    """The degraded publication primitive failed after capability selection."""


class CloneResult(StrEnum):
    CLONED = "cloned"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED_SCOPE = "unsupported-scope"
    UNSUPPORTED_IOCTL = "unsupported-ioctl"


class PublicationMethod(StrEnum):
    """The no-overwrite guarantee used to publish a completed tree."""

    ATOMIC_NO_REPLACE = "atomic-no-replace"
    LOCKED_RENAME_FALLBACK = "locked-rename-fallback"


class LinuxSystem(Protocol):
    def syncfs(self, descriptor: int) -> None: ...

    def rename_no_replace(self, source: bytes, destination: bytes) -> None: ...

    def rename_plain(self, source: bytes, destination: bytes) -> None: ...


class RawRenameSystem(Protocol):
    """Narrow syscall seam for v3's ordinary Placement primitive."""

    def rename_no_replace(self, source: bytes, destination: bytes) -> None: ...


class _CtypesLinuxSystem:
    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)

    def syncfs(self, descriptor: int) -> None:
        if self._libc.syncfs(descriptor) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def rename_no_replace(self, source: bytes, destination: bytes) -> None:
        if (
            self._libc.renameat2(
            _AT_FDCWD, source, _AT_FDCWD, destination, _RENAME_NOREPLACE
            )
            != 0
        ):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def rename_plain(self, source: bytes, destination: bytes) -> None:
        if self._libc.rename(source, destination) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))


@dataclass(frozen=True)
class DestinationCapabilities:
    writable: bool
    supports_atomic_no_replace: bool


@dataclass(frozen=True)
class EffectiveMount:
    root: Path
    filesystem_type: str
    source: str
    device: str
    read_only: bool


def _effective_mount_from_lines(path: Path, lines: list[str]) -> EffectiveMount:
    best: EffectiveMount | None = None
    for line in lines:
        left, separator, right = line.partition(" - ")
        fields = left.split()
        filesystem = right.split()
        if not separator or len(fields) < 6 or len(filesystem) < 2:
            continue
        mount = Path(_unescape_mount_path(fields[4]))
        if path != mount and mount not in path.parents:
            continue
        candidate = EffectiveMount(
            root=mount,
            filesystem_type=filesystem[0],
            source=_unescape_mount_path(filesystem[1]),
            device=fields[2],
            read_only="ro" in fields[5].split(","),
        )
        if best is None or len(candidate.root.parts) > len(best.root.parts):
            best = candidate
    if best is None:
        raise LinuxFilesystemError(f"cannot determine effective mount: {path}")
    return best


def inspect_effective_mount(path: Path) -> EffectiveMount:
    """Return the effective Linux mount selected by the longest path match."""
    _require_linux()
    resolved = path.resolve(strict=True)
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise LinuxFilesystemError("cannot inspect effective mount") from error
    return _effective_mount_from_lines(resolved, lines)


def _require_linux() -> None:
    platform = os.uname().sysname
    if platform != "Linux":
        raise LinuxFilesystemError(
            f"Linux filesystem support is unavailable: {platform}"
        )


def rename_no_replace_raw(
    source: Path, destination: Path, *, system: RawRenameSystem | None = None
) -> None:
    """Issue exactly one raw ``renameat2(RENAME_NOREPLACE)`` call.

    This is the v3 ordinary-Placement primitive.  It deliberately performs no
    metadata observation, parent creation, capability fallback, or errno
    translation; callers own all scheduling and exceptional-error policy.
    """
    _require_linux()
    (system or _CtypesLinuxSystem()).rename_no_replace(
        os.fsencode(source), os.fsencode(destination)
    )


def destination_lock_path(destination: Path) -> Path:
    """Return the persistent sidecar lock path for one canonical destination."""
    canonical = str(destination.expanduser().resolve(strict=False))
    token = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return destination.parent / f".filesystem-organizer-lock-{token}"


@contextmanager
def nonblocking_lock(path: Path) -> Iterator[int]:
    """Hold an advisory lock whose lifetime is bound to an open descriptor."""
    _require_linux()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LinuxFilesystemError(
                f"operation already holds Linux filesystem lock: {path}"
            ) from error
        yield descriptor
    finally:
        os.close(descriptor)


def try_native_clone(source: Path, destination: Path) -> CloneResult:
    """Clone on the destination filesystem, with a narrow fallback allowlist."""
    _require_linux()
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    try:
        destination_fd = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.ioctl(destination_fd, _FICLONE, source_fd)
        except OSError as error:
            if error.errno in {errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV}:
                destination.unlink(missing_ok=True)
                if error.errno == errno.ENOTTY:
                    return CloneResult.UNSUPPORTED_IOCTL
                if error.errno == errno.EOPNOTSUPP:
                    return CloneResult.UNSUPPORTED_SCOPE
                return CloneResult.UNAVAILABLE
            raise LinuxFilesystemError("native clone failed") from error
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    return CloneResult.CLONED


class CloneCapabilities:
    """Attempt-local negative capability cache for ordinary regular files.

    Device observations are taken per file, so nested mounts don't inherit root
    policy. ENOTTY is conclusive. EOPNOTSUPP is cached only after distinct regular
    files on the same device pair reject the whole-file operation; one rejection
    can still be a file-specific restriction. EXDEV is never cached.
    """

    def __init__(
        self, clone: Callable[[Path, Path], CloneResult] = try_native_clone
    ) -> None:
        self._clone = clone
        self._unsupported: set[tuple[int, int]] = set()
        self._scope_rejections: dict[tuple[int, int], set[tuple[int, int]]] = {}
        self.attempts = 0

    def clone(self, source: Path, destination: Path) -> CloneResult:
        scope = (source.stat().st_dev, destination.parent.stat().st_dev)
        if scope[0] != scope[1] or scope in self._unsupported:
            return CloneResult.UNAVAILABLE
        self.attempts += 1
        result = self._clone(source, destination)
        if result is CloneResult.UNSUPPORTED_IOCTL:
            self._unsupported.add(scope)
        elif result is CloneResult.UNSUPPORTED_SCOPE:
            source_stat = source.stat()
            rejected_files = self._scope_rejections.setdefault(scope, set())
            rejected_files.add((source_stat.st_dev, source_stat.st_ino))
            if len(rejected_files) >= 2:
                self._unsupported.add(scope)
        return result


def probe_hard_link_support(root: Path) -> None:
    """Prove that in-place protection can create and remove a hard link."""
    _require_linux()
    probe = root / f".filesystem-organizer-hard-link-probe-{uuid.uuid4()}"
    linked = probe.with_name(f"{probe.name}.link")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600
        )
        os.close(descriptor)
        descriptor = None
    except OSError as error:
        raise LinuxFilesystemError("hard-link probe file creation failed") from error
    try:
        os.link(probe, linked)
    except OSError as error:
        if error.errno in {
            errno.EPERM,
            errno.EOPNOTSUPP,
            errno.ENOSYS,
            errno.EINVAL,
        }:
            raise HardLinkUnsupportedError(
                "hard links are unsupported on the selected filesystem"
            ) from error
        raise LinuxFilesystemError("hard-link capability probe failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        linked.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)


def rename_no_replace(
    source: Path, destination: Path, *, system: LinuxSystem | None = None
) -> None:
    """Rename one entry on the same filesystem without replacing an occupant."""
    _require_linux()
    try:
        if source.lstat().st_dev != destination.parent.stat().st_dev:
            raise LinuxFilesystemError(
                "source and destination must be on the same filesystem"
            )
        (system or _CtypesLinuxSystem()).rename_no_replace(
            os.fsencode(source), os.fsencode(destination)
        )
    except FileExistsError as error:
        raise LinuxFilesystemError(
            f"destination already exists: {destination}"
        ) from error
    except LinuxFilesystemError:
        raise
    except OSError as error:
        if error.errno in _NO_REPLACE_UNSUPPORTED_ERRORS:
            raise LinuxFilesystemError(
                "strict atomic no-replace rename is unsupported"
            ) from error
        raise LinuxFilesystemError("atomic no-replace rename failed") from error


def probe_rename_protection_support(
    root: Path, *, system: LinuxSystem | None = None
) -> None:
    """Prove every filesystem primitive required by RenameProtection v1."""
    _require_linux()
    active_system = system or _CtypesLinuxSystem()
    token = uuid.uuid4().hex
    prefix = f".filesystem-organizer-rename-probe-{token}"
    file_source = root / f"{prefix}.file-source"
    file_destination = root / f"{prefix}.file-destination"
    occupied_source = root / f"{prefix}.occupied-source"
    occupied_destination = root / f"{prefix}.occupied-destination"
    directory_source = root / f"{prefix}.directory-source"
    directory_destination = root / f"{prefix}.directory-destination"
    lock = root / f"{prefix}.lock"
    try:
        file_source.write_bytes(b"rename-protection-file")
        rename_no_replace(file_source, file_destination, system=active_system)
        if file_destination.read_bytes() != b"rename-protection-file":
            raise LinuxFilesystemError("file rename probe changed payload bytes")

        directory_source.mkdir()
        rename_no_replace(directory_source, directory_destination, system=active_system)
        if not directory_destination.is_dir():
            raise LinuxFilesystemError("directory rename probe did not preserve entry type")

        occupied_source.write_bytes(b"source")
        occupied_destination.write_bytes(b"destination")
        try:
            rename_no_replace(
                occupied_source, occupied_destination, system=active_system
            )
        except LinuxFilesystemError as error:
            if "already exists" not in str(error):
                raise
        else:
            raise LinuxFilesystemError(
                "strict no-replace probe unexpectedly replaced an occupant"
            )
        if (
            occupied_source.read_bytes() != b"source"
            or occupied_destination.read_bytes() != b"destination"
        ):
            raise LinuxFilesystemError(
                "strict no-replace probe did not preserve both occupied entries"
            )
        sync_filesystem(root, system=active_system)
        with nonblocking_lock(lock):
            pass
    except (LinuxFilesystemError, OSError) as error:
        if isinstance(error, LinuxFilesystemError):
            raise
        raise LinuxFilesystemError(
            "RenameProtection filesystem capabilities are unavailable"
        ) from error
    finally:
        for path in (
            file_source,
            file_destination,
            occupied_source,
            occupied_destination,
            lock,
        ):
            path.unlink(missing_ok=True)
        for path in (directory_source, directory_destination):
            try:
                path.rmdir()
            except FileNotFoundError:
                pass


def probe_destination_capabilities(
    parent: Path, *, system: LinuxSystem | None = None
) -> DestinationCapabilities:
    """Exercise writable publication and exact no-replace support with cleanup."""
    _require_linux()
    token = uuid.uuid4().hex
    staging = parent / f".filesystem-organizer-probe-{token}.staging"
    destination = parent / f".filesystem-organizer-probe-{token}.destination"
    active_system = system or _CtypesLinuxSystem()
    try:
        staging.mkdir()
        active_system.rename_no_replace(os.fsencode(staging), os.fsencode(destination))
        return DestinationCapabilities(writable=True, supports_atomic_no_replace=True)
    except OSError as error:
        if error.errno in _NO_REPLACE_UNSUPPORTED_ERRORS:
            try:
                active_system.rename_plain(
                    os.fsencode(staging), os.fsencode(destination)
                )
            except OSError as fallback_error:
                raise LinuxFilesystemError(
                    f"destination lacks required write or atomic publication capability: {parent}"
                ) from fallback_error
            return DestinationCapabilities(
                writable=True, supports_atomic_no_replace=False
            )
        raise LinuxFilesystemError(
            f"destination lacks required write or atomic publication capability: {parent}"
        ) from error
    finally:
        destination.rmdir() if destination.is_dir() else None
        staging.rmdir() if staging.is_dir() else None


class FilesystemSync:
    """Retain the writeback-error cursor from before writes until publication.

    Linux >= 5.8 tracks syncfs errors per open file description. Reopening a
    descriptor at each checkpoint can miss errors already reported by writeback.
    """

    def __init__(self, path: Path, *, system: LinuxSystem | None = None) -> None:
        _require_linux()
        release = os.uname().release.split(".")
        if tuple(int(part) for part in release[:2]) < (5, 8):
            raise LinuxFilesystemError("batched durability requires Linux 5.8 or newer")
        self._descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self._device = os.fstat(self._descriptor).st_dev
        self._system = system or _CtypesLinuxSystem()

    def sync(self, path: Path) -> None:
        try:
            observed_device = path.stat().st_dev
        except OSError as error:
            raise LinuxFilesystemError(
                f"could not inspect filesystem for {path}"
            ) from error
        if observed_device != self._device:
            raise LinuxFilesystemError(
                "destination filesystem changed during materialization"
            )
        try:
            self._system.syncfs(self._descriptor)
        except OSError as error:
            raise LinuxFilesystemError(
                f"could not synchronize filesystem for {path}"
            ) from error

    def close(self) -> None:
        os.close(self._descriptor)


def sync_filesystem(path: Path, *, system: LinuxSystem | None = None) -> None:
    """Synchronize the filesystem containing ``path`` once per phase."""
    _require_linux()
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            (system or _CtypesLinuxSystem()).syncfs(descriptor)
        except OSError as error:
            raise LinuxFilesystemError(
                f"could not synchronize filesystem for {path}"
            ) from error
    finally:
        os.close(descriptor)


def publish_directory(
    staging: Path, destination: Path, *, system: LinuxSystem | None = None
) -> PublicationMethod:
    """Publish a completed tree, preferring exact atomic no-replace semantics.

    ``LOCKED_RENAME_FALLBACK`` is only for filesystems that reject
    ``RENAME_NOREPLACE``.  Its caller must hold the destination sidecar lock.
    It checks that the final name is absent immediately before a same-filesystem
    plain rename.  The tree remains atomically visible, but an uncooperative
    external writer can create that name in the intervening race window.
    """
    _require_linux()
    active_system = system or _CtypesLinuxSystem()
    try:
        staging_device = staging.lstat().st_dev
        destination_device = destination.parent.stat().st_dev
    except OSError as error:
        raise LinuxFilesystemError(
            "could not verify same-filesystem publication"
        ) from error
    if staging_device != destination_device:
        raise LinuxFilesystemError(
            "staging and final destination must be on the same filesystem"
        )
    try:
        active_system.rename_no_replace(os.fsencode(staging), os.fsencode(destination))
        return PublicationMethod.ATOMIC_NO_REPLACE
    except FileExistsError as error:
        raise LinuxFilesystemError(
            f"destination already exists: {destination}"
        ) from error
    except OSError as error:
        if error.errno not in _NO_REPLACE_UNSUPPORTED_ERRORS:
            raise LinuxFilesystemError(
                "atomic no-replace publication failed"
            ) from error
    if not _path_entry_absent(destination):
        conflict = FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), destination)
        raise LinuxFilesystemError(
            f"destination already exists: {destination}"
        ) from conflict
    try:
        active_system.rename_plain(os.fsencode(staging), os.fsencode(destination))
    except OSError as error:
        raise LockedRenameFallbackError(
            "locked rename fallback publication failed"
        ) from error
    return PublicationMethod.LOCKED_RENAME_FALLBACK


_NO_REPLACE_UNSUPPORTED_ERRORS = frozenset(
    {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}
)


def _path_entry_absent(path: Path) -> bool:
    """Check entry absence without following a possibly dangling symlink."""
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError as error:
        raise LinuxFilesystemError(
            f"could not verify destination absence: {path}"
        ) from error
    return False
