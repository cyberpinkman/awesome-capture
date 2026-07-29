#!/usr/bin/env python3
"""Validate sanitized smoke receipts and compute implementation digests."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contracts.contract_runtime import (  # noqa: E402
    ContractError,
    loads_strict,
    read_json_strict,
    validate_contract,
)

CASES_PATH = ROOT / "smoke" / "cases.json"
FUTURE_SKEW = dt.timedelta(minutes=5)
CASES_SCHEMA = "awesome-capture.smoke-cases/v2"
CONTROLLED_FAULT_PROFILE = "x-first-ytdlp-network-error-v1"
CONTROLLED_FAULT_TOOL = "awesome-capture-smoke-fault"
CONTROLLED_FAULT_WARNING = "controlled-ytdlp-network-error-injection"
CONTROLLED_FAULT_VERSION = "x-first-ytdlp-network-error-v1"
CONTROLLED_FAULT_ASSERTIONS = {
    "controlled-fault-profile-applied",
    "controlled-ytdlp-command-verified",
    "controlled-ytdlp-network-error-observed",
    "controlled-ytdlp-fault-triggered-exactly-once",
    "controlled-production-fallback-gate-accepted",
    "controlled-gallery-dl-command-verified",
    "controlled-gallery-dl-executed-exactly-once",
    "controlled-downloader-entrypoints-stable",
    "controlled-fallback-fresh-output-observed",
}
CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ENV_NAME_PATTERN = re.compile(r"^AWESOME_CAPTURE_SMOKE_[A-Z0-9_]+$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
BINARY_CASES = {
    "whisper-cpp-local",
    "whisper-cpp-cpu",
    "whisper-cpp-gpu-fallback",
    "external-local",
    "external-long-resume",
}


def tracked_files() -> list[Path]:
    """Return the implementation surface, including not-yet-tracked source files."""

    result: set[Path] = set()
    roots = (
        ROOT / "contracts",
        ROOT / "skills",
        ROOT / "tools",
        ROOT / "tests",
        ROOT / ".github" / "workflows",
    )
    for directory in roots:
        if not directory.is_dir():
            continue
        for target in directory.rglob("*"):
            if (
                target.is_file()
                and "__pycache__" not in target.parts
                and target.suffix not in {".pyc", ".pyo"}
            ):
                result.add(target.relative_to(ROOT))
    for relative in (
        Path("AGENTS.md"),
        Path("README.md"),
        Path("SECURITY.md"),
        Path("requirements-ci.lock"),
        Path("smoke/cases.json"),
    ):
        if (ROOT / relative).is_file():
            result.add(relative)
    return sorted(result, key=lambda item: item.as_posix())


def implementation_digest(paths: list[Path] | None = None) -> str:
    digest = hashlib.sha256()
    for relative in paths or tracked_files():
        target = ROOT / relative
        if not target.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(target.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def parse_timestamp(raw: str) -> dt.datetime:
    value = raw.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def load_case_registry() -> dict[str, dict[str, Any]]:
    try:
        value = loads_strict(CASES_PATH.read_bytes(), max_bytes=1024 * 1024)
    except OSError as exc:
        raise ContractError(
            "SMOKE_CASES_INVALID",
            "Preregistered smoke cases are unavailable.",
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "cases"}
        or value["schema_version"] != CASES_SCHEMA
        or not isinstance(value["cases"], list)
    ):
        raise ContractError(
            "SMOKE_CASES_INVALID",
            "Preregistered smoke cases are malformed.",
        )
    registry: dict[str, dict[str, Any]] = {}
    common = {"case_id", "suite", "platform", "source_env", "required_tools"}
    for raw in value["cases"]:
        if not isinstance(raw, dict):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke case identity is malformed.",
            )
        suite = raw.get("suite")
        if suite == "download":
            required = common | {"source_fingerprint"}
            allowed = required | {"expectation"}
            if raw.get("case_id") == "twitter-gallery-fallback":
                required |= {"fault_profile"}
                allowed |= {"fault_profile"}
        elif suite == "transcription":
            required = common | {"engine", "model_env"}
            if raw.get("case_id") in BINARY_CASES:
                required |= {"binary_env"}
            allowed = required | {"expectation"}
        else:
            required = set()
            allowed = set()
        if (
            not required
            or not required.issubset(raw)
            or not set(raw).issubset(allowed)
            or not isinstance(raw.get("case_id"), str)
            or CASE_ID_PATTERN.fullmatch(raw["case_id"]) is None
            or raw["case_id"] in registry
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke case identity is malformed.",
            )
        expected_platforms = (
            {"douyin", "tiktok", "bilibili", "youtube", "twitter"}
            if suite == "download"
            else {"local"}
        )
        if raw.get("platform") not in expected_platforms:
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke case platform is malformed.",
            )
        if suite == "transcription" and raw.get("engine") not in {
            "whisper-cpp",
            "faster-whisper",
            "mlx-whisper",
            "external",
        }:
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke engine is malformed.",
            )
        for key, item in raw.items():
            if key.endswith("_env") and (
                not isinstance(item, str)
                or ENV_NAME_PATTERN.fullmatch(item) is None
            ):
                raise ContractError(
                    "SMOKE_CASES_INVALID",
                    "Preregistered smoke environment reference is malformed.",
                )
        if suite == "download" and (
            not isinstance(raw.get("source_fingerprint"), str)
            or SHA_PATTERN.fullmatch(raw["source_fingerprint"]) is None
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered download source identity is malformed.",
            )
        expectation = raw.get("expectation")
        valid_expectations = (
            {"ephemeral_browser", "gallery-dl"}
            if suite == "download"
            else {"cpu_only", "gpu_fallback", "sigkill_resume"}
        )
        if expectation is not None and expectation not in valid_expectations:
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke expectation is malformed.",
            )
        fault_profile = raw.get("fault_profile")
        if fault_profile is not None and (
            fault_profile != CONTROLLED_FAULT_PROFILE
            or raw["case_id"] != "twitter-gallery-fallback"
            or suite != "download"
            or raw.get("platform") != "twitter"
            or expectation != "gallery-dl"
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered controlled fault binding is malformed.",
            )
        required_tools = raw.get("required_tools")
        if (
            not isinstance(required_tools, list)
            or not required_tools
            or any(
                not isinstance(item, str)
                or TOOL_NAME_PATTERN.fullmatch(item) is None
                for item in required_tools
            )
            or len(required_tools) != len(set(required_tools))
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke tool evidence is malformed.",
            )
        if (CONTROLLED_FAULT_TOOL in required_tools) != (
            fault_profile == CONTROLLED_FAULT_PROFILE
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered controlled tool evidence binding is malformed.",
            )
        registry[raw["case_id"]] = raw
    download_fingerprints = [
        case["source_fingerprint"]
        for case in registry.values()
        if case["suite"] == "download"
    ]
    if len(download_fingerprints) != len(set(download_fingerprints)):
        raise ContractError(
            "SMOKE_CASES_INVALID",
            "Preregistered download source identities are duplicated.",
        )
    return registry


def _required_assertions(case: dict[str, Any]) -> set[str]:
    if case["suite"] == "download":
        required = {
            "registered-source-detected",
            "registered-platform-matches",
            "registered-source-fingerprint-matches",
            "registered-source-is-canonical",
            "required-tools-observed",
            "download-command-succeeded",
            "video-artifact-v2-valid",
            "video-media-reverified",
        }
        if case.get("expectation"):
            required.add(f"required-{case['expectation']}-observed")
        else:
            required.add("anonymous-route-observed")
        if case.get("fault_profile") == CONTROLLED_FAULT_PROFILE:
            required.update(CONTROLLED_FAULT_ASSERTIONS)
        return required
    required = {
        "registered-local-media-exists",
        "explicit-local-model-exists",
        "required-tools-observed",
        "transcription-command-succeeded",
        "transcript-artifact-v2-valid",
        "transcript-evidence-reverified",
    }
    if "binary_env" in case:
        required.add("explicit-local-binary-exists")
    expectation_assertions = {
        "cpu_only": {"whisper-cpp-cpu-only-observed"},
        "gpu_fallback": {"whisper-cpp-gpu-fallback-observed"},
        "sigkill_resume": {
            "partial-chunk-state-observed",
            "transcription-process-sigkilled",
            "long-transcription-resumed-after-sigkill",
            "partial-chunk-results-reused",
        },
    }
    required.update(expectation_assertions.get(case.get("expectation"), set()))
    return required


def validate_case_evidence(value: dict[str, Any]) -> None:
    registry = load_case_registry()
    case = registry.get(value["case_id"])
    if case is None:
        raise ContractError(
            "UNKNOWN_SMOKE_CASE",
            "Smoke receipt case_id is not preregistered.",
        )
    passing = value["outcome"] == "pass"
    assertion_names = [item["name"] for item in value["assertions"]]
    if len(assertion_names) != len(set(assertion_names)):
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Smoke assertion names must be unique.",
        )
    tool_names = [item["name"] for item in value["tools"]]
    if len(tool_names) != len(set(tool_names)):
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Smoke tool evidence names must be unique.",
        )
    tools = {item["name"]: item["version"] for item in value["tools"]}
    unknown_controlled_assertions = {
        name
        for name in assertion_names
        if name.startswith("controlled-")
        and name not in CONTROLLED_FAULT_ASSERTIONS
    }
    unknown_controlled_warnings = {
        warning
        for warning in value["warnings"]
        if warning.startswith("controlled-")
        and warning != CONTROLLED_FAULT_WARNING
    }
    unknown_controlled_tools = {
        name
        for name in tool_names
        if name.startswith(CONTROLLED_FAULT_TOOL)
        and name != CONTROLLED_FAULT_TOOL
    }
    if (
        unknown_controlled_assertions
        or unknown_controlled_warnings
        or unknown_controlled_tools
    ):
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Smoke receipt contains unknown controlled fault evidence.",
        )
    controlled_assertions_present = (
        CONTROLLED_FAULT_ASSERTIONS & set(assertion_names)
    )
    controlled_claimed = bool(
        controlled_assertions_present
        or CONTROLLED_FAULT_WARNING in value["warnings"]
        or CONTROLLED_FAULT_TOOL in tools
    )
    registered_fault = case.get("fault_profile") == CONTROLLED_FAULT_PROFILE
    if controlled_claimed and not registered_fault:
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Unregistered smoke case claims a controlled fault.",
        )
    if registered_fault and controlled_claimed and (
        tools.get(CONTROLLED_FAULT_TOOL) != CONTROLLED_FAULT_VERSION
        or CONTROLLED_FAULT_WARNING not in value["warnings"]
        or controlled_assertions_present != CONTROLLED_FAULT_ASSERTIONS
    ):
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Controlled fault evidence is incomplete or inconsistent.",
        )
    if passing and registered_fault and not controlled_claimed:
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Passing controlled fallback receipt lacks its declared fault evidence.",
        )
    if case["suite"] == "download":
        if value["source"]["platform"] != case["platform"]:
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "Download smoke receipt does not match its registered platform.",
            )
        if passing and (
            value["engine"] is not None
            or value["source"]["fingerprint"] != case["source_fingerprint"]
        ):
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "Download smoke receipt does not match its registered source.",
            )
        if passing:
            expectation = case.get("expectation")
            if expectation is None:
                if (
                    value["source"]["auth_mode"] != "anonymous"
                    or value["source"]["fallback"] not in {None, "none"}
                ):
                    raise ContractError(
                        "SMOKE_CASE_MISMATCH",
                        "Anonymous smoke unexpectedly used authentication or fallback.",
                    )
            elif expectation == "ephemeral_browser":
                if (
                    value["source"]["auth_mode"] != "ephemeral_browser"
                    or value["source"]["fallback"] != expectation
                ):
                    raise ContractError(
                        "SMOKE_CASE_MISMATCH",
                        "Ephemeral browser evidence was not observed.",
                    )
            elif (
                value["source"]["fallback"] != expectation
                or (
                    expectation == "gallery-dl"
                    and value["source"]["auth_mode"] != "anonymous"
                )
            ):
                raise ContractError(
                    "SMOKE_CASE_MISMATCH",
                    "Registered download fallback evidence was not observed.",
                )
    else:
        engine = value["engine"]
        if value["source"]["platform"] != "local":
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "ASR smoke receipt does not match its registered engine.",
            )
        if not passing:
            if isinstance(engine, dict) and engine["name"] != case.get("engine"):
                raise ContractError(
                    "SMOKE_CASE_MISMATCH",
                    "Failed ASR smoke names another registered engine.",
                )
            return
        if (
            value["source"]["auth_mode"] != "not-applicable"
            or value["source"]["fallback"] is not None
            or not isinstance(engine, dict)
            or engine["name"] != case.get("engine")
        ):
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "ASR smoke receipt does not match its registered engine.",
            )
        if case.get("engine") == "mlx-whisper" and value["environment"]["os"] != "macos":
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "MLX smoke evidence must come from macOS.",
            )
    required_assertions = _required_assertions(case)
    if value["outcome"] == "pass" and set(assertion_names) != required_assertions:
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Passing receipt assertions do not exactly match the registered case.",
        )
    if value["outcome"] == "pass":
        if any(
            tools.get(name) in {None, "unavailable"}
            for name in case["required_tools"]
        ):
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "Passing receipt lacks required tool version evidence.",
            )


def validate_receipt(
    path: Path,
    *,
    require_pass: bool,
    require_current_digest: bool,
) -> dict[str, Any]:
    value = read_json_strict(path, expected="smoke-receipt")
    validate_contract(value, expected="smoke-receipt")
    validate_case_evidence(value)
    if require_pass and value["outcome"] != "pass":
        raise ContractError("SMOKE_FAILED", "Smoke receipt outcome is not pass.")
    if value["outcome"] == "pass" and not all(
        assertion["passed"] for assertion in value["assertions"]
    ):
        raise ContractError(
            "INCONSISTENT_SMOKE_RECEIPT",
            "Passing receipt contains a failed assertion.",
        )
    now = dt.datetime.now(dt.timezone.utc)
    created_at = parse_timestamp(value["created_at"])
    if created_at > now + FUTURE_SKEW:
        raise ContractError(
            "FUTURE_SMOKE_RECEIPT",
            "Smoke receipt timestamp is in the future.",
        )
    if require_current_digest and value["implementation_digest"] != implementation_digest():
        raise ContractError(
            "STALE_SMOKE_RECEIPT",
            "Smoke receipt implementation digest does not match the current tree.",
        )
    return {
        "path": str(path),
        "case_id": value["case_id"],
        "outcome": value["outcome"],
        "implementation_digest": value["implementation_digest"],
    }


def validate_required_case_coverage(
    receipts: list[dict[str, str]],
) -> dict[str, int]:
    required = set(load_case_registry())
    covered = {receipt["case_id"] for receipt in receipts}
    missing = sorted(required - covered)
    if missing:
        raise ContractError(
            "SMOKE_EVIDENCE_MISSING",
            "Required preregistered smoke evidence is missing.",
        )
    return {
        "required_case_count": len(required),
        "covered_case_count": len(required & covered),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    digest_parser = subparsers.add_parser("digest")
    digest_parser.add_argument("paths", nargs="*")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("receipts", nargs="+")
    validate_parser.add_argument("--require-pass", action="store_true")
    validate_parser.add_argument("--require-current-digest", action="store_true")
    validate_parser.add_argument("--require-single", action="store_true")
    validate_parser.add_argument("--require-case")
    existing_parser = subparsers.add_parser(
        "validate-existing",
        help="Validate tracked receipts when present; an empty directory is valid for PR CI.",
    )
    existing_parser.add_argument(
        "--directory",
        default=str(ROOT / "smoke" / "receipts"),
    )
    existing_parser.add_argument("--require-pass", action="store_true")
    existing_parser.add_argument("--require-current-digest", action="store_true")
    existing_parser.add_argument(
        "--require-all-cases",
        action="store_true",
        help="Require validated evidence for every preregistered smoke case.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "digest":
            paths = [Path(item) for item in args.paths] if args.paths else None
            result: dict[str, Any] = {
                "status": "ok",
                "implementation_digest": implementation_digest(paths),
            }
        elif args.command == "validate":
            if args.require_single and len(args.receipts) != 1:
                raise ContractError(
                    "SMOKE_RECEIPT_SET_INVALID",
                    "Exactly one smoke receipt is required.",
                )
            receipts = [
                validate_receipt(
                    Path(item),
                    require_pass=args.require_pass,
                    require_current_digest=args.require_current_digest,
                )
                for item in args.receipts
            ]
            if args.require_case is not None and any(
                receipt["case_id"] != args.require_case
                for receipt in receipts
            ):
                raise ContractError(
                    "SMOKE_CASE_MISMATCH",
                    "Smoke receipt does not match the requested case.",
                )
            result = {"status": "ok", "validated": receipts}
        else:
            directory = Path(args.directory)
            paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
            receipts = [
                validate_receipt(
                    path,
                    require_pass=args.require_pass,
                    require_current_digest=args.require_current_digest,
                )
                for path in paths
            ]
            coverage = (
                validate_required_case_coverage(receipts)
                if args.require_all_cases
                else {}
            )
            result = {
                "status": "ok",
                "validated": receipts,
                "receipt_count": len(receipts),
                **coverage,
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ContractError, OSError, ValueError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "SMOKE_RECEIPT_INVALID"
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {"code": code, "message": "Smoke receipt validation failed."},
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
