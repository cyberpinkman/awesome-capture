#!/usr/bin/env python3
"""Validate and synchronize Awesome Capture release metadata."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Sequence


ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    "download-video",
    "transcribe-media",
    "ingest-knowledge",
    "build-obsidian-vault",
)
VERSION_PATH = Path("VERSION")
CHANGELOG_PATH = Path("CHANGELOG.md")
MAX_VERSION_BYTES = 256
MAX_CHANGELOG_BYTES = 4 * 1024 * 1024

SEMVER_PATTERN = (
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
)
SEMVER_RE = re.compile(rf"\A{SEMVER_PATTERN}\Z")
RELEASE_HEADING_RE = re.compile(
    r"\A## \[([^\]\r\n]+)\] - ([0-9]{4}-[0-9]{2}-[0-9]{2})\Z"
)
LINK_DEFINITION_RE = re.compile(r"\A\[[^\]\r\n]+\]:[ \t]+\S(?:.*\S)?\Z")


class ReleaseMetadataError(Exception):
    """An expected, sanitized release metadata failure."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class JsonArgumentParser(argparse.ArgumentParser):
    """Raise a structured error instead of writing argparse prose."""

    def error(self, message: str) -> NoReturn:
        del message
        raise ReleaseMetadataError(
            "INVALID_ARGUMENTS",
            "Release command arguments are invalid.",
        )


@dataclass(frozen=True)
class SemVer:
    """A stable, strict X.Y.Z Semantic Version."""

    raw: str
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, raw: str) -> "SemVer":
        match = SEMVER_RE.fullmatch(raw)
        if match is None:
            raise ReleaseMetadataError(
                "VERSION_INVALID",
                "VERSION must contain one stable X.Y.Z Semantic Version without a v prefix.",
            )
        return cls(
            raw=raw,
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
        )

    def precedence_key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def compare(self, other: "SemVer") -> int:
        if self.precedence_key() == other.precedence_key():
            return 0
        return 1 if self.precedence_key() > other.precedence_key() else -1


@dataclass(frozen=True)
class ReleaseEntry:
    version: SemVer
    released_on: dt.date
    notes: str


@dataclass(frozen=True)
class Changelog:
    unreleased: str
    releases: tuple[ReleaseEntry, ...]


@dataclass(frozen=True)
class ReleaseMetadata:
    version: SemVer
    changelog: Changelog

    @property
    def current(self) -> ReleaseEntry:
        return self.changelog.releases[0]


def _emit(payload: dict[str, object], *, error: bool) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr if error else sys.stdout,
    )


def _read_limited_regular(
    path: Path,
    *,
    max_bytes: int,
    missing_code: str,
    invalid_code: str,
) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ReleaseMetadataError(
            missing_code,
            "Required release metadata is missing.",
        ) from exc
    except OSError as exc:
        raise ReleaseMetadataError(
            "RELEASE_IO_ERROR",
            "Release metadata could not be read.",
            exit_code=5,
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ReleaseMetadataError(
            invalid_code,
            "Release metadata must be a regular, non-linked file.",
        )
    if metadata.st_size > max_bytes:
        raise ReleaseMetadataError(
            invalid_code,
            "Release metadata exceeds its size limit.",
        )
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise ReleaseMetadataError(
            "RELEASE_IO_ERROR",
            "Release metadata could not be read.",
            exit_code=5,
        ) from exc
    if len(value) > max_bytes:
        raise ReleaseMetadataError(
            invalid_code,
            "Release metadata exceeds its size limit.",
        )
    return value


def read_version(root: Path) -> tuple[SemVer, bytes]:
    raw = _read_limited_regular(
        root / VERSION_PATH,
        max_bytes=MAX_VERSION_BYTES,
        missing_code="VERSION_MISSING",
        invalid_code="VERSION_INVALID",
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseMetadataError(
            "VERSION_INVALID",
            "VERSION must be canonical UTF-8 text.",
        ) from exc
    if not text.endswith("\n") or text.count("\n") != 1 or "\r" in text:
        raise ReleaseMetadataError(
            "VERSION_INVALID",
            "VERSION must contain exactly one line ending in LF.",
        )
    value = text[:-1]
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise ReleaseMetadataError(
            "VERSION_INVALID",
            "VERSION must not contain surrounding or embedded whitespace.",
        )
    return SemVer.parse(value), raw


def version_destinations(root: Path) -> tuple[Path, ...]:
    return tuple(root / "skills" / skill / "VERSION" for skill in SKILLS)


def _ensure_plain_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReleaseMetadataError(
            "VERSION_DESTINATION_INVALID",
            "A standalone skill directory is unavailable.",
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseMetadataError(
            "VERSION_DESTINATION_INVALID",
            "A standalone skill directory is unsafe.",
        )


def check_version_copies(root: Path, expected: bytes) -> None:
    _ensure_plain_directory(root / "skills")
    for skill, destination in zip(SKILLS, version_destinations(root)):
        _ensure_plain_directory(destination.parent)
        try:
            actual = destination.lstat()
        except FileNotFoundError as exc:
            raise ReleaseMetadataError(
                "VERSION_COPY_MISSING",
                f"The generated VERSION copy for {skill} is missing.",
            ) from exc
        except OSError as exc:
            raise ReleaseMetadataError(
                "RELEASE_IO_ERROR",
                "A generated VERSION copy could not be inspected.",
                exit_code=5,
            ) from exc
        if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
            raise ReleaseMetadataError(
                "VERSION_COPY_INVALID",
                f"The generated VERSION copy for {skill} is unsafe.",
            )
        try:
            value = destination.read_bytes()
        except OSError as exc:
            raise ReleaseMetadataError(
                "RELEASE_IO_ERROR",
                "A generated VERSION copy could not be read.",
                exit_code=5,
            ) from exc
        if value != expected:
            raise ReleaseMetadataError(
                "VERSION_COPY_MISMATCH",
                f"The generated VERSION copy for {skill} is out of sync.",
            )


def _decode_changelog(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseMetadataError(
            "CHANGELOG_INVALID",
            "CHANGELOG.md must be canonical UTF-8 text.",
        ) from exc
    if not text.endswith("\n") or "\r" in text or "\0" in text:
        raise ReleaseMetadataError(
            "CHANGELOG_INVALID",
            "CHANGELOG.md must use canonical LF-terminated text.",
        )
    return text


def _trim_blank_lines(lines: Sequence[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return list(lines[start:end])


def _without_trailing_link_footer(lines: Sequence[str]) -> list[str]:
    cursor = len(lines)
    while cursor and not lines[cursor - 1].strip():
        cursor -= 1
    saw_link = False
    while cursor:
        line = lines[cursor - 1]
        if LINK_DEFINITION_RE.fullmatch(line):
            saw_link = True
            cursor -= 1
            continue
        if saw_link and not line.strip():
            cursor -= 1
            continue
        break
    return list(lines[:cursor]) if saw_link else list(lines)


def _notes_text(lines: Sequence[str], *, strip_footer: bool) -> str:
    selected = (
        _without_trailing_link_footer(lines)
        if strip_footer
        else list(lines)
    )
    selected = _trim_blank_lines(selected)
    return "\n".join(selected) + ("\n" if selected else "")


def _has_substantive_notes(notes: str) -> bool:
    in_comment = False
    for raw_line in notes.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if line.startswith("<!--"):
            if "-->" not in line:
                in_comment = True
            continue
        if line.startswith("#") or LINK_DEFINITION_RE.fullmatch(line):
            continue
        return True
    return False


def parse_changelog(root: Path, current_version: SemVer) -> Changelog:
    raw = _read_limited_regular(
        root / CHANGELOG_PATH,
        max_bytes=MAX_CHANGELOG_BYTES,
        missing_code="CHANGELOG_MISSING",
        invalid_code="CHANGELOG_INVALID",
    )
    lines = _decode_changelog(raw).splitlines()
    if not lines or lines[0] != "# Changelog":
        raise ReleaseMetadataError(
            "CHANGELOG_INVALID",
            "CHANGELOG.md must start with the canonical title.",
        )

    headings = [
        index
        for index, line in enumerate(lines)
        if line.startswith("## ")
    ]
    if not headings or lines[headings[0]] != "## [Unreleased]":
        raise ReleaseMetadataError(
            "CHANGELOG_INVALID",
            "CHANGELOG.md must begin its version sections with [Unreleased].",
        )
    if sum(line == "## [Unreleased]" for line in lines) != 1:
        raise ReleaseMetadataError(
            "CHANGELOG_INVALID",
            "CHANGELOG.md must contain exactly one [Unreleased] section.",
        )
    if len(headings) < 2:
        raise ReleaseMetadataError(
            "CHANGELOG_INVALID",
            "CHANGELOG.md must contain at least one dated release.",
        )

    unreleased_lines = lines[headings[0] + 1 : headings[1]]
    unreleased = _notes_text(unreleased_lines, strip_footer=False)
    entries: list[ReleaseEntry] = []
    seen_versions: set[str] = set()

    for position, heading_index in enumerate(headings[1:], start=1):
        match = RELEASE_HEADING_RE.fullmatch(lines[heading_index])
        if match is None:
            raise ReleaseMetadataError(
                "CHANGELOG_INVALID",
                "CHANGELOG.md contains an invalid release heading.",
            )
        version_text = match.group(1)
        if version_text in seen_versions:
            raise ReleaseMetadataError(
                "CHANGELOG_INVALID",
                "CHANGELOG.md contains a duplicate release version.",
            )
        seen_versions.add(version_text)
        try:
            released_on = dt.date.fromisoformat(match.group(2))
        except ValueError as exc:
            raise ReleaseMetadataError(
                "CHANGELOG_INVALID",
                "CHANGELOG.md contains an invalid release date.",
            ) from exc
        next_heading = (
            headings[position + 1]
            if position + 1 < len(headings)
            else len(lines)
        )
        notes = _notes_text(
            lines[heading_index + 1 : next_heading],
            strip_footer=position == len(headings) - 1,
        )
        if not _has_substantive_notes(notes):
            raise ReleaseMetadataError(
                "CHANGELOG_INVALID",
                "Every dated release must contain substantive release notes.",
            )
        entries.append(
            ReleaseEntry(
                version=SemVer.parse(version_text),
                released_on=released_on,
                notes=notes,
            )
        )

    if entries[0].version.raw != current_version.raw:
        raise ReleaseMetadataError(
            "CHANGELOG_VERSION_MISMATCH",
            "VERSION must match the newest CHANGELOG.md release.",
        )
    for newer, older in zip(entries, entries[1:]):
        if newer.version.compare(older.version) <= 0:
            raise ReleaseMetadataError(
                "CHANGELOG_INVALID",
                "CHANGELOG.md releases must be in strict descending version order.",
            )
        if newer.released_on < older.released_on:
            raise ReleaseMetadataError(
                "CHANGELOG_INVALID",
                "CHANGELOG.md release dates must be in descending order.",
            )
    return Changelog(unreleased=unreleased, releases=tuple(entries))


def load_metadata(root: Path = ROOT) -> ReleaseMetadata:
    version, raw_version = read_version(root)
    check_version_copies(root, raw_version)
    changelog = parse_changelog(root, version)
    return ReleaseMetadata(version=version, changelog=changelog)


def _atomic_replace_regular(path: Path, data: bytes) -> None:
    _ensure_plain_directory(path.parent)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise ReleaseMetadataError(
            "RELEASE_IO_ERROR",
            "A generated VERSION copy could not be inspected.",
            exit_code=5,
        ) from exc
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
    ):
        raise ReleaseMetadataError(
            "VERSION_COPY_INVALID",
            "A generated VERSION copy is unsafe.",
        )

    descriptor = -1
    temporary = ""
    directory_descriptor = -1
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise ReleaseMetadataError(
            "RELEASE_IO_ERROR",
            "A generated VERSION copy could not be synchronized.",
            exit_code=5,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def sync(root: Path = ROOT) -> tuple[SemVer, tuple[str, ...]]:
    version, expected = read_version(root)
    _ensure_plain_directory(root / "skills")
    updated: list[str] = []
    for skill, destination in zip(SKILLS, version_destinations(root)):
        _ensure_plain_directory(destination.parent)
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            actual = None
        except OSError as exc:
            raise ReleaseMetadataError(
                "RELEASE_IO_ERROR",
                "A generated VERSION copy could not be inspected.",
                exit_code=5,
            ) from exc
        else:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ReleaseMetadataError(
                    "VERSION_COPY_INVALID",
                    f"The generated VERSION copy for {skill} is unsafe.",
                )
            try:
                actual = destination.read_bytes()
            except OSError as exc:
                raise ReleaseMetadataError(
                    "RELEASE_IO_ERROR",
                    "A generated VERSION copy could not be read.",
                    exit_code=5,
                ) from exc
        if actual != expected:
            _atomic_replace_regular(destination, expected)
            updated.append(f"skills/{skill}/VERSION")
    check_version_copies(root, expected)
    return version, tuple(updated)


def ensure_release_ready(
    metadata: ReleaseMetadata,
    *,
    requested_version: str | None,
) -> None:
    if requested_version is not None:
        requested = SemVer.parse(requested_version)
        if requested.raw != metadata.version.raw:
            raise ReleaseMetadataError(
                "REQUESTED_VERSION_MISMATCH",
                "The requested version does not match VERSION.",
            )
    if metadata.changelog.unreleased:
        raise ReleaseMetadataError(
            "RELEASE_NOT_READY",
            "The [Unreleased] section must be empty before publishing.",
        )
    if not _has_substantive_notes(metadata.current.notes):
        raise ReleaseMetadataError(
            "RELEASE_NOT_READY",
            "The current release notes are empty.",
        )
    for historical in metadata.changelog.releases[1:]:
        if metadata.version.compare(historical.version) <= 0:
            raise ReleaseMetadataError(
                "RELEASE_NOT_READY",
                "VERSION must have higher precedence than every prior release.",
            )


def _require_secure_notes_platform() -> None:
    required = (
        hasattr(os, "O_NOFOLLOW"),
        hasattr(os, "O_DIRECTORY"),
        os.open in os.supports_dir_fd,
        os.link in os.supports_dir_fd,
        os.unlink in os.supports_dir_fd,
    )
    if os.name != "posix" or not all(required):
        raise ReleaseMetadataError(
            "UNSUPPORTED_PLATFORM",
            "Secure release-note publication is unavailable on this platform.",
            exit_code=3,
        )


def _open_directory_without_symlinks(path: Path) -> int:
    _require_secure_notes_platform()
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for component in absolute.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise ReleaseMetadataError(
            "OUTPUT_PATH_UNSAFE",
            "The release-notes output directory is unavailable or unsafe.",
        ) from exc


def write_notes_no_clobber(output: Path, data: bytes) -> None:
    if (
        "\0" in os.fspath(output)
        or not output.name
        or output.name in {".", ".."}
    ):
        raise ReleaseMetadataError(
            "OUTPUT_PATH_UNSAFE",
            "The release-notes output path is invalid.",
        )
    absolute = Path(os.path.abspath(output))
    parent_descriptor = _open_directory_without_symlinks(absolute.parent)
    temporary_name = f".{absolute.name}.{secrets.token_hex(16)}.tmp"
    temporary_created = False
    descriptor = -1
    try:
        try:
            os.stat(absolute.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ReleaseMetadataError(
                "OUTPUT_PATH_UNSAFE",
                "The release-notes output path is unsafe.",
            ) from exc
        else:
            raise ReleaseMetadataError(
                "OUTPUT_EXISTS",
                "The release-notes output already exists.",
                exit_code=4,
            )

        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_created = True
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short release-note write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary_name,
                absolute.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ReleaseMetadataError(
                "OUTPUT_EXISTS",
                "The release-notes output already exists.",
                exit_code=4,
            ) from exc
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        temporary_created = False
        os.fsync(parent_descriptor)
    except ReleaseMetadataError:
        raise
    except OSError as exc:
        raise ReleaseMetadataError(
            "RELEASE_IO_ERROR",
            "Release notes could not be written.",
            exit_code=5,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except OSError:
                pass
        os.close(parent_descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__, add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check", add_help=False)
    subparsers.add_parser("sync", add_help=False)
    check_release = subparsers.add_parser("check-release", add_help=False)
    check_release.add_argument("--requested-version", required=True)
    notes = subparsers.add_parser("notes", add_help=False)
    notes.add_argument("--output", required=True, type=Path)
    return parser


def _help_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "operation": "help",
        "commands": {
            "check": "validate release metadata",
            "sync": "copy VERSION into every standalone skill",
            "check-release": "validate a requested release version",
            "notes": "write current release notes without overwriting",
        },
    }


def _run(args: argparse.Namespace, *, root: Path) -> dict[str, object]:
    if args.command is None:
        raise ReleaseMetadataError(
            "INVALID_ARGUMENTS",
            "A release command is required.",
        )
    if args.command == "sync":
        version, updated = sync(root)
        return {
            "status": "ok",
            "operation": "sync",
            "version": version.raw,
            "copies": len(SKILLS),
            "updated": list(updated),
        }

    metadata = load_metadata(root)
    if args.command == "check":
        return {
            "status": "ok",
            "operation": "check",
            "version": metadata.version.raw,
            "tag": f"v{metadata.version.raw}",
            "release_count": len(metadata.changelog.releases),
            "copies": len(SKILLS),
        }
    if args.command == "check-release":
        ensure_release_ready(
            metadata,
            requested_version=args.requested_version,
        )
        return {
            "status": "ok",
            "operation": "check-release",
            "version": metadata.version.raw,
            "tag": f"v{metadata.version.raw}",
            "released_on": metadata.current.released_on.isoformat(),
        }
    if args.command == "notes":
        ensure_release_ready(metadata, requested_version=None)
        encoded = metadata.current.notes.encode("utf-8")
        write_notes_no_clobber(args.output, encoded)
        return {
            "status": "ok",
            "operation": "notes",
            "version": metadata.version.raw,
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    raise ReleaseMetadataError(
        "INVALID_ARGUMENTS",
        "A release command is required.",
    )


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(argument in {"-h", "--help"} for argument in arguments):
        _emit(_help_payload(), error=False)
        return 0
    try:
        args = build_parser().parse_args(arguments)
        payload = _run(args, root=root)
    except ReleaseMetadataError as exc:
        _emit(
            {
                "status": "error",
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                },
            },
            error=True,
        )
        return exc.exit_code
    except KeyboardInterrupt:
        _emit(
            {
                "status": "error",
                "error": {
                    "code": "INTERRUPTED",
                    "message": "Release metadata operation was interrupted.",
                },
            },
            error=True,
        )
        return 130
    except OSError:
        _emit(
            {
                "status": "error",
                "error": {
                    "code": "RELEASE_IO_ERROR",
                    "message": "Release metadata operation failed.",
                },
            },
            error=True,
        )
        return 5
    _emit(payload, error=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
