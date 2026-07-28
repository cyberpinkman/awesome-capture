#!/usr/bin/env python3
"""Validate sanitized smoke receipts and compute implementation digests."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
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
        or value["schema_version"] != "awesome-capture.smoke-cases/v1"
        or not isinstance(value["cases"], list)
    ):
        raise ContractError(
            "SMOKE_CASES_INVALID",
            "Preregistered smoke cases are malformed.",
        )
    registry: dict[str, dict[str, Any]] = {}
    for raw in value["cases"]:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("case_id"), str)
            or raw["case_id"] in registry
            or raw.get("suite") not in {"download", "transcription"}
            or not isinstance(raw.get("platform"), str)
            or not isinstance(raw.get("required_tools"), list)
            or not raw["required_tools"]
            or any(
                not isinstance(item, str)
                for item in raw["required_tools"]
            )
            or len(raw["required_tools"]) != len(set(raw["required_tools"]))
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke case identity is malformed.",
            )
        registry[raw["case_id"]] = raw
    return registry


def _required_assertions(case: dict[str, Any]) -> set[str]:
    if case["suite"] == "download":
        required = {
            "registered-source-detected",
            "registered-platform-matches",
            "download-command-succeeded",
            "video-artifact-v2-valid",
            "video-media-reverified",
        }
        if case.get("expectation"):
            required.add(f"required-{case['expectation']}-observed")
        return required
    required = {
        "registered-local-media-exists",
        "explicit-local-model-exists",
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
    if case["suite"] == "download":
        if value["source"]["platform"] != case["platform"]:
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "Download smoke receipt does not match its registered platform.",
            )
        if not passing:
            return
        if value["engine"] is not None:
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "Download smoke receipt must not contain an ASR engine.",
            )
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
        elif value["source"]["fallback"] != expectation:
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
    assertion_names = [item["name"] for item in value["assertions"]]
    if len(assertion_names) != len(set(assertion_names)):
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Smoke assertion names must be unique.",
        )
    missing_assertions = _required_assertions(case) - set(assertion_names)
    if value["outcome"] == "pass" and missing_assertions:
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Passing receipt is missing registered smoke assertions.",
        )
    if value["outcome"] == "pass":
        tool_names = [item["name"] for item in value["tools"]]
        if len(tool_names) != len(set(tool_names)):
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "Smoke tool evidence names must be unique.",
            )
        tools = {item["name"]: item["version"] for item in value["tools"]}
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
            receipts = [
                validate_receipt(
                    Path(item),
                    require_pass=args.require_pass,
                    require_current_digest=args.require_current_digest,
                )
                for item in args.receipts
            ]
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
