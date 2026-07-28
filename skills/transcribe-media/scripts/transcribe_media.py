#!/usr/bin/env python3
"""Create a resumable, timestamped transcript artifact from local media."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SKILL_ROOT = SCRIPT_DIR.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from _contracts.media_runtime import (  # type: ignore  # noqa: E402
    SafeRuntimeError,
    assert_no_symlink_components,
    atomic_json as safe_atomic_json,
    atomic_text as safe_atomic_text,
    content_identity,
    copy_private_snapshot,
    create_private_directory,
    exclusive_lock,
    fsync_directory,
    open_directory_fd,
    publish_private_directory,
    quarantine_private_directory,
    quarantine_private_file,
    require_posix_security,
    secure_input_file,
    secure_mkdirs,
    secure_model_path,
    secure_tree_files,
    sha256_file as safe_file_sha256,
    validate_managed_file,
)

from _contracts.contract_runtime import (  # type: ignore  # noqa: E402
    ContractError,
    contract_digest,
    read_json_strict,
    read_json_strict_with_sha256,
    validate_contract,
    validate_file_context,
)
from _contracts.posix_runtime import test_failpoint  # type: ignore  # noqa: E402

SCHEMA_VERSION = "awesome-capture.artifact/v2"
STATE_SCHEMA_VERSION = "awesome-capture.transcription-state/v1"
CHUNK_SET_SCHEMA_VERSION = "awesome-capture.chunk-set/v1"
ALGORITHM_VERSION = "awesome-capture.transcription-algorithm/v1"
EXTERNAL_PROTOCOL = "awesome-capture.external-asr/v1"
MANAGED_ROOT_NAME = ".awesome-capture-media"
MANAGED_LAYOUT_VERSION = "v2"
JSON_LIMIT = 4 * 1024 * 1024
STATE_JSON_LIMIT = 64 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK_STAGING_RE = re.compile(
    r"^transcribe-([0-9a-f]{64})-chunks\.([0-9a-f]{32})$"
)
LEGACY_WORKSPACE_CHUNK_STAGING_RE = re.compile(
    r"^chunks\.staging\.([a-z0-9_]{8})$"
)
ATOMIC_WORKSPACE_STAGING_RE = re.compile(
    r"^\.(source\.snapshot|sidecar\.snapshot\.(?:srt|vtt)|state\.json|"
    r"transcript\.pending\.json|transcript\.json|transcript\.md|"
    r"transcript\.txt|transcript\.srt|transcript\.vtt)\."
    r"([0-9a-f]{32})\.tmp$"
)
SNAPSHOT_COPY_STAGING_RE = re.compile(r"^\.source\.([0-9a-f]{32})\.tmp$")


class TranscriptionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: str = "", exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details.strip()[-4000:]
        return {"status": "error", "error": error}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise TranscriptionError("INVALID_ARGUMENT", message, exit_code=2)


def json_print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False),
        file=stream,
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_bytes(raw: bytes, *, description: str) -> Any:
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TranscriptionError(
            "INVALID_JSON",
            f"{description} is not strict JSON.",
            details=str(exc),
            exit_code=2,
        ) from exc


def strict_json_file(path: Path, *, maximum_bytes: int, description: str) -> Any:
    try:
        return read_json_strict(
            path,
            validate=False,
            maximum_bytes=maximum_bytes,
        )
    except ContractError as exc:
        contract_code = getattr(exc, "code", "INVALID_JSON")
        code = {
            "JSON_TOO_LARGE": "INPUT_TOO_LARGE",
            "JSON_NOT_READABLE": "INPUT_NOT_FOUND",
            "UNSAFE_JSON_FILE": "UNSAFE_PATH",
        }.get(contract_code, contract_code)
        raise TranscriptionError(
            code,
            f"{description} is not a safe strict JSON file.",
            details=getattr(exc, "message", str(exc)),
            exit_code=2,
        ) from exc


def strict_json_file_with_sha256(
    path: Path,
    *,
    maximum_bytes: int,
    description: str,
) -> tuple[Any, str]:
    try:
        return read_json_strict_with_sha256(
            path,
            validate=False,
            maximum_bytes=maximum_bytes,
        )
    except ContractError as exc:
        raise TranscriptionError(
            "INVALID_JSON",
            f"{description} is not safe strict JSON.",
            details=f"{exc.code}:{exc.path}",
            exit_code=2,
        ) from exc


def current_source_artifact_sha256(path: Path) -> str:
    try:
        unused_value, digest = read_json_strict_with_sha256(
            path,
            validate=False,
            maximum_bytes=JSON_LIMIT,
        )
    except ContractError as exc:
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "Source video artifact is no longer a safe unchanged JSON file.",
            exit_code=7,
        ) from exc
    del unused_value
    return digest


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_tool(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise TranscriptionError("DEPENDENCY_MISSING", f"Required executable is missing: {name}", exit_code=3)
    return value


def reject_url(value: str) -> None:
    parts = urlsplit(value)
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        raise TranscriptionError(
            "USE_DOWNLOAD_VIDEO",
            "URL input must be downloaded with $download-video before transcription.",
            exit_code=2,
        )


def media_path(raw: str) -> Path:
    reject_url(raw)
    try:
        return secure_input_file(Path(raw))
    except SafeRuntimeError as exc:
        raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc


def file_sha256(path: Path) -> str:
    try:
        return safe_file_sha256(path)
    except (OSError, SafeRuntimeError) as exc:
        raise TranscriptionError(
            "INTEGRITY_ERROR",
            f"Could not hash a required local file: {path}",
            details=str(exc),
            exit_code=7,
        ) from exc


def transcription_algorithm_identity() -> dict[str, str]:
    """Hash the standalone deterministic transcription implementation."""

    digest = hashlib.sha256()
    implementation_files = (
        ("_contracts/media_runtime.py", SCRIPT_DIR / "_contracts" / "media_runtime.py"),
        ("transcribe_media.py", SCRIPT_DIR / "transcribe_media.py"),
    )
    for name, candidate in implementation_files:
        try:
            safe_path = secure_input_file(candidate)
            metadata = os.lstat(safe_path)
        except (OSError, SafeRuntimeError) as exc:
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                "The local transcription implementation cannot be identified safely.",
                exit_code=7,
            ) from exc
        if metadata.st_nlink != 1:
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                "The local transcription implementation must not be hard-linked.",
                exit_code=7,
            )
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(metadata.st_size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(file_sha256(safe_path)))
    return {
        "version": ALGORITHM_VERSION,
        "sha256": digest.hexdigest(),
    }


def _execution_stat(path: Path, *, relative_path: str) -> dict[str, Any]:
    """Capture metadata that exposes an in-place write even after restoration."""

    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "A local ASR identity component disappeared during transcription.",
            exit_code=7,
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not (
        stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
    ):
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "A local ASR identity component became an unsafe filesystem object.",
            exit_code=7,
        )
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "A local ASR identity component became hard-linked.",
            exit_code=7,
        )
    return {
        "relative_path": relative_path,
        "kind": "directory" if stat.S_ISDIR(metadata.st_mode) else "file",
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "links": metadata.st_nlink,
        "bytes": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }


def _component_execution_evidence(recorded: dict[str, Any]) -> dict[str, Any]:
    try:
        root = secure_model_path(Path(recorded["path"]))
        current = content_identity(root)
    except (KeyError, TypeError, SafeRuntimeError) as exc:
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "A local ASR identity component cannot be revalidated safely.",
            exit_code=7,
        ) from exc
    comparable = {key: value for key, value in recorded.items() if key != "version"}
    if current != comparable:
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "A local ASR identity component changed during transcription.",
            exit_code=7,
        )
    entries = [_execution_stat(root, relative_path=".")]
    if current["kind"] == "directory":
        try:
            files = secure_tree_files(root)
            directories: list[Path] = []
            for directory, names, unused_filenames in os.walk(
                root,
                followlinks=False,
            ):
                del unused_filenames
                current_directory = Path(directory)
                for name in sorted(names):
                    directories.append(current_directory / name)
        except SafeRuntimeError as exc:
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                "The local model tree changed during transcription.",
                exit_code=7,
            ) from exc
        for directory in sorted(
            directories,
            key=lambda item: item.relative_to(root).as_posix(),
        ):
            entries.append(
                _execution_stat(
                    directory,
                    relative_path=directory.relative_to(root).as_posix(),
                )
            )
        for item in files:
            entries.append(
                _execution_stat(
                    item,
                    relative_path=item.relative_to(root).as_posix(),
                )
            )
    return {
        "identity_sha256": recorded["sha256"],
        "entries": entries,
    }


def engine_execution_guard(engine: str, identity: dict[str, Any]) -> str:
    """Return non-job filesystem evidence for one concrete ASR execution."""

    components: list[dict[str, Any]] = []
    if engine != "sidecar-subtitle":
        for field in ("model", "executable", "adapter"):
            recorded = identity.get(field)
            if recorded is None:
                continue
            if not isinstance(recorded, dict):
                raise TranscriptionError(
                    "IDENTITY_CHANGED",
                    f"Invalid recorded {field} identity.",
                    exit_code=7,
                )
            components.append(
                {
                    "field": field,
                    **_component_execution_evidence(recorded),
                }
            )
    return canonical_json_sha256(
        {
            "engine": engine,
            "engine_identity_sha256": identity.get("identity_sha256"),
            "components": components,
        }
    )


def execution_guard_for_run(
    engine: str,
    identity: dict[str, Any],
    *,
    source_snapshot: Path,
    sidecar_snapshot: Path | None,
) -> str:
    private_inputs = [
        {
            "name": "source",
            **_execution_stat(source_snapshot, relative_path="source.snapshot"),
        }
    ]
    if sidecar_snapshot is not None:
        private_inputs.append(
            {
                "name": "sidecar",
                **_execution_stat(
                    sidecar_snapshot,
                    relative_path=sidecar_snapshot.name,
                ),
            }
        )
    return canonical_json_sha256(
        {
            "engine_guard_sha256": engine_execution_guard(engine, identity),
            "private_inputs": private_inputs,
        }
    )


def run_process(
    command: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    cwd_fd = -1
    try:
        preexec_fn = None
        inherited_fds = set(pass_fds)
        if cwd is not None:
            try:
                cwd_fd = open_directory_fd(cwd)
            except SafeRuntimeError as exc:
                raise TranscriptionError(
                    exc.code,
                    exc.message,
                    exit_code=exc.exit_code,
                ) from exc
            inherited_fds.add(cwd_fd)

            def pin_working_directory() -> None:
                os.fchdir(cwd_fd)

            preexec_fn = pin_working_directory
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
            pass_fds=tuple(sorted(inherited_fds)),
            preexec_fn=preexec_fn,
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscriptionError(
            "PROCESS_TIMEOUT",
            f"Process timed out after {timeout} seconds.",
            exit_code=5,
        ) from exc
    except OSError as exc:
        raise TranscriptionError(
            "PROCESS_START_FAILED",
            "Could not start the required executable.",
            exit_code=5,
        ) from exc
    finally:
        if cwd_fd >= 0:
            os.close(cwd_fd)


@contextlib.contextmanager
def opened_process_input(path: Path) -> Iterator[int]:
    parent_fd = -1
    descriptor = -1
    try:
        parent_fd = open_directory_fd(path.parent)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise TranscriptionError(
                "UNSAFE_PATH",
                "External-tool input is not an owned single-link regular file.",
                exit_code=2,
            )
        yield descriptor
    except SafeRuntimeError as exc:
        raise TranscriptionError(
            exc.code,
            exc.message,
            exit_code=exc.exit_code,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def _executable_identity_from_fd(descriptor: int, path: Path) -> dict[str, Any]:
    before = os.fstat(descriptor)
    unsafe = (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or before.st_size <= 0
        or not before.st_mode & stat.S_IXUSR
        or bool(before.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    )
    if unsafe:
        raise TranscriptionError(
            "UNSAFE_PATH",
            "ASR executable must be a non-empty, current-user-owned, "
            "single-link regular executable that is not writable by other users.",
            exit_code=2,
        )
    digest = hashlib.sha256()
    offset = 0
    try:
        while True:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
    except OSError as exc:
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "The ASR executable could not be read through its held descriptor.",
            exit_code=7,
        ) from exc
    after = os.fstat(descriptor)
    evidence_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        offset != before.st_size
        or any(getattr(before, field) != getattr(after, field) for field in evidence_fields)
    ):
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "The ASR executable changed while its content identity was being verified.",
            exit_code=7,
        )
    return {
        "path": str(path),
        "kind": "file",
        "bytes": before.st_size,
        "sha256": digest.hexdigest(),
    }


@contextlib.contextmanager
def opened_verified_executable(
    path: Path,
    *,
    expected_identity: dict[str, Any] | None = None,
) -> Iterator[tuple[int, dict[str, Any], str]]:
    """Hold, content-verify, and execute one immutable pathname identity.

    Linux executes the held descriptor through ``/proc/self/fd``. Darwin does
    not permit exec from ``/dev/fd``, so it executes a byte-for-byte private
    snapshot made from the already-verified descriptor. Both mechanisms sever
    executable selection from the caller-controlled pathname before exec.
    """

    parent_fd = -1
    descriptor = -1
    snapshot_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        parent_fd = open_directory_fd(path.parent)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        try:
            current = _executable_identity_from_fd(descriptor, path)
        except TranscriptionError as exc:
            if expected_identity is not None:
                raise TranscriptionError(
                    "IDENTITY_CHANGED",
                    "The ASR executable no longer has a safe recorded file identity.",
                    exit_code=7,
                ) from exc
            raise
        if expected_identity is not None:
            expected = {
                key: value
                for key, value in expected_identity.items()
                if key != "version"
            }
            if current != expected:
                raise TranscriptionError(
                    "IDENTITY_CHANGED",
                    "The ASR executable no longer matches its recorded content identity.",
                    exit_code=7,
                )
        if platform.system() == "Darwin":
            snapshot_directory = tempfile.TemporaryDirectory(
                prefix="awesome-capture-exec-"
            )
            snapshot_root = Path(snapshot_directory.name)
            os.chmod(snapshot_root, 0o700)
            snapshot_root_fd = -1
            snapshot_fd = -1
            snapshot_read_fd = -1
            try:
                snapshot_root_fd = open_directory_fd(snapshot_root)
                snapshot_fd = os.open(
                    "executable",
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o500,
                    dir_fd=snapshot_root_fd,
                )
                offset = 0
                while offset < current["bytes"]:
                    chunk = os.pread(
                        descriptor,
                        min(1024 * 1024, current["bytes"] - offset),
                        offset,
                    )
                    if not chunk:
                        raise TranscriptionError(
                            "IDENTITY_CHANGED",
                            "The ASR executable became unreadable while creating its private execution snapshot.",
                            exit_code=7,
                        )
                    view = memoryview(chunk)
                    written = 0
                    while written < len(view):
                        count = os.write(snapshot_fd, view[written:])
                        if count <= 0:
                            raise TranscriptionError(
                                "IDENTITY_CHANGED",
                                "The private ASR executable snapshot could not be completed.",
                                exit_code=7,
                            )
                        written += count
                    offset += len(chunk)
                os.fsync(snapshot_fd)
                os.close(snapshot_fd)
                snapshot_fd = -1
                os.fsync(snapshot_root_fd)
                snapshot_path = snapshot_root / "executable"
                snapshot_read_fd = os.open(
                    "executable",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=snapshot_root_fd,
                )
                snapshot_identity = _executable_identity_from_fd(
                    snapshot_read_fd,
                    snapshot_path,
                )
            finally:
                if snapshot_read_fd >= 0:
                    os.close(snapshot_read_fd)
                if snapshot_fd >= 0:
                    os.close(snapshot_fd)
                if snapshot_root_fd >= 0:
                    os.close(snapshot_root_fd)
            if (
                snapshot_identity["bytes"] != current["bytes"]
                or snapshot_identity["sha256"] != current["sha256"]
            ):
                raise TranscriptionError(
                    "IDENTITY_CHANGED",
                    "The private ASR executable snapshot failed content verification.",
                    exit_code=7,
                )
            yield descriptor, current, str(snapshot_path)
        else:
            yield descriptor, current, f"/proc/self/fd/{descriptor}"
        current_name_fd = -1
        try:
            current_name_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            after_execution = _executable_identity_from_fd(
                current_name_fd,
                path,
            )
        except (OSError, TranscriptionError) as exc:
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                "The ASR executable pathname changed during execution.",
                exit_code=7,
            ) from exc
        finally:
            if current_name_fd >= 0:
                os.close(current_name_fd)
        if after_execution != current:
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                "The ASR executable pathname changed during execution.",
                exit_code=7,
            )
    except SafeRuntimeError as exc:
        code = "IDENTITY_CHANGED" if expected_identity is not None else exc.code
        exit_code = 7 if expected_identity is not None else exc.exit_code
        raise TranscriptionError(code, exc.message, exit_code=exit_code) from exc
    except OSError as exc:
        code = "IDENTITY_CHANGED" if expected_identity is not None else "UNSAFE_PATH"
        exit_code = 7 if expected_identity is not None else 2
        raise TranscriptionError(
            code,
            "The ASR executable could not be opened through its pinned parent directory.",
            exit_code=exit_code,
        ) from exc
    finally:
        if snapshot_directory is not None:
            snapshot_directory.cleanup()
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def inspect_media(path: Path) -> dict[str, Any]:
    with opened_process_input(path) as input_fd:
        process = run_process(
            [
                require_tool("ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name,size:stream=index,codec_type,codec_name",
                "-of",
                "json",
                f"/dev/fd/{input_fd}",
            ],
            timeout=30,
            pass_fds=(input_fd,),
        )
    if process.returncode != 0:
        raise TranscriptionError(
            "INVALID_MEDIA",
            "ffprobe could not read the media file.",
            exit_code=2,
        )
    try:
        data = strict_json_bytes(process.stdout.encode("utf-8"), description="ffprobe output")
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TranscriptionError, TypeError, ValueError) as exc:
        raise TranscriptionError("INVALID_MEDIA", "ffprobe returned invalid metadata.", exit_code=2) from exc
    streams = data.get("streams") or []
    if not math.isfinite(duration) or duration <= 0:
        raise TranscriptionError(
            "INVALID_MEDIA",
            "Media duration must be positive.",
            exit_code=2,
        )
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "duration_ms": round(duration * 1000),
        "duration_seconds": duration,
        "container": str((data.get("format") or {}).get("format_name") or ""),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        "has_video": any(stream.get("codec_type") == "video" for stream in streams),
        "streams": streams,
    }


def _serialized_json_sha256(value: dict[str, Any]) -> str:
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return hashlib.sha256(encoded).hexdigest()


def atomic_json(
    path: Path,
    value: dict[str, Any],
    *,
    expected_previous: dict[str, Any] | None = None,
) -> None:
    try:
        safe_atomic_json(
            path,
            value,
            expected_existing_sha256=(
                _serialized_json_sha256(expected_previous)
                if expected_previous is not None
                else None
            ),
        )
    except SafeRuntimeError as exc:
        raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc


def exact_sidecar(path: Path) -> Path | None:
    for suffix in (".srt", ".vtt"):
        candidate = path.with_suffix(suffix)
        if candidate.exists() or candidate.is_symlink():
            try:
                return secure_input_file(candidate)
            except SafeRuntimeError as exc:
                raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
    return None


def parse_clock(value: str) -> int:
    clean = value.strip().replace(",", ".")
    parts = clean.split(":")
    if len(parts) == 2:
        hours = 0
        minutes = int(parts[0])
        seconds = float(parts[1])
    elif len(parts) == 3:
        hours, minutes = int(parts[0]), int(parts[1])
        seconds = float(parts[2])
    else:
        raise ValueError(value)
    return round((hours * 3600 + minutes * 60 + seconds) * 1000)


def strip_subtitle_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\{\\[^}]+\}", "", value)
    return " ".join(value.split()).strip()


def parse_sidecar(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    segments: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if "-->" not in line:
            index += 1
            continue
        start_raw, end_raw = [item.strip().split(" ", 1)[0] for item in line.split("-->", 1)]
        text_lines: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index].strip())
            index += 1
        try:
            start_ms, end_ms = parse_clock(start_raw), parse_clock(end_raw)
        except ValueError:
            continue
        text = strip_subtitle_markup(" ".join(text_lines))
        if text and end_ms > start_ms:
            segments.append(
                {"start_ms": start_ms, "end_ms": end_ms, "text": text, "chunk_index": 0}
            )
    return deduplicate_segments(segments)


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def executable_path(explicit: str | None, default_name: str) -> Path | None:
    if explicit:
        expanded = Path(explicit).expanduser()
        if expanded.parent != Path(".") or expanded.is_absolute():
            try:
                return secure_input_file(expanded, executable=True)
            except SafeRuntimeError:
                return None
        discovered = shutil.which(explicit)
    else:
        discovered = shutil.which(default_name)
    if not discovered:
        return None
    try:
        return secure_input_file(Path(discovered), executable=True)
    except SafeRuntimeError:
        return None


def local_model_path(
    value: str | None, *, require_directory: bool | None = False
) -> Path | None:
    if not value:
        return None
    try:
        candidate = secure_model_path(
            Path(value), require_directory=require_directory
        )
    except SafeRuntimeError:
        return None
    return candidate


def probe_whisper_cpp(binary: str | None, *, timeout: int = 10) -> dict[str, Any]:
    executable = executable_path(binary, "whisper-cli")
    if executable is None:
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "whisper.cpp executable was not found. Supply --whisper-cpp-bin or install whisper-cli.",
            exit_code=3,
        )
    try:
        with opened_verified_executable(executable) as (
            executable_fd,
            binary_identity,
            executable_command,
        ):
            process = run_process(
                [executable_command, "--version"],
                timeout=min(timeout, 10),
                pass_fds=(executable_fd,),
            )
    except TranscriptionError as exc:
        if exc.code in {"UNSAFE_PATH", "IDENTITY_CHANGED"}:
            raise TranscriptionError(
                "ENGINE_UNAVAILABLE",
                "whisper.cpp executable failed its secure identity probe.",
                exit_code=3,
            ) from exc
        raise
    if process.returncode != 0:
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "whisper.cpp executable failed its version probe.",
            exit_code=3,
        )
    version_output = next(
        (
            line.strip()
            for line in f"{process.stdout}\n{process.stderr}".splitlines()
            if line.strip()
        ),
        "",
    )
    if not version_output:
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "whisper.cpp executable returned no version information.",
            exit_code=3,
        )
    lowered_version = version_output.lower()
    if (
        len(version_output) > 256
        or "://" in version_output
        or version_output.startswith(("/", "~"))
        or any(word in lowered_version for word in ("cookie", "token", "secret", "password"))
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+():-]*", version_output)
    ):
        version_output = "unavailable"
    return {
        "binary_path": str(executable),
        "binary_version": version_output,
        "binary_bytes": binary_identity["bytes"],
        "binary_sha256": binary_identity["sha256"],
    }


def whisper_cpp_identity(
    model_name: str | None, binary: str | None, *, timeout: int
) -> dict[str, Any]:
    model = local_model_path(model_name)
    if model is None:
        raise TranscriptionError(
            "MODEL_UNAVAILABLE",
            "whisper.cpp requires --model to be an existing, non-empty local model file.",
            exit_code=3,
        )
    identity = probe_whisper_cpp(binary, timeout=timeout)
    model_identity = content_identity(model)
    return {
        **identity,
        "model_path": str(model),
        "model_bytes": model_identity["bytes"],
        "model_sha256": model_identity["sha256"],
    }


def select_engine(
    requested: str,
    model_name: str | None = None,
    whisper_cpp_bin: str | None = None,
    *,
    timeout: int = 10,
) -> str:
    if requested != "auto":
        return requested
    if whisper_cpp_bin is None:
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "auto requires an explicit --whisper-cpp-bin and local model file.",
            exit_code=3,
        )
    if local_model_path(model_name, require_directory=False) is not None:
        probe_whisper_cpp(whisper_cpp_bin, timeout=timeout)
        return "whisper-cpp"
    raise TranscriptionError(
        "ENGINE_UNAVAILABLE",
        "auto only selects whisper.cpp and requires an explicit local model file "
        "plus an explicit version-probed --whisper-cpp-bin.",
        exit_code=3,
    )


def normalize_chunks(
    path: Path,
    chunks_dir: Path,
    chunk_seconds: int,
    timeout: int,
    *,
    job_id: str | None = None,
    source_sha256: str | None = None,
    expected_duration_ms: int | None = None,
    staging_root: Path | None = None,
    quarantine_root: Path | None = None,
) -> list[Path]:
    """Build and publish one complete immutable chunk directory.

    The manifest is the chunk-set commit marker. Any pre-existing directory
    without an exact manifest/file match is a conflict, never partial progress.
    """

    manifest_path = chunks_dir / "chunks.manifest.json"
    if chunks_dir.exists() or chunks_dir.is_symlink():
        manifest = validate_chunk_set(
            chunks_dir,
            expected_job_id=job_id,
            expected_source_sha256=source_sha256,
        )
        if (
            expected_duration_ms is not None
            and manifest["total_duration_ms"] != expected_duration_ms
        ):
            raise TranscriptionError(
                "CHUNK_SET_CONFLICT",
                "The existing chunk set does not exactly cover the source duration.",
                exit_code=7,
            )
        return [chunks_dir / item["name"] for item in manifest["chunks"]]

    if (
        staging_root is None
        and chunks_dir.parent.parent.name == "transcriptions"
        and chunks_dir.parent.parent.parent.name == MANAGED_LAYOUT_VERSION
    ):
        managed_root = chunks_dir.parent.parent.parent
        staging_root = managed_root / "staging"
        quarantine_root = quarantine_root or managed_root / "quarantine"
    try:
        parent = secure_mkdirs(chunks_dir.parent)
        staging_parent = secure_mkdirs(staging_root or chunks_dir.parent)
        temporary_dir = create_private_directory(
            staging_parent,
            prefix=f"transcribe-{job_id or ('0' * 64)}-chunks.",
        )
    except SafeRuntimeError as exc:
        raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
    published = False
    try:
        with opened_process_input(path) as input_fd:
            command = [
                require_tool("ffmpeg"),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                f"/dev/fd/{input_fd}",
                "-map",
                "0:a:0",
                "-vn",
            ]
            if expected_duration_ms is not None:
                if (
                    isinstance(expected_duration_ms, bool)
                    or not isinstance(expected_duration_ms, int)
                    or expected_duration_ms <= 0
                ):
                    raise TranscriptionError(
                        "INVALID_MEDIA",
                        "Expected source duration must be a positive integer number of milliseconds.",
                        exit_code=2,
                    )
                expected_frames = expected_duration_ms * 16
                command.extend(
                    [
                        "-af",
                        f"aresample=16000,apad,atrim=end_sample={expected_frames}",
                    ]
                )
            command.extend(
                [
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "segment",
                    "-segment_time",
                    str(chunk_seconds),
                    "-reset_timestamps",
                    "1",
                    "chunk-%05d.wav",
                ]
            )
            process = run_process(
                command,
                timeout=timeout,
                cwd=temporary_dir,
                pass_fds=(input_fd,),
            )
        if process.returncode != 0:
            raise TranscriptionError(
                "AUDIO_EXTRACTION_FAILED",
                "ffmpeg could not normalize the audio stream.",
                exit_code=5,
            )
        timeline = secure_private_chunk_timeline(temporary_dir)
        if (
            expected_duration_ms is not None
            and sum(item["duration_ms"] for item in timeline)
            != expected_duration_ms
        ):
            raise TranscriptionError(
                "AUDIO_EXTRACTION_FAILED",
                "Normalized chunks do not exactly cover the source duration.",
                exit_code=7,
            )
        for entry in timeline:
            entry["path"] = str(chunks_dir / entry["name"])
        manifest = {
            "schema_version": CHUNK_SET_SCHEMA_VERSION,
            "job_id": job_id or ("0" * 64),
            "source_sha256": source_sha256 or file_sha256(path),
            "chunk_seconds": chunk_seconds,
            "audio": {"sample_rate": 16000, "channels": 1, "sample_width": 2},
            "count": len(timeline),
            "total_duration_ms": sum(item["duration_ms"] for item in timeline),
            "chunks": timeline,
        }
        try:
            validate_contract(manifest, expected="chunk-set")
        except ContractError as exc:
            raise TranscriptionError(
                getattr(exc, "code", "CONTRACT_VALIDATION_FAILED"),
                f"Chunk-set failed schema validation at {getattr(exc, 'path', '$')}.",
                details=getattr(exc, "message", str(exc)),
                exit_code=7,
            ) from exc
        safe_atomic_json(temporary_dir / "chunks.manifest.json", manifest)
        fsync_directory(temporary_dir)
        try:
            publish_private_directory(temporary_dir, chunks_dir)
            published = True
        except SafeRuntimeError as exc:
            if exc.code != "RECOVERY_CONFLICT":
                raise TranscriptionError(
                    exc.code,
                    exc.message,
                    exit_code=exc.exit_code,
                ) from exc
            validate_chunk_set(
                chunks_dir,
                expected_job_id=job_id,
                expected_source_sha256=source_sha256,
            )
            if quarantine_root is not None:
                try:
                    quarantine_private_directory(
                        temporary_dir,
                        quarantine_root,
                    )
                except SafeRuntimeError as quarantine_exc:
                    raise TranscriptionError(
                        quarantine_exc.code,
                        quarantine_exc.message,
                        exit_code=quarantine_exc.exit_code,
                    ) from quarantine_exc
        validated = validate_chunk_set(
            chunks_dir,
            expected_job_id=job_id,
            expected_source_sha256=source_sha256,
        )
        return [chunks_dir / item["name"] for item in validated["chunks"]]
    except BaseException:
        if not published and quarantine_root is not None:
            try:
                quarantine_private_directory(
                    temporary_dir,
                    quarantine_root,
                )
            except SafeRuntimeError:
                # Preserve the primary processing error. The untouched staging
                # entry will be handled (or rejected) by explicit recovery.
                pass
        raise


def wav_sample_duration(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            sample_rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
    except (OSError, EOFError, wave.Error) as exc:
        raise TranscriptionError(
            "INVALID_CHUNK",
            "Normalized audio chunk is not a readable WAV file.",
            details=exc.__class__.__name__,
            exit_code=7,
        ) from exc
    if frames <= 0 or sample_rate != 16000 or channels != 1 or sample_width != 2:
        raise TranscriptionError(
            "INVALID_CHUNK",
            f"Normalized chunk must be non-empty mono 16 kHz PCM16 WAV: {path}",
            exit_code=7,
        )
    return frames, sample_rate


def secure_private_chunk_timeline(directory: Path) -> list[dict[str, Any]]:
    """Validate every ffmpeg output before chmod and derive evidence from held FDs."""

    try:
        directory_fd = open_directory_fd(directory)
    except OSError as exc:
        raise TranscriptionError(
            "CHUNK_SET_CONFLICT",
            "Normalized chunk staging is not a safe directory.",
            exit_code=7,
        ) from exc
    opened: list[tuple[str, int, os.stat_result]] = []
    try:
        directory_metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise TranscriptionError(
                "CHUNK_SET_CONFLICT",
                "Normalized chunk staging is not a private owned directory.",
                exit_code=7,
            )
        names = sorted(os.listdir(directory_fd))
        expected_names = [
            f"chunk-{index:05d}.wav"
            for index in range(len(names))
        ]
        if not names:
            raise TranscriptionError(
                "AUDIO_EXTRACTION_FAILED",
                "ffmpeg produced no audio chunks.",
                exit_code=5,
            )
        if names != expected_names:
            raise TranscriptionError(
                "CHUNK_SET_CONFLICT",
                "ffmpeg output is not one complete contiguous chunk sequence.",
                exit_code=7,
            )
        for name in names:
            try:
                listed = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(listed.st_mode)
                    or listed.st_uid != os.geteuid()
                    or listed.st_nlink != 1
                ):
                    raise TranscriptionError(
                        "CHUNK_SET_CONFLICT",
                        "Normalized chunk output has unsafe ownership, type, or links.",
                        exit_code=7,
                    )
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise TranscriptionError(
                    "CHUNK_SET_CONFLICT",
                    "Normalized chunk output cannot be opened safely.",
                    exit_code=7,
                ) from exc
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or (listed.st_dev, listed.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                os.close(descriptor)
                raise TranscriptionError(
                    "CHUNK_SET_CONFLICT",
                    "Normalized chunk output has unsafe ownership, type, or links.",
                    exit_code=7,
                )
            opened.append((name, descriptor, metadata))

        # No output inode is modified until every entry has passed the
        # hardlink/type/owner scan above.
        for unused_name, descriptor, unused_metadata in opened:
            del unused_name, unused_metadata
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)

        cumulative = Fraction(0, 1)
        timeline: list[dict[str, Any]] = []
        for index, (name, descriptor, unused_metadata) in enumerate(opened):
            del unused_metadata
            before = os.fstat(descriptor)
            duplicate = os.dup(descriptor)
            try:
                with os.fdopen(duplicate, "rb") as raw_handle:
                    duplicate = -1
                    with wave.open(raw_handle, "rb") as wav_handle:
                        frames = wav_handle.getnframes()
                        sample_rate = wav_handle.getframerate()
                        channels = wav_handle.getnchannels()
                        sample_width = wav_handle.getsampwidth()
            except (OSError, EOFError, wave.Error) as exc:
                raise TranscriptionError(
                    "INVALID_CHUNK",
                    "Normalized audio chunk is not a readable WAV file.",
                    details=exc.__class__.__name__,
                    exit_code=7,
                ) from exc
            finally:
                if duplicate >= 0:
                    os.close(duplicate)
            if (
                frames <= 0
                or sample_rate != 16000
                or channels != 1
                or sample_width != 2
            ):
                raise TranscriptionError(
                    "INVALID_CHUNK",
                    "Normalized chunk must be non-empty mono 16 kHz PCM16 WAV.",
                    exit_code=7,
                )
            digest = hashlib.sha256()
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
            after = os.fstat(descriptor)
            current = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_nlink)
                != (after.st_dev, after.st_ino, after.st_size, after.st_nlink)
                or (after.st_dev, after.st_ino)
                != (current.st_dev, current.st_ino)
                or after.st_nlink != 1
                or stat.S_IMODE(after.st_mode) != 0o600
            ):
                raise TranscriptionError(
                    "CHUNK_SET_CONFLICT",
                    "Normalized chunk changed during pinned validation.",
                    exit_code=7,
                )
            start = cumulative
            cumulative += Fraction(frames, sample_rate)
            start_ms = round(start * 1000)
            end_ms = round(cumulative * 1000)
            timeline.append(
                {
                    "index": index,
                    "name": name,
                    "path": str(directory / name),
                    "bytes": after.st_size,
                    "sha256": digest.hexdigest(),
                    "sample_frames": frames,
                    "sample_rate": sample_rate,
                    "offset_ms": start_ms,
                    "duration_ms": end_ms - start_ms,
                }
            )
        return timeline
    except OSError as exc:
        raise TranscriptionError(
            "CHUNK_SET_CONFLICT",
            "Normalized chunk validation failed.",
            exit_code=7,
        ) from exc
    finally:
        for unused_name, descriptor, unused_metadata in opened:
            del unused_name, unused_metadata
            os.close(descriptor)
        os.close(directory_fd)


def chunk_timeline(chunks: list[Path]) -> list[dict[str, Any]]:
    expected_names = [f"chunk-{index:05d}.wav" for index in range(len(chunks))]
    names = [chunk.name for chunk in chunks]
    if names != expected_names:
        raise TranscriptionError(
            "CHUNK_SET_CONFLICT",
            "Normalized chunks are not a complete contiguous sequence.",
            details=json.dumps({"expected": expected_names, "actual": names}),
            exit_code=7,
        )
    cumulative = Fraction(0, 1)
    timeline: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        frames, sample_rate = wav_sample_duration(chunk)
        start = cumulative
        cumulative += Fraction(frames, sample_rate)
        start_ms = round(start * 1000)
        end_ms = round(cumulative * 1000)
        timeline.append(
            {
                "index": index,
                "name": chunk.name,
                "path": str(chunk),
                "bytes": os.lstat(chunk).st_size,
                "sha256": file_sha256(chunk),
                "sample_frames": frames,
                "sample_rate": sample_rate,
                "offset_ms": start_ms,
                "duration_ms": end_ms - start_ms,
            }
        )
    return timeline


def validate_chunk_set(
    chunks_dir: Path,
    *,
    expected_job_id: str | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        assert_no_symlink_components(chunks_dir)
    except SafeRuntimeError as exc:
        raise TranscriptionError("CHUNK_SET_CONFLICT", exc.message, exit_code=7) from exc
    if not chunks_dir.is_dir():
        raise TranscriptionError(
            "CHUNK_SET_CONFLICT",
            "The normalized chunk set is not a directory.",
            exit_code=7,
        )
    manifest_path = chunks_dir / "chunks.manifest.json"
    manifest = strict_json_file(
        manifest_path,
        maximum_bytes=JSON_LIMIT,
        description="chunk-set manifest",
    )
    if not isinstance(manifest, dict):
        raise TranscriptionError("CHUNK_SET_CONFLICT", "Chunk-set manifest must be an object.", exit_code=7)
    try:
        validate_contract(manifest, expected="chunk-set")
    except ContractError as exc:
        raise TranscriptionError(
            "CHUNK_SET_CONFLICT",
            f"Chunk-set failed schema validation at {getattr(exc, 'path', '$')}.",
            details=getattr(exc, "message", str(exc)),
            exit_code=7,
        ) from exc
    expected_keys = {
        "schema_version",
        "job_id",
        "source_sha256",
        "chunk_seconds",
        "audio",
        "count",
        "total_duration_ms",
        "chunks",
    }
    if set(manifest) != expected_keys or manifest.get("schema_version") != CHUNK_SET_SCHEMA_VERSION:
        raise TranscriptionError(
            "CHUNK_SET_CONFLICT",
            "Chunk-set manifest has an unsupported or malformed schema.",
            exit_code=7,
        )
    if expected_job_id is not None and manifest.get("job_id") != expected_job_id:
        raise TranscriptionError("CHUNK_SET_CONFLICT", "Chunk-set job identity does not match.", exit_code=7)
    if expected_source_sha256 is not None and manifest.get("source_sha256") != expected_source_sha256:
        raise TranscriptionError("CHUNK_SET_CONFLICT", "Chunk-set source identity does not match.", exit_code=7)
    audio = manifest.get("audio")
    if audio != {"sample_rate": 16000, "channels": 1, "sample_width": 2}:
        raise TranscriptionError("CHUNK_SET_CONFLICT", "Chunk-set audio format is invalid.", exit_code=7)
    chunks = manifest.get("chunks")
    if (
        not isinstance(chunks, list)
        or isinstance(manifest.get("count"), bool)
        or manifest.get("count") != len(chunks)
        or not chunks
    ):
        raise TranscriptionError("CHUNK_SET_CONFLICT", "Chunk-set count is invalid.", exit_code=7)
    expected_names = {f"chunk-{index:05d}.wav" for index in range(len(chunks))}
    actual_names = {
        item.name
        for item in chunks_dir.iterdir()
        if item.name != "chunks.manifest.json"
    }
    if actual_names != expected_names:
        raise TranscriptionError(
            "CHUNK_SET_CONFLICT",
            "Chunk directory has missing or extra files.",
            details=json.dumps(
                {"expected": sorted(expected_names), "actual": sorted(actual_names)}
            ),
            exit_code=7,
        )
    cumulative = 0
    normalized: list[dict[str, Any]] = []
    item_keys = {
        "index",
        "name",
        "path",
        "bytes",
        "sha256",
        "sample_frames",
        "sample_rate",
        "offset_ms",
        "duration_ms",
    }
    for index, item in enumerate(chunks):
        if not isinstance(item, dict) or set(item) != item_keys:
            raise TranscriptionError("CHUNK_SET_CONFLICT", f"Chunk record {index} is malformed.", exit_code=7)
        expected_name = f"chunk-{index:05d}.wav"
        chunk = chunks_dir / expected_name
        try:
            metadata = validate_managed_file(chunk)
        except SafeRuntimeError as exc:
            raise TranscriptionError("CHUNK_SET_CONFLICT", exc.message, exit_code=7) from exc
        frames, rate = wav_sample_duration(chunk)
        duration_ms = round(Fraction(frames, rate) * 1000)
        if (
            item.get("index") != index
            or item.get("name") != expected_name
            or item.get("path") != str(chunk)
            or item.get("bytes") != metadata.st_size
            or not SHA256_RE.fullmatch(str(item.get("sha256") or ""))
            or item.get("sha256") != file_sha256(chunk)
            or item.get("sample_frames") != frames
            or item.get("sample_rate") != rate
            or item.get("offset_ms") != cumulative
            or item.get("duration_ms") != duration_ms
        ):
            raise TranscriptionError(
                "CHUNK_SET_CONFLICT",
                f"Chunk record or content mismatch: {expected_name}",
                exit_code=7,
            )
        cumulative += duration_ms
        normalized.append(item)
    if manifest.get("total_duration_ms") != cumulative:
        raise TranscriptionError("CHUNK_SET_CONFLICT", "Chunk-set total duration is inconsistent.", exit_code=7)
    return manifest


def chunk_has_signal(path: Path) -> bool:
    with opened_process_input(path) as input_fd:
        process = run_process(
            [
                require_tool("ffmpeg"),
                "-hide_banner",
                "-i",
                f"/dev/fd/{input_fd}",
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            timeout=120,
            pass_fds=(input_fd,),
        )
    if process.returncode != 0:
        raise TranscriptionError(
            "AUDIO_INSPECTION_FAILED",
            "ffmpeg could not inspect normalized audio.",
            exit_code=5,
        )
    output = f"{process.stdout}\n{process.stderr}".lower()
    return "max_volume: -inf db" not in output


def normalize_engine_segments(
    raw_segments: list[dict[str, Any]],
    *,
    chunk_index: int,
    offset_ms: int,
    chunk_duration_ms: int | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_segments, list):
        raise TranscriptionError(
            "INVALID_ENGINE_OUTPUT",
            "ASR output segments must be an array.",
            exit_code=5,
        )
    result: list[dict[str, Any]] = []
    previous_start = -1
    previous_end = -1
    for position, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"ASR segment {position} must be an object.",
                exit_code=5,
            )
        if not isinstance(item.get("text"), str):
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"ASR segment {position} has no string text.",
                exit_code=5,
            )
        text = " ".join(item["text"].split()).strip()
        if not text:
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"ASR segment {position} has empty text.",
                exit_code=5,
            )
        start_raw = item.get("start")
        end_raw = item.get("end")
        if (
            isinstance(start_raw, bool)
            or isinstance(end_raw, bool)
            or not isinstance(start_raw, (int, float))
            or not isinstance(end_raw, (int, float))
        ):
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"ASR segment {position} has non-numeric timestamps.",
                exit_code=5,
            )
        try:
            start_seconds = float(start_raw)
            end_seconds = float(end_raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"ASR segment {position} has invalid timestamps.",
                exit_code=5,
            ) from exc
        if (
            not math.isfinite(start_seconds)
            or not math.isfinite(end_seconds)
            or start_seconds < 0
            or end_seconds <= start_seconds
        ):
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"ASR segment {position} has out-of-range timestamps.",
                exit_code=5,
            )
        relative_start_ms = round(start_seconds * 1000)
        relative_end_ms = round(end_seconds * 1000)
        if chunk_duration_ms is not None and relative_end_ms > chunk_duration_ms:
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"ASR segment {position} exceeds its chunk duration.",
                exit_code=5,
            )
        start_ms = offset_ms + relative_start_ms
        end_ms = offset_ms + relative_end_ms
        if start_ms < previous_start or end_ms < previous_end:
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"ASR segment {position} is not timestamp-monotonic.",
                exit_code=5,
            )
        segment: dict[str, Any] = {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
            "chunk_index": chunk_index,
        }
        if item.get("avg_logprob") is not None:
            probability = item["avg_logprob"]
            if (
                isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not math.isfinite(float(probability))
            ):
                raise TranscriptionError(
                    "INVALID_ENGINE_OUTPUT",
                    f"ASR segment {position} has invalid avg_logprob.",
                    exit_code=5,
                )
            segment["avg_logprob"] = float(probability)
        result.append(segment)
        previous_start = start_ms
        previous_end = end_ms
    return result


def whisper_cpp_milliseconds(item: dict[str, Any], edge: str) -> int:
    offsets = item.get("offsets")
    if isinstance(offsets, dict) and offsets.get(edge) is not None:
        value = offsets[edge]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid {edge} offset")
        return value
    timestamps = item.get("timestamps")
    if isinstance(timestamps, dict) and timestamps.get(edge) is not None:
        if not isinstance(timestamps[edge], str):
            raise ValueError(f"invalid {edge} timestamp")
        try:
            return parse_clock(timestamps[edge])
        except ValueError:
            pass
    raise ValueError(f"missing {edge} timestamp")


def parse_whisper_cpp_json(raw: bytes, requested_language: str | None) -> dict[str, Any]:
    try:
        value = strict_json_bytes(raw, description="whisper.cpp output")
    except TranscriptionError as exc:
        raise TranscriptionError(
            "INVALID_ENGINE_OUTPUT",
            "whisper.cpp returned invalid JSON.",
            details=str(exc),
            exit_code=5,
        ) from exc
    transcription = value.get("transcription") if isinstance(value, dict) else None
    if not isinstance(transcription, list):
        raise TranscriptionError(
            "INVALID_ENGINE_OUTPUT",
            "whisper.cpp JSON has no transcription array.",
            exit_code=5,
        )
    segments: list[dict[str, Any]] = []
    for index, item in enumerate(transcription):
        if not isinstance(item, dict):
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"whisper.cpp segment {index} is not an object.",
                exit_code=5,
            )
        if not isinstance(item.get("text"), str):
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"whisper.cpp segment {index} has no string text.",
                exit_code=5,
            )
        text = " ".join(item["text"].split()).strip()
        if not text:
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"whisper.cpp segment {index} has empty text.",
                exit_code=5,
            )
        try:
            start_ms = whisper_cpp_milliseconds(item, "from")
            end_ms = whisper_cpp_milliseconds(item, "to")
        except ValueError as exc:
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"whisper.cpp segment {index} has invalid timestamps.",
                exit_code=5,
            ) from exc
        if start_ms < 0 or end_ms < start_ms:
            raise TranscriptionError(
                "INVALID_ENGINE_OUTPUT",
                f"whisper.cpp segment {index} has out-of-range timestamps.",
                exit_code=5,
            )
        segment: dict[str, Any] = {
            "start": start_ms / 1000,
            "end": end_ms / 1000,
            "text": text,
        }
        segments.append(segment)
    result = value.get("result") if isinstance(value, dict) else None
    params = value.get("params") if isinstance(value, dict) else None
    language = result.get("language") if isinstance(result, dict) else None
    if not language and isinstance(params, dict):
        candidate = params.get("language")
        language = None if candidate == "auto" else candidate
    return {
        "segments": segments,
        "language": language or requested_language,
        "raw_output_sha256": hashlib.sha256(raw).hexdigest(),
    }


def whisper_cpp_runner(
    identity: dict[str, Any],
    language: str | None,
    timeout: int,
    *,
    cpu_only: bool,
    gpu_previously_failed: bool,
) -> Callable[[Path], dict[str, Any]]:
    executable_identity = identity.get("executable")
    model_identity = identity.get("model")
    if not isinstance(executable_identity, dict) or not isinstance(model_identity, dict):
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "whisper.cpp identity is missing its executable or local model.",
            exit_code=3,
        )
    executable = Path(executable_identity["path"])
    model = str(model_identity["path"])
    gpu_available = not cpu_only and not gpu_previously_failed
    gpu_disabled_by_failure = gpu_previously_failed

    def run_attempt(path: Path, output_prefix: Path, *, use_gpu: bool) -> tuple[dict[str, Any] | None, str]:
        try:
            with (
                opened_verified_executable(
                    executable,
                    expected_identity=executable_identity,
                ) as (
                    executable_fd,
                    unused_executable_identity,
                    executable_command,
                ),
                opened_process_input(path) as input_fd,
                opened_process_input(Path(model)) as model_fd,
            ):
                del unused_executable_identity
                command = [
                    executable_command,
                    "-m",
                    f"/dev/fd/{model_fd}",
                    "-f",
                    f"/dev/fd/{input_fd}",
                    "-l",
                    language or "auto",
                    "-ojf",
                    "-of",
                    output_prefix.name,
                    "-np",
                ]
                if not use_gpu:
                    command.append("-ng")
                process = run_process(
                    command,
                    timeout=timeout,
                    cwd=output_prefix.parent,
                    pass_fds=(executable_fd, input_fd, model_fd),
                )
        except TranscriptionError as exc:
            if exc.code == "IDENTITY_CHANGED":
                raise
            return None, exc.code
        if process.returncode != 0:
            return None, "PROCESS_FAILED"
        output_path = Path(f"{output_prefix}.json")
        if not output_path.is_file():
            return None, "OUTPUT_MISSING"
        try:
            parsed = parse_whisper_cpp_json(output_path.read_bytes(), language)
        except (OSError, TranscriptionError) as exc:
            if isinstance(exc, TranscriptionError):
                return None, exc.code
            return None, "OUTPUT_UNREADABLE"
        return parsed, ""

    def run(path: Path) -> dict[str, Any]:
        nonlocal gpu_available, gpu_disabled_by_failure
        with tempfile.TemporaryDirectory(prefix="transcribe-whisper-cpp-") as temporary:
            temporary_path = Path(temporary)
            gpu_failure = ""
            gpu_attempted = gpu_available
            if gpu_attempted:
                result, gpu_failure = run_attempt(
                    path, temporary_path / "gpu-result", use_gpu=True
                )
                if result is not None:
                    result["runtime"] = {
                        "device": "gpu",
                        "gpu_attempted": True,
                        "gpu_fallback": False,
                        "gpu_failure": None,
                        "gpu_disabled_after_failure": False,
                    }
                    return result
                gpu_available = False
                gpu_disabled_by_failure = True
            result, cpu_failure = run_attempt(
                path, temporary_path / "cpu-result", use_gpu=False
            )
            if result is None:
                details = f"CPU attempt failed: {cpu_failure}"
                if gpu_failure:
                    details = f"GPU attempt failed: {gpu_failure}\n{details}"
                raise TranscriptionError(
                    "TRANSCRIPTION_FAILED",
                    "whisper.cpp failed to transcribe the audio chunk.",
                    details=details,
                    exit_code=5,
                )
            result["runtime"] = {
                "device": "cpu",
                "gpu_attempted": gpu_attempted,
                "gpu_fallback": gpu_attempted and bool(gpu_failure),
                "gpu_failure": gpu_failure or None,
                "gpu_disabled_after_failure": gpu_disabled_by_failure,
            }
            return result

    return run


def offline_environment() -> dict[str, str]:
    allowed = ("PATH", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    )
    return environment


def faster_whisper_runner(model_name: str | None, language: str | None) -> Callable[[Path], dict[str, Any]]:
    model_path_value = local_model_path(model_name, require_directory=True)
    if model_path_value is None:
        raise TranscriptionError(
            "MODEL_UNAVAILABLE",
            "faster-whisper requires --model to be a fully local model directory.",
            exit_code=3,
        )
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "faster-whisper could not be imported.",
            exit_code=3,
        ) from exc
    selected_model = str(model_path_value)
    try:
        previous = {key: os.environ.get(key) for key in offline_environment()}
        os.environ.update(offline_environment())
        try:
            model = WhisperModel(
                selected_model,
                device="cpu",
                compute_type="int8",
                local_files_only=True,
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    except Exception as exc:
        raise TranscriptionError(
            "MODEL_UNAVAILABLE",
            "The local faster-whisper model could not be loaded.",
            exit_code=3,
        ) from exc

    def run(path: Path) -> dict[str, Any]:
        try:
            previous = {key: os.environ.get(key) for key in offline_environment()}
            os.environ.update(offline_environment())
            try:
                with opened_process_input(path) as input_fd:
                    generated, info = model.transcribe(
                        f"/dev/fd/{input_fd}",
                        language=language,
                        vad_filter=True,
                        condition_on_previous_text=False,
                    )
                    # faster-whisper yields segments lazily; keep both the
                    # offline guard and held input descriptor active.
                    segments = [
                        {
                            "start": float(segment.start),
                            "end": float(segment.end),
                            "text": str(segment.text),
                            "avg_logprob": getattr(segment, "avg_logprob", None),
                        }
                        for segment in generated
                    ]
            finally:
                for key, old_value in previous.items():
                    if old_value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old_value
            return {"segments": segments, "language": getattr(info, "language", language)}
        except Exception as exc:
            raise TranscriptionError(
                "TRANSCRIPTION_FAILED",
                "faster-whisper failed to transcribe the audio chunk.",
                exit_code=5,
            ) from exc

    return run


def mlx_whisper_runner(model_name: str | None, language: str | None) -> Callable[[Path], dict[str, Any]]:
    model_path_value = local_model_path(model_name, require_directory=True)
    if model_path_value is None:
        raise TranscriptionError(
            "MODEL_UNAVAILABLE",
            "mlx-whisper requires --model to be a fully local model directory.",
            exit_code=3,
        )
    try:
        import mlx_whisper  # type: ignore
    except Exception as exc:
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "MLX Whisper could not be imported.",
            exit_code=3,
        ) from exc

    def run(path: Path) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"path_or_hf_repo": str(model_path_value)}
        if language:
            kwargs["language"] = language
        try:
            previous = {key: os.environ.get(key) for key in offline_environment()}
            os.environ.update(offline_environment())
            try:
                with opened_process_input(path) as input_fd:
                    value = mlx_whisper.transcribe(
                        f"/dev/fd/{input_fd}",
                        **kwargs,
                    )
            finally:
                for key, old_value in previous.items():
                    if old_value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old_value
        except Exception as exc:
            raise TranscriptionError(
                "TRANSCRIPTION_FAILED",
                "MLX Whisper failed to transcribe the audio chunk.",
                exit_code=5,
            ) from exc
        return {
            "segments": value.get("segments") or [],
            "language": value.get("language") or language,
        }

    return run


def external_runner(
    identity: dict[str, Any],
    language: str | None,
    timeout: int,
) -> Callable[[Path], dict[str, Any]]:
    adapter_identity = identity.get("adapter")
    model_identity = identity.get("model")
    if not isinstance(adapter_identity, dict) or not isinstance(model_identity, dict):
        raise TranscriptionError(
            "INVALID_ADAPTER",
            "External engine identity is missing its adapter or local model.",
            exit_code=2,
        )
    executable = Path(adapter_identity["path"])
    model_path_value = Path(model_identity["path"])

    def run(path: Path) -> dict[str, Any]:
        with (
            opened_verified_executable(
                executable,
                expected_identity=adapter_identity,
            ) as (
                executable_fd,
                unused_executable_identity,
                executable_command,
            ),
            opened_process_input(path) as input_fd,
        ):
            del unused_executable_identity
            command = [
                executable_command,
                "--protocol",
                EXTERNAL_PROTOCOL,
                "--model",
                str(model_path_value),
                "--input",
                f"/dev/fd/{input_fd}",
            ]
            if language:
                command.extend(["--language", language])
            process = run_process(
                command,
                timeout=timeout,
                env=offline_environment(),
                cwd=path.parent,
                pass_fds=(executable_fd, input_fd),
            )
        if process.returncode != 0:
            raise TranscriptionError(
                "TRANSCRIPTION_FAILED",
                "External adapter failed.",
                exit_code=5,
            )
        try:
            value = strict_json_bytes(
                process.stdout.encode("utf-8"),
                description="external adapter output",
            )
        except TranscriptionError as exc:
            raise TranscriptionError(
                "INVALID_ADAPTER_OUTPUT",
                "External adapter returned invalid JSON.",
                exit_code=5,
            ) from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"protocol", "language", "segments"}
            or value.get("protocol") != EXTERNAL_PROTOCOL
            or (
                value.get("language") is not None
                and not isinstance(value.get("language"), str)
            )
            or not isinstance(value.get("segments"), list)
        ):
            raise TranscriptionError(
                "INVALID_ADAPTER_OUTPUT",
                "External adapter output does not match awesome-capture.external-asr/v1.",
                exit_code=5,
            )
        return {"segments": value["segments"], "language": value.get("language") or language}

    return run


def content_identity_projection(
    component: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if component is None:
        return None
    return {
        key: component[key]
        for key in ("kind", "sha256", "bytes", "file_count", "version")
        if key in component
    }


def engine_identity_projection(
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "model": content_identity_projection(identity["model"]),
        "executable": content_identity_projection(identity["executable"]),
        "adapter": content_identity_projection(identity["adapter"]),
        "packages": identity["packages"],
    }


def transcription_settings_identity(
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "contract_digest": settings["contract_digest"],
        "algorithm": settings["algorithm"],
        "source_sha256": settings["source_sha256"],
        "source_bytes": settings["source_bytes"],
        "upstream_artifact_sha256": settings["upstream_artifact_sha256"],
        "engine": settings["engine"],
        "engine_identity_sha256": settings["engine_identity"]["identity_sha256"],
        "requested_language": settings["requested_language"],
        "chunk_seconds": settings["chunk_seconds"],
        "whisper_cpp_cpu_only": settings["whisper_cpp_cpu_only"],
        "sidecar_sha256": settings["sidecar_sha256"],
    }


def engine_identity_for(
    engine: str,
    model_name: str | None,
    adapter: str | None,
    whisper_cpp_bin: str | None,
    *,
    timeout: int,
    trust_external_adapter: bool = False,
) -> dict[str, Any]:
    if engine == "whisper-cpp":
        raw = whisper_cpp_identity(model_name, whisper_cpp_bin, timeout=timeout)
        core = {
            "model": {
                "kind": "file",
                "path": raw["model_path"],
                "sha256": raw["model_sha256"],
                "bytes": raw["model_bytes"],
            },
            "executable": {
                "kind": "file",
                "path": raw["binary_path"],
                "sha256": raw["binary_sha256"],
                "bytes": raw["binary_bytes"],
                "version": raw["binary_version"],
            },
            "adapter": None,
            "packages": [],
        }
        return {
            "identity_sha256": canonical_json_sha256(
                engine_identity_projection(core)
            ),
            **core,
        }
    if engine == "external":
        if not trust_external_adapter:
            raise TranscriptionError(
                "EXTERNAL_ADAPTER_NOT_TRUSTED",
                "External ASR requires --trust-external-adapter.",
                exit_code=2,
            )
        if not adapter:
            raise TranscriptionError("INVALID_ADAPTER", "--adapter is required for external ASR.", exit_code=2)
        if not model_name:
            raise TranscriptionError("MODEL_UNAVAILABLE", "--model is required for external ASR.", exit_code=3)
        try:
            executable = secure_input_file(Path(adapter), executable=True)
            adapter_value = content_identity(executable)
            model_value = content_identity(secure_model_path(Path(model_name)))
        except SafeRuntimeError as exc:
            raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
        core = {
            "model": model_value,
            "executable": None,
            "adapter": adapter_value,
            "packages": [],
        }
        return {
            "identity_sha256": canonical_json_sha256(
                engine_identity_projection(core)
            ),
            **core,
        }
    if engine == "faster-whisper":
        model = local_model_path(model_name, require_directory=True)
        if model is None:
            raise TranscriptionError(
                "MODEL_UNAVAILABLE",
                "faster-whisper requires an explicit local model directory.",
                exit_code=3,
            )
        packages = [
            {"name": "faster-whisper", "version": package_version("faster-whisper")},
            {"name": "ctranslate2", "version": package_version("ctranslate2")},
        ]
        if any(item["version"] is None for item in packages):
            raise TranscriptionError("ENGINE_UNAVAILABLE", "faster-whisper packages are unavailable.", exit_code=3)
        core = {
            "model": content_identity(model),
            "executable": None,
            "adapter": None,
            "packages": packages,
        }
        return {
            "identity_sha256": canonical_json_sha256(
                engine_identity_projection(core)
            ),
            **core,
        }
    if engine == "mlx-whisper":
        model = local_model_path(model_name, require_directory=True)
        if model is None:
            raise TranscriptionError(
                "MODEL_UNAVAILABLE",
                "mlx-whisper requires an explicit local model directory.",
                exit_code=3,
            )
        packages = [
            {"name": "mlx-whisper", "version": package_version("mlx-whisper")},
            {"name": "mlx", "version": package_version("mlx")},
        ]
        if any(item["version"] is None for item in packages):
            raise TranscriptionError("ENGINE_UNAVAILABLE", "MLX Whisper packages are unavailable.", exit_code=3)
        core = {
            "model": content_identity(model),
            "executable": None,
            "adapter": None,
            "packages": packages,
        }
        return {
            "identity_sha256": canonical_json_sha256(
                engine_identity_projection(core)
            ),
            **core,
        }
    raise TranscriptionError("ENGINE_UNAVAILABLE", f"Unsupported ASR engine: {engine}", exit_code=3)


def runner_for(
    engine: str,
    model_name: str | None,
    language: str | None,
    adapter: str | None,
    timeout: int,
    *,
    engine_identity: dict[str, Any],
    whisper_cpp_cpu_only: bool,
    whisper_cpp_gpu_previously_failed: bool,
) -> Callable[[Path], dict[str, Any]]:
    if engine == "whisper-cpp":
        return whisper_cpp_runner(
            engine_identity,
            language,
            timeout,
            cpu_only=whisper_cpp_cpu_only,
            gpu_previously_failed=whisper_cpp_gpu_previously_failed,
        )
    if engine == "faster-whisper":
        return faster_whisper_runner(model_name, language)
    if engine == "mlx-whisper":
        return mlx_whisper_runner(model_name, language)
    return external_runner(engine_identity, language, timeout)


def deduplicate_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(segments, key=lambda item: (item["start_ms"], item["end_ms"], item["text"]))
    result: list[dict[str, Any]] = []
    for segment in ordered:
        if result:
            previous = result[-1]
            same_text = segment["text"].casefold() == previous["text"].casefold()
            near = abs(segment["start_ms"] - previous["start_ms"]) <= 1500
            if same_text and near:
                if segment["end_ms"] > previous["end_ms"]:
                    previous["end_ms"] = segment["end_ms"]
                continue
        result.append(segment)
    return result


def format_time(milliseconds: int) -> str:
    seconds, ms = divmod(max(0, milliseconds), 1000)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}.{ms:03d}"


def transcript_markdown(artifact: dict[str, Any]) -> str:
    source = artifact["source"]
    transcription = artifact["transcription"]
    model_identity = transcription.get("engine_identity", {}).get("model")
    model_label = (
        model_identity.get("path")
        if isinstance(model_identity, dict)
        else "sidecar"
    )
    lines = [
        "# Transcript",
        "",
        f"- Source: `{source['path']}`",
        f"- Source SHA-256: `{source['sha256']}`",
        f"- Engine: `{transcription['engine']}`",
        f"- Model: `{model_label}`",
        f"- Language: `{transcription.get('detected_language') or transcription.get('requested_language') or 'auto'}`",
        "",
        "## Timestamped text",
        "",
    ]
    if not artifact["segments"]:
        lines.append("_No speech was detected._")
    else:
        for segment in artifact["segments"]:
            lines.append(
                f"[{format_time(segment['start_ms'])} --> {format_time(segment['end_ms'])}] "
                f"{segment['text']}"
            )
    lines.append("")
    return "\n".join(lines)


def transcript_text(segments: list[dict[str, Any]]) -> str:
    return "\n".join(segment["text"] for segment in segments) + ("\n" if segments else "")


def srt_clock(milliseconds: int) -> str:
    return format_time(milliseconds).replace(".", ",")


def transcript_srt(segments: list[dict[str, Any]]) -> str:
    blocks = [
        f"{index}\n{srt_clock(segment['start_ms'])} --> {srt_clock(segment['end_ms'])}\n{segment['text']}"
        for index, segment in enumerate(segments, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def transcript_vtt(segments: list[dict[str, Any]]) -> str:
    blocks = [
        f"{format_time(segment['start_ms'])} --> {format_time(segment['end_ms'])}\n{segment['text']}"
        for segment in segments
    ]
    body = "\n\n".join(blocks)
    return f"WEBVTT\n\n{body}{chr(10) if body else ''}"


def _require_exact_keys(
    value: Any,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    description: str,
    exit_code: int = 2,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TranscriptionError(
            "CONTRACT_VALIDATION_FAILED",
            f"{description} must be an object.",
            exit_code=exit_code,
        )
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing or unknown:
        raise TranscriptionError(
            "CONTRACT_VALIDATION_FAILED",
            f"{description} has missing or unknown fields.",
            details=json.dumps({"missing": missing, "unknown": unknown}),
            exit_code=exit_code,
        )
    return value


def validate_video_artifact(
    manifest_path: Path,
    media_path_value: Path,
    media: dict[str, Any],
    source_hash: str,
) -> tuple[dict[str, Any], str]:
    try:
        manifest_path = secure_input_file(manifest_path)
    except SafeRuntimeError as exc:
        raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
    metadata = os.lstat(manifest_path)
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise TranscriptionError(
            "UNSAFE_PATH",
            "Source video artifact must be a private mode-0600 owned file.",
            exit_code=2,
        )
    value, raw_hash = strict_json_file_with_sha256(
        manifest_path,
        maximum_bytes=JSON_LIMIT,
        description="source video artifact",
    )
    try:
        validate_contract(value, expected="video-artifact")
    except ContractError as exc:
        contract_code = getattr(exc, "code", "CONTRACT_VALIDATION_FAILED")
        raise TranscriptionError(
            contract_code,
            f"Source artifact failed schema validation at {getattr(exc, 'path', '$')}.",
            details=getattr(exc, "message", str(exc)),
            exit_code=7 if contract_code == "CONTRACT_BUILD_MISMATCH" else 2,
        ) from exc
    root = _require_exact_keys(
        value,
        required={
            "schema_version",
            "artifact_type",
            "status",
            "created_at",
            "source",
            "media",
            "acquisition",
            "producer",
        },
        description="source video artifact",
    )
    if (
        root.get("schema_version") != SCHEMA_VERSION
        or root.get("artifact_type") != "video"
        or root.get("status") != "complete"
    ):
        raise TranscriptionError(
            "UNSUPPORTED_SCHEMA_VERSION",
            "Transcription accepts only a complete video artifact/v2.",
            exit_code=2,
        )
    source = _require_exact_keys(
        root["source"],
        required={"platform", "fingerprint"},
        optional={"url", "webpage_url", "id", "title", "author", "extractor"},
        description="video artifact source",
    )
    media_record = _require_exact_keys(
        root["media"],
        required={
            "path",
            "bytes",
            "sha256",
            "duration_ms",
            "has_video",
            "has_audio",
            "container",
            "video_streams",
            "audio_streams",
            "ffprobe",
        },
        description="video artifact media",
    )
    acquisition = _require_exact_keys(
        root["acquisition"],
        required={"auth_mode", "fallback", "warnings"},
        description="video artifact acquisition",
    )
    producer = _require_exact_keys(
        root["producer"],
        required={"skill", "contract_digest", "tool", "version"},
        description="video artifact producer",
    )
    del acquisition
    if producer.get("skill") != "download-video":
        raise TranscriptionError(
            "CONTRACT_VALIDATION_FAILED",
            "Source artifact producer is not download-video.",
            exit_code=2,
        )
    if producer.get("contract_digest") != contract_digest():
        raise TranscriptionError(
            "CONTRACT_BUILD_MISMATCH",
            "Source artifact and transcribe-media use different contract builds.",
            exit_code=7,
        )
    try:
        artifact_media_path = secure_input_file(Path(str(media_record["path"])))
    except SafeRuntimeError as exc:
        raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
    if artifact_media_path != media_path_value:
        raise TranscriptionError(
            "SOURCE_ARTIFACT_MISMATCH",
            "The explicit source artifact points to a different media path.",
            exit_code=7,
        )
    expected = {
        "bytes": media["bytes"],
        "sha256": source_hash,
        "duration_ms": media["duration_ms"],
        "has_video": media["has_video"],
        "has_audio": media["has_audio"],
        "container": media["container"],
        "video_streams": sum(
            stream.get("codec_type") == "video"
            for stream in media["streams"]
            if isinstance(stream, dict)
        ),
        "audio_streams": sum(
            stream.get("codec_type") == "audio"
            for stream in media["streams"]
            if isinstance(stream, dict)
        ),
    }
    actual = {key: media_record.get(key) for key in expected}
    if actual != expected or media_record.get("has_video") is not True:
        raise TranscriptionError(
            "SOURCE_ARTIFACT_MISMATCH",
            "The source artifact does not match current media bytes or ffprobe evidence.",
            details=json.dumps({"expected": expected, "artifact": actual}),
            exit_code=7,
        )
    try:
        validate_file_context(
            value,
            verify_source=True,
            verify_outputs=False,
            verify_chunks=False,
        )
    except ContractError as exc:
        raise TranscriptionError(
            getattr(exc, "code", "SOURCE_ARTIFACT_MISMATCH"),
            getattr(exc, "message", "Source artifact file evidence is invalid."),
            exit_code=7,
        ) from exc
    return {
        "artifact_path": str(manifest_path),
        "artifact_sha256": raw_hash,
        "platform": source["platform"],
        "fingerprint": source["fingerprint"],
    }, raw_hash


def upstream_source(
    path: Path,
    source_hash: str,
    *,
    source_artifact: str | None = None,
    media: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Consume only the explicitly supplied video artifact.

    This compatibility wrapper intentionally never probes a neighbouring file.
    """

    if not source_artifact:
        return None, []
    if media is None:
        media = inspect_media(path)
    upstream, _ = validate_video_artifact(
        Path(source_artifact),
        path,
        media,
        source_hash,
    )
    return upstream, []


def _descriptor(path: Path) -> dict[str, Any]:
    try:
        metadata = validate_managed_file(path)
    except SafeRuntimeError as exc:
        raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
    return {"path": str(path), "bytes": metadata.st_size, "sha256": file_sha256(path)}


def _identity_still_matches(
    identity: dict[str, Any],
    *,
    engine: str | None = None,
) -> None:
    fields = () if engine == "sidecar-subtitle" else ("model", "executable", "adapter")
    for field in fields:
        recorded = identity.get(field)
        if recorded is None:
            continue
        if not isinstance(recorded, dict) or not isinstance(recorded.get("path"), str):
            raise TranscriptionError("IDENTITY_CHANGED", f"Invalid recorded {field} identity.", exit_code=7)
        try:
            current = content_identity(Path(recorded["path"]))
        except SafeRuntimeError as exc:
            raise TranscriptionError("IDENTITY_CHANGED", exc.message, exit_code=7) from exc
        comparable = {key: value for key, value in recorded.items() if key != "version"}
        if current != comparable:
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                f"The local {field} changed during transcription.",
                exit_code=7,
            )
    packages = identity.get("packages")
    if not isinstance(packages, list):
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "Engine package identity is malformed.",
            exit_code=7,
        )
    for package in packages:
        if (
            not isinstance(package, dict)
            or set(package) != {"name", "version"}
            or not isinstance(package.get("name"), str)
            or not isinstance(package.get("version"), str)
            or package_version(package["name"]) != package["version"]
        ):
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                "A local ASR package changed during transcription.",
                exit_code=7,
            )
    if identity.get("identity_sha256") != canonical_json_sha256(
        engine_identity_projection(identity)
    ):
        raise TranscriptionError("IDENTITY_CHANGED", "Engine identity digest is inconsistent.", exit_code=7)


def _validate_segments(
    segments: list[dict[str, Any]],
    *,
    duration_ms: int,
    chunk_count: int,
) -> None:
    previous_start = -1
    previous_end = -1
    for index, segment in enumerate(segments):
        allowed = {"start_ms", "end_ms", "text", "chunk_index", "avg_logprob"}
        required = {"start_ms", "end_ms", "text", "chunk_index"}
        if not isinstance(segment, dict) or not required.issubset(segment) or set(segment) - allowed:
            raise TranscriptionError("INVALID_TRANSCRIPT", f"Segment {index} is malformed.", exit_code=7)
        start = segment["start_ms"]
        end = segment["end_ms"]
        chunk_index = segment["chunk_index"]
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or isinstance(chunk_index, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not isinstance(chunk_index, int)
            or start < 0
            or end <= start
            or end > duration_ms
            or start < previous_start
            or end < previous_end
            or chunk_index < 0
            or chunk_index >= max(1, chunk_count)
            or not isinstance(segment["text"], str)
            or not segment["text"].strip()
        ):
            raise TranscriptionError(
                "INVALID_TRANSCRIPT",
                f"Segment {index} violates timestamp, text, or chunk bounds.",
                exit_code=7,
            )
        if "avg_logprob" in segment and (
            isinstance(segment["avg_logprob"], bool)
            or not isinstance(segment["avg_logprob"], (int, float))
            or not math.isfinite(float(segment["avg_logprob"]))
            or float(segment["avg_logprob"]) < -1000
            or float(segment["avg_logprob"]) > 0
        ):
            raise TranscriptionError("INVALID_TRANSCRIPT", f"Segment {index} has invalid confidence.", exit_code=7)
        previous_start, previous_end = start, end


def _read_state(path: Path, *, settings_sha256: str, job_id: str) -> dict[str, Any]:
    state = strict_json_file(path, maximum_bytes=STATE_JSON_LIMIT, description="transcription state")
    required = {
        "schema_version",
        "status",
        "job_id",
        "settings_sha256",
        "execution_guard_sha256",
        "settings",
        "chunk_set",
        "chunks",
    }
    _require_exact_keys(state, required=required, description="transcription state", exit_code=4)
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise TranscriptionError(
            "UNSUPPORTED_SCHEMA_VERSION",
            "Existing transcription state is not transcription-state/v1.",
            exit_code=4,
        )
    try:
        validate_contract(state, expected="transcription-state")
    except ContractError as exc:
        raise TranscriptionError(
            getattr(exc, "code", "STATE_CONFLICT"),
            f"Existing transcription state failed validation at {getattr(exc, 'path', '$')}.",
            details=getattr(exc, "message", str(exc)),
            exit_code=7
            if getattr(exc, "code", "") == "CONTRACT_BUILD_MISMATCH"
            else 4,
        ) from exc
    if state.get("job_id") != job_id or state.get("settings_sha256") != settings_sha256:
        raise TranscriptionError("STATE_CONFLICT", "Existing state belongs to another job.", exit_code=4)
    if canonical_json_sha256(
        transcription_settings_identity(state.get("settings"))
    ) != settings_sha256:
        raise TranscriptionError("STATE_CONFLICT", "Existing state settings digest is inconsistent.", exit_code=4)
    if state.get("status") not in {
        "running",
        "ready_to_publish",
        "complete",
    } or not isinstance(state.get("chunks"), dict):
        raise TranscriptionError("STATE_CONFLICT", "Existing transcription state is malformed.", exit_code=4)
    return state


def _write_state(
    path: Path,
    state: dict[str, Any],
    *,
    expected_previous: dict[str, Any],
) -> None:
    try:
        validate_contract(state, expected="transcription-state")
    except ContractError as exc:
        raise TranscriptionError(
            getattr(exc, "code", "CONTRACT_VALIDATION_FAILED"),
            f"Transcription state failed schema validation at {getattr(exc, 'path', '$')}.",
            details=getattr(exc, "message", str(exc)),
            exit_code=7,
        ) from exc
    atomic_json(
        path,
        state,
        expected_previous=expected_previous,
    )


def _write_new_state(path: Path, state: dict[str, Any]) -> None:
    try:
        validate_contract(state, expected="transcription-state")
        safe_atomic_json(path, state, replace=False)
    except ContractError as exc:
        raise TranscriptionError(
            getattr(exc, "code", "CONTRACT_VALIDATION_FAILED"),
            f"Transcription state failed schema validation at {getattr(exc, 'path', '$')}.",
            details=getattr(exc, "message", str(exc)),
            exit_code=7,
        ) from exc
    except SafeRuntimeError as exc:
        raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc


def _load_chunk_results(
    results_dir: Path,
    *,
    chunks: list[dict[str, Any]],
    media_duration_ms: int,
    job_id: str,
    settings_sha256: str,
    execution_guard_sha256: str,
    chunk_manifest_sha256: str,
) -> dict[str, dict[str, Any]]:
    expected = {
        f"{item['name']}.result.json": item
        for item in chunks
    }
    actual = {item.name for item in results_dir.iterdir()}
    if actual - set(expected):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Chunk result directory contains an unknown entry.",
            exit_code=4,
        )
    loaded: dict[str, dict[str, Any]] = {}
    for name in sorted(actual):
        chunk_info = expected[name]
        envelope = strict_json_file(
            results_dir / name,
            maximum_bytes=STATE_JSON_LIMIT,
            description="chunk result",
        )
        envelope = _require_exact_keys(
            envelope,
            required={
                "schema_version",
                "job_id",
                "settings_sha256",
                "execution_guard_sha256",
                "chunk_manifest_sha256",
                "chunk_name",
                "result",
            },
            description="chunk result envelope",
            exit_code=4,
        )
        if (
            envelope["schema_version"] != "awesome-capture.chunk-result/v1"
            or envelope["job_id"] != job_id
            or envelope["settings_sha256"] != settings_sha256
            or envelope["execution_guard_sha256"] != execution_guard_sha256
            or envelope["chunk_manifest_sha256"] != chunk_manifest_sha256
            or envelope["chunk_name"] != chunk_info["name"]
        ):
            raise TranscriptionError(
                "STATE_CONFLICT",
                f"Chunk result belongs to another transcription job: {chunk_info['name']}",
                exit_code=4,
            )
        record = envelope["result"]
        record = _validate_chunk_state_record(
            record,
            key=chunk_info["name"],
            duration_ms=media_duration_ms,
            chunk_count=len(chunks),
        )
        if {
            "chunk_sha256": record["chunk_sha256"],
            "offset_ms": record["offset_ms"],
            "duration_ms": record["duration_ms"],
        } != {
            "chunk_sha256": chunk_info["sha256"],
            "offset_ms": chunk_info["offset_ms"],
            "duration_ms": chunk_info["duration_ms"],
        }:
            raise TranscriptionError(
                "STATE_CONFLICT",
                f"Chunk result identity differs: {chunk_info['name']}",
                exit_code=4,
            )
        loaded[chunk_info["name"]] = record
    return loaded


def _write_chunk_result(
    results_dir: Path,
    *,
    chunk_info: dict[str, Any],
    record: dict[str, Any],
    media_duration_ms: int,
    chunk_count: int,
    job_id: str,
    settings_sha256: str,
    execution_guard_sha256: str,
    chunk_manifest_sha256: str,
) -> None:
    _validate_chunk_state_record(
        record,
        key=chunk_info["name"],
        duration_ms=media_duration_ms,
        chunk_count=chunk_count,
    )
    path = results_dir / f"{chunk_info['name']}.result.json"
    envelope = {
        "schema_version": "awesome-capture.chunk-result/v1",
        "job_id": job_id,
        "settings_sha256": settings_sha256,
        "execution_guard_sha256": execution_guard_sha256,
        "chunk_manifest_sha256": chunk_manifest_sha256,
        "chunk_name": chunk_info["name"],
        "result": record,
    }
    try:
        safe_atomic_json(path, envelope, replace=False)
    except SafeRuntimeError as exc:
        raise TranscriptionError(
            exc.code,
            exc.message,
            exit_code=exc.exit_code,
        ) from exc
    persisted = strict_json_file(
        path,
        maximum_bytes=STATE_JSON_LIMIT,
        description="chunk result",
    )
    if persisted != envelope:
        raise TranscriptionError(
            "INTEGRITY_ERROR",
            "Persisted chunk result differs from the verified engine output.",
            exit_code=7,
        )


def _publish_output_text(
    path: Path,
    value: str,
    *,
    allow_existing_match: bool,
) -> None:
    encoded = value.encode("utf-8")
    if path.exists() or path.is_symlink():
        if not allow_existing_match:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "An output path was occupied before this transaction published it.",
                exit_code=4,
            )
        try:
            metadata = validate_managed_file(path)
        except SafeRuntimeError as exc:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "A resumable output path is unsafe.",
                exit_code=4,
            ) from exc
        if (
            metadata.st_size != len(encoded)
            or file_sha256(path) != hashlib.sha256(encoded).hexdigest()
        ):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "A resumable output differs from its deterministic content.",
                exit_code=4,
            )
        return
    try:
        safe_atomic_text(path, value, replace=False)
    except SafeRuntimeError as exc:
        raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc


def _open_or_create_workspace(parent: Path, job_id: str) -> tuple[Path, bool]:
    parent_fd = -1
    workspace_fd = -1
    created = False
    try:
        parent_fd = open_directory_fd(parent)
        try:
            os.mkdir(job_id, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            created = False
        workspace_fd = os.open(
            job_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(workspace_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Transcription workspace is not a private owned directory.",
                exit_code=4,
            )
        if created:
            os.fsync(parent_fd)
        return parent / job_id, created
    except TranscriptionError:
        raise
    except OSError as exc:
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Transcription workspace could not be opened safely.",
            exit_code=4,
        ) from exc
    finally:
        if workspace_fd >= 0:
            os.close(workspace_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _validate_chunk_state_record(
    value: Any,
    *,
    key: str,
    duration_ms: int,
    chunk_count: int,
) -> dict[str, Any]:
    required = {
        "status",
        "language",
        "silent",
        "chunk_sha256",
        "offset_ms",
        "duration_ms",
        "raw_output_sha256",
        "runtime",
        "segments",
    }
    record = _require_exact_keys(
        value,
        required=required,
        description=f"chunk state {key}",
        exit_code=4,
    )
    if (
        record.get("status") != "complete"
        or (
            record.get("language") is not None
            and not isinstance(record.get("language"), str)
        )
        or not isinstance(record.get("silent"), bool)
        or not SHA256_RE.fullmatch(str(record.get("chunk_sha256") or ""))
        or isinstance(record.get("offset_ms"), bool)
        or not isinstance(record.get("offset_ms"), int)
        or record["offset_ms"] < 0
        or isinstance(record.get("duration_ms"), bool)
        or not isinstance(record.get("duration_ms"), int)
        or record["duration_ms"] <= 0
        or (
            record.get("raw_output_sha256") is not None
            and not SHA256_RE.fullmatch(str(record.get("raw_output_sha256")))
        )
    ):
        raise TranscriptionError("STATE_CONFLICT", f"Chunk state is invalid: {key}", exit_code=4)
    runtime = record.get("runtime")
    if runtime is not None:
        runtime_keys = {
            "device",
            "gpu_attempted",
            "gpu_fallback",
            "gpu_failure",
            "gpu_disabled_after_failure",
        }
        if (
            not isinstance(runtime, dict)
            or set(runtime) != runtime_keys
            or not isinstance(runtime.get("device"), str)
            or not all(
                isinstance(runtime.get(field), bool)
                for field in (
                    "gpu_attempted",
                    "gpu_fallback",
                    "gpu_disabled_after_failure",
                )
            )
            or (
                runtime.get("gpu_failure") is not None
                and not isinstance(runtime.get("gpu_failure"), str)
            )
        ):
            raise TranscriptionError(
                "STATE_CONFLICT",
                f"Chunk runtime is invalid: {key}",
                exit_code=4,
            )
    if not isinstance(record.get("segments"), list):
        raise TranscriptionError("STATE_CONFLICT", f"Chunk segments are invalid: {key}", exit_code=4)
    _validate_segments(
        record["segments"],
        duration_ms=duration_ms,
        chunk_count=chunk_count,
    )
    if record["silent"] and record["segments"]:
        raise TranscriptionError(
            "STATE_CONFLICT",
            f"Silent chunk has transcript segments: {key}",
            exit_code=4,
        )
    return record


def _validate_output_descriptors(
    artifact: dict[str, Any],
    *,
    skip_file_evidence: frozenset[str] = frozenset(),
) -> None:
    outputs = artifact.get("outputs")
    if not isinstance(outputs, dict):
        raise TranscriptionError("INVALID_TRANSCRIPT", "Transcript output descriptors are missing.", exit_code=7)
    for name in ("markdown", "text", "srt", "vtt", "state"):
        descriptor = outputs.get(name)
        if not isinstance(descriptor, dict) or set(descriptor) != {"path", "bytes", "sha256"}:
            raise TranscriptionError("INVALID_TRANSCRIPT", f"Output descriptor {name} is malformed.", exit_code=7)
        if name in skip_file_evidence:
            continue
        path = Path(str(descriptor["path"]))
        actual = _descriptor(path)
        if actual != descriptor:
            raise TranscriptionError("INTEGRITY_ERROR", f"Transcript output changed: {name}", exit_code=7)
    chunk_manifest = outputs.get("chunk_manifest")
    if chunk_manifest is not None:
        if not isinstance(chunk_manifest, dict):
            raise TranscriptionError(
                "INVALID_TRANSCRIPT",
                "Chunk manifest output descriptor is malformed.",
                exit_code=7,
            )
        if (
            "chunk_manifest" not in skip_file_evidence
            and _descriptor(Path(chunk_manifest["path"])) != chunk_manifest
        ):
            raise TranscriptionError("INTEGRITY_ERROR", "Chunk manifest output changed.", exit_code=7)


def _validate_artifact_workspace_paths(
    artifact: dict[str, Any],
    *,
    workspace: Path,
) -> None:
    source = artifact.get("source") or {}
    transcription = artifact.get("transcription") or {}
    outputs = artifact.get("outputs") or {}
    expected_outputs = {
        "markdown": workspace / "transcript.md",
        "text": workspace / "transcript.txt",
        "srt": workspace / "transcript.srt",
        "vtt": workspace / "transcript.vtt",
        "state": workspace / "state.json",
    }
    if (
        transcription.get("job_id") != workspace.name
        or Path(str(source.get("snapshot_path") or "")) != workspace / "source.snapshot"
    ):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Transcript artifact is not bound to its private workspace.",
            exit_code=4,
        )
    for name, expected_path in expected_outputs.items():
        descriptor = outputs.get(name)
        if (
            not isinstance(descriptor, dict)
            or Path(str(descriptor.get("path") or "")) != expected_path
        ):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                f"Transcript output {name} escapes its private workspace.",
                exit_code=4,
            )
    chunk_reference = transcription.get("chunk_set")
    chunk_descriptor = outputs.get("chunk_manifest")
    if chunk_reference is None:
        if chunk_descriptor is not None:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Sidecar transcript unexpectedly references a chunk manifest.",
                exit_code=4,
            )
    elif (
        not isinstance(chunk_reference, dict)
        or not isinstance(chunk_descriptor, dict)
        or Path(str(chunk_reference.get("manifest_path") or ""))
        != workspace / "chunks" / "chunks.manifest.json"
        or Path(str(chunk_descriptor.get("path") or ""))
        != workspace / "chunks" / "chunks.manifest.json"
    ):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Transcript chunk manifest escapes its private workspace.",
            exit_code=4,
        )


def _validate_completed_artifact(
    path: Path,
    *,
    job_id: str | None = None,
    source_sha256: str | None = None,
    workspace: Path | None = None,
    skip_output_file_evidence: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if workspace is not None:
        try:
            validate_managed_file(path)
        except SafeRuntimeError as exc:
            raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
    value = strict_json_file(path, maximum_bytes=STATE_JSON_LIMIT, description="transcript artifact")
    try:
        validate_contract(value, expected="transcript-artifact")
    except ContractError as exc:
        raise TranscriptionError(
            getattr(exc, "code", "CONTRACT_VALIDATION_FAILED"),
            f"Transcript artifact failed schema validation at {getattr(exc, 'path', '$')}.",
            details=getattr(exc, "message", str(exc)),
            exit_code=7,
        ) from exc
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("artifact_type") != "transcript"
        or value.get("status") != "complete"
    ):
        raise TranscriptionError("UNSUPPORTED_SCHEMA_VERSION", "Only transcript artifact/v2 is reusable.", exit_code=7)
    if job_id is not None and (value.get("transcription") or {}).get("job_id") != job_id:
        raise TranscriptionError("RECOVERY_CONFLICT", "Transcript artifact belongs to another job.", exit_code=4)
    if source_sha256 is not None and (value.get("source") or {}).get("sha256") != source_sha256:
        raise TranscriptionError("RECOVERY_CONFLICT", "Transcript artifact has another source identity.", exit_code=4)
    if workspace is not None:
        _validate_artifact_workspace_paths(value, workspace=workspace)
    _validate_segments(
        value.get("segments") or [],
        duration_ms=int((value.get("source") or {}).get("duration_ms") or -1),
        chunk_count=int(((value.get("transcription") or {}).get("chunk_set") or {}).get("count") or 1),
    )
    expected_text = "\n".join(segment["text"] for segment in value["segments"])
    if value.get("text") != expected_text or value.get("no_speech_detected") is not (not value["segments"]):
        raise TranscriptionError("INVALID_TRANSCRIPT", "Transcript text/no-speech fields are inconsistent.", exit_code=7)
    chunk_reference = (value.get("transcription") or {}).get("chunk_set")
    if chunk_reference is not None:
        if not isinstance(chunk_reference, dict):
            raise TranscriptionError("INVALID_TRANSCRIPT", "Transcript chunk-set reference is malformed.", exit_code=7)
        manifest_path = Path(str(chunk_reference.get("manifest_path") or ""))
        manifest = validate_chunk_set(
            manifest_path.parent,
            expected_job_id=(value.get("transcription") or {}).get("job_id"),
            expected_source_sha256=(value.get("source") or {}).get("sha256"),
        )
        if (
            manifest_path.name != "chunks.manifest.json"
            or file_sha256(manifest_path) != chunk_reference.get("manifest_sha256")
            or manifest.get("count") != chunk_reference.get("count")
        ):
            raise TranscriptionError(
                "CHUNK_SET_CONFLICT",
                "Transcript chunk-set reference does not match its complete manifest.",
                exit_code=7,
            )
    _validate_output_descriptors(
        value,
        skip_file_evidence=skip_output_file_evidence,
    )
    if not skip_output_file_evidence:
        try:
            validate_file_context(
                value,
                verify_source=False,
                verify_outputs=True,
                verify_chunks=True,
            )
        except ContractError as exc:
            raise TranscriptionError(
                getattr(exc, "code", "INTEGRITY_ERROR"),
                getattr(exc, "message", "Transcript file evidence is invalid."),
                exit_code=7,
            ) from exc
    return value


def _state_for_pending_artifact(
    artifact: dict[str, Any],
    *,
    workspace: Path,
    allowed_statuses: frozenset[str] = frozenset(
        {"ready_to_publish", "complete"}
    ),
) -> dict[str, Any]:
    state_path = workspace / "state.json"
    untrusted = strict_json_file(
        state_path,
        maximum_bytes=STATE_JSON_LIMIT,
        description="transcription state",
    )
    if not isinstance(untrusted, dict):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Pending transcript has no valid state object.",
            exit_code=4,
        )
    settings_sha256 = untrusted.get("settings_sha256")
    job_id = (artifact.get("transcription") or {}).get("job_id")
    if not isinstance(settings_sha256, str) or not isinstance(job_id, str):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Pending transcript state identity is malformed.",
            exit_code=4,
        )
    state = _read_state(
        state_path,
        settings_sha256=settings_sha256,
        job_id=job_id,
    )
    expected_job_id = hashlib.sha256(
        b"awesome-capture.transcription-job/v2\0"
        + settings_sha256.encode("ascii")
    ).hexdigest()
    source = artifact["source"]
    transcription = artifact["transcription"]
    settings = state["settings"]
    sidecar = source.get("sidecar")
    upstream = source.get("upstream")
    if (
        expected_job_id != job_id
        or transcription.get("settings_sha256") != settings_sha256
        or settings.get("algorithm") != transcription.get("algorithm")
        or state.get("execution_guard_sha256")
        != transcription.get("execution_guard_sha256")
        or state["status"] not in allowed_statuses
        or settings.get("source_path") != source.get("path")
        or settings.get("source_sha256") != source.get("sha256")
        or settings.get("source_bytes") != source.get("bytes")
        or settings.get("upstream_artifact_sha256")
        != (upstream or {}).get("artifact_sha256")
        or settings.get("engine") != transcription.get("engine")
        or settings.get("engine_identity") != transcription.get("engine_identity")
        or settings.get("requested_language")
        != transcription.get("requested_language")
        or settings.get("chunk_seconds") != transcription.get("chunk_seconds")
        or settings.get("whisper_cpp_cpu_only")
        != transcription.get("whisper_cpp_cpu_only")
        or settings.get("sidecar_sha256") != (sidecar or {}).get("sha256")
        or state.get("chunk_set") != transcription.get("chunk_set")
    ):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Pending transcript and transcription state identities differ.",
            exit_code=4,
        )
    if transcription["engine"] == "sidecar-subtitle":
        ordered_names = ["sidecar"]
        sidecar_state = state["chunks"].get("sidecar")
        if (
            not isinstance(sidecar_state, dict)
            or sidecar_state.get("chunk_sha256")
            != (source.get("sidecar") or {}).get("sha256")
            or sidecar_state.get("offset_ms") != 0
            or sidecar_state.get("duration_ms") != source.get("duration_ms")
        ):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Sidecar state does not match transcript source evidence.",
                exit_code=4,
            )
    else:
        chunk_set = transcription.get("chunk_set") or {}
        ordered_names = [
            f"chunk-{index:05d}.wav"
            for index in range(int(chunk_set.get("count") or 0))
        ]
        manifest_path = Path(str(chunk_set.get("manifest_path") or ""))
        manifest = validate_chunk_set(
            manifest_path.parent,
            expected_job_id=job_id,
            expected_source_sha256=source.get("sha256"),
        )
        for descriptor in manifest["chunks"]:
            chunk_state = state["chunks"].get(descriptor["name"])
            if (
                not isinstance(chunk_state, dict)
                or chunk_state.get("chunk_sha256") != descriptor["sha256"]
                or chunk_state.get("offset_ms") != descriptor["offset_ms"]
                or chunk_state.get("duration_ms") != descriptor["duration_ms"]
            ):
                raise TranscriptionError(
                    "RECOVERY_CONFLICT",
                    "Chunk state does not match the published chunk manifest.",
                    exit_code=4,
                )
    if set(state["chunks"]) != set(ordered_names):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Pending transcript state does not contain every expected chunk.",
            exit_code=4,
        )
    state_segments = [
        segment
        for name in ordered_names
        for segment in state["chunks"][name]["segments"]
    ]
    if state_segments != artifact.get("segments"):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Pending transcript text is not derived from its completed chunk state.",
            exit_code=4,
        )
    return state


def _validate_final_artifact_state(
    artifact: dict[str, Any],
    *,
    workspace: Path,
) -> None:
    state = _state_for_pending_artifact(artifact, workspace=workspace)
    if state["status"] != "complete":
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Final transcript exists without a complete transcription state.",
            exit_code=4,
        )


def _validate_publish_identity(
    artifact: dict[str, Any],
    *,
    workspace: Path,
) -> None:
    source = artifact["source"]
    if (
        transcription_algorithm_identity()
        != artifact["transcription"]["algorithm"]
    ):
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "The transcription implementation changed during the job.",
            exit_code=7,
        )
    try:
        validate_file_context(
            artifact,
            verify_source=True,
            verify_outputs=False,
            verify_chunks=False,
        )
    except ContractError as exc:
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "Private source snapshot changed before transcript publication.",
            exit_code=7,
        ) from exc
    try:
        snapshot_media = inspect_media(Path(source["snapshot_path"]))
    except TranscriptionError as exc:
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "Private source snapshot no longer has valid media evidence.",
            exit_code=7,
        ) from exc
    if (
        snapshot_media["bytes"] != source["bytes"]
        or snapshot_media["duration_ms"] != source["duration_ms"]
        or snapshot_media["has_audio"] != source["has_audio"]
        or snapshot_media["has_video"] != source["has_video"]
    ):
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "Private source snapshot media evidence changed.",
            exit_code=7,
        )
    sidecar = source.get("sidecar")
    if sidecar is not None:
        suffix = Path(sidecar["path"]).suffix.lower()
        if suffix not in {".srt", ".vtt"}:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Pending transcript sidecar identity is malformed.",
                exit_code=4,
            )
        snapshot = workspace / f"sidecar.snapshot{suffix}"
        actual = _descriptor(snapshot)
        if (
            actual["bytes"] != sidecar["bytes"]
            or actual["sha256"] != sidecar["sha256"]
        ):
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                "Private sidecar snapshot changed before transcript publication.",
                exit_code=7,
            )
    engine = artifact["transcription"]["engine"]
    identity = artifact["transcription"]["engine_identity"]
    _identity_still_matches(identity, engine=engine)
    source_snapshot = Path(source["snapshot_path"])
    sidecar_snapshot = (
        workspace / f"sidecar.snapshot{Path(sidecar['path']).suffix.lower()}"
        if sidecar is not None
        else None
    )
    if (
        execution_guard_for_run(
            engine,
            identity,
            source_snapshot=source_snapshot,
            sidecar_snapshot=sidecar_snapshot,
        )
        != artifact["transcription"]["execution_guard_sha256"]
    ):
        raise TranscriptionError(
            "IDENTITY_CHANGED",
            "A local ASR identity component changed during transcription.",
            exit_code=7,
        )


def _promote_pending_artifact(workspace: Path) -> dict[str, Any]:
    pending_path = workspace / "transcript.pending.json"
    transcript_path = workspace / "transcript.json"
    pending = _validate_completed_artifact(
        pending_path,
        job_id=workspace.name,
        workspace=workspace,
        skip_output_file_evidence=frozenset({"state"}),
    )
    state = _state_for_pending_artifact(pending, workspace=workspace)
    _validate_publish_identity(pending, workspace=workspace)
    if state["status"] == "ready_to_publish":
        completed_state = {**state, "status": "complete"}
        _write_state(
            workspace / "state.json",
            completed_state,
            expected_previous=state,
        )
        state = completed_state
        test_failpoint("transcribe.after-complete-state")
    updated_pending = json.loads(json.dumps(pending))
    updated_pending["outputs"]["state"] = _descriptor(workspace / "state.json")
    try:
        validate_contract(updated_pending, expected="transcript-artifact")
    except ContractError as exc:
        raise TranscriptionError(
            getattr(exc, "code", "CONTRACT_VALIDATION_FAILED"),
            f"Pending transcript failed validation at {getattr(exc, 'path', '$')}.",
            details=getattr(exc, "message", str(exc)),
            exit_code=7,
        ) from exc
    if updated_pending != pending:
        atomic_json(
            pending_path,
            updated_pending,
            expected_previous=pending,
        )
    pending = updated_pending
    test_failpoint("transcribe.after-pending-refresh")
    pending = _validate_completed_artifact(
        pending_path,
        job_id=workspace.name,
        source_sha256=pending["source"]["sha256"],
        workspace=workspace,
    )
    _state_for_pending_artifact(pending, workspace=workspace)
    _validate_publish_identity(pending, workspace=workspace)
    safe_atomic_json(transcript_path, pending, replace=False)
    test_failpoint("transcribe.after-artifact")
    artifact = _validate_completed_artifact(
        transcript_path,
        job_id=workspace.name,
        source_sha256=pending["source"]["sha256"],
        workspace=workspace,
    )
    _validate_final_artifact_state(artifact, workspace=workspace)
    return artifact


def _result_for_artifact(path: Path, artifact: dict[str, Any], *, result: str) -> dict[str, Any]:
    outputs = artifact["outputs"]
    return {
        "status": "ok",
        "operation": "transcribe",
        "result": result,
        "transcript_path": str(path),
        "markdown_path": outputs["markdown"]["path"],
        "text_path": outputs["text"]["path"],
        "srt_path": outputs["srt"]["path"],
        "vtt_path": outputs["vtt"]["path"],
        "state_path": outputs["state"]["path"],
        "segment_count": len(artifact["segments"]),
        "no_speech_detected": artifact["no_speech_detected"],
        "engine": artifact["transcription"]["engine"],
        "detected_language": artifact["transcription"]["detected_language"],
        "warnings": artifact["warnings"],
    }


def _quarantine_workspace_chunk_staging(workspace: Path) -> list[Path]:
    if (
        workspace.parent.name != "transcriptions"
        or workspace.parent.parent.name != MANAGED_LAYOUT_VERSION
        or workspace.name == ""
        or not SHA256_RE.fullmatch(workspace.name)
    ):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Transcription workspace is outside the managed v2 layout.",
            exit_code=4,
        )
    try:
        workspace_metadata = os.lstat(workspace)
    except OSError as exc:
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Transcription workspace disappeared during recovery.",
            exit_code=4,
        ) from exc
    if (
        not stat.S_ISDIR(workspace_metadata.st_mode)
        or stat.S_ISLNK(workspace_metadata.st_mode)
        or workspace_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(workspace_metadata.st_mode) != 0o700
    ):
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Transcription workspace is not a private current-user-owned directory.",
            exit_code=4,
        )
    quarantine_root = workspace.parent.parent / "quarantine"
    recovered: list[Path] = []
    for entry in sorted(workspace.iterdir(), key=lambda item: item.name):
        match = LEGACY_WORKSPACE_CHUNK_STAGING_RE.fullmatch(entry.name)
        if match is None:
            continue
        try:
            recovered.append(
                quarantine_private_directory(
                    entry,
                    quarantine_root,
                    target_name=(
                        f"transcribe-{workspace.name}-legacy-chunks."
                        f"{match.group(1)}"
                    ),
                )
            )
        except SafeRuntimeError as exc:
            raise TranscriptionError(
                exc.code,
                exc.message,
                exit_code=exc.exit_code,
            ) from exc
    return recovered


def _recover_workspace_atomic_staging(workspace: Path) -> list[Path]:
    quarantine_root = workspace.parent.parent / "quarantine"
    recovered: list[Path] = []
    for entry in sorted(workspace.iterdir(), key=lambda item: item.name):
        atomic_match = ATOMIC_WORKSPACE_STAGING_RE.fullmatch(entry.name)
        snapshot_match = SNAPSHOT_COPY_STAGING_RE.fullmatch(entry.name)
        if atomic_match is None and snapshot_match is None:
            continue
        try:
            metadata = os.lstat(entry)
        except OSError as exc:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Atomic staging entry changed during recovery.",
                exit_code=4,
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink not in {1, 2}
        ):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Atomic staging entry is not a provably private transaction file.",
                exit_code=4,
            )
        if atomic_match is not None:
            token = atomic_match.group(2)
            label = atomic_match.group(1)
        else:
            token = snapshot_match.group(1)
            label = "source-copy"
        if metadata.st_nlink == 2:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Legacy hard-linked atomic staging is not migrated by the strict runtime.",
                exit_code=4,
            )
        target_name = (
            f"transcribe-{workspace.name}-atomic-{token}-"
            f"{label.replace('.', '-')}"
        )
        try:
            recovered.append(
                quarantine_private_file(
                    entry,
                    quarantine_root,
                    target_name=target_name,
                )
            )
        except SafeRuntimeError as exc:
            raise TranscriptionError(
                exc.code,
                exc.message,
                exit_code=exc.exit_code,
            ) from exc
    return recovered


def _quarantine_global_chunk_staging(
    managed_root: Path,
    *,
    job_id: str,
) -> list[Path]:
    staging_root = secure_mkdirs(managed_root / "staging")
    quarantine_root = secure_mkdirs(managed_root / "quarantine")
    recovered: list[Path] = []
    for entry in sorted(staging_root.iterdir(), key=lambda item: item.name):
        match = CHUNK_STAGING_RE.fullmatch(entry.name)
        if match is None:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Managed staging contains an unknown entry.",
                exit_code=4,
            )
        try:
            metadata = os.lstat(entry)
        except OSError as exc:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Managed staging changed during recovery.",
                exit_code=4,
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Managed staging contains a non-private transaction entry.",
                exit_code=4,
            )
        if match.group(1) != job_id:
            continue
        try:
            recovered.append(
                quarantine_private_directory(
                    entry,
                    quarantine_root,
                )
            )
        except SafeRuntimeError as exc:
            raise TranscriptionError(
                exc.code,
                exc.message,
                exit_code=exc.exit_code,
            ) from exc
    return recovered


def _validate_workspace_entries(workspace: Path) -> None:
    allowed = {
        "source.snapshot",
        "sidecar.snapshot.srt",
        "sidecar.snapshot.vtt",
        "chunks",
        "chunk-results",
        "state.json",
        "transcript.pending.json",
        "transcript.json",
        "transcript.md",
        "transcript.txt",
        "transcript.srt",
        "transcript.vtt",
    }
    unknown = sorted(item.name for item in workspace.iterdir() if item.name not in allowed)
    if unknown:
        raise TranscriptionError(
            "RECOVERY_CONFLICT",
            "Workspace contains files that are not owned by the transcription transaction.",
            details=json.dumps({"unknown": unknown}),
            exit_code=4,
        )
    for item in workspace.iterdir():
        try:
            metadata = os.lstat(item)
        except OSError as exc:
            raise TranscriptionError("RECOVERY_CONFLICT", "Workspace changed during inspection.", exit_code=4) from exc
        if item.name in {"chunks", "chunk-results"}:
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise TranscriptionError("RECOVERY_CONFLICT", "chunks is not a safe directory.", exit_code=4)
            if item.name == "chunk-results":
                for result in item.iterdir():
                    result_metadata = os.lstat(result)
                    if (
                        not stat.S_ISREG(result_metadata.st_mode)
                        or stat.S_ISLNK(result_metadata.st_mode)
                        or result_metadata.st_uid != os.geteuid()
                        or result_metadata.st_nlink != 1
                        or stat.S_IMODE(result_metadata.st_mode) != 0o600
                    ):
                        raise TranscriptionError(
                            "RECOVERY_CONFLICT",
                            "Chunk result directory contains an unsafe entry.",
                            exit_code=4,
                        )
        elif (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise TranscriptionError("RECOVERY_CONFLICT", f"Unsafe workspace entry: {item.name}", exit_code=4)


def recover_workspace(workspace: Path) -> dict[str, Any]:
    _quarantine_workspace_chunk_staging(workspace)
    _recover_workspace_atomic_staging(workspace)
    _validate_workspace_entries(workspace)
    transcript_path = workspace / "transcript.json"
    pending_path = workspace / "transcript.pending.json"
    if transcript_path.exists() or transcript_path.is_symlink():
        artifact = _validate_completed_artifact(
            transcript_path,
            job_id=workspace.name,
            workspace=workspace,
        )
        _validate_final_artifact_state(artifact, workspace=workspace)
        return {"status": "complete", "transcript_path": str(transcript_path), "artifact": artifact}
    if pending_path.exists() or pending_path.is_symlink():
        pending = _validate_completed_artifact(
            pending_path,
            job_id=workspace.name,
            workspace=workspace,
            skip_output_file_evidence=frozenset({"state"}),
        )
        pending_state = _state_for_pending_artifact(
            pending,
            workspace=workspace,
            allowed_statuses=frozenset(
                {"running", "ready_to_publish", "complete"}
            ),
        )
        if pending_state["status"] == "running":
            _validate_completed_artifact(
                pending_path,
                job_id=workspace.name,
                source_sha256=pending["source"]["sha256"],
                workspace=workspace,
            )
            return {
                "status": "pending",
                "workspace": str(workspace),
            }
        artifact = _promote_pending_artifact(workspace)
        return {"status": "recovered", "transcript_path": str(transcript_path), "artifact": artifact}
    state_path = workspace / "state.json"
    if state_path.exists() or state_path.is_symlink():
        state = strict_json_file(state_path, maximum_bytes=STATE_JSON_LIMIT, description="transcription state")
        if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise TranscriptionError("RECOVERY_CONFLICT", "Workspace has unsupported state.", exit_code=4)
        return {"status": "pending", "workspace": str(workspace)}
    raise TranscriptionError(
        "RECOVERY_CONFLICT",
        "A pre-existing transcription workspace has no durable transaction marker.",
        exit_code=4,
    )


def recover_output(output_dir: str, *, lock_timeout: float = 30.0) -> dict[str, Any]:
    require_posix_security()
    root = secure_mkdirs(
        Path(output_dir).expanduser() / MANAGED_ROOT_NAME / MANAGED_LAYOUT_VERSION
    )
    transcriptions = secure_mkdirs(root / "transcriptions")
    locks = secure_mkdirs(root / "locks")
    staging = secure_mkdirs(root / "staging")
    secure_mkdirs(root / "quarantine")
    workspaces = sorted(transcriptions.iterdir(), key=lambda item: item.name)
    for workspace in workspaces:
        try:
            metadata = os.lstat(workspace)
        except OSError as exc:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Transcription root changed during recovery.",
                exit_code=4,
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Transcription root contains an unsafe workspace.",
                exit_code=4,
            )
    workspace_names = {workspace.name for workspace in workspaces}
    for entry in sorted(staging.iterdir(), key=lambda item: item.name):
        match = CHUNK_STAGING_RE.fullmatch(entry.name)
        if match is None or match.group(1) not in workspace_names:
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Managed staging contains an unknown transaction.",
                exit_code=4,
            )
    results: list[dict[str, Any]] = []
    for workspace in workspaces:
        if not re.fullmatch(r"[0-9a-f]{64}", workspace.name):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "Transcription root contains an unknown workspace.",
                exit_code=4,
            )
        with exclusive_lock(locks / f"transcribe-{workspace.name}.lock", timeout=lock_timeout):
            _quarantine_global_chunk_staging(
                root,
                job_id=workspace.name,
            )
            recovered = recover_workspace(workspace)
            results.append(
                {
                    "job_id": workspace.name,
                    "status": recovered["status"],
                    "transcript_path": recovered.get("transcript_path"),
                    "workspace": recovered.get("workspace"),
                }
            )
    return {"status": "ok", "operation": "recover", "workspaces": results}


def transcribe(args: argparse.Namespace) -> dict[str, Any]:
    require_posix_security()
    path = media_path(args.media)
    media = inspect_media(path)
    if not media["has_audio"]:
        raise TranscriptionError("NO_AUDIO_STREAM", "The media has no audio stream.", exit_code=2)
    source_hash = file_sha256(path)
    if os.lstat(path).st_size != media["bytes"]:
        raise TranscriptionError("IDENTITY_CHANGED", "Media changed during inspection.", exit_code=7)
    source_artifact_arg = getattr(args, "source_artifact", None)
    upstream, warnings = upstream_source(
        path,
        source_hash,
        source_artifact=source_artifact_arg,
        media=media,
    )
    sidecar = None if getattr(args, "ignore_sidecar", False) else exact_sidecar(path)
    whisper_cpp_bin = getattr(args, "whisper_cpp_bin", None)
    whisper_cpp_cpu_only = bool(getattr(args, "whisper_cpp_cpu_only", False))
    if sidecar:
        engine = "sidecar-subtitle"
        sidecar_identity = content_identity(sidecar)
        identity_core = {
            "model": None,
            "executable": None,
            "adapter": sidecar_identity,
            "packages": [],
        }
        engine_identity = {
            "identity_sha256": canonical_json_sha256(
                engine_identity_projection(identity_core)
            ),
            **identity_core,
        }
    else:
        if not getattr(args, "model", None):
            raise TranscriptionError(
                "MODEL_UNAVAILABLE",
                "Every ASR engine requires an explicit local --model.",
                exit_code=3,
            )
        engine = select_engine(
            args.engine,
            args.model,
            whisper_cpp_bin,
            timeout=min(args.timeout, 10),
        )
        engine_identity = engine_identity_for(
            engine,
            args.model,
            getattr(args, "adapter", None),
            whisper_cpp_bin,
            timeout=args.timeout,
            trust_external_adapter=bool(
                getattr(args, "trust_external_adapter", False)
            ),
        )
    settings = {
        "contract_digest": contract_digest(),
        "algorithm": transcription_algorithm_identity(),
        "source_path": str(path),
        "source_sha256": source_hash,
        "source_bytes": media["bytes"],
        "upstream_artifact_sha256": upstream["artifact_sha256"] if upstream else None,
        "engine": engine,
        "engine_identity": engine_identity,
        "requested_language": getattr(args, "language", None),
        "chunk_seconds": args.chunk_seconds,
        "whisper_cpp_cpu_only": whisper_cpp_cpu_only if engine == "whisper-cpp" else False,
        "sidecar_sha256": sidecar_identity["sha256"] if sidecar else None,
    }
    settings_sha256 = canonical_json_sha256(
        transcription_settings_identity(settings)
    )
    job_id = hashlib.sha256(
        b"awesome-capture.transcription-job/v2\0"
        + settings_sha256.encode("ascii")
    ).hexdigest()
    try:
        managed_root = secure_mkdirs(
            Path(args.output_dir).expanduser()
            / MANAGED_ROOT_NAME
            / MANAGED_LAYOUT_VERSION
        )
        locks_dir = secure_mkdirs(managed_root / "locks")
        transcriptions_dir = secure_mkdirs(managed_root / "transcriptions")
        secure_mkdirs(managed_root / "staging")
        secure_mkdirs(managed_root / "quarantine")
    except SafeRuntimeError as exc:
        raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
    lock_timeout = float(getattr(args, "lock_timeout", 30.0))
    with exclusive_lock(
        locks_dir / f"transcribe-{job_id}.lock",
        timeout=lock_timeout,
    ):
        _quarantine_global_chunk_staging(
            managed_root,
            job_id=job_id,
        )
        workspace, workspace_created = _open_or_create_workspace(
            transcriptions_dir,
            job_id,
        )
        if workspace_created and any(workspace.iterdir()):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "A newly-created transcription workspace was occupied concurrently.",
                exit_code=4,
            )
        state_path = workspace / "state.json"
        if not workspace_created:
            _quarantine_workspace_chunk_staging(workspace)
            _recover_workspace_atomic_staging(workspace)
        _validate_workspace_entries(workspace)
        transcript_path = workspace / "transcript.json"
        pending_path = workspace / "transcript.pending.json"
        markdown_path = workspace / "transcript.md"
        text_path = workspace / "transcript.txt"
        srt_path = workspace / "transcript.srt"
        vtt_path = workspace / "transcript.vtt"
        snapshot_path = workspace / "source.snapshot"
        if not workspace_created and not any(
            candidate.exists() or candidate.is_symlink()
            for candidate in (state_path, pending_path, transcript_path)
        ):
            raise TranscriptionError(
                "RECOVERY_CONFLICT",
                "A pre-existing transcription workspace has no durable transaction marker.",
                exit_code=4,
            )
        job_resuming = not workspace_created
        try:
            copy_private_snapshot(
                path,
                snapshot_path,
                expected_sha256=source_hash,
                expected_bytes=media["bytes"],
            )
        except SafeRuntimeError as exc:
            raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
        snapshot_media = inspect_media(snapshot_path)
        if (
            file_sha256(snapshot_path) != source_hash
            or snapshot_media["bytes"] != media["bytes"]
            or snapshot_media["duration_ms"] != media["duration_ms"]
            or snapshot_media["has_audio"] != media["has_audio"]
            or snapshot_media["has_video"] != media["has_video"]
        ):
            raise TranscriptionError("INTEGRITY_ERROR", "Private source snapshot failed revalidation.", exit_code=7)
        sidecar_snapshot: Path | None = None
        if sidecar:
            sidecar_snapshot = workspace / f"sidecar.snapshot{sidecar.suffix.lower()}"
            try:
                copy_private_snapshot(
                    sidecar,
                    sidecar_snapshot,
                    expected_sha256=sidecar_identity["sha256"],
                    expected_bytes=sidecar_identity["bytes"],
                )
            except SafeRuntimeError as exc:
                raise TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code) from exc
        execution_guard_sha256 = execution_guard_for_run(
            engine,
            engine_identity,
            source_snapshot=snapshot_path,
            sidecar_snapshot=sidecar_snapshot,
        )
        if transcript_path.exists() or transcript_path.is_symlink():
            _identity_still_matches(engine_identity, engine=engine)
            if file_sha256(path) != source_hash:
                raise TranscriptionError("IDENTITY_CHANGED", "Source media changed.", exit_code=7)
            artifact = _validate_completed_artifact(
                transcript_path,
                job_id=job_id,
                source_sha256=source_hash,
                workspace=workspace,
            )
            _validate_final_artifact_state(artifact, workspace=workspace)
            return _result_for_artifact(transcript_path, artifact, result="reused")
        if pending_path.exists() or pending_path.is_symlink():
            recovered = recover_workspace(workspace)
            artifact = recovered.get("artifact")
            if isinstance(artifact, dict):
                return _result_for_artifact(
                    Path(recovered["transcript_path"]),
                    artifact,
                    result="recovered",
                )
            pending = _validate_completed_artifact(
                pending_path,
                job_id=job_id,
                source_sha256=source_hash,
                workspace=workspace,
            )
            pending_state = _state_for_pending_artifact(
                pending,
                workspace=workspace,
                allowed_statuses=frozenset({"running"}),
            )
            ready_state = {
                **pending_state,
                "status": "ready_to_publish",
            }
            _write_state(
                state_path,
                ready_state,
                expected_previous=pending_state,
            )
            artifact = _promote_pending_artifact(workspace)
            return _result_for_artifact(
                transcript_path,
                artifact,
                result="created",
            )
        state: dict[str, Any] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "status": "running",
            "job_id": job_id,
            "settings_sha256": settings_sha256,
            "execution_guard_sha256": execution_guard_sha256,
            "settings": settings,
            "chunk_set": None,
            "chunks": {},
        }
        if state_path.exists() or state_path.is_symlink():
            state = _read_state(
                state_path,
                settings_sha256=settings_sha256,
                job_id=job_id,
            )
            if state.get("execution_guard_sha256") != execution_guard_sha256:
                raise TranscriptionError(
                    "IDENTITY_CHANGED",
                    "A local ASR identity component changed while the job was resumable.",
                    exit_code=7,
                )
            if state["status"] != "running":
                raise TranscriptionError(
                    "RECOVERY_CONFLICT",
                    "A non-running state without its pending artifact cannot be resumed.",
                    exit_code=4,
                )
        else:
            _write_new_state(state_path, state)
        persisted_state = json.loads(json.dumps(state))
        detected_languages: list[str] = []
        if sidecar_snapshot:
            segments = parse_sidecar(sidecar_snapshot)
            state["chunks"] = {
                "sidecar": {
                    "status": "complete",
                    "language": getattr(args, "language", None),
                    "silent": not bool(segments),
                    "chunk_sha256": sidecar_identity["sha256"],
                    "offset_ms": 0,
                    "duration_ms": media["duration_ms"],
                    "raw_output_sha256": sidecar_identity["sha256"],
                    "runtime": None,
                    "segments": segments,
                }
            }
            chunk_manifest = None
            chunk_reference = None
        else:
            chunks_dir = workspace / "chunks"
            normalize_chunks(
                snapshot_path,
                chunks_dir,
                args.chunk_seconds,
                args.timeout,
                job_id=job_id,
                source_sha256=source_hash,
                expected_duration_ms=media["duration_ms"],
            )
            chunk_manifest = validate_chunk_set(
                chunks_dir,
                expected_job_id=job_id,
                expected_source_sha256=source_hash,
            )
            manifest_path = chunks_dir / "chunks.manifest.json"
            chunk_reference = {
                "manifest_path": str(manifest_path),
                "manifest_sha256": file_sha256(manifest_path),
                "count": chunk_manifest["count"],
                "timeline": [
                    {
                        "index": item["index"],
                        "offset_ms": item["offset_ms"],
                        "duration_ms": item["duration_ms"],
                        "bytes": item["bytes"],
                        "sha256": item["sha256"],
                    }
                    for item in chunk_manifest["chunks"]
                ],
            }
            if state.get("chunk_set") not in (None, chunk_reference):
                raise TranscriptionError("STATE_CONFLICT", "State references another chunk set.", exit_code=4)
            state["chunk_set"] = chunk_reference
            expected_chunk_keys = {item["name"] for item in chunk_manifest["chunks"]}
            if set(state["chunks"]) - expected_chunk_keys:
                raise TranscriptionError("STATE_CONFLICT", "State contains an unknown chunk result.", exit_code=4)
            try:
                results_dir = secure_mkdirs(workspace / "chunk-results")
            except SafeRuntimeError as exc:
                raise TranscriptionError(
                    exc.code,
                    exc.message,
                    exit_code=exc.exit_code,
                ) from exc
            persisted_results = _load_chunk_results(
                results_dir,
                chunks=chunk_manifest["chunks"],
                media_duration_ms=media["duration_ms"],
                job_id=job_id,
                settings_sha256=settings_sha256,
                execution_guard_sha256=execution_guard_sha256,
                chunk_manifest_sha256=chunk_reference["manifest_sha256"],
            )
            for key, record in persisted_results.items():
                existing_record = state["chunks"].get(key)
                if existing_record is not None and existing_record != record:
                    raise TranscriptionError(
                        "STATE_CONFLICT",
                        f"State and immutable chunk result differ: {key}",
                        exit_code=4,
                    )
                state["chunks"][key] = record
            whisper_cpp_gpu_previously_failed = engine == "whisper-cpp" and any(
                isinstance(chunk_state, dict)
                and isinstance(chunk_state.get("runtime"), dict)
                and (
                    chunk_state["runtime"].get("gpu_fallback")
                    or chunk_state["runtime"].get("gpu_disabled_after_failure")
                )
                for chunk_state in state["chunks"].values()
            )
            run_engine = runner_for(
                engine,
                args.model,
                getattr(args, "language", None),
                getattr(args, "adapter", None),
                args.timeout,
                engine_identity=engine_identity,
                whisper_cpp_cpu_only=whisper_cpp_cpu_only,
                whisper_cpp_gpu_previously_failed=whisper_cpp_gpu_previously_failed,
            )
            for chunk_info in chunk_manifest["chunks"]:
                key = chunk_info["name"]
                existing = state["chunks"].get(key)
                expected_identity = {
                    "chunk_sha256": chunk_info["sha256"],
                    "offset_ms": chunk_info["offset_ms"],
                    "duration_ms": chunk_info["duration_ms"],
                }
                if existing is not None:
                    existing = _validate_chunk_state_record(
                        existing,
                        key=key,
                        duration_ms=media["duration_ms"],
                        chunk_count=chunk_manifest["count"],
                    )
                    if {field: existing.get(field) for field in expected_identity} != expected_identity:
                        raise TranscriptionError("STATE_CONFLICT", f"Chunk state no longer matches: {key}", exit_code=4)
                    if existing.get("language"):
                        detected_languages.append(existing["language"])
                    continue
                chunk = chunks_dir / key
                if not chunk_has_signal(chunk):
                    result = {
                        "segments": [],
                        "language": getattr(args, "language", None),
                        "silent": True,
                    }
                else:
                    result = run_engine(chunk)
                normalized = normalize_engine_segments(
                    result.get("segments"),
                    chunk_index=chunk_info["index"],
                    offset_ms=chunk_info["offset_ms"],
                    chunk_duration_ms=chunk_info["duration_ms"],
                )
                derived_silent = not bool(normalized)
                reported_silent = result.get("silent")
                if reported_silent is not None and (
                    not isinstance(reported_silent, bool)
                    or reported_silent is not derived_silent
                ):
                    raise TranscriptionError(
                        "INVALID_ENGINE_OUTPUT",
                        "ASR silent status contradicts its normalized segments.",
                        exit_code=5,
                    )
                chunk_record = {
                    "status": "complete",
                    "language": result.get("language"),
                    "silent": derived_silent,
                    **expected_identity,
                    "raw_output_sha256": result.get("raw_output_sha256"),
                    "runtime": result.get("runtime"),
                    "segments": normalized,
                }
                _write_chunk_result(
                    results_dir,
                    chunk_info=chunk_info,
                    record=chunk_record,
                    media_duration_ms=media["duration_ms"],
                    chunk_count=chunk_manifest["count"],
                    job_id=job_id,
                    settings_sha256=settings_sha256,
                    execution_guard_sha256=execution_guard_sha256,
                    chunk_manifest_sha256=chunk_reference["manifest_sha256"],
                )
                state["chunks"][key] = chunk_record
                if result.get("language"):
                    detected_languages.append(result["language"])
            if set(state["chunks"]) != expected_chunk_keys:
                raise TranscriptionError(
                    "INCOMPLETE_TRANSCRIPTION",
                    "Every manifest chunk must have one complete result.",
                    exit_code=7,
                )
            for key in sorted(expected_chunk_keys):
                _validate_chunk_state_record(
                    state["chunks"][key],
                    key=key,
                    duration_ms=media["duration_ms"],
                    chunk_count=chunk_manifest["count"],
                )
            revalidated_results = _load_chunk_results(
                results_dir,
                chunks=chunk_manifest["chunks"],
                media_duration_ms=media["duration_ms"],
                job_id=job_id,
                settings_sha256=settings_sha256,
                execution_guard_sha256=execution_guard_sha256,
                chunk_manifest_sha256=chunk_reference["manifest_sha256"],
            )
            if revalidated_results != state["chunks"]:
                raise TranscriptionError(
                    "STATE_CONFLICT",
                    "Final state and immutable chunk result set differ.",
                    exit_code=4,
                )
            segments = [
                segment
                for item in chunk_manifest["chunks"]
                for segment in state["chunks"][item["name"]]["segments"]
            ]
        chunk_count = chunk_reference["count"] if chunk_reference else 1
        _validate_segments(
            segments,
            duration_ms=media["duration_ms"],
            chunk_count=chunk_count,
        )
        runtimes = [
            item["runtime"]
            for item in state["chunks"].values()
            if isinstance(item, dict) and isinstance(item.get("runtime"), dict)
        ]
        devices_used = sorted(
            {
                runtime["device"]
                for runtime in runtimes
                if isinstance(runtime.get("device"), str) and runtime["device"]
            }
        )
        gpu_fallback_count = sum(bool(runtime.get("gpu_fallback")) for runtime in runtimes)
        if gpu_fallback_count:
            warnings.append(
                f"whisper.cpp GPU failed for {gpu_fallback_count} chunk(s); CPU retry succeeded."
            )
        detected_language = (
            max(set(detected_languages), key=detected_languages.count)
            if detected_languages
            else getattr(args, "language", None)
        )
        source_sidecar = (
            {
                "path": str(sidecar),
                "sha256": sidecar_identity["sha256"],
                "bytes": sidecar_identity["bytes"],
            }
            if sidecar
            else None
        )
        artifact_seed: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "artifact_type": "transcript",
            "status": "complete",
            "created_at": dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "source": {
                "path": str(path),
                "snapshot_path": str(snapshot_path),
                "sha256": source_hash,
                "bytes": media["bytes"],
                "duration_ms": media["duration_ms"],
                "has_audio": media["has_audio"],
                "has_video": media["has_video"],
                "upstream": upstream,
                "sidecar": source_sidecar,
            },
            "transcription": {
                "job_id": job_id,
                "settings_sha256": settings_sha256,
                "algorithm": settings["algorithm"],
                "execution_guard_sha256": execution_guard_sha256,
                "engine": engine,
                "engine_identity": engine_identity,
                "requested_language": getattr(args, "language", None),
                "detected_language": detected_language,
                "chunk_seconds": args.chunk_seconds,
                "whisper_cpp_cpu_only": (
                    whisper_cpp_cpu_only if engine == "whisper-cpp" else False
                ),
                "chunk_set": chunk_reference,
                "devices_used": devices_used,
                "gpu_fallback_count": gpu_fallback_count,
            },
            "segments": segments,
            "text": "\n".join(segment["text"] for segment in segments),
            "no_speech_detected": not segments,
            "outputs": {},
            "warnings": warnings,
            "producer": {
                "skill": "transcribe-media",
                "contract_digest": contract_digest(),
            },
        }
        _publish_output_text(
            markdown_path,
            transcript_markdown(artifact_seed),
            allow_existing_match=job_resuming,
        )
        _publish_output_text(
            text_path,
            transcript_text(segments),
            allow_existing_match=job_resuming,
        )
        _publish_output_text(
            srt_path,
            transcript_srt(segments),
            allow_existing_match=job_resuming,
        )
        _publish_output_text(
            vtt_path,
            transcript_vtt(segments),
            allow_existing_match=job_resuming,
        )
        _identity_still_matches(engine_identity, engine=engine)
        if (
            execution_guard_for_run(
                engine,
                engine_identity,
                source_snapshot=snapshot_path,
                sidecar_snapshot=sidecar_snapshot,
            )
            != execution_guard_sha256
        ):
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                "A local ASR identity component changed during transcription.",
                exit_code=7,
            )
        if (
            os.lstat(snapshot_path).st_size != media["bytes"]
            or file_sha256(snapshot_path) != source_hash
        ):
            raise TranscriptionError(
                "IDENTITY_CHANGED",
                "Private source snapshot changed before transcript publication.",
                exit_code=7,
            )
        if state != persisted_state:
            _write_state(
                state_path,
                state,
                expected_previous=persisted_state,
            )
            persisted_state = json.loads(json.dumps(state))
        artifact_seed["outputs"] = {
            "markdown": _descriptor(markdown_path),
            "text": _descriptor(text_path),
            "srt": _descriptor(srt_path),
            "vtt": _descriptor(vtt_path),
            "state": _descriptor(state_path),
            "chunk_manifest": _descriptor(workspace / "chunks" / "chunks.manifest.json")
            if chunk_reference
            else None,
        }
        _validate_segments(
            artifact_seed["segments"],
            duration_ms=media["duration_ms"],
            chunk_count=chunk_count,
        )
        try:
            validate_contract(artifact_seed, expected="transcript-artifact")
        except ContractError as exc:
            raise TranscriptionError(
                getattr(exc, "code", "CONTRACT_VALIDATION_FAILED"),
                f"Generated transcript failed schema validation at {getattr(exc, 'path', '$')}.",
                details=getattr(exc, "message", str(exc)),
                exit_code=7,
            ) from exc
        try:
            safe_atomic_json(pending_path, artifact_seed, replace=False)
        except SafeRuntimeError as exc:
            raise TranscriptionError(
                exc.code,
                exc.message,
                exit_code=exc.exit_code,
            ) from exc
        test_failpoint("transcribe.after-pending")
        _validate_completed_artifact(
            pending_path,
            job_id=job_id,
            source_sha256=source_hash,
            workspace=workspace,
        )
        ready_state = {
            **state,
            "status": "ready_to_publish",
        }
        _write_state(
            state_path,
            ready_state,
            expected_previous=persisted_state,
        )
        test_failpoint("transcribe.after-ready")
        artifact = _promote_pending_artifact(workspace)
        return _result_for_artifact(transcript_path, artifact, result="created")


def doctor(args: argparse.Namespace) -> dict[str, Any]:
    tools = {name: {"available": bool(shutil.which(name)), "path": shutil.which(name) or ""} for name in ("ffmpeg", "ffprobe")}
    whisper_cpp_details: dict[str, Any]
    try:
        whisper_cpp_details = {
            "available": True,
            **probe_whisper_cpp(getattr(args, "whisper_cpp_bin", None)),
        }
    except TranscriptionError as exc:
        whisper_cpp_details = {
            "available": False,
            "error": exc.as_dict()["error"],
        }
    model = local_model_path(getattr(args, "model", None))
    whisper_cpp_details["model_ready"] = model is not None
    whisper_cpp_details["model_path"] = str(model) if model else None
    engines = {
        "whisper-cpp": bool(whisper_cpp_details["available"]),
        "faster-whisper": module_available("faster_whisper"),
        "mlx-whisper": module_available("mlx_whisper"),
        "external": True,
    }
    compatible_mlx = platform.system() == "Darwin" and platform.machine() == "arm64"
    auto_engine = None
    try:
        auto_engine = select_engine(
            "auto",
            getattr(args, "model", None),
            getattr(args, "whisper_cpp_bin", None),
        )
    except TranscriptionError:
        pass
    ready_for_inspection = all(item["available"] for item in tools.values())
    return {
        "status": "ok" if ready_for_inspection else "error",
        "operation": "doctor",
        "ready_for_inspection": ready_for_inspection,
        "ready_for_asr": ready_for_inspection and auto_engine is not None,
        "tools": tools,
        "engines": engines,
        "whisper_cpp": whisper_cpp_details,
        "platform": {"system": platform.system(), "machine": platform.machine(), "mlx_compatible": compatible_mlx},
        "auto_engine": auto_engine,
        "auto_policy": "verified whisper.cpp with an explicit local model file only",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--model")
    doctor_parser.add_argument("--whisper-cpp-bin")
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("media")
    transcribe_parser = subparsers.add_parser("transcribe")
    transcribe_parser.add_argument("media")
    transcribe_parser.add_argument("--output-dir", required=True)
    transcribe_parser.add_argument(
        "--source-artifact",
        help="Explicit completed video artifact/v2. Adjacent files are never guessed.",
    )
    transcribe_parser.add_argument(
        "--engine",
        choices=("auto", "whisper-cpp", "faster-whisper", "mlx-whisper", "external"),
        default="auto",
    )
    transcribe_parser.add_argument("--model")
    transcribe_parser.add_argument("--language")
    transcribe_parser.add_argument("--adapter")
    transcribe_parser.add_argument(
        "--trust-external-adapter",
        action="store_true",
        help="Acknowledge that --adapter is trusted local executable code.",
    )
    transcribe_parser.add_argument("--whisper-cpp-bin")
    transcribe_parser.add_argument("--whisper-cpp-cpu-only", action="store_true")
    transcribe_parser.add_argument("--chunk-seconds", type=int, default=600)
    transcribe_parser.add_argument("--timeout", type=int, default=3600)
    transcribe_parser.add_argument("--ignore-sidecar", action="store_true")
    transcribe_parser.add_argument("--lock-timeout", type=float, default=30.0)
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--output-dir", required=True)
    recover_parser.add_argument("--lock-timeout", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if any(item in {"-h", "--help"} for item in actual_argv):
            json_print(
                {
                    "status": "ok",
                    "operation": "help",
                    "commands": ["doctor", "inspect", "transcribe", "recover"],
                }
            )
            return 0
        args = build_parser().parse_args(actual_argv)
        try:
            contract_digest()
        except ContractError as exc:
            raise TranscriptionError(
                "CONTRACT_BUILD_MISMATCH",
                "The vendored contract bundle failed its integrity check.",
                exit_code=7,
            ) from exc
        if args.command == "doctor":
            result = doctor(args)
            if result["status"] != "ok":
                missing = sorted(
                    name
                    for name, details in result["tools"].items()
                    if not details["available"]
                )
                raise TranscriptionError(
                    "DEPENDENCY_MISSING",
                    "Required inspection tools are unavailable.",
                    details=json.dumps({"missing": missing}),
                    exit_code=3,
                )
        elif args.command == "inspect":
            require_posix_security()
            path = media_path(args.media)
            result = {"status": "ok", "operation": "inspect", "media": inspect_media(path)}
        elif args.command == "recover":
            if not math.isfinite(args.lock_timeout) or args.lock_timeout < 0:
                raise TranscriptionError(
                    "INVALID_ARGUMENT",
                    "--lock-timeout must be a finite non-negative number.",
                    exit_code=2,
                )
            result = recover_output(
                args.output_dir,
                lock_timeout=args.lock_timeout,
            )
        else:
            if args.chunk_seconds < 30:
                raise TranscriptionError("INVALID_ARGUMENT", "--chunk-seconds must be at least 30.", exit_code=2)
            if not math.isfinite(args.lock_timeout) or args.lock_timeout < 0:
                raise TranscriptionError(
                    "INVALID_ARGUMENT",
                    "--lock-timeout must be a finite non-negative number.",
                    exit_code=2,
                )
            result = transcribe(args)
        json_print(result)
        return 0
    except TranscriptionError as exc:
        json_print(exc.as_dict(), stream=sys.stderr)
        return exc.exit_code
    except SafeRuntimeError as exc:
        wrapped = TranscriptionError(exc.code, exc.message, exit_code=exc.exit_code)
        json_print(wrapped.as_dict(), stream=sys.stderr)
        return wrapped.exit_code
    except KeyboardInterrupt:
        json_print(
            TranscriptionError(
                "INTERRUPTED",
                "Transcription was interrupted.",
                exit_code=130,
            ).as_dict(),
            stream=sys.stderr,
        )
        return 130
    except Exception as exc:
        wrapped = TranscriptionError(
            "RUNTIME_FAILED",
            "The transcription operation failed unexpectedly.",
            details=exc.__class__.__name__,
            exit_code=5,
        )
        json_print(wrapped.as_dict(), stream=sys.stderr)
        return wrapped.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
