#!/usr/bin/env python3
"""Validate a transcript and atomically ingest a structured note into Obsidian."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _contracts.contract_runtime import (  # noqa: E402
    ContractError,
    canonical_json_sha256,
    contract_digest,
    read_json_strict,
    validate_contract,
    validate_file_context,
)
from _contracts.posix_runtime import (  # noqa: E402
    PosixRuntimeError,
    file_evidence,
    file_sha256 as secure_file_sha256,
    read_regular_file,
    require_posix as require_contract_posix,
    test_failpoint,
)
from _contracts.vault_runtime import (  # noqa: E402
    VaultRuntimeError,
    create_transaction_directory as runtime_create_transaction_directory,
    ensure_directory_path as runtime_ensure_directory_path,
    ensure_relative_directory as runtime_ensure_relative_directory,
    fsync_directory as runtime_fsync_directory,
    open_root as runtime_open_root,
    publish_relative as runtime_publish_relative,
    quarantine_completed_transaction as runtime_quarantine_completed_transaction,
    sha256_descriptor as runtime_sha256_descriptor,
    vault_lock as runtime_vault_lock,
    walk_directory as runtime_walk_directory,
    write_new_file as runtime_write_new_file,
)


SCHEMA_VERSION = "awesome-capture.artifact/v2"
RECEIPT_SCHEMA = "awesome-capture.ingest-receipt/v1"
BUILD_RECEIPT_SCHEMA = "awesome-capture.vault-build-receipt/v1"
TRANSACTION_SCHEMA = "awesome-capture.transaction/v1"
INGEST_ID_SCHEMA = "awesome-capture.ingest-id/v1"
VAULT_ID_SCHEMA = "awesome-capture.vault-id/v1"
DEFAULT_LOCK_TIMEOUT = 30.0


class IngestError(RuntimeError):
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
        raise IngestError("INVALID_ARGUMENT", message, exit_code=2)


def json_print(value: Any, *, stream: Any = None) -> None:
    import sys

    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        file=stream or sys.stdout,
    )


def require_posix() -> None:
    try:
        require_contract_posix()
    except PosixRuntimeError as exc:
        raise IngestError(
            "UNSUPPORTED_PLATFORM",
            exc.message,
            exit_code=3,
        ) from exc


def reject_nonfinite(value: str) -> Any:
    raise ValueError(f"Non-finite number: {value}")


def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path, *, maximum_bytes: int = 64 * 1024 * 1024) -> dict[str, Any]:
    require_posix()
    try:
        value = read_json_strict(
            path,
            validate=False,
            maximum_bytes=maximum_bytes,
        )
    except ContractError as exc:
        code = "INPUT_NOT_FOUND" if exc.code == "JSON_NOT_READABLE" else (
            "UNSAFE_INPUT" if exc.code == "UNSAFE_JSON_FILE" else "INVALID_JSON"
        )
        raise IngestError(code, "Input JSON could not be read safely.", exit_code=2) from exc
    if not isinstance(value, dict):
        raise IngestError("INVALID_JSON", "Expected one JSON object.", exit_code=2)
    if len(canonical_json(value)) > maximum_bytes:
        raise IngestError("INVALID_JSON", "JSON input exceeds the size limit.", exit_code=2)
    return value


def read_transcript_artifact(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise IngestError(
            "INPUT_NOT_FOUND",
            "Transcript artifact is unavailable.",
            exit_code=2,
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise IngestError(
            "UNSAFE_INPUT",
            "Transcript artifact must be a private mode-0600 owned file.",
            exit_code=2,
        )
    return read_json(path)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _runtime_error(exc: VaultRuntimeError) -> IngestError:
    code = "INVALID_DESTINATION" if exc.code == "UNSAFE_PATH" else exc.code
    return IngestError(
        code,
        exc.message,
        exit_code=exc.exit_code,
    )


def fsync_directory(path: Path) -> None:
    try:
        runtime_fsync_directory(path)
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def ensure_directory(path: Path, *, mode: int) -> None:
    try:
        runtime_ensure_directory_path(path, mode=mode)
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def _open_root(path: Path) -> int:
    try:
        return runtime_open_root(path)
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def _vault_root_identity(path: Path) -> tuple[int, int]:
    descriptor = _open_root(path)
    try:
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def _walk_directory(
    root: Path,
    relative: Path,
    *,
    create: bool,
    final_mode: int = 0o700,
) -> int:
    try:
        safe = safe_relative_dir(relative.as_posix(), "managed directory")
        return runtime_walk_directory(
            root,
            safe,
            create=create,
            final_mode=final_mode,
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def ensure_relative_directory(root: Path, relative: Path, *, mode: int) -> None:
    try:
        runtime_ensure_relative_directory(
            root,
            safe_relative_dir(relative.as_posix(), "managed directory"),
            mode=mode,
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def _sha256_descriptor(descriptor: int) -> str:
    return runtime_sha256_descriptor(descriptor)


def publish_relative(
    staged: Path,
    vault: Path,
    relative: Path,
    expected_hash: str,
    *,
    mode: int,
) -> None:
    try:
        runtime_publish_relative(
            staged,
            vault,
            relative,
            expected_hash,
            mode=mode,
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


@contextlib.contextmanager
def vault_lock(
    vault: Path,
    *,
    exclusive: bool,
    timeout: float,
    create: bool,
):
    try:
        with runtime_vault_lock(
            vault,
            exclusive=exclusive,
            timeout=timeout,
            create=create,
        ) as descriptor:
            yield descriptor
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def _legacy_validate_transcript(value: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    top_keys = {
        "schema_version",
        "artifact_type",
        "status",
        "created_at",
        "source",
        "transcription",
        "segments",
        "text",
        "no_speech_detected",
        "outputs",
        "warnings",
        "producer",
    }
    if set(value) != top_keys:
        errors.append("top-level keys do not match transcript artifact/v2")
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if value.get("artifact_type") != "transcript":
        errors.append("artifact_type must be transcript")
    if value.get("status") != "complete":
        errors.append("status must be complete")
    source = value.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
        source = {}
    source_keys = {
        "path",
        "snapshot_path",
        "sha256",
        "bytes",
        "duration_ms",
        "has_audio",
        "has_video",
        "upstream",
        "sidecar",
    }
    if set(source) != source_keys:
        errors.append("source keys do not match transcript artifact/v2")
    source_hash = str(source.get("sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        errors.append("source.sha256 must be a lowercase SHA-256 hex digest")
    source_bytes = source.get("bytes")
    media_duration_ms = source.get("duration_ms")
    if isinstance(source_bytes, bool) or not isinstance(source_bytes, int) or source_bytes <= 0:
        errors.append("source.bytes must be a positive integer")
    if isinstance(media_duration_ms, bool) or not isinstance(media_duration_ms, int) or media_duration_ms <= 0:
        errors.append("source.duration_ms must be a positive integer")
        media_duration_ms = None
    for key in ("path", "snapshot_path"):
        candidate = source.get(key)
        if not isinstance(candidate, str) or not candidate.startswith("/") or "\x00" in candidate:
            errors.append(f"source.{key} must be an absolute POSIX path")
    if source.get("has_audio") is not True or not isinstance(source.get("has_video"), bool):
        errors.append("source stream flags are invalid")
    upstream = source.get("upstream")
    if upstream is not None:
        if not isinstance(upstream, dict) or set(upstream) != {
            "artifact_path",
            "artifact_sha256",
            "platform",
            "fingerprint",
        }:
            errors.append("source.upstream is invalid")
        else:
            if not re.fullmatch(r"[0-9a-f]{64}", str(upstream.get("artifact_sha256") or "")):
                errors.append("source.upstream.artifact_sha256 must be SHA-256")
            if not str(upstream.get("artifact_path") or "").startswith("/"):
                errors.append("source.upstream.artifact_path must be absolute")
    sidecar = source.get("sidecar")
    if sidecar is not None:
        if not isinstance(sidecar, dict) or set(sidecar) != {"path", "sha256", "bytes"}:
            errors.append("source.sidecar is invalid")
        elif not re.fullmatch(r"[0-9a-f]{64}", str(sidecar.get("sha256") or "")):
            errors.append("source.sidecar.sha256 must be SHA-256")
    transcription = value.get("transcription")
    if not isinstance(transcription, dict):
        errors.append("transcription must be an object")
        transcription = {}
    transcription_keys = {
        "job_id",
        "engine",
        "engine_identity",
        "requested_language",
        "detected_language",
        "chunk_seconds",
        "chunk_set",
        "devices_used",
        "gpu_fallback_count",
    }
    if set(transcription) != transcription_keys:
        errors.append("transcription keys do not match transcript artifact/v2")
    if transcription.get("engine") not in {
        "sidecar-subtitle",
        "whisper-cpp",
        "faster-whisper",
        "mlx-whisper",
        "external",
    }:
        errors.append("transcription.engine is unsupported")
    identity = transcription.get("engine_identity")
    if not isinstance(identity, dict):
        errors.append("transcription.engine_identity must be an object")
    elif transcription.get("engine") != "sidecar-subtitle":
        identity_text = canonical_json(identity).decode("utf-8")
        if "model_sha256" not in identity_text and "tree_sha256" not in identity_text:
            errors.append("ASR engine identity lacks a content-level model digest")
    chunk_set = transcription.get("chunk_set")
    if chunk_set is not None:
        if not isinstance(chunk_set, dict) or set(chunk_set) != {
            "manifest_path",
            "manifest_sha256",
            "count",
        }:
            errors.append("transcription.chunk_set is invalid")
        else:
            if not re.fullmatch(r"[0-9a-f]{64}", str(chunk_set.get("manifest_sha256") or "")):
                errors.append("transcription.chunk_set.manifest_sha256 must be SHA-256")
            if (
                isinstance(chunk_set.get("count"), bool)
                or not isinstance(chunk_set.get("count"), int)
                or chunk_set["count"] <= 0
            ):
                errors.append("transcription.chunk_set.count must be positive")
    segments = value.get("segments")
    if not isinstance(segments, list):
        errors.append("segments must be an array")
        segments = []
    previous_start = -1
    nonempty_text: list[str] = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            errors.append(f"segments[{index}] must be an object")
            continue
        allowed_segment_keys = {"start_ms", "end_ms", "text", "chunk_index", "avg_logprob"}
        if not {"start_ms", "end_ms", "text", "chunk_index"}.issubset(segment) or set(segment) - allowed_segment_keys:
            errors.append(f"segments[{index}] keys are invalid")
        start, end = segment.get("start_ms"), segment.get("end_ms")
        chunk_index = segment.get("chunk_index")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end < start
        ):
            errors.append(f"segments[{index}] has invalid timestamps")
        elif start < previous_start:
            errors.append(f"segments[{index}] is out of order")
        else:
            previous_start = start
            if media_duration_ms is not None and end > media_duration_ms + 2000:
                errors.append(f"segments[{index}] exceeds media duration by more than 2 seconds")
        if (
            isinstance(chunk_index, bool)
            or not isinstance(chunk_index, int)
            or chunk_index < 0
        ):
            errors.append(f"segments[{index}].chunk_index is invalid")
        text = str(segment.get("text") or "").strip()
        if not text:
            errors.append(f"segments[{index}] has empty text")
        else:
            nonempty_text.append(text)
    claimed_text = value.get("text")
    if not isinstance(claimed_text, str):
        errors.append("text must be a string")
        claimed_text = ""
    if nonempty_text and not claimed_text:
        errors.append("text is empty although segments contain speech")
    elif not nonempty_text and claimed_text:
        errors.append("text claims speech although segments are empty")
    elif nonempty_text:
        expected_text = "\n".join(nonempty_text)
        if claimed_text != expected_text:
            errors.append("text does not match the ordered segment text")
    if value.get("no_speech_detected") is not (not nonempty_text):
        errors.append("no_speech_detected is inconsistent with segments")
    outputs = value.get("outputs")
    expected_outputs = {"markdown", "text", "srt", "vtt", "state", "chunk_manifest"}
    if not isinstance(outputs, dict) or set(outputs) != expected_outputs:
        errors.append("outputs keys do not match transcript artifact/v2")
    else:
        for name, descriptor in outputs.items():
            if name == "chunk_manifest" and descriptor is None:
                continue
            if not isinstance(descriptor, dict) or set(descriptor) != {"path", "bytes", "sha256"}:
                errors.append(f"outputs.{name} descriptor is invalid")
                continue
            if not str(descriptor.get("path") or "").startswith("/"):
                errors.append(f"outputs.{name}.path must be absolute")
            if (
                isinstance(descriptor.get("bytes"), bool)
                or not isinstance(descriptor.get("bytes"), int)
                or descriptor["bytes"] < 0
            ):
                errors.append(f"outputs.{name}.bytes is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", str(descriptor.get("sha256") or "")):
                errors.append(f"outputs.{name}.sha256 must be SHA-256")
    warnings = value.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        errors.append("warnings must be an array of strings")
    producer = value.get("producer")
    if (
        not isinstance(producer, dict)
        or set(producer) != {"skill", "contract_digest"}
        or producer.get("skill") != "transcribe-media"
        or not re.fullmatch(r"[0-9a-f]{64}", str(producer.get("contract_digest") or ""))
    ):
        errors.append("producer identity is invalid")
    if errors:
        raise IngestError(
            "INVALID_TRANSCRIPT",
            "Transcript artifact validation failed.",
            details="\n".join(errors),
            exit_code=2,
        )
    artifact_sha256 = hashlib.sha256(canonical_json(value)).hexdigest()
    semantic_value = {
        "source": source,
        "transcription": transcription,
        "segments": segments,
        "text": claimed_text,
        "no_speech_detected": value["no_speech_detected"],
    }
    return {
        "schema_version": value["schema_version"],
        "source_sha256": source_hash,
        "artifact_sha256": artifact_sha256,
        "semantic_sha256": hashlib.sha256(canonical_json(semantic_value)).hexdigest(),
        "segment_count": len(segments),
        "has_speech": bool(nonempty_text),
    }


def contract_failure(
    exc: ContractError,
    *,
    receipt: bool = False,
) -> IngestError:
    if exc.code == "CONTRACT_BUILD_MISMATCH":
        return IngestError(
            exc.code,
            exc.message,
            details=exc.path,
            exit_code=7,
        )
    return IngestError(
        "BROKEN_RECEIPT" if receipt else "INVALID_TRANSCRIPT",
        "Receipt validation failed." if receipt else "Transcript artifact validation failed.",
        details=f"{exc.code}:{exc.path}:{exc.message}",
        exit_code=4 if receipt else 2,
    )


def validate_transcript(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise IngestError(
            "UNSUPPORTED_SCHEMA_VERSION",
            f"Transcript artifact must use {SCHEMA_VERSION}; legacy artifacts are not migrated.",
            exit_code=2,
        )
    try:
        validate_contract(value, expected="transcript-artifact")
    except ContractError as exc:
        raise contract_failure(exc) from exc
    semantic_value = {
        "source": value["source"],
        "transcription": value["transcription"],
        "segments": value["segments"],
        "text": value["text"],
        "no_speech_detected": value["no_speech_detected"],
    }
    return {
        "schema_version": value["schema_version"],
        "source_sha256": value["source"]["sha256"],
        "artifact_sha256": canonical_json_sha256(value),
        "semantic_sha256": canonical_json_sha256(semantic_value),
        "segment_count": len(value["segments"]),
        "has_speech": bool(value["segments"]),
    }


def resolve_vault(raw: str, *, allow_plain_folder: bool) -> Path:
    require_posix()
    expanded = Path(raw).expanduser().absolute()
    if ".." in expanded.parts:
        raise IngestError("UNSAFE_VAULT_TARGET", "Vault target contains parent traversal.", exit_code=2)
    if not expanded.exists() and not expanded.is_symlink():
        raise IngestError("VAULT_NOT_FOUND", f"Vault directory does not exist: {expanded}", exit_code=2)
    try:
        descriptor = _open_root(expanded)
    except IngestError as exc:
        raise IngestError(
            "UNSAFE_VAULT_TARGET",
            "Vault root ownership, type, or permissions are unsafe.",
            exit_code=2,
        ) from exc
    os.close(descriptor)
    if expanded in {Path(expanded.anchor), Path.home().expanduser().absolute()} or expanded.name == ".obsidian":
        raise IngestError(
            "UNSAFE_VAULT_TARGET",
            f"Refusing a filesystem root, home directory, or .obsidian directory as a vault: {expanded}",
            exit_code=2,
        )
    if not allow_plain_folder and not (expanded / ".obsidian").is_dir():
        raise IngestError(
            "NOT_AN_OBSIDIAN_VAULT",
            f"Missing .obsidian directory in: {expanded}",
            details="Use $build-obsidian-vault to create a vault or pass --allow-plain-folder explicitly.",
            exit_code=2,
        )
    return expanded


def safe_relative_dir(raw: str, label: str) -> Path:
    value = Path(raw)
    unsafe = re.compile(r'[\x00-\x1f\x7f<>:"\\|?*#^%\[\]]')
    if (
        value.is_absolute()
        or not value.parts
        or any(part in {"", ".", ".."} for part in value.parts)
        or unsafe.search(raw)
    ):
        raise IngestError("INVALID_DESTINATION", f"{label} must be a safe relative directory.", exit_code=2)
    return value


def is_within(path: Path, root: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def safe_filename(value: str, *, maximum: int = 100) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*#^%\[\]]+', " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    if not normalized:
        normalized = "Untitled"
    return normalized[:maximum].rstrip(" .") or "Untitled"


def strip_frontmatter(value: str) -> str:
    if not value.startswith("---\n"):
        return value.strip()
    end = value.find("\n---", 4)
    if end < 0:
        raise IngestError("INVALID_DOCUMENT", "Draft has an unterminated frontmatter block.", exit_code=2)
    return value[end + 4 :].lstrip("\n").strip()


def display_title(value: str) -> str:
    title = " ".join(unicodedata.normalize("NFC", value).split()).strip()
    if not title or len(title) > 200 or any(character in title for character in "\0\r\n"):
        raise IngestError("INVALID_TITLE", "Title is empty, too long, or unsafe.", exit_code=2)
    return title


def validate_document(
    value: str,
    *,
    transcript_has_speech: bool,
    expected_title: str,
) -> str:
    body = strip_frontmatter(value)
    if len(body) < 40:
        raise IngestError("INVALID_DOCUMENT", "Structured note is too short to be useful.", exit_code=2)
    headings = list(re.finditer(r"(?m)^#\s+\S.*$", body))
    if len(headings) != 1:
        raise IngestError("INVALID_DOCUMENT", "Structured note must contain exactly one level-1 title.", exit_code=2)
    if len(re.findall(r"(?m)^##\s+\S", body)) < 2:
        raise IngestError("INVALID_DOCUMENT", "Structured note must contain at least two level-2 sections.", exit_code=2)
    if transcript_has_speech and not re.search(r"\[\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}", body):
        raise IngestError(
            "MISSING_EVIDENCE",
            "A non-empty transcript note must cite at least one timestamp.",
            exit_code=2,
        )
    heading = headings[0]
    return f"{body[:heading.start()]}# {expected_title}{body[heading.end():]}"


def yaml_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def normalize_tags(values: list[str]) -> list[str]:
    tags: list[str] = []
    for raw in values:
        tag = re.sub(r"[\s#]+", "-", unicodedata.normalize("NFKC", raw).strip().lower())
        tag = re.sub(r"[^0-9a-zA-Z_\-/\u4e00-\u9fff]+", "-", tag).strip("-/")
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def timestamp(milliseconds: int) -> str:
    seconds, ms = divmod(max(0, milliseconds), 1000)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}.{ms:03d}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def vault_link(
    target_relative: Path,
    *,
    from_relative: Path,
    link_style: str,
    label: str,
) -> str:
    target = target_relative.as_posix()
    if link_style == "wikilink":
        return f"[[{target_relative.with_suffix('').as_posix()}|{label}]]"
    relative = posixpath.relpath(target, start=from_relative.parent.as_posix())
    return f"[{label}]({quote(relative, safe='/.-_~')})"


def source_markdown(transcript: dict[str, Any], title: str, uid: str) -> str:
    source = transcript["source"]
    created = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    lines = [
        "---",
        f"title: {yaml_scalar(f'{title} — 原始转写')}",
        f"created: {yaml_scalar(created)}",
        'type: "source-transcript"',
        f"awesome_capture_id: {yaml_scalar(uid)}",
        f"source_path: {yaml_scalar(str(source.get('path') or ''))}",
        f"source_sha256: {yaml_scalar(str(source.get('sha256') or ''))}",
        "tags:",
        '  - "source/transcript"',
        "---",
        "",
        f"# {title} — 原始转写",
        "",
        f"- 原始文件：`{source.get('path') or ''}`",
        f"- SHA-256：`{source.get('sha256') or ''}`",
        "",
        "## 带时间戳转写",
        "",
    ]
    segments = transcript.get("segments") or []
    if not segments:
        lines.append("_未检测到可转写语音。_")
    else:
        for segment in segments:
            lines.append(
                f"[{timestamp(segment['start_ms'])}–{timestamp(segment['end_ms'])}] {segment['text']}"
            )
    lines.append("")
    return "\n".join(lines)


def knowledge_markdown(
    body: str,
    *,
    title: str,
    uid: str,
    transcript: dict[str, Any],
    tags: list[str],
    source_relative: Path,
    note_relative: Path,
    link_style: str,
) -> str:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    source = transcript["source"]
    upstream = source.get("upstream") if isinstance(source.get("upstream"), dict) else {}
    source_link = vault_link(
        source_relative,
        from_relative=note_relative,
        link_style=link_style,
        label="原始转写",
    )
    lines = [
        "---",
        f"title: {yaml_scalar(title)}",
        f"created: {yaml_scalar(now)}",
        f"updated: {yaml_scalar(now)}",
        'type: "knowledge"',
        'status: "captured"',
        f"awesome_capture_id: {yaml_scalar(uid)}",
        f"source_sha256: {yaml_scalar(str(source.get('sha256') or ''))}",
        f"source_path: {yaml_scalar(str(source.get('path') or ''))}",
    ]
    if upstream.get("url"):
        lines.append(f"source_url: {yaml_scalar(str(upstream['url']))}")
    lines.extend(
        [
            f"source_note: {yaml_scalar(source_link)}",
            "tags:",
            *[f"  - {yaml_scalar(tag)}" for tag in tags],
            "---",
            "",
            body,
            "",
            "## 来源",
            "",
            f"- 原始转写：{source_link}",
            "",
        ]
    )
    return "\n".join(lines)


def fsync_write(path: Path, value: str) -> None:
    try:
        runtime_write_new_file(path, value, mode=0o600)
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def receipt_id(artifact_sha256: str) -> str:
    return hashlib.sha256(f"{INGEST_ID_SCHEMA}\0{artifact_sha256}".encode("utf-8")).hexdigest()


def vault_identity(vault: Path) -> str:
    return hashlib.sha256(
        VAULT_ID_SCHEMA.encode("utf-8") + b"\0" + os.fsencode(str(vault))
    ).hexdigest()


def _legacy_validate_receipt(value: dict[str, Any], *, expected_id: str | None = None) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "created_at",
        "ingest_id",
        "transcript",
        "request",
        "files",
        "source_verification",
    }
    if value.get("schema_version") != RECEIPT_SCHEMA:
        raise IngestError(
            "UNSUPPORTED_RECEIPT_SCHEMA",
            f"Ingest receipt must use {RECEIPT_SCHEMA}.",
            exit_code=4,
        )
    if set(value) != expected_keys:
        raise IngestError("BROKEN_RECEIPT", "Receipt keys do not match the schema.", exit_code=4)
    ingest_id = value.get("ingest_id")
    transcript = value.get("transcript")
    request = value.get("request")
    files = value.get("files")
    if not isinstance(ingest_id, str) or not re.fullmatch(r"[0-9a-f]{64}", ingest_id):
        raise IngestError("BROKEN_RECEIPT", "Receipt ingest_id is invalid.", exit_code=4)
    if expected_id is not None and ingest_id != expected_id:
        raise IngestError("BROKEN_RECEIPT", "Receipt identity does not match its path.", exit_code=4)
    if not isinstance(transcript, dict) or set(transcript) != {
        "schema_version",
        "artifact_sha256",
        "semantic_sha256",
        "source_sha256",
        "segment_count",
    }:
        raise IngestError("BROKEN_RECEIPT", "Receipt transcript identity is invalid.", exit_code=4)
    if receipt_id(str(transcript.get("artifact_sha256") or "")) != ingest_id:
        raise IngestError("BROKEN_RECEIPT", "Receipt stable identity is inconsistent.", exit_code=4)
    for key in ("artifact_sha256", "semantic_sha256", "source_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(transcript.get(key) or "")):
            raise IngestError("BROKEN_RECEIPT", f"Receipt transcript.{key} is invalid.", exit_code=4)
    if not isinstance(request, dict) or set(request) != {
        "draft_sha256",
        "title",
        "collection",
        "sources_dir",
        "link_style",
        "tags",
        "plan_sha256",
    }:
        raise IngestError("BROKEN_RECEIPT", "Receipt request identity is invalid.", exit_code=4)
    if not isinstance(files, list) or len(files) != 2:
        raise IngestError("BROKEN_RECEIPT", "Receipt files are invalid.", exit_code=4)
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"kind", "path", "bytes", "sha256"}
            or item["kind"] not in {"knowledge", "source"}
            or not isinstance(item["path"], str)
            or not isinstance(item["bytes"], int)
            or item["bytes"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"]))
        ):
            raise IngestError("BROKEN_RECEIPT", "Receipt file entry is invalid.", exit_code=4)
        safe_relative_dir(str(Path(item["path"]).parent), "receipt file parent")
    if value.get("source_verification") not in {"not_checked", "not_available", "verified"}:
        raise IngestError("BROKEN_RECEIPT", "Receipt source verification is invalid.", exit_code=4)
    return value


def validate_receipt(value: dict[str, Any], *, expected_id: str | None = None) -> dict[str, Any]:
    if value.get("schema_version") != RECEIPT_SCHEMA:
        raise IngestError(
            "UNSUPPORTED_RECEIPT_SCHEMA",
            f"Ingest receipt must use {RECEIPT_SCHEMA}; legacy receipts are not migrated.",
            exit_code=4,
        )
    try:
        validate_contract(value, expected="ingest-receipt")
    except ContractError as exc:
        raise contract_failure(exc, receipt=True) from exc
    if expected_id is not None and value["ingest_id"] != expected_id:
        raise IngestError(
            "BROKEN_RECEIPT",
            "Receipt identity does not match its filename.",
            exit_code=4,
        )
    return value


def identity_present(path: Path, ingest_id: str, source_sha256: str) -> bool:
    try:
        text = read_regular_file(path, 64 * 1024 * 1024).decode("utf-8")
    except (OSError, UnicodeError, PosixRuntimeError):
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return False
    values: dict[str, list[Any]] = {
        "awesome_capture_id": [],
        "source_sha256": [],
    }
    for line in lines[1:end]:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):\s*(.+)", line)
        if match is None or match.group(1) not in values:
            continue
        try:
            parsed = json.loads(match.group(2))
        except json.JSONDecodeError:
            return False
        values[match.group(1)].append(parsed)
    return values == {
        "awesome_capture_id": [ingest_id],
        "source_sha256": [source_sha256],
    }


def note_identity(path: Path) -> tuple[str, str] | None:
    """Return formal ingest identity from YAML frontmatter, if present."""

    try:
        text = read_regular_file(path, 64 * 1024 * 1024).decode("utf-8")
    except (OSError, UnicodeError, PosixRuntimeError):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return None
    values: dict[str, list[Any]] = {
        "awesome_capture_id": [],
        "source_sha256": [],
    }
    owned_field_seen = False
    for line in lines[1:end]:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):\s*(.+)", line)
        if match is None or match.group(1) not in values:
            continue
        owned_field_seen = True
        try:
            parsed = json.loads(match.group(2))
        except json.JSONDecodeError as exc:
            raise IngestError(
                "BROKEN_NOTE_IDENTITY",
                "Managed note identity frontmatter is malformed.",
                exit_code=4,
            ) from exc
        values[match.group(1)].append(parsed)
    if not owned_field_seen:
        return None
    ingest_ids = values["awesome_capture_id"]
    source_hashes = values["source_sha256"]
    if (
        len(ingest_ids) != 1
        or len(source_hashes) != 1
        or not isinstance(ingest_ids[0], str)
        or not isinstance(source_hashes[0], str)
        or not re.fullmatch(r"[0-9a-f]{64}", ingest_ids[0])
        or not re.fullmatch(r"[0-9a-f]{64}", source_hashes[0])
    ):
        raise IngestError(
            "BROKEN_NOTE_IDENTITY",
            "Managed note identity frontmatter is incomplete or invalid.",
            exit_code=4,
        )
    return ingest_ids[0], source_hashes[0]


def managed_note_identities(
    vault: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Discover receipt-owned Markdown without following directory symlinks."""

    notes: list[dict[str, str]] = []
    findings: list[dict[str, str]] = []
    for directory, names, filenames in os.walk(vault, followlinks=False):
        current = Path(directory)
        safe_names: list[str] = []
        for name in sorted(names):
            candidate = current / name
            if candidate == vault / ".awesome-capture":
                continue
            try:
                metadata = candidate.lstat()
            except OSError:
                continue
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                safe_names.append(name)
        names[:] = safe_names
        for filename in sorted(filenames):
            if not filename.endswith(".md"):
                continue
            path = current / filename
            relative = path.relative_to(vault).as_posix()
            try:
                identity = note_identity(path)
            except IngestError as exc:
                findings.append(
                    {
                        "severity": "error",
                        "code": exc.code,
                        "path": relative,
                    }
                )
                continue
            if identity is not None:
                notes.append(
                    {
                        "path": relative,
                        "ingest_id": identity[0],
                        "source_sha256": identity[1],
                    }
                )
    return notes, findings


def _plan_file_state(
    vault: Path,
    relative: Path,
    *,
    ingest_id: str | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    candidate = vault / relative
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError:
        return {"state": "absent"}
    if stat.S_ISLNK(metadata.st_mode):
        return {"state": "unsafe", "kind": "symlink"}
    if stat.S_ISREG(metadata.st_mode):
        if (
            ingest_id is not None
            and source_sha256 is not None
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o644
            and identity_present(candidate, ingest_id, source_sha256)
        ):
            return {"state": "managed-note"}
        try:
            digest = secure_file_sha256(candidate)
        except PosixRuntimeError:
            return {
                "state": "unsafe",
                "kind": "regular",
                "mode": stat.S_IMODE(metadata.st_mode),
                "links": metadata.st_nlink,
            }
        return {
            "state": "regular",
            "bytes": metadata.st_size,
            "mode": stat.S_IMODE(metadata.st_mode),
            "sha256": digest,
        }
    if stat.S_ISDIR(metadata.st_mode):
        return {
            "state": "directory",
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    return {
        "state": "unsafe",
        "kind": "special",
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def ingest_plan_state(
    vault: Path,
    *,
    note_relative: Path,
    source_relative: Path,
    receipt_relative: Path,
    ingest_id: str,
    source_sha256: str,
    request_sha256: str,
) -> dict[str, Any]:
    transactions = vault / ".awesome-capture" / "transactions"
    try:
        transaction_metadata = os.lstat(transactions)
    except FileNotFoundError:
        transaction_state: dict[str, Any] = {"state": "clean"}
    else:
        if (
            stat.S_ISLNK(transaction_metadata.st_mode)
            or not stat.S_ISDIR(transaction_metadata.st_mode)
        ):
            transaction_state = {"state": "unsafe"}
        else:
            try:
                entries = sorted(item.name for item in transactions.iterdir())
            except OSError:
                transaction_state = {"state": "unsafe"}
            else:
                transaction_state = {
                    "state": "clean" if not entries else "pending",
                    "entries": entries,
                }
    receipt_state = _plan_file_state(vault, receipt_relative)
    matching_receipt = False
    if receipt_state["state"] == "absent":
        receipt_state = {"state": "available"}
    elif receipt_state["state"] == "regular":
        try:
            receipt_value = validate_receipt(
                read_json(
                    vault / receipt_relative,
                    maximum_bytes=4 * 1024 * 1024,
                ),
                expected_id=ingest_id,
            )
        except IngestError:
            pass
        else:
            if receipt_value["request_sha256"] == request_sha256:
                receipt_state = {"state": "available"}
                matching_receipt = True
    knowledge_state = _plan_file_state(
        vault,
        note_relative,
        ingest_id=ingest_id,
        source_sha256=source_sha256,
    )
    source_state = _plan_file_state(
        vault,
        source_relative,
        ingest_id=ingest_id,
        source_sha256=source_sha256,
    )
    for target_state in (knowledge_state, source_state):
        if target_state["state"] == "absent" or (
            matching_receipt and target_state["state"] == "managed-note"
        ):
            target_state.clear()
            target_state["state"] = "available"
    return {
        "knowledge_note": knowledge_state,
        "source_note": source_state,
        "ingest_receipt": receipt_state,
        "build_receipt": _plan_file_state(
            vault,
            Path(".awesome-capture/vault-build.json"),
        ),
        "pending_transactions": transaction_state,
    }


def existing_receipt(
    path: Path,
    vault: Path,
    *,
    expected_id: str,
    expected_request: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.exists() and not path.is_symlink():
        return None, []
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise IngestError(
            "BROKEN_RECEIPT",
            "Receipt must be a private, owned, single-link regular file.",
            exit_code=4,
        )
    value = validate_receipt(read_json(path, maximum_bytes=4 * 1024 * 1024), expected_id=expected_id)
    receipt_request = {
        "draft_sha256": value["draft_sha256"],
        "title": value["title"],
        "link_style": value["link_style"],
        "request_sha256": value["request_sha256"],
    }
    requested_identity = {
        key: expected_request[key]
        for key in ("draft_sha256", "title", "link_style", "request_sha256")
    }
    if (
        receipt_request != requested_identity
        or value["knowledge_note"] != expected_request["knowledge_note"]
        or value["source_note"] != expected_request["source_note"]
    ):
        raise IngestError(
            "INGEST_ID_CONFLICT",
            "The same transcript was previously ingested with a different draft or destination.",
            exit_code=4,
        )
    warnings: list[str] = []
    source_sha256 = value["source_sha256"]
    for item in value["initial_files"]:
        relative = Path(item["path"])
        candidate = vault / relative
        try:
            note_metadata = os.lstat(candidate)
        except OSError as exc:
            raise IngestError(
                "BROKEN_RECEIPT",
                "Receipt target is missing or cannot be inspected.",
                exit_code=4,
            ) from exc
        if (
            not stat.S_ISREG(note_metadata.st_mode)
            or stat.S_ISLNK(note_metadata.st_mode)
            or note_metadata.st_uid != os.geteuid()
            or note_metadata.st_nlink != 1
            or stat.S_IMODE(note_metadata.st_mode) != 0o644
        ):
            raise IngestError(
                "UNSAFE_NOTE_MODE",
                "A managed vault note no longer has the required mode 0644.",
                exit_code=4,
            )
        if not is_within(candidate, vault) or not identity_present(candidate, expected_id, source_sha256):
            raise IngestError(
                "BROKEN_RECEIPT",
                "Receipt points to a missing, unsafe, or unrelated note.",
                exit_code=4,
            )
        try:
            current_hash = secure_file_sha256(candidate)
        except PosixRuntimeError as exc:
            raise IngestError(
                "BROKEN_RECEIPT",
                "Receipt target cannot be verified safely.",
                exit_code=4,
            ) from exc
        if current_hash != item["sha256"]:
            warnings.append(f"CONTENT_MODIFIED:{item['path']}")
    return value, warnings


def configured_link_style(vault: Path, requested: str) -> str:
    if requested in {"wikilink", "markdown"}:
        return requested
    receipt_path = vault / ".awesome-capture" / "vault-build.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        metadata = receipt_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise IngestError(
                "BROKEN_RECEIPT",
                "Vault build receipt is not a private, owned, single-link file.",
                exit_code=4,
            )
        value = read_json(receipt_path, maximum_bytes=4 * 1024 * 1024)
        if value.get("schema_version") != BUILD_RECEIPT_SCHEMA:
            raise IngestError(
                "UNSUPPORTED_RECEIPT_SCHEMA",
                "Vault build receipt uses an unsupported schema.",
                exit_code=4,
            )
        try:
            validate_contract(value, expected="vault-build-receipt")
        except ContractError as exc:
            raise contract_failure(exc, receipt=True) from exc
        if value["vault_id"] != vault_identity(vault):
            raise IngestError(
                "BROKEN_RECEIPT",
                "Vault build receipt belongs to a different vault.",
                exit_code=4,
            )
        style = str(value.get("link_style") or "")
        if style in {"wikilink", "markdown"}:
            return style
        raise IngestError("BROKEN_RECEIPT", "Vault build receipt link_style is invalid.", exit_code=4)
    return "markdown"


def build_request(
    *,
    summary: dict[str, Any],
    draft_sha256: str,
    title: str,
    collection: Path,
    sources_dir: Path,
    link_style: str,
    tags: list[str],
    note_relative: Path,
    source_relative: Path,
    vault_state: (
        dict[str, Any]
        | Callable[[str], dict[str, Any]]
    ),
) -> tuple[dict[str, Any], str]:
    request_value = {
        "transcript_artifact_sha256": summary["artifact_sha256"],
        "draft_sha256": draft_sha256,
        "title": title,
        "collection": collection.as_posix(),
        "sources_dir": sources_dir.as_posix(),
        "link_style": link_style,
        "tags": tags,
        "knowledge_note": note_relative.as_posix(),
        "source_note": source_relative.as_posix(),
    }
    request_digest = hashlib.sha256(canonical_json(request_value)).hexdigest()
    observed_vault_state = (
        vault_state(request_digest)
        if callable(vault_state)
        else vault_state
    )
    plan_value = {
        "request_sha256": request_digest,
        "vault_state": observed_vault_state,
    }
    digest = hashlib.sha256(canonical_json(plan_value)).hexdigest()
    request = {
        "draft_sha256": draft_sha256,
        "title": title,
        "collection": collection.as_posix(),
        "sources_dir": sources_dir.as_posix(),
        "link_style": link_style,
        "tags": tags,
        "knowledge_note": note_relative.as_posix(),
        "source_note": source_relative.as_posix(),
        "request_sha256": request_digest,
        "plan_sha256": digest,
    }
    return request, digest


def source_verification(transcript: dict[str, Any], *, requested: bool) -> str:
    if not requested:
        return "not_checked"
    source = transcript["source"]
    path = Path(str(source.get("path") or ""))
    try:
        evidence = file_evidence(path)
    except PosixRuntimeError as exc:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return "not_available"
        raise IngestError(
            "SOURCE_INTEGRITY_FAILED",
            "Source media path is unsafe or changed during verification.",
            exit_code=7,
        ) from exc
    if evidence["bytes"] != source["bytes"] or evidence["sha256"] != source["sha256"]:
        raise IngestError("SOURCE_INTEGRITY_FAILED", "Source media no longer matches transcript evidence.", exit_code=7)
    return "verified"


def transaction_directory(vault: Path) -> Path:
    try:
        return runtime_create_transaction_directory(
            vault,
            prefix="ingest-",
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def completed_transaction_journal(journal: dict[str, Any]) -> dict[str, Any]:
    completed = json.loads(canonical_json(journal))
    completed["status"] = "complete"
    for step in completed["steps"]:
        step["status"] = "published"
    return completed


def _strict_json_bytes(data: bytes) -> dict[str, Any]:
    if len(data) > 4 * 1024 * 1024:
        raise IngestError("RECOVERY_CONFLICT", "Completion receipt exceeds its size limit.", exit_code=4)
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_nonfinite,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise IngestError("RECOVERY_CONFLICT", "Completion receipt is not strict JSON.", exit_code=4) from exc
    if not isinstance(value, dict):
        raise IngestError("RECOVERY_CONFLICT", "Completion receipt is not a JSON object.", exit_code=4)
    return value


def _destination_evidence(
    vault: Path,
    relative: Path,
    *,
    expected_bytes: int,
    expected_hash: str,
    expected_mode: int,
    staged: Path | None,
) -> bytes:
    safe_relative_dir(relative.parent.as_posix(), "transaction destination")
    if relative.parent == Path("."):
        parent_descriptor = _open_root(vault)
    else:
        parent_descriptor = _walk_directory(
            vault,
            relative.parent,
            create=False,
            final_mode=0o700,
        )
    try:
        try:
            descriptor = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise IngestError(
                "RECOVERY_CONFLICT",
                "A completed transaction destination is missing or unsafe.",
                exit_code=4,
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != expected_mode
                or metadata.st_size != expected_bytes
                or _sha256_descriptor(descriptor) != expected_hash
            ):
                raise IngestError(
                    "RECOVERY_CONFLICT",
                    "A completed transaction destination differs.",
                    exit_code=4,
                )
            if metadata.st_nlink != 1:
                raise IngestError(
                    "RECOVERY_CONFLICT",
                    "A completed transaction destination has an unexpected hardlink.",
                    exit_code=4,
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _verify_completed_ingest(
    vault: Path,
    transaction: Path,
    journal: dict[str, Any],
) -> None:
    if not journal["steps"] or journal["steps"][-1]["operation"] != "publish-receipt":
        raise IngestError("RECOVERY_CONFLICT", "Transaction has no final completion receipt.", exit_code=4)
    receipt_step = journal["steps"][-1]
    expected_receipt = f".awesome-capture/receipts/{journal['job_id']}.json"
    if receipt_step["destination"] != expected_receipt:
        raise IngestError("RECOVERY_CONFLICT", "Ingest completion receipt path is invalid.", exit_code=4)
    receipt_raw = _destination_evidence(
        vault,
        Path(str(receipt_step["destination"])),
        expected_bytes=int(receipt_step["bytes"]),
        expected_hash=str(receipt_step["sha256"]),
        expected_mode=0o600,
        staged=transaction / str(receipt_step["source"]),
    )
    receipt = _strict_json_bytes(receipt_raw)
    try:
        receipt = validate_receipt(receipt, expected_id=str(journal["job_id"]))
    except IngestError as exc:
        raise IngestError(
            "RECOVERY_CONFLICT",
            "Ingest completion receipt is invalid.",
            details=exc.code,
            exit_code=4,
        ) from exc
    published_steps = {str(item["destination"]): item for item in journal["steps"][:-1]}
    receipt_files = {str(item["path"]): item for item in receipt["initial_files"]}
    if set(published_steps) != set(receipt_files):
        raise IngestError("RECOVERY_CONFLICT", "Ingest receipt file set differs.", exit_code=4)
    for relative, item in receipt_files.items():
        step = published_steps[relative]
        if (
            step["operation"] != "publish-file"
            or step["bytes"] != item["bytes"]
            or step["sha256"] != item["sha256"]
            or item["identity_marker"] != f"awesome_capture_id: {journal['job_id']}"
        ):
            raise IngestError("RECOVERY_CONFLICT", "Ingest receipt file evidence differs.", exit_code=4)
        raw = _destination_evidence(
            vault,
            Path(relative),
            expected_bytes=int(item["bytes"]),
            expected_hash=str(item["sha256"]),
            expected_mode=0o644,
            staged=transaction / str(step["source"]),
        )
        try:
            prefix = raw[:8192].decode("utf-8")
        except UnicodeError as exc:
            raise IngestError("RECOVERY_CONFLICT", "Ingest note identity is not UTF-8.", exit_code=4) from exc
        if (
            f"awesome_capture_id: {yaml_scalar(journal['job_id'])}" not in prefix
            or f"source_sha256: {yaml_scalar(receipt['source_sha256'])}" not in prefix
        ):
            raise IngestError("RECOVERY_CONFLICT", "Ingest note identity differs.", exit_code=4)


def _write_completion_journal(path: Path, value: dict[str, Any]) -> None:
    try:
        validate_contract(value, expected="transaction")
    except ContractError as exc:
        raise contract_failure(exc) from exc
    fsync_write(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    fsync_directory(path.parent)


def _promote_completed_journal(
    transaction: Path,
    journal: dict[str, Any],
) -> dict[str, Any]:
    completed = completed_transaction_journal(journal)
    completion_path = transaction / ".journal-complete.json"
    if completion_path.exists() or completion_path.is_symlink():
        try:
            existing = (
                None
                if completion_path.is_symlink()
                else read_json(completion_path, maximum_bytes=4 * 1024 * 1024)
            )
        except IngestError as exc:
            raise IngestError("RECOVERY_CONFLICT", "Completion journal is unsafe.", exit_code=4) from exc
        if existing != completed:
            raise IngestError("RECOVERY_CONFLICT", "Completion journal differs.", exit_code=4)
    else:
        _write_completion_journal(completion_path, completed)
    return completed


def _cleanup_completed_transaction(
    root: Path,
    transaction: Path,
    journal: dict[str, Any],
    completed: dict[str, Any],
) -> None:
    actual = {item.name for item in transaction.iterdir()}
    expected_values = (
        {"journal.json": journal}
        if journal["status"] == "complete"
        else {
            "journal.json": journal,
            ".journal-complete.json": completed,
        }
    )
    if actual != set(expected_values):
        raise IngestError(
            "RECOVERY_CONFLICT",
            "Completed transaction contains unexpected residue.",
            exit_code=4,
        )
    expected_files = {
        name: hashlib.sha256(
            (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        for name, value in expected_values.items()
    }
    try:
        runtime_quarantine_completed_transaction(
            root.parents[1],
            transaction,
            expected_files=expected_files,
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def recover_transactions_locked(vault: Path) -> list[str]:
    root = vault / ".awesome-capture" / "transactions"
    recovered: list[str] = []
    if not root.exists():
        return recovered
    if root.is_symlink() or not root.is_dir():
        raise IngestError("RECOVERY_CONFLICT", "Transactions path is unsafe.", exit_code=4)
    for transaction in sorted(root.iterdir()):
        if not transaction.name.startswith("ingest-"):
            continue
        if transaction.is_symlink() or not transaction.is_dir():
            raise IngestError("RECOVERY_CONFLICT", "Transaction entry is unsafe.", exit_code=4)
        transaction_metadata = transaction.lstat()
        if transaction_metadata.st_uid != os.geteuid() or transaction_metadata.st_mode & 0o022:
            raise IngestError("RECOVERY_CONFLICT", "Transaction directory ownership is unsafe.", exit_code=4)
        try:
            initial_entries = {item.name for item in transaction.iterdir()}
        except OSError as exc:
            raise IngestError("RECOVERY_CONFLICT", "Transaction cannot be inspected.", exit_code=4) from exc
        if not initial_entries:
            raise IngestError(
                "RECOVERY_CONFLICT",
                "Empty transaction has no durable ownership marker.",
                exit_code=4,
            )
        journal_path = transaction / "journal.json"
        if not journal_path.is_file() or journal_path.is_symlink():
            raise IngestError("RECOVERY_CONFLICT", "Transaction has no trustworthy journal.", exit_code=4)
        journal = read_json(journal_path, maximum_bytes=4 * 1024 * 1024)
        try:
            validate_contract(journal, expected="transaction")
        except ContractError as exc:
            raise IngestError(
                "RECOVERY_CONFLICT",
                "Transaction journal does not satisfy transaction/v1.",
                details=f"{exc.code}:{exc.path}",
                exit_code=4,
            ) from exc
        if (
            journal.get("kind") != "ingest"
            or journal.get("root") != str(vault)
            or journal.get("staging_root") != str(transaction)
            or journal.get("status") not in {"publishing", "recovery_required", "complete"}
            or transaction.name != f"ingest-{journal.get('transaction_id')}"
        ):
            raise IngestError("RECOVERY_CONFLICT", "Transaction journal is invalid.", exit_code=4)
        completed = completed_transaction_journal(journal)
        if journal["status"] == "complete" and journal != completed:
            raise IngestError("RECOVERY_CONFLICT", "Completed transaction steps are not final.", exit_code=4)
        expected_sources = {str(item["source"]) for item in journal["steps"]}
        actual_entries = {item.name for item in transaction.iterdir()}
        allowed_entries = {"journal.json", *expected_sources}
        if journal["status"] != "complete":
            allowed_entries.add(".journal-complete.json")
        if actual_entries - allowed_entries:
            raise IngestError(
                "RECOVERY_CONFLICT", "Transaction contains unknown staged files.",
                exit_code=4,
            )
        if "journal.json" not in actual_entries:
            raise IngestError(
                "RECOVERY_CONFLICT",
                "Transaction is missing its durable journal.",
                exit_code=4,
            )
        if journal["status"] == "complete" and ".journal-complete.json" in actual_entries:
            raise IngestError("RECOVERY_CONFLICT", "Completed transaction has a stale journal.", exit_code=4)
        if ".journal-complete.json" in actual_entries:
            try:
                completion = read_json(
                    transaction / ".journal-complete.json",
                    maximum_bytes=4 * 1024 * 1024,
                )
            except IngestError as exc:
                raise IngestError("RECOVERY_CONFLICT", "Completion journal is unsafe.", exit_code=4) from exc
            if completion != completed:
                raise IngestError("RECOVERY_CONFLICT", "Completion journal differs.", exit_code=4)
        for expected_index, item in enumerate(journal["steps"]):
            if (
                not isinstance(item, dict)
                or set(item)
                != {
                    "index",
                    "operation",
                    "source",
                    "destination",
                    "bytes",
                    "sha256",
                    "status",
                }
                or item["index"] != expected_index
                or item["status"] not in {"pending", "published"}
            ):
                raise IngestError("RECOVERY_CONFLICT", "Transaction file entry is invalid.", exit_code=4)
            relative = Path(item["destination"])
            safe_relative_dir(relative.parent.as_posix(), "transaction destination")
            staged = transaction / str(item["source"])
            if journal["status"] != "complete":
                if str(item["source"]) in actual_entries:
                    if (
                        staged.is_symlink()
                        or not staged.is_file()
                        or staged.stat().st_size != item["bytes"]
                        or file_sha256(staged) != item["sha256"]
                    ):
                        raise IngestError(
                            "RECOVERY_CONFLICT",
                            "Staged transaction file changed.",
                            exit_code=4,
                        )
                publish_relative(
                    staged,
                    vault,
                    relative,
                    str(item["sha256"]),
                    mode=0o600 if item["operation"] == "publish-receipt" else 0o644,
                )
                test_failpoint(f"ingest.after-publish-{expected_index}")
        _verify_completed_ingest(vault, transaction, completed)
        completion_journal = completed
        if journal["status"] != "complete":
            completion_journal = _promote_completed_journal(transaction, journal)
            test_failpoint("ingest.after-complete-journal")
        test_failpoint("ingest.before-cleanup")
        _cleanup_completed_transaction(
            root,
            transaction,
            journal,
            completion_journal,
        )
        recovered.append(transaction.name)
    return recovered


def recover(vault: Path, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> dict[str, Any]:
    resolved = resolve_vault(str(vault), allow_plain_folder=True)
    with vault_lock(resolved, exclusive=True, timeout=lock_timeout, create=True):
        recovered = recover_transactions_locked(resolved)
    return {"status": "ok", "operation": "recover", "vault": str(resolved), "recovered": recovered}


def commit(args: argparse.Namespace) -> dict[str, Any]:
    transcript_path = Path(args.transcript).expanduser().absolute()
    transcript = read_transcript_artifact(transcript_path)
    summary = validate_transcript(transcript)
    document_path = Path(args.document).expanduser().absolute()
    title = display_title(args.title)
    try:
        draft_raw = read_regular_file(
            document_path,
            4 * 1024 * 1024,
        ).decode("utf-8")
    except UnicodeError as exc:
        raise IngestError(
            "INVALID_DOCUMENT",
            "Draft must be valid UTF-8 text.",
            exit_code=2,
        ) from exc
    except PosixRuntimeError as exc:
        code = "INPUT_NOT_FOUND" if not document_path.exists() else "UNSAFE_INPUT"
        raise IngestError(
            code,
            "Draft must be a safe current-user-owned regular file.",
            exit_code=2,
        ) from exc
    body = validate_document(
        draft_raw,
        transcript_has_speech=summary["has_speech"],
        expected_title=title,
    )
    vault = resolve_vault(args.vault, allow_plain_folder=args.allow_plain_folder)
    collection = safe_relative_dir(args.collection, "collection")
    sources_dir = safe_relative_dir(args.sources_dir, "sources-dir")
    uid = receipt_id(summary["artifact_sha256"])
    filename_title = safe_filename(title)
    note_relative = collection / f"{filename_title}--{uid[:8]}.md"
    source_relative = sources_dir / f"{filename_title}--{uid[:8]}-transcript.md"
    note_path = vault / note_relative
    source_path = vault / source_relative
    if not is_within(note_path, vault) or not is_within(source_path, vault):
        raise IngestError("INVALID_DESTINATION", "Resolved destination escapes the vault.", exit_code=2)
    receipt_dir = vault / ".awesome-capture" / "receipts"
    if (vault / ".awesome-capture").is_symlink() or not is_within(receipt_dir, vault):
        raise IngestError(
            "INVALID_DESTINATION",
            "The skill-owned metadata directory is a symbolic link or escapes the vault.",
            exit_code=2,
        )
    receipt_relative = Path(".awesome-capture/receipts") / f"{uid}.json"
    receipt_path = vault / receipt_relative
    if (receipt_path.exists() or receipt_path.is_symlink()) and (
        receipt_path.is_symlink() or not is_within(receipt_path, vault)
    ):
        raise IngestError("BROKEN_RECEIPT", "Receipt path is unsafe.", exit_code=4)
    tags = normalize_tags(["knowledge", *args.tag])
    link_style = configured_link_style(vault, getattr(args, "link_style", "auto"))
    source_text = source_markdown(transcript, title, uid)
    knowledge_text = knowledge_markdown(
        body,
        title=title,
        uid=uid,
        transcript=transcript,
        tags=tags,
        source_relative=source_relative,
        note_relative=note_relative,
        link_style=link_style,
    )
    request, confirmed_plan_sha256 = build_request(
        summary=summary,
        draft_sha256=hashlib.sha256(draft_raw.encode("utf-8")).hexdigest(),
        title=title,
        collection=collection,
        sources_dir=sources_dir,
        link_style=link_style,
        tags=tags,
        note_relative=note_relative,
        source_relative=source_relative,
        vault_state=lambda request_digest: ingest_plan_state(
            vault,
            note_relative=note_relative,
            source_relative=source_relative,
            receipt_relative=receipt_relative,
            ingest_id=uid,
            source_sha256=summary["source_sha256"],
            request_sha256=request_digest,
        ),
    )
    result = {
        "status": "ok",
        "operation": "commit",
        "result": "dry-run" if args.dry_run else "created",
        "knowledge_note": str(note_path),
        "source_note": str(source_path),
        "receipt_path": str(receipt_path),
        "awesome_capture_id": uid,
        "plan_sha256": confirmed_plan_sha256,
        "segment_count": summary["segment_count"],
        "link_style": link_style,
        "title": title,
    }
    if args.dry_run:
        return result
    expected_plan = getattr(args, "expected_plan_sha256", None)
    if not expected_plan:
        raise IngestError(
            "MISSING_PLAN_CONFIRMATION",
            "--expected-plan-sha256 is required for commit.",
            exit_code=2,
        )
    lock_timeout = float(getattr(args, "lock_timeout", DEFAULT_LOCK_TIMEOUT))
    with vault_lock(vault, exclusive=True, timeout=lock_timeout, create=True):
        recover_transactions_locked(vault)
        locked_link_style = configured_link_style(vault, getattr(args, "link_style", "auto"))
        locked_request, locked_plan_sha256 = build_request(
            summary=summary,
            draft_sha256=hashlib.sha256(draft_raw.encode("utf-8")).hexdigest(),
            title=title,
            collection=collection,
            sources_dir=sources_dir,
            link_style=locked_link_style,
            tags=tags,
            note_relative=note_relative,
            source_relative=source_relative,
            vault_state=lambda request_digest: ingest_plan_state(
                vault,
                note_relative=note_relative,
                source_relative=source_relative,
                receipt_relative=receipt_relative,
                ingest_id=uid,
                source_sha256=summary["source_sha256"],
                request_sha256=request_digest,
            ),
        )
        legacy_uid = hashlib.sha256(
            f"awesome-capture.artifact/v1\0{summary['source_sha256']}".encode("utf-8")
        ).hexdigest()[:16]
        legacy_path = receipt_dir / f"{legacy_uid}.json"
        if legacy_path.exists() and legacy_path != receipt_path:
            raise IngestError(
                "UNSUPPORTED_RECEIPT_SCHEMA",
                "A legacy ingest receipt exists for this source; automatic migration is disabled.",
                exit_code=4,
            )
        receipt, warnings = existing_receipt(
            receipt_path,
            vault,
            expected_id=uid,
            expected_request=locked_request,
        )
        if receipt:
            if expected_plan not in {
                locked_plan_sha256,
                receipt["plan_sha256"],
            }:
                raise IngestError(
                    "STALE_PLAN",
                    "The confirmed ingest plan is stale.",
                    exit_code=4,
                )
            verification = source_verification(
                transcript,
                requested=bool(getattr(args, "verify_source_media", False)),
            )
            return {
                **result,
                "result": "reused",
                "warnings": warnings,
                "source_media_verification": verification,
                "knowledge_note": str(vault / receipt["knowledge_note"]),
                "source_note": str(vault / receipt["source_note"]),
                "link_style": receipt["link_style"],
            }
        if locked_plan_sha256 != expected_plan:
            raise IngestError("STALE_PLAN", "The vault changed after dry-run.", exit_code=4)
        for destination in (note_path, source_path):
            if destination.exists() or destination.is_symlink():
                raise IngestError(
                    "PATH_COLLISION",
                    "Destination exists without a matching receipt.",
                    exit_code=4,
                )
        verification = source_verification(
            transcript,
            requested=bool(getattr(args, "verify_source_media", False)),
        )
        transaction = transaction_directory(vault)
        fsync_write(transaction / "source.md", source_text)
        fsync_write(transaction / "knowledge.md", knowledge_text)
        source_bytes = source_text.encode("utf-8")
        knowledge_bytes = knowledge_text.encode("utf-8")
        receipt_value = {
            "schema_version": RECEIPT_SCHEMA,
            "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "ingest_id": uid,
            "transcript_schema": SCHEMA_VERSION,
            "transcript_artifact_sha256": summary["artifact_sha256"],
            "transcript_semantic_sha256": summary["semantic_sha256"],
            "source_sha256": summary["source_sha256"],
            "draft_sha256": locked_request["draft_sha256"],
            "request_sha256": locked_request["request_sha256"],
            "plan_sha256": locked_request["plan_sha256"],
            "title": title,
            "knowledge_note": note_relative.as_posix(),
            "source_note": source_relative.as_posix(),
            "initial_files": [
                {
                    "path": source_relative.as_posix(),
                    "bytes": len(source_bytes),
                    "sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "identity_marker": f"awesome_capture_id: {uid}",
                },
                {
                    "path": note_relative.as_posix(),
                    "bytes": len(knowledge_bytes),
                    "sha256": hashlib.sha256(knowledge_bytes).hexdigest(),
                    "identity_marker": f"awesome_capture_id: {uid}",
                },
            ],
            "link_style": locked_link_style,
            "segment_count": summary["segment_count"],
            "source_media_verification": verification,
            "producer": {
                "skill": "ingest-knowledge",
                "contract_digest": contract_digest(),
            },
        }
        try:
            validate_contract(receipt_value, expected="ingest-receipt")
        except ContractError as exc:
            raise contract_failure(exc) from exc
        receipt_text = json.dumps(receipt_value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fsync_write(transaction / "receipt.json", receipt_text)
        steps = [
            {
                "index": 0,
                "operation": "publish-file",
                "source": "source.md",
                "destination": source_relative.as_posix(),
                "bytes": len(source_bytes),
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "status": "pending",
            },
            {
                "index": 1,
                "operation": "publish-file",
                "source": "knowledge.md",
                "destination": note_relative.as_posix(),
                "bytes": len(knowledge_bytes),
                "sha256": hashlib.sha256(knowledge_bytes).hexdigest(),
                "status": "pending",
            },
            {
                "index": 2,
                "operation": "publish-receipt",
                "source": "receipt.json",
                "destination": receipt_path.relative_to(vault).as_posix(),
                "bytes": len(receipt_text.encode("utf-8")),
                "sha256": hashlib.sha256(receipt_text.encode("utf-8")).hexdigest(),
                "status": "pending",
            },
        ]
        journal = {
            "schema_version": TRANSACTION_SCHEMA,
            "transaction_id": transaction.name.removeprefix("ingest-"),
            "kind": "ingest",
            "status": "publishing",
            "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "job_id": uid,
            "root": str(vault),
            "staging_root": str(transaction),
            "steps": steps,
        }
        try:
            validate_contract(journal, expected="transaction")
        except ContractError as exc:
            raise contract_failure(exc) from exc
        fsync_write(
            transaction / "journal.json",
            json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        fsync_directory(transaction)
        test_failpoint("ingest.after-journal")
        recover_transactions_locked(vault)
        return result


def audit(vault: Path, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> dict[str, Any]:
    requested_vault = vault.expanduser().absolute()
    try:
        resolved = resolve_vault(str(vault), allow_plain_folder=True)
    except IngestError as exc:
        if exc.code != "UNSAFE_VAULT_TARGET":
            raise
        findings = [
            {
                "severity": "error",
                "code": "UNSAFE_VAULT_ROOT",
                "path": ".",
            }
        ]
        return {
            "status": "ok",
            "operation": "audit",
            "vault": str(requested_vault),
            "healthy": False,
            "clean": False,
            "finding_count": len(findings),
            "findings": findings,
        }
    initial_root_identity = _vault_root_identity(resolved)
    findings: list[dict[str, str]] = []
    metadata_dir = resolved / ".awesome-capture"
    metadata_safe = True
    try:
        metadata = metadata_dir.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        metadata_safe = False
        findings.append(
            {"severity": "error", "code": "UNSAFE_METADATA_DIRECTORY", "path": ".awesome-capture"}
        )
    lock_path = metadata_dir / "vault.lock"
    if metadata_safe and metadata is not None:
        try:
            lock_metadata = lock_path.lstat()
        except FileNotFoundError:
            lock_metadata = None
        if lock_metadata is None:
            metadata_safe = False
            findings.append(
                {"severity": "error", "code": "MISSING_LOCK", "path": ".awesome-capture/vault.lock"}
            )
        elif (
            stat.S_ISLNK(lock_metadata.st_mode)
            or not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or lock_metadata.st_nlink != 1
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            metadata_safe = False
            findings.append(
                {"severity": "error", "code": "UNSAFE_LOCK", "path": ".awesome-capture/vault.lock"}
            )
    if not metadata_safe:
        try:
            current_root_identity = _vault_root_identity(resolved)
        except IngestError:
            current_root_identity = None
        if current_root_identity != initial_root_identity:
            findings.append(
                {"severity": "error", "code": "UNSAFE_VAULT_ROOT", "path": "."}
            )
        return {
            "status": "ok",
            "operation": "audit",
            "vault": str(resolved),
            "healthy": False,
            "clean": False,
            "finding_count": len(findings),
            "findings": findings,
        }
    with vault_lock(resolved, exclusive=False, timeout=lock_timeout, create=False):
        receipt_dir = metadata_dir / "receipts"
        receipt_values: dict[str, dict[str, Any]] = {}
        try:
            receipt_directory_metadata = receipt_dir.lstat()
        except FileNotFoundError:
            receipt_directory_metadata = None
        receipt_directory_safe = (
            receipt_directory_metadata is not None
            and stat.S_ISDIR(receipt_directory_metadata.st_mode)
            and not stat.S_ISLNK(receipt_directory_metadata.st_mode)
            and receipt_directory_metadata.st_uid == os.geteuid()
            and stat.S_IMODE(receipt_directory_metadata.st_mode) == 0o700
        )
        if receipt_directory_metadata is not None and not receipt_directory_safe:
            findings.append(
                {"severity": "error", "code": "UNSAFE_RECEIPT_DIRECTORY", "path": ".awesome-capture/receipts"}
            )
        elif receipt_directory_safe:
            for path in sorted(receipt_dir.iterdir()):
                relative_receipt = path.relative_to(resolved).as_posix()
                try:
                    metadata = path.lstat()
                except OSError:
                    metadata = None
                if (
                    metadata is None
                    or not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or path.suffix != ".json"
                ):
                    findings.append({"severity": "error", "code": "UNSAFE_RECEIPT", "path": relative_receipt})
                    continue
                try:
                    value = validate_receipt(
                        read_json(path, maximum_bytes=4 * 1024 * 1024),
                        expected_id=path.stem,
                    )
                    receipt_values[value["ingest_id"]] = value
                    source_sha256 = value["source_sha256"]
                    for item in value["initial_files"]:
                        note = resolved / item["path"]
                        try:
                            note_evidence = file_evidence(note)
                        except PosixRuntimeError:
                            findings.append(
                                {"severity": "error", "code": "BROKEN_RECEIPT_TARGET", "path": item["path"]}
                            )
                            continue
                        if note_evidence["mode"] != 0o644:
                            findings.append(
                                {"severity": "error", "code": "UNSAFE_NOTE_MODE", "path": item["path"]}
                            )
                            continue
                        if not identity_present(note, value["ingest_id"], source_sha256):
                            findings.append(
                                {"severity": "error", "code": "BROKEN_RECEIPT_TARGET", "path": item["path"]}
                            )
                        else:
                            if note_evidence["sha256"] != item["sha256"]:
                                findings.append(
                                    {"severity": "warning", "code": "CONTENT_MODIFIED", "path": item["path"]}
                                )
                except IngestError as exc:
                    findings.append({"severity": "error", "code": exc.code, "path": relative_receipt})
        managed_notes, identity_findings = managed_note_identities(resolved)
        findings.extend(identity_findings)
        for note in managed_notes:
            receipt = receipt_values.get(note["ingest_id"])
            if receipt is None:
                findings.append(
                    {
                        "severity": "error",
                        "code": "MISSING_INGEST_RECEIPT",
                        "path": note["path"],
                    }
                )
                continue
            receipt_paths = {
                item["path"] for item in receipt["initial_files"]
            }
            if (
                note["source_sha256"] != receipt["source_sha256"]
                or note["path"] not in receipt_paths
            ):
                findings.append(
                    {
                        "severity": "error",
                        "code": "BROKEN_NOTE_IDENTITY",
                        "path": note["path"],
                    }
                )
        transactions = metadata_dir / "transactions"
        try:
            transaction_metadata = transactions.lstat()
        except FileNotFoundError:
            transaction_metadata = None
        if transaction_metadata is not None:
            if (
                stat.S_ISLNK(transaction_metadata.st_mode)
                or not stat.S_ISDIR(transaction_metadata.st_mode)
                or transaction_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(transaction_metadata.st_mode) != 0o700
            ):
                findings.append(
                    {"severity": "error", "code": "UNSAFE_TRANSACTIONS", "path": ".awesome-capture/transactions"}
                )
            elif any(
                item.name.startswith("ingest-")
                for item in transactions.iterdir()
            ):
                findings.append(
                    {"severity": "error", "code": "RECOVERY_REQUIRED", "path": ".awesome-capture/transactions"}
                )
    try:
        current_root_identity = _vault_root_identity(resolved)
    except IngestError:
        current_root_identity = None
    if current_root_identity != initial_root_identity:
        findings.append(
            {"severity": "error", "code": "UNSAFE_VAULT_ROOT", "path": "."}
        )
    return {
        "status": "ok",
        "operation": "audit",
        "vault": str(resolved),
        "healthy": not any(item["severity"] == "error" for item in findings),
        "clean": not findings,
        "finding_count": len(findings),
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-transcript")
    validate_parser.add_argument("transcript")
    commit_parser = subparsers.add_parser("commit")
    commit_parser.add_argument("--transcript", required=True)
    commit_parser.add_argument("--document", required=True)
    commit_parser.add_argument("--vault", required=True)
    commit_parser.add_argument("--title", required=True)
    commit_parser.add_argument("--collection", default="00 Inbox")
    commit_parser.add_argument("--sources-dir", default="90 Sources")
    commit_parser.add_argument("--tag", action="append", default=[])
    commit_parser.add_argument(
        "--link-style",
        choices=("auto", "wikilink", "markdown"),
        default="auto",
        help="auto reads a build receipt and otherwise uses portable Markdown links.",
    )
    commit_parser.add_argument("--dry-run", action="store_true")
    commit_parser.add_argument("--allow-plain-folder", action="store_true")
    commit_parser.add_argument("--expected-plan-sha256")
    commit_parser.add_argument("--verify-source-media", action="store_true")
    commit_parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--vault", required=True)
    audit_parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT)
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--vault", required=True)
    recover_parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    import sys

    actual_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if any(item in {"-h", "--help"} for item in actual_argv):
            json_print(
                {
                    "status": "ok",
                    "operation": "help",
                    "commands": ["validate-transcript", "commit", "audit", "recover"],
                }
            )
            return 0
        args = build_parser().parse_args(actual_argv)
        require_posix()
        try:
            contract_digest()
        except ContractError as exc:
            raise IngestError(
                "CONTRACT_BUILD_MISMATCH",
                "The vendored contract bundle failed its integrity check.",
                exit_code=7,
            ) from exc
        if args.command == "validate-transcript":
            path = Path(args.transcript).expanduser().absolute()
            result = {
                "status": "ok",
                "operation": "validate-transcript",
                "path": str(path),
                **validate_transcript(read_transcript_artifact(path)),
            }
        elif args.command == "audit":
            result = audit(Path(args.vault), lock_timeout=args.lock_timeout)
        elif args.command == "recover":
            result = recover(Path(args.vault), lock_timeout=args.lock_timeout)
        else:
            result = commit(args)
        json_print(result)
        return 0
    except IngestError as exc:
        json_print(exc.as_dict(), stream=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        json_print(
            {
                "status": "error",
                "error": {"code": "INTERRUPTED", "message": "Operation interrupted."},
            },
            stream=sys.stderr,
        )
        return 130
    except OSError:
        json_print(
            {
                "status": "error",
                "error": {
                    "code": "FILESYSTEM_FAILED",
                    "message": "A filesystem operation failed; run recover before retrying.",
                },
            },
            stream=sys.stderr,
        )
        return 5
    except ContractError:
        json_print(
            IngestError(
                "CONTRACT_BUILD_MISMATCH",
                "The vendored contract bundle failed its integrity check.",
                exit_code=7,
            ).as_dict(),
            stream=sys.stderr,
        )
        return 7
    except Exception as exc:
        json_print(
            IngestError(
                "RUNTIME_FAILED",
                "The ingest operation failed unexpectedly.",
                details=exc.__class__.__name__,
                exit_code=5,
            ).as_dict(),
            stream=sys.stderr,
        )
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
