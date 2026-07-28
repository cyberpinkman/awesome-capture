#!/usr/bin/env python3
"""Fail when generated, private, or sensitive files remain in the repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]

# These are contract test vectors, not runtime artifacts. Keep this list narrow:
# adding a fixture with an artifact/state-looking name requires an explicit review.
ALLOWED_CONTRACT_FIXTURES = frozenset(
    {
        "contracts/fixtures/invalid/legacy-artifact.json",
        "contracts/fixtures/valid/transcript-artifact.json",
        "contracts/fixtures/valid/transcription-state.json",
        "contracts/fixtures/valid/video-artifact.json",
    }
)

FORBIDDEN_DIRECTORIES = {
    "__pycache__": "python-cache-directory",
    ".awesome-capture": "awesome-capture-runtime-directory",
    ".awesome-capture-media": "awesome-capture-runtime-directory",
    ".obsidian": "private-application-directory",
    ".pytest_cache": "test-cache-directory",
    ".venv": "local-environment-directory",
    "chunks": "capture-runtime-directory",
    "downloads": "capture-runtime-directory",
    "htmlcov": "test-cache-directory",
    "output": "capture-runtime-directory",
    "outputs": "capture-runtime-directory",
}

FORBIDDEN_EXACT_FILES = {
    ".coverage": "test-cache-file",
    ".ds_store": "operating-system-metadata",
    "transcript.json": "capture-runtime-output",
    "transcript.md": "capture-runtime-output",
    "transcript.srt": "capture-runtime-output",
    "transcript.txt": "capture-runtime-output",
    "transcript.vtt": "capture-runtime-output",
}

BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})
MEDIA_SUFFIXES = frozenset(
    {
        ".3gp",
        ".aac",
        ".aiff",
        ".avi",
        ".caf",
        ".flac",
        ".flv",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".oga",
        ".ogg",
        ".ogv",
        ".opus",
        ".wav",
        ".webm",
        ".wmv",
    }
)
MODEL_SUFFIXES = frozenset(
    {
        ".bin",
        ".ckpt",
        ".ggml",
        ".gguf",
        ".h5",
        ".hdf5",
        ".mlmodel",
        ".mlpackage",
        ".onnx",
        ".pt",
        ".pth",
        ".safetensors",
        ".tflite",
        ".weights",
    }
)
PRIVATE_KEY_SUFFIXES = frozenset({".jks", ".key", ".p12", ".pem", ".pfx"})
RUNTIME_SUFFIXES = {
    ".part": "partial-runtime-output",
    ".tmp": "temporary-runtime-output",
    ".log": "runtime-log",
}

COOKIE_NAME_RE = re.compile(
    r"(?:^|[._-])cookies?(?:\.(?:db|json|sqlite|txt))?\Z",
    re.IGNORECASE,
)
ARTIFACT_STATE_RE = re.compile(
    r"(?:^|[._-])(?:artifact|state)\.json\Z",
    re.IGNORECASE,
)
SECRET_CONFIG_RE = re.compile(
    r"(?:auth|credentials?|secrets?|service[-_]account|tokens?)"
    r"\.(?:ini|json|toml|ya?ml)\Z",
    re.IGNORECASE,
)
SSH_PRIVATE_KEY_RE = re.compile(
    r"id_(?:dsa|ecdsa|ed25519|rsa)(?:_sk)?\Z",
    re.IGNORECASE,
)


class HygieneScanError(Exception):
    """Raised when the repository cannot be scanned completely."""


def _is_allowed(relative: Path) -> bool:
    return relative.as_posix() in ALLOWED_CONTRACT_FIXTURES


def _classify(relative: Path) -> str | None:
    if _is_allowed(relative):
        return None

    name = relative.name
    lowered = name.casefold()
    suffix = Path(lowered).suffix

    if lowered in FORBIDDEN_DIRECTORIES:
        return FORBIDDEN_DIRECTORIES[lowered]
    if lowered in FORBIDDEN_EXACT_FILES:
        return FORBIDDEN_EXACT_FILES[lowered]
    if suffix in BYTECODE_SUFFIXES:
        return "python-bytecode"
    if suffix in MEDIA_SUFFIXES:
        return "media-file"
    if suffix in MODEL_SUFFIXES:
        return "model-file"
    if COOKIE_NAME_RE.search(lowered):
        return "cookie-file"
    if ARTIFACT_STATE_RE.search(lowered) or lowered.endswith(".info.json"):
        return "capture-artifact-or-state"
    if lowered == ".env" or lowered == ".envrc" or lowered.startswith(".env."):
        if lowered.endswith((".example", ".sample", ".template")):
            return None
        return "secret-environment-file"
    if (
        SECRET_CONFIG_RE.fullmatch(lowered)
        or SSH_PRIVATE_KEY_RE.fullmatch(lowered)
        or suffix in PRIVATE_KEY_SUFFIXES
        or lowered in {".npmrc", ".pypirc"}
    ):
        return "secret-file"
    return RUNTIME_SUFFIXES.get(suffix)


def _iter_entries(root: Path) -> Iterator[tuple[Path, bool]]:
    pending: list[tuple[Path, Path]] = [(root, Path())]
    while pending:
        directory, relative_directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise HygieneScanError from exc

        child_directories: list[tuple[Path, Path]] = []
        for entry in entries:
            if entry.name == ".git":
                continue
            relative = relative_directory / entry.name
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise HygieneScanError from exc
            yield relative, is_directory
            if is_directory and entry.name.casefold() not in FORBIDDEN_DIRECTORIES:
                child_directories.append((Path(entry.path), relative))
        pending.extend(reversed(child_directories))


def scan_repository(root: Path) -> list[dict[str, str]]:
    """Return sorted hygiene violations without consulting Git or .gitignore."""

    try:
        mode = root.lstat().st_mode
    except OSError as exc:
        raise HygieneScanError from exc
    if not stat.S_ISDIR(mode):
        raise HygieneScanError

    violations = []
    for relative, _is_directory in _iter_entries(root):
        rule = _classify(relative)
        if rule is not None:
            violations.append({"path": relative.as_posix(), "rule": rule})
    return sorted(violations, key=lambda item: (item["path"], item["rule"]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to scan (defaults to this checkout).",
    )
    return parser


def _emit(payload: dict[str, object], *, error: bool) -> None:
    print(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        violations = scan_repository(args.root)
    except HygieneScanError:
        _emit(
            {
                "error": {
                    "code": "REPOSITORY_SCAN_ERROR",
                    "message": "Repository hygiene scan could not be completed.",
                },
                "status": "error",
            },
            error=True,
        )
        return 2

    if violations:
        _emit(
            {
                "error": {
                    "code": "REPOSITORY_HYGIENE_FAILED",
                    "message": "Repository contains generated, private, or sensitive files.",
                },
                "status": "error",
                "violation_count": len(violations),
                "violations": violations,
            },
            error=True,
        )
        return 1

    _emit(
        {"status": "ok", "violation_count": 0, "violations": []},
        error=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
