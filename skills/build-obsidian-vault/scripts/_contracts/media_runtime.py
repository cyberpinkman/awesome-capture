"""Canonical POSIX media filesystem primitives vendored into every skill."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import math
import os
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from .posix_runtime import (
    PosixRuntimeError,
    exchange_verified,
    move_verified_noreplace,
    rename_exchange,
    rename_exchange_available,
    rename_noreplace,
    rename_noreplace_available,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - guarded by require_posix_security
    fcntl = None  # type: ignore[assignment]


class SafeRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _move_private_noreplace(
    source_name: str,
    destination_name: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
    source_fd: int,
    expected_kind: str,
) -> int:
    try:
        return move_verified_noreplace(
            source_name,
            destination_name,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
            source_fd=source_fd,
            expected_kind=expected_kind,
            rename_impl=rename_noreplace,
        )
    except PosixRuntimeError as exc:
        raise SafeRuntimeError(
            exc.code,
            exc.message,
            exit_code=4 if exc.code == "RECOVERY_CONFLICT" else 7,
        ) from exc


def _exchange_private(
    source_name: str,
    destination_name: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
    source_fd: int,
    destination_fd: int,
    expected_kind: str,
) -> tuple[int, int]:
    try:
        return exchange_verified(
            source_name,
            destination_name,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
            source_fd=source_fd,
            destination_fd=destination_fd,
            expected_kind=expected_kind,
            exchange_impl=rename_exchange,
        )
    except PosixRuntimeError as exc:
        raise SafeRuntimeError(
            exc.code,
            exc.message,
            exit_code=4 if exc.code == "RECOVERY_CONFLICT" else 7,
        ) from exc


def require_posix_security() -> None:
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC")
    missing = [name for name in required if not hasattr(os, name)]
    dir_fd_functions = (os.open, os.mkdir, os.stat, os.unlink, os.link, os.rename)
    if (
        not ((3, 11) <= sys.version_info[:2] <= (3, 14))
        or os.name != "posix"
        or fcntl is None
        or not rename_exchange_available()
        or not rename_noreplace_available()
        or missing
        or any(function not in os.supports_dir_fd for function in dir_fd_functions)
    ):
        detail = (
            ", ".join(missing)
            if missing
            else "Python 3.11-3.14 and required POSIX dir_fd/flock operations"
        )
        raise SafeRuntimeError(
            "UNSUPPORTED_PLATFORM",
            f"Secure media processing requires POSIX support for {detail}.",
            exit_code=3,
        )
    # Directory fsync is part of the durability contract. Probe it without
    # changing repository state.
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise SafeRuntimeError(
            "UNSUPPORTED_PLATFORM",
            f"Directory fsync is unavailable: {exc}",
            exit_code=3,
        ) from exc


def _absolute(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    absolute = Path(os.path.abspath(expanded))
    # macOS exposes these two root-owned aliases on every supported system.
    # Normalize only those fixed aliases; arbitrary symlinks remain forbidden.
    if sys.platform == "darwin" and len(absolute.parts) > 1:
        aliases = {"var": Path("/private/var"), "tmp": Path("/private/tmp")}
        replacement = aliases.get(absolute.parts[1])
        if (
            replacement is not None
            and Path(os.path.realpath(f"/{absolute.parts[1]}")) == replacement
        ):
            absolute = replacement.joinpath(*absolute.parts[2:])
    return absolute


def assert_no_symlink_components(path: Path, *, allow_missing_leaf: bool = False) -> Path:
    absolute = _absolute(path)
    parts = absolute.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], start=1):
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return absolute
            raise SafeRuntimeError(
                "UNSAFE_PATH",
                f"Path component does not exist: {current}",
                exit_code=2,
            )
        if stat.S_ISLNK(metadata.st_mode):
            raise SafeRuntimeError(
                "UNSAFE_PATH",
                f"Symbolic links are not accepted: {current}",
                exit_code=2,
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise SafeRuntimeError(
                "UNSAFE_PATH",
                f"Non-directory path component: {current}",
                exit_code=2,
            )
        if (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_mode & stat.S_IWOTH
            and not metadata.st_mode & stat.S_ISVTX
        ):
            raise SafeRuntimeError(
                "UNSAFE_PATH",
                f"Non-sticky path component is writable by other users: {current}",
                exit_code=2,
            )
    return absolute


def _open_file_no_follow(path: Path, flags: int = os.O_RDONLY) -> int:
    absolute = _absolute(path)
    parts = absolute.parts
    directory_fd = _open_directory(Path(parts[0]))
    try:
        for part in parts[1:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(
            parts[-1],
            flags | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)


def secure_input_file(path: Path, *, executable: bool = False) -> Path:
    absolute = assert_no_symlink_components(path)
    try:
        fd = _open_file_no_follow(absolute)
    except OSError as exc:
        raise SafeRuntimeError(
            "UNSAFE_PATH",
            f"Could not securely open local file: {absolute}",
            exit_code=2,
        ) from exc
    try:
        metadata = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise SafeRuntimeError(
            "UNSAFE_PATH",
            f"Expected a regular file: {absolute}",
            exit_code=2,
        )
    if executable and not os.access(absolute, os.X_OK):
        raise SafeRuntimeError(
            "UNSAFE_PATH",
            f"Expected an executable file: {absolute}",
            exit_code=2,
        )
    return absolute


def secure_model_path(path: Path, *, require_directory: bool | None = None) -> Path:
    absolute = assert_no_symlink_components(path)
    metadata = os.lstat(absolute)
    if require_directory is True and not stat.S_ISDIR(metadata.st_mode):
        raise SafeRuntimeError("MODEL_UNAVAILABLE", "The selected engine requires a local model directory.", exit_code=3)
    if require_directory is False and not stat.S_ISREG(metadata.st_mode):
        raise SafeRuntimeError("MODEL_UNAVAILABLE", "The selected engine requires a local model file.", exit_code=3)
    if require_directory is None and not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise SafeRuntimeError("MODEL_UNAVAILABLE", "The model must be a regular file or directory.", exit_code=3)
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise SafeRuntimeError(
            "UNSAFE_MODEL",
            "Local model, executable, and adapter files must not be hard-linked.",
            exit_code=3,
        )
    if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= 0:
        raise SafeRuntimeError("MODEL_UNAVAILABLE", "The local model file is empty.", exit_code=3)
    if stat.S_ISDIR(metadata.st_mode):
        files = secure_tree_files(absolute)
        if not files:
            raise SafeRuntimeError("MODEL_UNAVAILABLE", "The local model directory contains no files.", exit_code=3)
    return absolute


def secure_tree_files(root: Path) -> list[Path]:
    root = assert_no_symlink_components(root)
    found: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        names.sort()
        filenames.sort()
        for name in list(names):
            candidate = current / name
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise SafeRuntimeError(
                    "UNSAFE_MODEL",
                    f"Model tree contains an unsafe directory entry: {candidate.relative_to(root)}",
                    exit_code=3,
                )
        for name in filenames:
            candidate = current / name
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SafeRuntimeError(
                    "UNSAFE_MODEL",
                    f"Model tree contains an unsafe file entry: {candidate.relative_to(root)}",
                    exit_code=3,
                )
            if metadata.st_nlink != 1:
                raise SafeRuntimeError(
                    "UNSAFE_MODEL",
                    f"Model tree contains a hard-linked file: {candidate.relative_to(root)}",
                    exit_code=3,
                )
            found.append(candidate)
    return found


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    fd = _open_file_no_follow(path)
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def content_identity(path: Path) -> dict[str, Any]:
    path = secure_model_path(path)
    metadata = os.lstat(path)
    if stat.S_ISREG(metadata.st_mode):
        return {
            "path": str(path),
            "kind": "file",
            "bytes": metadata.st_size,
            "sha256": sha256_file(path),
        }
    digest = hashlib.sha256()
    total = 0
    count = 0
    for item in secure_tree_files(path):
        relative = item.relative_to(path).as_posix()
        size = os.lstat(item).st_size
        item_hash = sha256_file(item)
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(item_hash))
        total += size
        count += 1
    if total <= 0:
        raise SafeRuntimeError(
            "MODEL_UNAVAILABLE",
            "The local model directory contains no non-empty model content.",
            exit_code=3,
        )
    return {
        "path": str(path),
        "kind": "directory",
        "bytes": total,
        "file_count": count,
        "sha256": digest.hexdigest(),
    }


def _open_directory(path: Path) -> int:
    absolute = _absolute(path)
    parts = absolute.parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = os.open(parts[0], flags)
    try:
        for part in parts[1:]:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def open_directory_fd(path: Path) -> int:
    """Open a directory chain without following any component symlink."""

    return _open_directory(_absolute(path))


def fsync_directory(path: Path) -> None:
    fd = _open_directory(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def secure_mkdirs(path: Path, *, mode: int = 0o700) -> Path:
    absolute = _absolute(path)
    parts = absolute.parts
    fd = _open_directory(Path(parts[0]))
    current = Path(parts[0])
    inside_managed_boundary = False
    try:
        for part in parts[1:]:
            current = current / part
            if part == ".awesome-capture-media":
                inside_managed_boundary = True
            try:
                metadata = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(part, mode=mode, dir_fd=fd)
                os.fsync(fd)
                metadata = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise SafeRuntimeError(
                    "UNSAFE_PATH",
                    f"Managed path component is not a directory: {current}",
                    exit_code=2,
                )
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
            metadata = os.fstat(fd)
            if inside_managed_boundary and (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != mode
            ):
                raise SafeRuntimeError(
                    "UNSAFE_PATH",
                    "Managed directory ownership or permissions are unsafe.",
                    exit_code=2,
                )
        metadata = os.fstat(fd)
        if (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise SafeRuntimeError(
                "UNSAFE_PATH",
                "Private directory ownership or permissions are unsafe.",
                exit_code=2,
            )
        os.fsync(fd)
    finally:
        os.close(fd)
    return absolute


def create_private_directory(parent: Path, *, prefix: str) -> Path:
    if (
        not prefix
        or prefix in {".", ".."}
        or "/" in prefix
        or "\0" in prefix
    ):
        raise SafeRuntimeError(
            "UNSAFE_PATH",
            "Private staging directory prefix is invalid.",
            exit_code=2,
        )
    parent = secure_mkdirs(parent)
    parent_fd = _open_directory(parent)
    try:
        for _ in range(128):
            name = f"{prefix}{secrets.token_hex(16)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                metadata = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                ):
                    raise SafeRuntimeError(
                        "INTEGRITY_ERROR",
                        "Private staging entry is not a current-user-owned directory.",
                        exit_code=7,
                    )
                os.fchmod(child_fd, 0o700)
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            os.fsync(parent_fd)
            return parent / name
    finally:
        os.close(parent_fd)
    raise SafeRuntimeError(
        "FILESYSTEM_ERROR",
        "Could not allocate a private staging directory.",
        exit_code=5,
    )


def quarantine_private_directory(
    source: Path,
    quarantine_root: Path,
    *,
    target_name: str | None = None,
) -> Path:
    source = _absolute(source)
    quarantine_root = secure_mkdirs(quarantine_root)
    destination_name = target_name or source.name
    if (
        not destination_name
        or destination_name in {".", ".."}
        or "/" in destination_name
        or "\0" in destination_name
    ):
        raise SafeRuntimeError(
            "UNSAFE_PATH",
            "Quarantine destination name is invalid.",
            exit_code=2,
        )
    source_parent_fd = _open_directory(source.parent)
    quarantine_fd = _open_directory(quarantine_root)
    source_fd = -1
    published_fd = -1
    try:
        try:
            source_fd = os.open(
                source.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_parent_fd,
            )
        except FileNotFoundError as exc:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Interrupted staging directory disappeared during recovery.",
                exit_code=4,
            ) from exc
        metadata = os.fstat(source_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Interrupted staging entry is not a provably private directory.",
                exit_code=4,
            )
        try:
            os.stat(
                destination_name,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Quarantine already contains an entry with this transaction identity.",
                exit_code=4,
            )
        try:
            published_fd = _move_private_noreplace(
                source.name,
                destination_name,
                source_dir_fd=source_parent_fd,
                destination_dir_fd=quarantine_fd,
                source_fd=source_fd,
                expected_kind="directory",
            )
        except FileExistsError as exc:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Quarantine destination raced with another filesystem object.",
                exit_code=4,
            ) from exc
        os.fsync(published_fd)
        os.fsync(source_parent_fd)
        os.fsync(quarantine_fd)
    except OSError as exc:
        raise SafeRuntimeError(
            "FILESYSTEM_ERROR",
            "Could not quarantine an interrupted staging directory.",
            exit_code=5,
        ) from exc
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if published_fd >= 0:
            os.close(published_fd)
        os.close(source_parent_fd)
        os.close(quarantine_fd)
    return quarantine_root / destination_name


def quarantine_private_file(
    source: Path,
    quarantine_root: Path,
    *,
    target_name: str | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> Path:
    source = _absolute(source)
    quarantine_root = secure_mkdirs(quarantine_root)
    destination_name = target_name or source.name
    if (
        not destination_name
        or destination_name in {".", ".."}
        or "/" in destination_name
        or "\0" in destination_name
    ):
        raise SafeRuntimeError(
            "UNSAFE_PATH",
            "Quarantine destination name is invalid.",
            exit_code=2,
        )
    source_parent_fd = _open_directory(source.parent)
    quarantine_fd = _open_directory(quarantine_root)
    source_fd = -1
    published_fd = -1
    try:
        try:
            source_fd = os.open(
                source.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=source_parent_fd,
            )
        except FileNotFoundError as exc:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Interrupted staging file disappeared during recovery.",
                exit_code=4,
            ) from exc
        metadata = os.fstat(source_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (
                expected_identity is not None
                and (metadata.st_dev, metadata.st_ino) != expected_identity
            )
        ):
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Interrupted staging entry is not a provably private file.",
                exit_code=4,
            )
        try:
            os.stat(
                destination_name,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Quarantine already contains an entry with this transaction identity.",
                exit_code=4,
            )
        try:
            published_fd = _move_private_noreplace(
                source.name,
                destination_name,
                source_dir_fd=source_parent_fd,
                destination_dir_fd=quarantine_fd,
                source_fd=source_fd,
                expected_kind="file",
            )
        except FileExistsError as exc:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Quarantine destination raced with another filesystem object.",
                exit_code=4,
            ) from exc
        os.fsync(published_fd)
        os.fsync(source_parent_fd)
        os.fsync(quarantine_fd)
    except OSError as exc:
        raise SafeRuntimeError(
            "FILESYSTEM_ERROR",
            "Could not quarantine an interrupted staging file.",
            exit_code=5,
        ) from exc
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if published_fd >= 0:
            os.close(published_fd)
        os.close(source_parent_fd)
        os.close(quarantine_fd)
    return quarantine_root / destination_name


def publish_private_directory(source: Path, target: Path) -> None:
    source = _absolute(source)
    target = _absolute(target)
    target_parent = secure_mkdirs(target.parent)
    source_parent_fd = _open_directory(source.parent)
    target_parent_fd = _open_directory(target_parent)
    source_fd = -1
    published_fd = -1
    try:
        try:
            source_fd = os.open(
                source.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_parent_fd,
            )
        except FileNotFoundError as exc:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Private staging directory disappeared before publication.",
                exit_code=4,
            ) from exc
        metadata = os.fstat(source_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise SafeRuntimeError(
                "INTEGRITY_ERROR",
                "Private staging directory identity changed before publication.",
                exit_code=7,
            )
        try:
            os.stat(
                target.name,
                dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Refusing to replace an existing chunk directory.",
                exit_code=4,
            )
        try:
            published_fd = _move_private_noreplace(
                source.name,
                target.name,
                source_dir_fd=source_parent_fd,
                destination_dir_fd=target_parent_fd,
                source_fd=source_fd,
                expected_kind="directory",
            )
        except FileExistsError as exc:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Chunk directory publication raced with an existing object.",
                exit_code=4,
            ) from exc
        os.fsync(published_fd)
        os.fsync(source_parent_fd)
        if target_parent_fd != source_parent_fd:
            os.fsync(target_parent_fd)
    except OSError as exc:
        raise SafeRuntimeError(
            "FILESYSTEM_ERROR",
            "Could not publish the complete chunk directory.",
            exit_code=5,
        ) from exc
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if published_fd >= 0:
            os.close(published_fd)
        os.close(source_parent_fd)
        os.close(target_parent_fd)


def validate_managed_file(
    path: Path,
    *,
    expected_mode: int = 0o600,
) -> os.stat_result:
    path = assert_no_symlink_components(path)
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        raise SafeRuntimeError(
            "INTEGRITY_ERROR",
            "Managed file must be a single-link regular file with mode "
            f"{expected_mode:04o}: {path}",
            exit_code=7,
        )
    if metadata.st_uid != os.geteuid():
        raise SafeRuntimeError(
            "INTEGRITY_ERROR",
            f"Managed file is not owned by the current user: {path}",
            exit_code=7,
        )
    return metadata


def _validate_managed_metadata(metadata: os.stat_result, *, description: str) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise SafeRuntimeError(
            "INTEGRITY_ERROR",
            f"{description} must be a current-user-owned mode-0600 single-link regular file.",
            exit_code=7,
        )


def _open_managed_at(parent_fd: int, name: str) -> tuple[int, os.stat_result] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SafeRuntimeError(
            "INTEGRITY_ERROR",
            "Managed destination is not a safe regular file.",
            exit_code=7,
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        _validate_managed_metadata(metadata, description="Managed destination")
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _create_private_staging(parent_fd: int, prefix: str, mode: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    for _ in range(128):
        name = f"{prefix}{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(name, flags, mode, dir_fd=parent_fd)
        except FileExistsError:
            continue
        os.fchmod(descriptor, mode)
        return descriptor, name
    raise SafeRuntimeError(
        "FILESYSTEM_ERROR",
        "Could not allocate a private staging file.",
        exit_code=5,
    )


def _write_all(descriptor: int, value: bytes) -> None:
    remaining = memoryview(value)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise SafeRuntimeError(
                "FILESYSTEM_ERROR",
                "Short write while staging a managed file.",
                exit_code=5,
            )
        remaining = remaining[written:]


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def _verify_existing_at(
    parent_fd: int,
    name: str,
    *,
    expected_bytes: int,
    expected_sha256: str,
    conflict_message: str,
) -> bool:
    existing = _open_managed_at(parent_fd, name)
    if existing is None:
        return False
    descriptor, metadata = existing
    try:
        if (
            metadata.st_size != expected_bytes
            or _sha256_descriptor(descriptor) != expected_sha256
        ):
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                conflict_message,
                exit_code=4,
            )
        return True
    finally:
        os.close(descriptor)


def _managed_quarantine_root(target: Path) -> Path | None:
    for parent in target.parents:
        if (
            parent.name == "v2"
            and parent.parent.name == ".awesome-capture-media"
        ):
            return parent / "quarantine"
    return None


def atomic_bytes(
    path: Path,
    value: bytes,
    *,
    mode: int = 0o600,
    replace: bool = True,
    expected_existing_sha256: str | None = None,
) -> None:
    target = _absolute(path)
    parent = secure_mkdirs(target.parent)
    parent_fd = _open_directory(parent)
    temporary_name = ""
    descriptor = -1
    published_fd = -1
    existing_fd = -1
    retired_fd = -1
    try:
        existing = _open_managed_at(parent_fd, target.name)
        if existing is not None:
            existing_fd, existing_metadata = existing
            if not replace:
                raise SafeRuntimeError(
                    "RECOVERY_CONFLICT",
                    "Refusing to replace an existing managed file.",
                    exit_code=4,
                )
            if (
                expected_existing_sha256 is None
                or len(expected_existing_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_existing_sha256
                )
            ):
                raise SafeRuntimeError(
                    "RECOVERY_CONFLICT",
                    "Mutable managed write is missing its expected prior digest.",
                    exit_code=4,
                )
            existing_digest = _sha256_descriptor(existing_fd)
            existing_after = os.fstat(existing_fd)
            if (
                existing_digest != expected_existing_sha256
                or (
                    existing_after.st_dev,
                    existing_after.st_ino,
                    existing_after.st_size,
                    existing_after.st_nlink,
                    existing_after.st_mtime_ns,
                    existing_after.st_ctime_ns,
                )
                != (
                    existing_metadata.st_dev,
                    existing_metadata.st_ino,
                    existing_metadata.st_size,
                    existing_metadata.st_nlink,
                    existing_metadata.st_mtime_ns,
                    existing_metadata.st_ctime_ns,
                )
            ):
                raise SafeRuntimeError(
                    "RECOVERY_CONFLICT",
                    "Managed destination changed since it was read.",
                    exit_code=4,
                )
        elif expected_existing_sha256 is not None:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "Expected managed destination disappeared before update.",
                exit_code=4,
            )
        descriptor, temporary_name = _create_private_staging(
            parent_fd,
            f".{target.name}.",
            mode,
        )
        _write_all(descriptor, value)
        os.fsync(descriptor)
        staged_metadata = os.fstat(descriptor)
        if existing_fd >= 0:
            quarantine_root = _managed_quarantine_root(target)
            if quarantine_root is None:
                raise SafeRuntimeError(
                    "RECOVERY_CONFLICT",
                    "Mutable managed files require a private recovery quarantine.",
                    exit_code=4,
                )
            published_fd, retired_fd = _exchange_private(
                temporary_name,
                target.name,
                source_dir_fd=parent_fd,
                destination_dir_fd=parent_fd,
                source_fd=descriptor,
                destination_fd=existing_fd,
                expected_kind="file",
            )
        else:
            try:
                published_fd = _move_private_noreplace(
                    temporary_name,
                    target.name,
                    source_dir_fd=parent_fd,
                    destination_dir_fd=parent_fd,
                    source_fd=descriptor,
                    expected_kind="file",
                )
            except FileExistsError as exc:
                raise SafeRuntimeError(
                    "RECOVERY_CONFLICT",
                    "Refusing to replace an existing managed file.",
                    exit_code=4,
                ) from exc
        published_metadata = os.fstat(published_fd)
        try:
            if (
                published_metadata.st_dev != staged_metadata.st_dev
                or published_metadata.st_ino != staged_metadata.st_ino
                or published_metadata.st_size != len(value)
            ):
                raise SafeRuntimeError(
                    "INTEGRITY_ERROR",
                    "Published managed file identity changed.",
                    exit_code=7,
                )
            os.fchmod(published_fd, mode)
            os.fsync(published_fd)
        finally:
            os.close(published_fd)
            published_fd = -1
        if retired_fd >= 0:
            retired_metadata = os.fstat(retired_fd)
            quarantine_private_file(
                target.parent / temporary_name,
                quarantine_root,
                target_name=(
                    f"retired-{target.name}-{secrets.token_hex(16)}"
                ),
                expected_identity=(
                    retired_metadata.st_dev,
                    retired_metadata.st_ino,
                ),
            )
            os.close(retired_fd)
            retired_fd = -1
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if published_fd >= 0:
            os.close(published_fd)
        if existing_fd >= 0:
            os.close(existing_fd)
        if retired_fd >= 0:
            os.close(retired_fd)
        os.close(parent_fd)


def atomic_json(
    path: Path,
    value: dict[str, Any],
    *,
    replace: bool = True,
    expected_existing_sha256: str | None = None,
) -> None:
    data = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    atomic_bytes(
        path,
        data,
        mode=0o600,
        replace=replace,
        expected_existing_sha256=expected_existing_sha256,
    )


def atomic_text(
    path: Path,
    value: str,
    *,
    mode: int = 0o600,
    replace: bool = True,
    expected_existing_sha256: str | None = None,
) -> None:
    atomic_bytes(
        path,
        value.encode("utf-8"),
        mode=mode,
        replace=replace,
        expected_existing_sha256=expected_existing_sha256,
    )


def copy_private_snapshot(source: Path, target: Path, *, expected_sha256: str, expected_bytes: int) -> None:
    target = _absolute(target)
    parent = secure_mkdirs(target.parent)
    parent_fd = _open_directory(parent)
    temporary_name = ""
    temporary_exists = False
    staging_fd = -1
    published_fd = -1
    digest = hashlib.sha256()
    total = 0
    try:
        if _verify_existing_at(
            parent_fd,
            target.name,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            conflict_message="Existing source snapshot does not match the requested media.",
        ):
            return
        staging_fd, temporary_name = _create_private_staging(
            parent_fd,
            ".source.",
            0o600,
        )
        temporary_exists = True
        source_fd = _open_file_no_follow(source)
        try:
            while True:
                block = os.read(source_fd, 1024 * 1024)
                if not block:
                    break
                _write_all(staging_fd, block)
                digest.update(block)
                total += len(block)
        finally:
            os.close(source_fd)
        if total != expected_bytes or digest.hexdigest() != expected_sha256:
            raise SafeRuntimeError(
                "IDENTITY_CHANGED",
                "Source media changed while its private snapshot was created.",
                exit_code=7,
            )
        os.fsync(staging_fd)
        staged_metadata = os.fstat(staging_fd)
        try:
            published_fd = _move_private_noreplace(
                temporary_name,
                target.name,
                source_dir_fd=parent_fd,
                destination_dir_fd=parent_fd,
                source_fd=staging_fd,
                expected_kind="file",
            )
        except FileExistsError as exc:
            raise SafeRuntimeError(
                "RECOVERY_CONFLICT",
                "A conflicting source snapshot appeared.",
                exit_code=4,
            ) from exc
        else:
            temporary_exists = False
            published_metadata = os.fstat(published_fd)
            try:
                if (
                    published_metadata.st_dev != staged_metadata.st_dev
                    or published_metadata.st_ino != staged_metadata.st_ino
                    or published_metadata.st_size != expected_bytes
                    or _sha256_descriptor(published_fd) != expected_sha256
                ):
                    raise SafeRuntimeError(
                        "INTEGRITY_ERROR",
                        "Published source snapshot identity changed.",
                        exit_code=7,
                    )
                os.fchmod(published_fd, 0o600)
                os.fsync(published_fd)
            finally:
                os.close(published_fd)
                published_fd = -1
        os.close(staging_fd)
        staging_fd = -1
        os.fsync(parent_fd)
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        if published_fd >= 0:
            os.close(published_fd)
        os.close(parent_fd)


@contextlib.contextmanager
def exclusive_lock(path: Path, *, timeout: float) -> Iterator[None]:
    require_posix_security()
    if not math.isfinite(timeout) or timeout < 0:
        raise SafeRuntimeError(
            "INVALID_ARGUMENT",
            "Lock timeout must be a finite non-negative number.",
            exit_code=2,
        )
    parent = secure_mkdirs(path.parent)
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    parent_fd = _open_directory(parent)
    fd: int | None = None
    created = False
    try:
        for _ in range(8):
            try:
                fd = os.open(path.name, flags, dir_fd=parent_fd)
                break
            except FileNotFoundError:
                try:
                    fd = os.open(
                        path.name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    created = True
                    os.fsync(parent_fd)
                    break
                except FileExistsError:
                    continue
        if fd is None:
            raise SafeRuntimeError(
                "UNSAFE_PATH",
                "Could not open the transcription lock without a creation race.",
                exit_code=2,
            )
    finally:
        os.close(parent_fd)
    try:
        assert fd is not None
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or (not created and stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            raise SafeRuntimeError(
                "UNSAFE_PATH",
                "Transcription lock ownership, type, mode, or link count is unsafe.",
                exit_code=2,
            )
        if created:
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise SafeRuntimeError(
                        "RESOURCE_BUSY",
                        f"Timed out waiting for transcription lock after {timeout:g} seconds.",
                        exit_code=4,
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
