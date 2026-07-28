#!/usr/bin/env python3
"""Generate or verify standalone copies of Awesome Capture contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "contracts"
SKILLS = (
    "download-video",
    "transcribe-media",
    "ingest-knowledge",
    "build-obsidian-vault",
)
CONTRACT_RUNTIME_FILES = (
    "contract_runtime.py",
)
RUNTIME_FILES = (
    "__init__.py",
    "bundle_runtime.py",
    "media_runtime.py",
    "posix_runtime.py",
    "vault_runtime.py",
)
SCHEMA_FILES = (
    "artifact-transcript-v2.schema.json",
    "artifact-video-v2.schema.json",
    "chunk-set-v1.schema.json",
    "ingest-receipt-v1.schema.json",
    "smoke-receipt-v1.schema.json",
    "transaction-v1.schema.json",
    "transcription-state-v1.schema.json",
    "vault-build-receipt-v1.schema.json",
    "vault-config-v1.schema.json",
)
MANIFEST_SCHEMA = "awesome-capture.contract-manifest/v1"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for filename in (*CONTRACT_RUNTIME_FILES, *RUNTIME_FILES):
        files[filename] = (CANONICAL / filename).read_bytes()
    for filename in SCHEMA_FILES:
        files[f"schemas/{filename}"] = (CANONICAL / "schemas" / filename).read_bytes()
    return files


def aggregate_digest(hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, checksum in sorted(hashes.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(checksum.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def manifest_bytes(files: dict[str, bytes]) -> bytes:
    hashes = {relative: sha256_bytes(data) for relative, data in sorted(files.items())}
    contract_names = {
        *CONTRACT_RUNTIME_FILES,
        *(f"schemas/{filename}" for filename in SCHEMA_FILES),
    }
    contract_hashes = {
        relative: digest
        for relative, digest in hashes.items()
        if relative in contract_names
    }
    runtime_hashes = {
        relative: digest
        for relative, digest in hashes.items()
        if relative in RUNTIME_FILES
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "contract_digest": aggregate_digest(contract_hashes),
        "runtime_digest": aggregate_digest(runtime_hashes),
        "contract_files": contract_hashes,
        "runtime_files": runtime_hashes,
    }
    return (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def destinations() -> tuple[Path, ...]:
    return tuple(ROOT / "skills" / skill / "scripts" / "_contracts" for skill in SKILLS)


def atomic_replace(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def apply() -> None:
    files = source_files()
    manifest = manifest_bytes(files)
    atomic_replace(CANONICAL / "manifest.json", manifest)
    for destination in destinations():
        for relative, data in files.items():
            atomic_replace(destination / relative, data)
        atomic_replace(destination / "manifest.json", manifest)


def _check_tree(root: Path, expected: dict[str, bytes]) -> list[str]:
    problems: list[str] = []
    expected_names = set(expected)
    if not root.is_dir():
        return [f"missing directory: {root.relative_to(ROOT)}"]
    actual_names = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    for relative in sorted(expected_names - actual_names):
        problems.append(f"missing: {(root / relative).relative_to(ROOT)}")
    for relative in sorted(actual_names - expected_names):
        problems.append(f"unexpected generated file: {(root / relative).relative_to(ROOT)}")
    for relative in sorted(expected_names & actual_names):
        if (root / relative).read_bytes() != expected[relative]:
            problems.append(f"out of sync: {(root / relative).relative_to(ROOT)}")
    return problems


def check() -> list[str]:
    files = source_files()
    expected = {**files, "manifest.json": manifest_bytes(files)}
    problems: list[str] = []
    for relative, data in expected.items():
        path = CANONICAL / relative
        if not path.is_file():
            problems.append(f"missing: {path.relative_to(ROOT)}")
        elif path.read_bytes() != data:
            problems.append(f"out of sync: {path.relative_to(ROOT)}")
    for destination in destinations():
        problems.extend(_check_tree(destination, expected))
    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true", help="write canonical manifest and vendored copies")
    mode.add_argument("--check", action="store_true", help="fail if any generated copy differs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply:
        apply()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "operation": "sync-vendored",
                    "destinations": len(destinations()),
                },
                sort_keys=True,
            )
        )
        return 0
    problems = check()
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "operation": "check-vendored",
                "destinations": len(destinations()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
