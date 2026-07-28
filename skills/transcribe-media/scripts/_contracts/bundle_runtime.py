"""Verify canonical/vendored contract and safety runtime bundle identities."""

from __future__ import annotations

from pathlib import Path

from .contract_runtime import (
    ContractError,
    _aggregate_digest,
    _contract_manifest_entries,
    _manifest_path,
    _require_sha256,
    _sha256_file,
    loads_strict,
)


RUNTIME_FILES = (
    "__init__.py",
    "bundle_runtime.py",
    "media_runtime.py",
    "posix_runtime.py",
    "vault_runtime.py",
)


def _runtime_manifest_entries() -> list[tuple[str, str]]:
    root = Path(__file__).resolve().parent
    return [
        (name, _sha256_file(root / name))
        for name in RUNTIME_FILES
    ]


def _verified_manifest() -> dict[str, object]:
    try:
        manifest = loads_strict(_manifest_path().read_bytes())
    except OSError as exc:
        raise ContractError(
            "CONTRACT_BUILD_MISMATCH",
            "Contract manifest is missing.",
        ) from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "contract_digest",
        "runtime_digest",
        "contract_files",
        "runtime_files",
    }:
        raise ContractError(
            "CONTRACT_BUILD_MISMATCH",
            "Contract manifest shape is invalid.",
        )
    if manifest["schema_version"] != "awesome-capture.contract-manifest/v1":
        raise ContractError(
            "CONTRACT_BUILD_MISMATCH",
            "Contract manifest version is unsupported.",
        )

    contract_entries = dict(_contract_manifest_entries())
    runtime_entries = dict(_runtime_manifest_entries())
    if manifest["contract_files"] != contract_entries:
        raise ContractError(
            "CONTRACT_BUILD_MISMATCH",
            "Wire contract file hashes differ.",
        )
    if manifest["runtime_files"] != runtime_entries:
        raise ContractError(
            "CONTRACT_BUILD_MISMATCH",
            "Safety runtime file hashes differ.",
        )
    expected_contract_digest = _aggregate_digest(
        sorted(contract_entries.items())
    )
    expected_runtime_digest = _aggregate_digest(
        sorted(runtime_entries.items())
    )
    if manifest["contract_digest"] != expected_contract_digest:
        raise ContractError(
            "CONTRACT_BUILD_MISMATCH",
            "Wire contract digest differs.",
        )
    if manifest["runtime_digest"] != expected_runtime_digest:
        raise ContractError(
            "CONTRACT_BUILD_MISMATCH",
            "Safety runtime digest differs.",
        )
    return manifest


def verify_contract_bundle() -> str:
    """Verify both groups and return the wire-compatible contract digest."""

    manifest = _verified_manifest()
    return _require_sha256(
        manifest["contract_digest"],
        "$.contract_digest",
    )


def runtime_digest(*, verify: bool = True) -> str:
    manifest = _verified_manifest() if verify else loads_strict(
        _manifest_path().read_bytes()
    )
    if not isinstance(manifest, dict):
        raise ContractError(
            "CONTRACT_BUILD_MISMATCH",
            "Contract manifest is not an object.",
        )
    return _require_sha256(
        manifest.get("runtime_digest"),
        "$.runtime_digest",
    )
