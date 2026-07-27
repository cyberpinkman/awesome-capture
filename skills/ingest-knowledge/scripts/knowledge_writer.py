#!/usr/bin/env python3
"""Validate a transcript and atomically ingest a structured note into Obsidian."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import unicodedata
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote


SCHEMA_VERSION = "awesome-capture.artifact/v1"


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


def json_print(value: Any, *, stream: Any = None) -> None:
    import sys

    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        file=stream or sys.stdout,
    )


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise IngestError("INPUT_NOT_FOUND", f"File does not exist: {path}", exit_code=2)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestError("INVALID_JSON", f"Cannot read JSON: {path}", details=str(exc), exit_code=2) from exc
    if not isinstance(value, dict):
        raise IngestError("INVALID_JSON", "Expected one JSON object.", exit_code=2)
    return value


def validate_transcript(value: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
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
    source_hash = str(source.get("sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        errors.append("source.sha256 must be a lowercase SHA-256 hex digest")
    try:
        media_duration_ms = round(float(source.get("duration_seconds")) * 1000)
    except (TypeError, ValueError):
        media_duration_ms = None
    if media_duration_ms is not None and media_duration_ms <= 0:
        errors.append("source.duration_seconds must be positive when present")
    transcription = value.get("transcription")
    if transcription is not None and not isinstance(transcription, dict):
        errors.append("transcription must be an object when present")
        transcription = {}
    elif transcription is None:
        transcription = {}
    if transcription.get("engine") == "whisper-cpp":
        identity = transcription.get("engine_identity")
        if not isinstance(identity, dict):
            errors.append("whisper-cpp transcription requires engine_identity")
        else:
            for field in ("binary_sha256", "model_sha256"):
                if not re.fullmatch(r"[0-9a-f]{64}", str(identity.get(field) or "")):
                    errors.append(f"whisper-cpp engine_identity.{field} must be SHA-256")
    timeline = transcription.get("chunk_timeline")
    if timeline is not None:
        if not isinstance(timeline, list):
            errors.append("transcription.chunk_timeline must be an array")
        else:
            expected_offset = 0
            for index, chunk in enumerate(timeline):
                if not isinstance(chunk, dict):
                    errors.append(f"chunk_timeline[{index}] must be an object")
                    continue
                offset, duration = chunk.get("offset_ms"), chunk.get("duration_ms")
                chunk_hash = str(chunk.get("sha256") or "")
                if (
                    not isinstance(offset, int)
                    or not isinstance(duration, int)
                    or offset != expected_offset
                    or duration < 0
                ):
                    errors.append(f"chunk_timeline[{index}] has inconsistent cumulative timing")
                else:
                    expected_offset = offset + duration
                if not re.fullmatch(r"[0-9a-f]{64}", chunk_hash):
                    errors.append(f"chunk_timeline[{index}].sha256 must be SHA-256")
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
        start, end = segment.get("start_ms"), segment.get("end_ms")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            errors.append(f"segments[{index}] has invalid timestamps")
        elif start < previous_start:
            errors.append(f"segments[{index}] is out of order")
        else:
            previous_start = start
            if media_duration_ms is not None and end > media_duration_ms + 2000:
                errors.append(f"segments[{index}] exceeds media duration by more than 2 seconds")
        text = str(segment.get("text") or "").strip()
        if not text:
            errors.append(f"segments[{index}] has empty text")
        else:
            nonempty_text.append(text)
    claimed_text = str(value.get("text") or "").strip()
    if nonempty_text and not claimed_text:
        errors.append("text is empty although segments contain speech")
    elif not nonempty_text and claimed_text:
        errors.append("text claims speech although segments are empty")
    elif nonempty_text:
        expected_text = "\n".join(nonempty_text)
        if " ".join(claimed_text.split()) != " ".join(expected_text.split()):
            errors.append("text does not match the ordered segment text")
    if errors:
        raise IngestError(
            "INVALID_TRANSCRIPT",
            "Transcript artifact validation failed.",
            details="\n".join(errors),
            exit_code=2,
        )
    return {
        "schema_version": value["schema_version"],
        "source_sha256": source_hash,
        "segment_count": len(segments),
        "has_speech": bool(nonempty_text),
    }


def resolve_vault(raw: str, *, allow_plain_folder: bool) -> Path:
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        raise IngestError("VAULT_NOT_FOUND", f"Vault directory does not exist: {vault}", exit_code=2)
    if vault in {Path(vault.anchor).resolve(), Path.home().resolve()} or vault.name == ".obsidian":
        raise IngestError(
            "UNSAFE_VAULT_TARGET",
            f"Refusing a filesystem root, home directory, or .obsidian directory as a vault: {vault}",
            exit_code=2,
        )
    if not allow_plain_folder and not (vault / ".obsidian").is_dir():
        raise IngestError(
            "NOT_AN_OBSIDIAN_VAULT",
            f"Missing .obsidian directory in: {vault}",
            details="Use $build-obsidian-vault to create a vault or pass --allow-plain-folder explicitly.",
            exit_code=2,
        )
    return vault


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
        path.resolve().relative_to(root.resolve())
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def write_receipt(path: Path, value: dict[str, Any]) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise IngestError("PATH_COLLISION", f"Receipt appeared during commit: {path}", exit_code=4)
        os.link(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def existing_receipt(path: Path, vault: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = read_json(path)
    for key in ("knowledge_note", "source_note"):
        candidate = Path(str(value.get(key) or ""))
        if not candidate.is_file() or not is_within(candidate, vault):
            raise IngestError(
                "BROKEN_RECEIPT",
                f"Receipt points to a missing or unsafe {key}.",
                details=str(path),
                exit_code=4,
            )
    return value


def configured_link_style(vault: Path, requested: str) -> str:
    if requested in {"wikilink", "markdown"}:
        return requested
    receipt_path = vault / ".awesome-capture" / "vault-build.json"
    if receipt_path.is_file() and not receipt_path.is_symlink():
        try:
            value = read_json(receipt_path)
            style = str(((value.get("config") or {}).get("link_style") or ""))
        except IngestError:
            style = ""
        if style in {"wikilink", "markdown"}:
            return style
    return "markdown"


def commit(args: argparse.Namespace) -> dict[str, Any]:
    transcript_path = Path(args.transcript).expanduser().resolve()
    transcript = read_json(transcript_path)
    summary = validate_transcript(transcript)
    document_path = Path(args.document).expanduser().resolve()
    if not document_path.is_file():
        raise IngestError("INPUT_NOT_FOUND", f"Draft does not exist: {document_path}", exit_code=2)
    title = display_title(args.title)
    body = validate_document(
        document_path.read_text(encoding="utf-8"),
        transcript_has_speech=summary["has_speech"],
        expected_title=title,
    )
    vault = resolve_vault(args.vault, allow_plain_folder=args.allow_plain_folder)
    collection = safe_relative_dir(args.collection, "collection")
    sources_dir = safe_relative_dir(args.sources_dir, "sources-dir")
    uid = hashlib.sha256(f"{SCHEMA_VERSION}\0{summary['source_sha256']}".encode()).hexdigest()[:16]
    filename_title = safe_filename(title)
    note_relative = collection / f"{filename_title}--{uid[:8]}.md"
    source_relative = sources_dir / f"{filename_title}--{uid[:8]}-transcript.md"
    note_path = (vault / note_relative).resolve()
    source_path = (vault / source_relative).resolve()
    if not is_within(note_path, vault) or not is_within(source_path, vault):
        raise IngestError("INVALID_DESTINATION", "Resolved destination escapes the vault.", exit_code=2)
    receipt_dir = vault / ".awesome-capture" / "receipts"
    if (vault / ".awesome-capture").is_symlink() or not is_within(receipt_dir, vault):
        raise IngestError(
            "INVALID_DESTINATION",
            "The skill-owned metadata directory is a symbolic link or escapes the vault.",
            exit_code=2,
        )
    receipt_path = receipt_dir / f"{uid}.json"
    if receipt_path.exists() and (receipt_path.is_symlink() or not is_within(receipt_path, vault)):
        raise IngestError("BROKEN_RECEIPT", "Receipt path is unsafe.", exit_code=4)
    receipt = existing_receipt(receipt_path, vault)
    if receipt:
        return {
            "status": "ok",
            "operation": "commit",
            "result": "reused",
            "title": receipt.get("title"),
            "knowledge_note": receipt["knowledge_note"],
            "source_note": receipt["source_note"],
            "receipt_path": str(receipt_path),
            "awesome_capture_id": uid,
            "segment_count": receipt.get("segment_count"),
            "link_style": receipt.get("link_style"),
        }
    for destination in (note_path, source_path):
        if destination.exists():
            raise IngestError(
                "PATH_COLLISION",
                f"Destination already exists without a matching receipt: {destination}",
                exit_code=4,
            )
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
    result = {
        "status": "ok",
        "operation": "commit",
        "result": "dry-run" if args.dry_run else "created",
        "knowledge_note": str(note_path),
        "source_note": str(source_path),
        "receipt_path": str(receipt_path),
        "awesome_capture_id": uid,
        "segment_count": summary["segment_count"],
        "link_style": link_style,
        "title": title,
    }
    if args.dry_run:
        return result
    transaction_dir = vault / ".awesome-capture" / "transactions" / str(uuid.uuid4())
    transaction_dir.mkdir(parents=True, exist_ok=False)
    staged_source = transaction_dir / "source.md"
    staged_note = transaction_dir / "knowledge.md"
    moved: list[tuple[Path, str]] = []
    try:
        fsync_write(staged_source, source_text)
        fsync_write(staged_note, knowledge_text)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_dir.mkdir(parents=True, exist_ok=True)
        os.link(staged_source, source_path)
        moved.append((source_path, file_sha256(source_path)))
        os.link(staged_note, note_path)
        moved.append((note_path, file_sha256(note_path)))
        receipt_value = {
            **result,
            "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "transcript_path": str(transcript_path),
        }
        write_receipt(receipt_path, receipt_value)
    except Exception:
        for destination, expected_hash in reversed(moved):
            try:
                if destination.is_file() and not destination.is_symlink():
                    if file_sha256(destination) == expected_hash:
                        destination.unlink()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(transaction_dir, ignore_errors=True)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    import sys

    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-transcript":
            path = Path(args.transcript).expanduser().resolve()
            result = {
                "status": "ok",
                "operation": "validate-transcript",
                "path": str(path),
                **validate_transcript(read_json(path)),
            }
        else:
            result = commit(args)
        json_print(result)
        return 0
    except IngestError as exc:
        json_print(exc.as_dict(), stream=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
