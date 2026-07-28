"""Fail-closed POSIX filesystem primitives for standalone skills."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import secrets
import signal
import stat
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by capability mocking
    fcntl = None  # type: ignore[assignment]


_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAME_NOREPLACE_RAW: Any | None = None
_RENAME_NOREPLACE_FLAG = 0
_RENAME_EXCHANGE_RAW: Any | None = None
_RENAME_EXCHANGE_FLAG = 0
if sys.platform == "darwin" and hasattr(_LIBC, "renameatx_np"):
    _RENAME_NOREPLACE_RAW = _LIBC.renameatx_np
    _RENAME_NOREPLACE_RAW.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAME_NOREPLACE_RAW.restype = ctypes.c_int
    _RENAME_NOREPLACE_FLAG = 0x00000004  # RENAME_EXCL
    _RENAME_EXCHANGE_RAW = _RENAME_NOREPLACE_RAW
    _RENAME_EXCHANGE_FLAG = 0x00000002  # RENAME_SWAP
elif sys.platform.startswith("linux") and hasattr(_LIBC, "renameat2"):
    _RENAME_NOREPLACE_RAW = _LIBC.renameat2
    _RENAME_NOREPLACE_RAW.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAME_NOREPLACE_RAW.restype = ctypes.c_int
    _RENAME_NOREPLACE_FLAG = 0x00000001  # RENAME_NOREPLACE
    _RENAME_EXCHANGE_RAW = _RENAME_NOREPLACE_RAW
    _RENAME_EXCHANGE_FLAG = 0x00000002  # RENAME_EXCHANGE


class PosixRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def rename_noreplace_available() -> bool:
    return _RENAME_NOREPLACE_RAW is not None


def rename_exchange_available() -> bool:
    return _RENAME_EXCHANGE_RAW is not None


def rename_noreplace(
    source_name: str,
    destination_name: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
) -> None:
    """Atomically rename one child without replacing any destination object."""

    for value in (source_name, destination_name):
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\0" in value
        ):
            raise ValueError("rename_noreplace accepts one safe child name")
    if _RENAME_NOREPLACE_RAW is None:
        raise PosixRuntimeError(
            "UNSUPPORTED_PLATFORM",
            "Atomic rename-no-replace is unavailable.",
        )
    ctypes.set_errno(0)
    result = _RENAME_NOREPLACE_RAW(
        source_dir_fd,
        os.fsencode(source_name),
        destination_dir_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE_FLAG,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno() or errno.EIO
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination_name,
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        destination_name,
    )


def rename_exchange(
    source_name: str,
    destination_name: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
) -> None:
    """Atomically exchange two existing child names."""

    for value in (source_name, destination_name):
        if not value or value in {".", ".."} or "/" in value or "\0" in value:
            raise ValueError("rename_exchange accepts one safe child name")
    if _RENAME_EXCHANGE_RAW is None:
        raise PosixRuntimeError(
            "UNSUPPORTED_PLATFORM",
            "Atomic rename-exchange is unavailable.",
        )
    ctypes.set_errno(0)
    result = _RENAME_EXCHANGE_RAW(
        source_dir_fd,
        os.fsencode(source_name),
        destination_dir_fd,
        os.fsencode(destination_name),
        _RENAME_EXCHANGE_FLAG,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno() or errno.EIO
    raise OSError(
        error_number,
        os.strerror(error_number),
        destination_name,
    )


def _rollback_raced_move(
    source_name: str,
    destination_name: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
) -> str | None:
    """Clear a raced formal destination without overwriting or deleting data."""

    try:
        rename_noreplace(
            destination_name,
            source_name,
            source_dir_fd=destination_dir_fd,
            destination_dir_fd=source_dir_fd,
        )
        os.fsync(source_dir_fd)
        if destination_dir_fd != source_dir_fd:
            os.fsync(destination_dir_fd)
        return source_name
    except FileNotFoundError:
        # A concurrent actor already moved the raced object. The formal
        # destination is no longer populated by the object we observed.
        return None
    except FileExistsError:
        pass

    # The original source name was repopulated concurrently. Preserve both
    # objects by spilling the raced destination to a unique recovery name in
    # the private source directory. Never replace or unlink either object.
    for _ in range(32):
        recovery_name = f".awesome-capture-raced-{secrets.token_hex(16)}"
        try:
            rename_noreplace(
                destination_name,
                recovery_name,
                source_dir_fd=destination_dir_fd,
                destination_dir_fd=source_dir_fd,
            )
        except FileExistsError:
            continue
        except FileNotFoundError:
            return None
        os.fsync(source_dir_fd)
        if destination_dir_fd != source_dir_fd:
            os.fsync(destination_dir_fd)
        return recovery_name
    raise PosixRuntimeError(
        "RECOVERY_CONFLICT",
        "A raced filesystem object could not be preserved outside the formal destination.",
    )


def move_verified_noreplace(
    source_name: str,
    destination_name: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
    source_fd: int,
    expected_kind: str,
    rename_impl: Callable[..., None] | None = None,
) -> int:
    """Move one held file/directory and verify the moved pathname by inode.

    The caller owns the returned descriptor. If the source pathname is swapped
    after validation, the raced object is restored (or preserved under a
    private recovery name) and the formal destination is left uncommitted.
    """

    if expected_kind not in {"file", "directory"}:
        raise ValueError("expected_kind must be 'file' or 'directory'")
    operation = rename_impl or rename_noreplace
    expected = os.fstat(source_fd)
    if (
        expected_kind == "file"
        and not stat.S_ISREG(expected.st_mode)
    ) or (
        expected_kind == "directory"
        and not stat.S_ISDIR(expected.st_mode)
    ):
        raise PosixRuntimeError(
            "RECOVERY_CONFLICT",
            "Atomic move source descriptor has the wrong object type.",
        )

    operation(
        source_name,
        destination_name,
        source_dir_fd=source_dir_fd,
        destination_dir_fd=destination_dir_fd,
    )

    moved_fd = -1
    moved: os.stat_result | None = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        if expected_kind == "directory":
            flags |= os.O_DIRECTORY
        moved_fd = os.open(
            destination_name,
            flags,
            dir_fd=destination_dir_fd,
        )
        moved = os.fstat(moved_fd)
        correct_type = (
            stat.S_ISREG(moved.st_mode)
            if expected_kind == "file"
            else stat.S_ISDIR(moved.st_mode)
        )
        if (
            correct_type
            and (moved.st_dev, moved.st_ino) == (expected.st_dev, expected.st_ino)
        ):
            return moved_fd
    except OSError:
        pass

    if moved_fd >= 0:
        os.close(moved_fd)
    try:
        _rollback_raced_move(
            source_name,
            destination_name,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )
    except OSError as exc:
        raise PosixRuntimeError(
            "RECOVERY_CONFLICT",
            "A raced filesystem object could not be restored safely.",
        ) from exc
    raise PosixRuntimeError(
        "RECOVERY_CONFLICT",
        "Atomic move source pathname changed; the raced object was preserved.",
    )


def exchange_verified(
    source_name: str,
    destination_name: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
    source_fd: int,
    destination_fd: int,
    expected_kind: str,
    exchange_impl: Callable[..., None] | None = None,
) -> tuple[int, int]:
    """CAS-style atomic exchange that preserves both pre-existing objects."""

    if expected_kind not in {"file", "directory"}:
        raise ValueError("expected_kind must be 'file' or 'directory'")
    operation = exchange_impl or rename_exchange
    expected_source = os.fstat(source_fd)
    expected_destination = os.fstat(destination_fd)
    type_check = stat.S_ISREG if expected_kind == "file" else stat.S_ISDIR
    if not type_check(expected_source.st_mode) or not type_check(
        expected_destination.st_mode
    ):
        raise PosixRuntimeError(
            "RECOVERY_CONFLICT",
            "Atomic exchange descriptors have the wrong object type.",
        )

    operation(
        source_name,
        destination_name,
        source_dir_fd=source_dir_fd,
        destination_dir_fd=destination_dir_fd,
    )

    published_fd = -1
    retired_fd = -1
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        if expected_kind == "directory":
            flags |= os.O_DIRECTORY
        published_fd = os.open(
            destination_name,
            flags,
            dir_fd=destination_dir_fd,
        )
        retired_fd = os.open(
            source_name,
            flags,
            dir_fd=source_dir_fd,
        )
        published = os.fstat(published_fd)
        retired = os.fstat(retired_fd)
        if (
            type_check(published.st_mode)
            and type_check(retired.st_mode)
            and (published.st_dev, published.st_ino)
            == (expected_source.st_dev, expected_source.st_ino)
            and (retired.st_dev, retired.st_ino)
            == (expected_destination.st_dev, expected_destination.st_ino)
        ):
            return published_fd, retired_fd
    except OSError:
        pass

    if published_fd >= 0:
        os.close(published_fd)
    if retired_fd >= 0:
        os.close(retired_fd)
    try:
        rename_exchange(
            source_name,
            destination_name,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )
        os.fsync(source_dir_fd)
        if destination_dir_fd != source_dir_fd:
            os.fsync(destination_dir_fd)
    except OSError as exc:
        raise PosixRuntimeError(
            "RECOVERY_CONFLICT",
            "A raced atomic exchange could not be rolled back safely.",
        ) from exc
    raise PosixRuntimeError(
        "RECOVERY_CONFLICT",
        "Atomic exchange path identity changed; both objects were preserved.",
    )


def test_failpoint(name: str) -> None:
    """Terminate at an explicitly enabled deterministic test boundary."""

    if (
        os.environ.get("AWESOME_CAPTURE_ENABLE_TEST_FAILPOINTS") == "1"
        and os.environ.get("AWESOME_CAPTURE_TEST_FAILPOINT") == name
    ):
        os.kill(os.getpid(), signal.SIGKILL)


def require_posix() -> None:
    """Fail unless all primitives needed for the security boundary are present."""

    required_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    missing = [name for name in required_flags if not hasattr(os, name)]
    required_dir_fd = (
        os.open,
        os.mkdir,
        os.stat,
        os.unlink,
        os.link,
        os.rename,
        os.rmdir,
    )
    if not ((3, 11) <= sys.version_info[:2] <= (3, 14)):
        missing.append("Python 3.11-3.14")
    if os.name != "posix" or fcntl is None:
        missing.append("POSIX/fcntl")
    if not rename_noreplace_available():
        missing.append("atomic rename-no-replace")
    if not rename_exchange_available():
        missing.append("atomic rename-exchange")
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        missing.append("dir_fd")
    if missing:
        raise PosixRuntimeError(
            "UNSUPPORTED_PLATFORM",
            "Required POSIX filesystem capabilities are unavailable: " + ", ".join(missing),
        )
    try:
        descriptor = os.open(
            "/",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PosixRuntimeError(
            "UNSUPPORTED_PLATFORM",
            "Directory fsync is unavailable.",
        ) from exc


def canonical_json_bytes(value: Any) -> bytes:
    _reject_nonfinite(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise PosixRuntimeError("INVALID_JSON", "Non-finite JSON numbers are forbidden.")
    if isinstance(value, str) and any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PosixRuntimeError(
            "INVALID_JSON",
            "Strings must contain only valid Unicode scalar values.",
        )
    if isinstance(value, dict):
        for key, child in value.items():
            if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                raise PosixRuntimeError(
                    "INVALID_JSON",
                    "JSON object keys must contain valid Unicode scalar values.",
                )
            _reject_nonfinite(child)
    elif isinstance(value, list):
        for child in value:
            _reject_nonfinite(child)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise PosixRuntimeError("DUPLICATE_JSON_KEY", f"Duplicate JSON key: {key}")
        value[key] = child
    return value


def _reject_constant(value: str) -> None:
    raise PosixRuntimeError("INVALID_JSON", f"Non-finite JSON number is forbidden: {value}")


def _path_parts(path: Path) -> tuple[bool, tuple[str, ...]]:
    raw = os.fspath(path)
    if "\0" in raw:
        raise PosixRuntimeError("UNSAFE_PATH", "NUL is forbidden in paths.")
    pure = PurePosixPath(raw)
    if pure.is_absolute() and sys.platform == "darwin" and len(pure.parts) > 1:
        aliases = {"tmp": Path("/private/tmp"), "var": Path("/private/var")}
        replacement = aliases.get(pure.parts[1])
        if (
            replacement is not None
            and Path(os.path.realpath(f"/{pure.parts[1]}")) == replacement
        ):
            pure = PurePosixPath(replacement.joinpath(*pure.parts[2:]).as_posix())
    if any(part in {".."} for part in pure.parts):
        raise PosixRuntimeError("UNSAFE_PATH", "Parent traversal is forbidden.")
    anchor = pure.is_absolute()
    parts = tuple(part for part in pure.parts if part not in {"", "/", "."})
    return anchor, parts


def _open_directory_chain(path: Path) -> int:
    """Open a directory without following any path-component symlink."""

    require_posix()
    absolute, parts = _path_parts(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open("/" if absolute else ".", flags)
    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except Exception:
        os.close(current)
        raise


def validate_relative_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw or "\0" in raw or "\\" in raw:
        raise PosixRuntimeError("UNSAFE_PATH", "Expected a non-empty relative POSIX path.")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PosixRuntimeError("UNSAFE_PATH", "Path must be relative and contain no dot traversal.")
    return Path(*pure.parts)


def reject_final_symlink(path: str | os.PathLike[str]) -> None:
    target = Path(path)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PosixRuntimeError("UNSAFE_PATH", f"Cannot inspect destination: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PosixRuntimeError("UNSAFE_PATH", "Final path is a symbolic link.")


def ensure_dir(
    path: str | os.PathLike[str],
    mode: int = 0o700,
    *,
    private: bool = False,
) -> Path:
    """Create/open a directory one component at a time without following links."""

    require_posix()
    target = Path(path)
    absolute, parts = _path_parts(target)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open("/" if absolute else ".", flags)
    created_final = False
    try:
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            try:
                next_fd = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(part, mode if final else 0o700, dir_fd=current)
                next_fd = os.open(part, flags, dir_fd=current)
                if final:
                    created_final = True
            metadata = os.fstat(next_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_fd)
                raise PosixRuntimeError("UNSAFE_DIRECTORY", "Managed path component is not a directory.")
            os.close(current)
            current = next_fd
        metadata = os.fstat(current)
        if private:
            if metadata.st_uid != os.geteuid():
                raise PosixRuntimeError("UNSAFE_DIRECTORY", "Private directory is not owned by the current user.")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                if created_final:
                    os.fchmod(current, mode)
                else:
                    raise PosixRuntimeError("UNSAFE_DIRECTORY", "Private directory permissions are too broad.")
        os.fsync(current)
    except PosixRuntimeError:
        raise
    except OSError as exc:
        raise PosixRuntimeError("UNSAFE_DIRECTORY", f"Cannot create/open directory: {exc}") from exc
    finally:
        os.close(current)
    return target


def fsync_dir(path: str | os.PathLike[str]) -> None:
    descriptor = _open_directory_chain(Path(path))
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise PosixRuntimeError("FILESYSTEM_ERROR", f"Cannot fsync directory: {exc}") from exc
    finally:
        os.close(descriptor)


def read_regular_file(path: str | os.PathLike[str], max_bytes: int) -> bytes:
    """Read an owned, single-link regular file without following its final path."""

    require_posix()
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    target = Path(path)
    parent_fd = -1
    descriptor = -1
    try:
        parent_fd = _open_directory_chain(target.parent)
        descriptor = os.open(
            target.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise PosixRuntimeError(
                "UNSAFE_FILE",
                "File must be regular, current-user-owned, and have exactly one link.",
            )
        if metadata.st_size > max_bytes:
            raise PosixRuntimeError("FILE_TOO_LARGE", f"File exceeds {max_bytes} bytes.")
        blocks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            blocks.append(block)
            remaining -= len(block)
        data = b"".join(blocks)
        if len(data) > max_bytes:
            raise PosixRuntimeError("FILE_TOO_LARGE", f"File exceeds {max_bytes} bytes.")
        after = os.fstat(descriptor)
        if (
            after.st_dev != metadata.st_dev
            or after.st_ino != metadata.st_ino
            or after.st_size != metadata.st_size
        ):
            raise PosixRuntimeError("FILE_CHANGED", "File changed while it was being read.")
        return data
    except PosixRuntimeError:
        raise
    except OSError as exc:
        raise PosixRuntimeError("UNSAFE_FILE", f"Cannot safely read file: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def strict_read_json(path: str | os.PathLike[str], max_bytes: int) -> Any:
    raw = read_regular_file(path, max_bytes)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_nonfinite(value)
        return value
    except PosixRuntimeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PosixRuntimeError("INVALID_JSON", f"Malformed UTF-8 JSON: {exc}") from exc


def file_evidence(
    path: str | os.PathLike[str],
    *,
    max_bytes: int | None = None,
) -> dict[str, int | str]:
    """Return stable evidence for an owned, single-link regular file."""

    require_posix()
    if max_bytes is not None and (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 0
    ):
        raise ValueError("max_bytes must be a non-negative integer")
    target = Path(path)
    parent_fd = -1
    descriptor = -1
    try:
        parent_fd = _open_directory_chain(target.parent)
        descriptor = os.open(
            target.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
        ):
            raise PosixRuntimeError("UNSAFE_FILE", "Hash input must be an owned single-link regular file.")
        if max_bytes is not None and before.st_size > max_bytes:
            raise PosixRuntimeError(
                "FILE_TOO_LARGE",
                f"File exceeds {max_bytes} bytes.",
            )
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_nlink != before.st_nlink
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise PosixRuntimeError("FILE_CHANGED", "File changed while it was being hashed.")
        return {
            "bytes": before.st_size,
            "sha256": digest.hexdigest(),
            "mode": stat.S_IMODE(before.st_mode),
        }
    except PosixRuntimeError:
        raise
    except OSError as exc:
        raise PosixRuntimeError("UNSAFE_FILE", f"Cannot safely hash file: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def file_sha256(path: str | os.PathLike[str]) -> str:
    return str(file_evidence(path)["sha256"])


class FileLock:
    """Persistent advisory lock with a bounded wait and secure file opening."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        exclusive: bool = True,
        timeout: float = 30,
        busy_code: str = "RESOURCE_BUSY",
    ):
        self.path = Path(path)
        self.exclusive = exclusive
        self.timeout = timeout
        self.busy_code = busy_code
        self._descriptor: int | None = None

    def __enter__(self) -> "FileLock":
        require_posix()
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(float(self.timeout))
            or self.timeout < 0
        ):
            raise ValueError("timeout must be a finite non-negative number")
        ensure_dir(self.path.parent, 0o700, private=True)
        parent_fd = _open_directory_chain(self.path.parent)
        descriptor: int | None = None
        created = False
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            for _ in range(8):
                try:
                    descriptor = os.open(self.path.name, flags, dir_fd=parent_fd)
                    break
                except FileNotFoundError:
                    try:
                        descriptor = os.open(
                            self.path.name,
                            flags | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=parent_fd,
                        )
                        created = True
                        os.fchmod(descriptor, 0o600)
                        os.fsync(parent_fd)
                        break
                    except FileExistsError:
                        continue
            if descriptor is None:
                raise PosixRuntimeError(
                    "UNSAFE_LOCK",
                    "Cannot open lock file without a creation race.",
                )
        except PosixRuntimeError:
            raise
        except OSError as exc:
            raise PosixRuntimeError("UNSAFE_LOCK", f"Cannot open lock file: {exc}") from exc
        finally:
            os.close(parent_fd)
        assert descriptor is not None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(descriptor)
            raise PosixRuntimeError("UNSAFE_LOCK", "Lock file ownership, type, links, or mode is unsafe.")
        operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                self._descriptor = descriptor
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise PosixRuntimeError(self.busy_code, "Timed out waiting for lock.")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            except OSError as exc:
                os.close(descriptor)
                raise PosixRuntimeError("UNSAFE_LOCK", f"Cannot acquire lock: {exc}") from exc

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        if self._descriptor is not None:
            try:
                fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self._descriptor)
                self._descriptor = None


def atomic_write_noclobber(
    path: str | os.PathLike[str],
    data: bytes | str,
    mode: int = 0o600,
) -> None:
    """Durably publish bytes at a new path; never replace an existing entry."""

    require_posix()
    target = Path(path)
    if not target.name or target.name in {".", ".."}:
        raise PosixRuntimeError("UNSAFE_PATH", "Destination filename is invalid.")
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    parent_fd = _open_directory_chain(target.parent)
    temporary = f".{target.name}.staging-{secrets.token_hex(16)}"
    temporary_exists = False
    descriptor = -1
    published_fd = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=parent_fd,
        )
        temporary_exists = True
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise PosixRuntimeError("FILESYSTEM_ERROR", "Short write while staging file.")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        try:
            published_fd = move_verified_noreplace(
                temporary,
                target.name,
                source_dir_fd=parent_fd,
                destination_dir_fd=parent_fd,
                source_fd=descriptor,
                expected_kind="file",
                rename_impl=rename_noreplace,
            )
        except FileExistsError as exc:
            raise PosixRuntimeError("PATH_COLLISION", "Destination already exists.") from exc
        temporary_exists = False
        published = os.fstat(published_fd)
        staged = os.fstat(descriptor)
        if (
            not stat.S_ISREG(published.st_mode)
            or (published.st_dev, published.st_ino)
            != (staged.st_dev, staged.st_ino)
            or published.st_nlink != 1
            or stat.S_IMODE(published.st_mode) != mode
        ):
            raise PosixRuntimeError(
                "INTEGRITY_FAILED",
                "Published file identity changed.",
            )
        os.fsync(published_fd)
        os.fsync(parent_fd)
    except PosixRuntimeError:
        raise
    except OSError as exc:
        raise PosixRuntimeError("FILESYSTEM_ERROR", f"Cannot atomically publish file: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if published_fd >= 0:
            os.close(published_fd)
        if temporary_exists:
            quarantine_fd = -1
            try:
                quarantine_name = ".awesome-capture-quarantine"
                try:
                    os.mkdir(quarantine_name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                quarantine_fd = os.open(
                    quarantine_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                try:
                    quarantine_metadata = os.fstat(quarantine_fd)
                    if (
                        quarantine_metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(quarantine_metadata.st_mode) != 0o700
                    ):
                        raise PosixRuntimeError(
                            "RECOVERY_CONFLICT",
                            "Atomic-write quarantine is unsafe.",
                        )
                    source_fd = os.open(
                        temporary,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=parent_fd,
                    )
                    try:
                        quarantined_fd = move_verified_noreplace(
                            temporary,
                            f"aborted-{target.name}-{secrets.token_hex(16)}",
                            source_dir_fd=parent_fd,
                            destination_dir_fd=quarantine_fd,
                            source_fd=source_fd,
                            expected_kind="file",
                            rename_impl=rename_noreplace,
                        )
                        os.fsync(quarantined_fd)
                        os.close(quarantined_fd)
                    finally:
                        os.close(source_fd)
                    os.fsync(parent_fd)
                    os.fsync(quarantine_fd)
                finally:
                    if quarantine_fd >= 0:
                        os.close(quarantine_fd)
            except (FileNotFoundError, OSError, PosixRuntimeError):
                # Never delete an unverified pathname while unwinding. A
                # private residue is safer than deleting a raced placeholder.
                pass
        os.close(parent_fd)
