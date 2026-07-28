"""Canonical POSIX vault filesystem primitives vendored into every skill."""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator

from .posix_runtime import (
    PosixRuntimeError,
    move_verified_noreplace,
    rename_noreplace,
    require_posix,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - guarded by require_posix
    fcntl = None  # type: ignore[assignment]


class VaultRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _raise_posix(exc: PosixRuntimeError) -> None:
    exit_code = {
        "UNSUPPORTED_PLATFORM": 3,
        "RECOVERY_CONFLICT": 4,
        "RESOURCE_BUSY": 4,
    }.get(exc.code, 2)
    raise VaultRuntimeError(
        exc.code,
        exc.message,
        exit_code=exit_code,
    ) from exc


def _safe_relative(relative: str | Path) -> Path:
    raw = os.fspath(relative)
    value = Path(raw)
    if (
        not raw
        or "\0" in raw
        or "\\" in raw
        or value.is_absolute()
        or not value.parts
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise VaultRuntimeError(
            "UNSAFE_PATH",
            "Expected a traversal-free relative POSIX path.",
            exit_code=2,
        )
    return value


def _absolute_path(path: Path) -> Path:
    value = Path(os.path.abspath(path.expanduser()))
    if sys.platform == "darwin" and len(value.parts) > 1:
        aliases = {"tmp": Path("/private/tmp"), "var": Path("/private/var")}
        replacement = aliases.get(value.parts[1])
        if (
            replacement is not None
            and Path(os.path.realpath(f"/{value.parts[1]}")) == replacement
        ):
            value = replacement.joinpath(*value.parts[2:])
    return value


def open_root(path: Path, *, require_owner: bool = True) -> int:
    """Open a safe directory chain without following any component symlink.

    A directory used as a managed root must be owned by the current user and
    must not be writable by group or world.  The returned descriptor is the
    authority; callers must not treat a prior pathname ``stat`` as equivalent.
    """

    raw = path.expanduser()
    if ".." in raw.parts:
        raise VaultRuntimeError(
            "UNSAFE_PATH",
            "Parent traversal is forbidden.",
            exit_code=2,
        )
    absolute = _absolute_path(raw)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (require_owner and metadata.st_uid != os.geteuid())
            or (require_owner and bool(metadata.st_mode & 0o022))
        ):
            raise VaultRuntimeError(
                "UNSAFE_PATH",
                "Managed root owner, type, or permissions are unsafe.",
                exit_code=2,
            )
        result = descriptor
        descriptor = -1
        return result
    except VaultRuntimeError:
        raise
    except OSError as exc:
        raise VaultRuntimeError(
            "UNSAFE_PATH",
            "Vault root cannot be opened safely.",
            exit_code=2,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def ensure_directory_path(path: Path, *, mode: int) -> None:
    """Create/open a directory using held FDs; never chmod through a pathname."""

    if mode not in {0o700, 0o755}:
        raise ValueError("directory mode must be 0700 or 0755")
    raw = path.expanduser()
    if ".." in raw.parts:
        raise VaultRuntimeError(
            "UNSAFE_PATH",
            "Parent traversal is forbidden.",
            exit_code=2,
        )
    absolute = _absolute_path(raw)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        parts = absolute.parts[1:]
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            requested_mode = mode if final else 0o755
            created = False
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, requested_mode, dir_fd=descriptor)
                    os.fsync(descriptor)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise VaultRuntimeError(
                    "UNSAFE_PATH",
                    "Managed path contains a non-directory.",
                    exit_code=2,
                )
            if final:
                if metadata.st_uid != os.geteuid():
                    os.close(child)
                    raise VaultRuntimeError(
                        "UNSAFE_PATH",
                        "Managed directory is not owned by the current user.",
                        exit_code=2,
                    )
                if created:
                    os.fchmod(child, requested_mode)
                    metadata = os.fstat(child)
                if mode == 0o700:
                    unsafe_mode = stat.S_IMODE(metadata.st_mode) != 0o700
                else:
                    unsafe_mode = bool(metadata.st_mode & 0o022)
                if unsafe_mode:
                    os.close(child)
                    raise VaultRuntimeError(
                        "UNSAFE_PATH",
                        "Managed directory permissions are unsafe.",
                        exit_code=2,
                    )
            if created:
                os.fsync(child)
            os.close(descriptor)
            descriptor = child
        os.fsync(descriptor)
    except VaultRuntimeError:
        raise
    except OSError as exc:
        raise VaultRuntimeError(
            "UNSAFE_PATH",
            "Managed directory cannot be opened safely.",
            exit_code=2,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def ensure_vault_root(path: Path) -> None:
    """Create a private vault root beneath an already-safe parent."""

    try:
        descriptor = open_root(path)
    except VaultRuntimeError:
        descriptor = -1
    else:
        os.close(descriptor)
        return

    parent_descriptor = open_root(path.parent, require_owner=False)
    child_descriptor = -1
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            bool(parent_metadata.st_mode & 0o022)
            and not bool(parent_metadata.st_mode & stat.S_ISVTX)
        ):
            raise VaultRuntimeError(
                "UNSAFE_PATH",
                "Vault parent is writable by another user.",
                exit_code=2,
            )
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except FileExistsError:
            pass
        child_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(child_descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise VaultRuntimeError(
                "UNSAFE_PATH",
                "Vault root ownership or permissions are unsafe.",
                exit_code=2,
            )
        os.fsync(child_descriptor)
    except VaultRuntimeError:
        raise
    except OSError as exc:
        raise VaultRuntimeError(
            "UNSAFE_PATH",
            "Vault root cannot be created safely.",
            exit_code=2,
        ) from exc
    finally:
        if child_descriptor >= 0:
            os.close(child_descriptor)
        os.close(parent_descriptor)


def walk_directory(
    root: Path,
    relative: str | Path,
    *,
    create: bool,
    final_mode: int = 0o700,
) -> int:
    """Open a current-user-owned exact-mode descendant directory."""

    safe = _safe_relative(relative)
    descriptor = open_root(root)
    try:
        for index, part in enumerate(safe.parts):
            requested_mode = final_mode if index == len(safe.parts) - 1 else 0o700
            created = False
            if create:
                try:
                    os.mkdir(part, requested_mode, dir_fd=descriptor)
                    os.fsync(descriptor)
                    created = True
                except FileExistsError:
                    pass
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise VaultRuntimeError(
                    "UNSAFE_PATH",
                    "Managed path contains a symlink or non-directory.",
                    exit_code=2,
                ) from exc
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if created:
                os.fchmod(descriptor, requested_mode)
                metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != requested_mode
            ):
                raise VaultRuntimeError(
                    "UNSAFE_PATH",
                    "Managed directory permissions are unsafe.",
                    exit_code=2,
                )
            if created:
                os.fsync(descriptor)
        result = descriptor
        descriptor = -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def ensure_relative_directory(
    root: Path,
    relative: str | Path,
    *,
    mode: int,
) -> None:
    descriptor = walk_directory(
        root,
        relative,
        create=True,
        final_mode=mode,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    descriptor = open_root(path)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise VaultRuntimeError(
            "FILESYSTEM_ERROR",
            "Managed directory could not be synchronized.",
            exit_code=5,
        ) from exc
    finally:
        os.close(descriptor)


def create_transaction_directory(
    vault: Path,
    *,
    parent: str | Path = ".awesome-capture/transactions",
    prefix: str,
) -> Path:
    """Allocate one private UUID-named transaction directory using mkdirat."""

    if (
        not prefix
        or prefix in {".", ".."}
        or "/" in prefix
        or "\\" in prefix
        or "\0" in prefix
    ):
        raise VaultRuntimeError(
            "UNSAFE_PATH",
            "Transaction prefix is unsafe.",
            exit_code=2,
        )
    safe_parent = _safe_relative(parent)
    ensure_relative_directory(vault, safe_parent, mode=0o700)
    parent_descriptor = walk_directory(
        vault,
        safe_parent,
        create=False,
    )
    child_descriptor = -1
    try:
        for _ in range(8):
            name = f"{prefix}{uuid.uuid4()}"
            try:
                os.mkdir(name, 0o700, dir_fd=parent_descriptor)
                break
            except FileExistsError:
                continue
        else:
            raise VaultRuntimeError(
                "RESOURCE_BUSY",
                "A unique transaction directory could not be allocated.",
                exit_code=4,
            )
        child_descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(child_descriptor)
        os.fchmod(child_descriptor, 0o700)
        metadata = os.fstat(child_descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Transaction directory identity is unsafe.",
                exit_code=4,
            )
        os.fsync(child_descriptor)
        os.fsync(parent_descriptor)
        return vault / safe_parent / name
    except VaultRuntimeError:
        raise
    except OSError as exc:
        raise VaultRuntimeError(
            "RECOVERY_CONFLICT",
            "Transaction directory could not be created safely.",
            exit_code=4,
        ) from exc
    finally:
        if child_descriptor >= 0:
            os.close(child_descriptor)
        os.close(parent_descriptor)


def write_new_file(path: Path, data: bytes | str, *, mode: int = 0o600) -> None:
    """Durably create one file through a no-follow parent descriptor."""

    if (
        not path.name
        or path.name in {".", ".."}
        or "/" in path.name
        or "\0" in path.name
    ):
        raise VaultRuntimeError(
            "UNSAFE_PATH",
            "Staged filename is unsafe.",
            exit_code=2,
        )
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    parent_descriptor = open_root(path.parent)
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            mode,
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        os.fchmod(descriptor, mode)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Staged file ownership, type, links, or mode is unsafe.",
                exit_code=4,
            )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise VaultRuntimeError(
                    "FILESYSTEM_ERROR",
                    "Short write while staging transaction content.",
                    exit_code=5,
                )
            view = view[written:]
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
    except VaultRuntimeError:
        raise
    except FileExistsError as exc:
        raise VaultRuntimeError(
            "RECOVERY_CONFLICT",
            "Staged file already exists.",
            exit_code=4,
        ) from exc
    except OSError as exc:
        raise VaultRuntimeError(
            "FILESYSTEM_ERROR",
            "Staged file could not be written safely.",
            exit_code=5,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def quarantine_completed_transaction(
    vault: Path,
    transaction: Path,
    *,
    expected_files: dict[str, str],
) -> Path:
    """Move a verified completed transaction into private quarantine."""

    transactions_relative = Path(".awesome-capture/transactions")
    quarantine_relative = Path(".awesome-capture/quarantine")
    expected_parent = vault / transactions_relative
    if transaction.parent != expected_parent:
        raise VaultRuntimeError(
            "RECOVERY_CONFLICT",
            "Transaction path is outside the managed transaction root.",
            exit_code=4,
        )
    if (
        not expected_files
        or len(expected_files) > 4
        or any(
            Path(name).name != name
            or name in {"", ".", ".."}
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for name, digest in expected_files.items()
        )
    ):
        raise VaultRuntimeError(
            "RECOVERY_CONFLICT",
            "Completed transaction evidence is invalid.",
            exit_code=4,
        )
    ensure_relative_directory(vault, quarantine_relative, mode=0o700)
    transactions_descriptor = walk_directory(
        vault,
        transactions_relative,
        create=False,
    )
    quarantine_descriptor = walk_directory(
        vault,
        quarantine_relative,
        create=False,
    )
    transaction_descriptor = -1
    evidence_descriptors: list[int] = []
    moved_descriptor = -1
    destination_name = f"completed-{transaction.name}"
    try:
        transaction_descriptor = os.open(
            transaction.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=transactions_descriptor,
        )
        transaction_metadata = os.fstat(transaction_descriptor)
        if (
            not stat.S_ISDIR(transaction_metadata.st_mode)
            or transaction_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(transaction_metadata.st_mode) != 0o700
        ):
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Completed transaction directory identity is unsafe.",
                exit_code=4,
            )
        with os.scandir(transaction_descriptor) as entries:
            names = sorted(entry.name for entry in entries)
        if names != sorted(expected_files):
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Completed transaction contains unexpected residue.",
                exit_code=4,
            )
        for name, expected_digest in sorted(expected_files.items()):
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=transaction_descriptor,
            )
            evidence_descriptors.append(descriptor)
            before = os.fstat(descriptor)
            digest = sha256_descriptor(descriptor)
            after = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or digest != expected_digest
                or (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_nlink,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                != (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_nlink,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
            ):
                raise VaultRuntimeError(
                    "RECOVERY_CONFLICT",
                    "Completed transaction evidence identity is unsafe.",
                    exit_code=4,
                )
        current = os.stat(
            transaction.name,
            dir_fd=transactions_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (transaction_metadata.st_dev, transaction_metadata.st_ino)
        ):
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Completed transaction path changed before quarantine.",
                exit_code=4,
            )
        try:
            moved_descriptor = move_verified_noreplace(
                transaction.name,
                destination_name,
                source_dir_fd=transactions_descriptor,
                destination_dir_fd=quarantine_descriptor,
                source_fd=transaction_descriptor,
                expected_kind="directory",
                rename_impl=rename_noreplace,
            )
        except FileExistsError as exc:
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Completed transaction quarantine destination already exists.",
                exit_code=4,
            ) from exc
        except PosixRuntimeError as exc:
            _raise_posix(exc)
        os.fsync(moved_descriptor)
        os.fsync(transactions_descriptor)
        os.fsync(quarantine_descriptor)
        return vault / quarantine_relative / destination_name
    except VaultRuntimeError:
        raise
    except OSError as exc:
        raise VaultRuntimeError(
            "RECOVERY_CONFLICT",
            "Completed transaction could not be quarantined safely.",
            exit_code=4,
        ) from exc
    finally:
        if moved_descriptor >= 0:
            os.close(moved_descriptor)
        for descriptor in evidence_descriptors:
            os.close(descriptor)
        if transaction_descriptor >= 0:
            os.close(transaction_descriptor)
        os.close(quarantine_descriptor)
        os.close(transactions_descriptor)


def sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def publish_relative(
    staged: Path,
    vault: Path,
    relative: str | Path,
    expected_hash: str,
    *,
    mode: int,
) -> None:
    """Move one staged file into place atomically without clobbering."""

    safe = _safe_relative(relative)
    if safe.parent == Path("."):
        parent_descriptor = open_root(vault)
    else:
        parent_descriptor = walk_directory(
            vault,
            safe.parent,
            create=True,
            final_mode=0o700,
        )
    source = -1
    source_parent = -1
    destination = -1
    try:
        try:
            staged_relative = staged.relative_to(vault)
        except ValueError as exc:
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Transaction staging file escapes the vault.",
                exit_code=4,
            ) from exc
        staged_relative = _safe_relative(staged_relative)
        source_parent = walk_directory(
            vault,
            staged_relative.parent,
            create=False,
        )
        try:
            source = os.open(
                staged_relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_parent,
            )
        except FileNotFoundError:
            source = -1
        except OSError as exc:
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Transaction staging file cannot be opened safely.",
                exit_code=4,
            ) from exc

        try:
            destination = os.open(
                safe.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            destination = -1
        except OSError as exc:
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Destination is not a safe regular file.",
                exit_code=4,
            ) from exc

        if destination >= 0:
            metadata = os.fstat(destination)
            if source >= 0:
                raise VaultRuntimeError(
                    "RECOVERY_CONFLICT",
                    "Both staging and destination paths exist.",
                    exit_code=4,
                )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) not in {0o600, mode}
                or sha256_descriptor(destination) != expected_hash
            ):
                raise VaultRuntimeError(
                    "RECOVERY_CONFLICT",
                    "A transaction destination differs.",
                    exit_code=4,
                )
            after = os.fstat(destination)
            if (
                (after.st_dev, after.st_ino, after.st_size, after.st_nlink)
                != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_nlink,
                )
                or after.st_mtime_ns != metadata.st_mtime_ns
                or after.st_ctime_ns != metadata.st_ctime_ns
            ):
                raise VaultRuntimeError(
                    "RECOVERY_CONFLICT",
                    "Destination changed while it was revalidated.",
                    exit_code=4,
                )
            if stat.S_IMODE(after.st_mode) != mode:
                os.fchmod(destination, mode)
            os.fsync(destination)
            os.fsync(parent_descriptor)
            return

        if source < 0:
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Transaction step has neither staging nor destination content.",
                exit_code=4,
            )
        source_metadata = os.fstat(source)
        if (
            not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_uid != os.geteuid()
            or source_metadata.st_nlink != 1
            or stat.S_IMODE(source_metadata.st_mode) != 0o600
            or sha256_descriptor(source) != expected_hash
        ):
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Transaction staging file ownership, links, mode, or hash is unsafe.",
                exit_code=4,
            )
        source_after = os.fstat(source)
        if (
            (
                source_after.st_dev,
                source_after.st_ino,
                source_after.st_size,
                source_after.st_nlink,
            )
            != (
                source_metadata.st_dev,
                source_metadata.st_ino,
                source_metadata.st_size,
                source_metadata.st_nlink,
            )
            or source_after.st_mtime_ns != source_metadata.st_mtime_ns
            or source_after.st_ctime_ns != source_metadata.st_ctime_ns
        ):
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Transaction staging file changed while it was revalidated.",
                exit_code=4,
            )
        try:
            destination = move_verified_noreplace(
                staged_relative.name,
                safe.name,
                source_dir_fd=source_parent,
                destination_dir_fd=parent_descriptor,
                source_fd=source,
                expected_kind="file",
                rename_impl=rename_noreplace,
            )
        except FileExistsError as exc:
            raise VaultRuntimeError(
                "RECOVERY_CONFLICT",
                "Destination appeared during publish.",
                exit_code=4,
            ) from exc
        except PosixRuntimeError as exc:
            _raise_posix(exc)
        published = os.fstat(destination)
        current_source = os.fstat(source)
        if (
            (published.st_dev, published.st_ino)
            != (source_metadata.st_dev, source_metadata.st_ino)
            or (current_source.st_dev, current_source.st_ino)
            != (source_metadata.st_dev, source_metadata.st_ino)
            or published.st_nlink != 1
            or current_source.st_nlink != 1
            or sha256_descriptor(destination) != expected_hash
        ):
            raise VaultRuntimeError(
                "INTEGRITY_FAILED",
                "Published transaction identity changed.",
                exit_code=7,
            )
        os.fchmod(destination, mode)
        os.fsync(destination)
        os.fsync(source_parent)
        os.fsync(parent_descriptor)
    finally:
        if destination >= 0:
            os.close(destination)
        if source >= 0:
            os.close(source)
        if source_parent >= 0:
            os.close(source_parent)
        os.close(parent_descriptor)


@contextlib.contextmanager
def vault_lock(
    vault: Path,
    *,
    exclusive: bool,
    timeout: float,
    create: bool,
) -> Iterator[int | None]:
    """Hold the one persistent lock shared by vault build and ingest.

    The vault root descriptor remains open for the complete lock lifetime.
    Metadata lookup is relative to that descriptor, so a pathname exchange
    cannot silently move the lock operation to another vault.
    """

    try:
        require_posix()
    except PosixRuntimeError as exc:
        _raise_posix(exc)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or timeout < 0
    ):
        raise VaultRuntimeError(
            "INVALID_ARGUMENT",
            "--lock-timeout must be a finite non-negative number.",
            exit_code=2,
        )
    root_descriptor = open_root(vault)
    metadata_descriptor = -1
    descriptor = -1
    try:
        try:
            try:
                metadata_descriptor = os.open(
                    ".awesome-capture",
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    dir_fd=root_descriptor,
                )
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(".awesome-capture", 0o700, dir_fd=root_descriptor)
                    os.fsync(root_descriptor)
                except FileExistsError:
                    pass
                metadata_descriptor = os.open(
                    ".awesome-capture",
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    dir_fd=root_descriptor,
                )
            metadata = os.fstat(metadata_descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise VaultRuntimeError(
                    "UNSAFE_PATH",
                    "Vault metadata directory ownership or permissions are unsafe.",
                    exit_code=2,
                )
            os.fsync(metadata_descriptor)
        except VaultRuntimeError:
            raise
        except OSError as exc:
            if not create and isinstance(
                exc,
                (FileNotFoundError, NotADirectoryError),
            ):
                yield None
                return
            raise VaultRuntimeError(
                "UNSAFE_PATH",
                "Vault metadata directory cannot be opened safely.",
                exit_code=2,
            ) from exc

        flags = (
            (os.O_RDWR if exclusive else os.O_RDONLY)
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
        )
        created = False
        for _ in range(8):
            try:
                descriptor = os.open(
                    "vault.lock",
                    flags,
                    dir_fd=metadata_descriptor,
                )
                break
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    descriptor = os.open(
                        "vault.lock",
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=metadata_descriptor,
                    )
                    created = True
                    os.fsync(metadata_descriptor)
                    break
                except FileExistsError:
                    continue
        if descriptor < 0:
            raise VaultRuntimeError(
                "UNSAFE_LOCK",
                "Vault lock could not be opened without a race.",
                exit_code=2,
            )
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise VaultRuntimeError(
                "UNSAFE_LOCK",
                "Vault lock is not a private regular file.",
                exit_code=2,
            )
        assert fcntl is not None
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + float(timeout)
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise VaultRuntimeError(
                        "VAULT_BUSY",
                        "Timed out waiting for the vault lock.",
                        exit_code=4,
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield descriptor
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if metadata_descriptor >= 0:
            os.close(metadata_descriptor)
        os.close(root_descriptor)
