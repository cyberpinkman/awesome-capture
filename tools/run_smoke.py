#!/usr/bin/env python3
"""Run one preregistered smoke case and emit a sanitized receipt."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "smoke" / "cases.json"
DOWNLOAD_SCRIPT = ROOT / "skills" / "download-video" / "scripts" / "download_video.py"
TRANSCRIBE_SCRIPT = (
    ROOT / "skills" / "transcribe-media" / "scripts" / "transcribe_media.py"
)
sys.path.insert(0, str(ROOT))

from contracts.contract_runtime import (  # noqa: E402
    ContractError,
    loads_strict,
    read_json_strict,
    validate_contract,
    validate_file_context,
)
from contracts.media_runtime import SafeRuntimeError, secure_mkdirs  # noqa: E402
from contracts.posix_runtime import (  # noqa: E402
    PosixRuntimeError,
    atomic_write_noclobber,
    ensure_dir,
)
from tools.smoke_receipts import (  # noqa: E402
    implementation_digest,
    validate_case_evidence,
    validate_receipt as validate_smoke_receipt,
)


CASES_SCHEMA = "awesome-capture.smoke-cases/v1"
RECEIPT_SCHEMA = "awesome-capture.smoke-receipt/v1"
CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ENV_NAME_PATTERN = re.compile(r"^AWESOME_CAPTURE_SMOKE_[A-Z0-9_]+$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_STRING_PATTERN = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|[/\\@]|"
    r"\b(?:host(?:name)?|user(?:name)?|login|cookie|authorization|"
    r"bearer|token|secret|password|api[_-]?key|header)\s*[:=]|"
    r"\bbuilt\s+on\b|"
    r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:local|lan|internal)\b|"
    r"\b[^ \t\r\n/\\]+\.(?:mp4|mov|mkv|webm|avi|wav|mp3|m4a|flac|"
    r"aac|srt|vtt|log|txt|md|json|bin|gguf|model)\b)",
    re.IGNORECASE,
)
SAFE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+():-]{0,127}$")


class SmokeError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _strict_cases(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "cases"}:
        raise SmokeError("INVALID_CASES", "Smoke cases root is invalid.")
    if value["schema_version"] != CASES_SCHEMA or not isinstance(value["cases"], list):
        raise SmokeError("INVALID_CASES", "Smoke cases schema is unsupported.")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    common = {"case_id", "suite", "platform", "source_env"}
    for raw in value["cases"]:
        if not isinstance(raw, dict):
            raise SmokeError("INVALID_CASES", "Every smoke case must be an object.")
        suite = raw.get("suite")
        required = (
            common
            if suite == "download"
            else common | {"engine", "model_env"} | ({"binary_env"} if raw.get("case_id") in {
                "whisper-cpp-local",
                "whisper-cpp-cpu",
                "whisper-cpp-gpu-fallback",
                "external-local",
                "external-long-resume",
            } else set())
            if suite == "transcription"
            else set()
        )
        allowed = required | {"expectation", "required_tools"}
        if not required or not required.issubset(raw) or not set(raw).issubset(allowed):
            raise SmokeError("INVALID_CASES", "Smoke case fields are invalid.")
        if (
            not isinstance(raw.get("case_id"), str)
            or CASE_ID_PATTERN.fullmatch(raw["case_id"]) is None
            or raw["case_id"] in seen
        ):
            raise SmokeError("INVALID_CASES", "Smoke case id is invalid or duplicated.")
        seen.add(raw["case_id"])
        expected_platforms = (
            {"douyin", "tiktok", "bilibili", "youtube", "twitter"}
            if suite == "download"
            else {"local"}
        )
        if raw.get("platform") not in expected_platforms:
            raise SmokeError("INVALID_CASES", "Smoke case platform is invalid.")
        if suite == "transcription" and raw.get("engine") not in {
            "whisper-cpp",
            "faster-whisper",
            "mlx-whisper",
            "external",
        }:
            raise SmokeError("INVALID_CASES", "Smoke case engine is invalid.")
        for key, item in raw.items():
            if key.endswith("_env") and (
                not isinstance(item, str) or ENV_NAME_PATTERN.fullmatch(item) is None
            ):
                raise SmokeError("INVALID_CASES", "Smoke environment reference is invalid.")
            if key in {"case_id", "suite", "platform"} and not isinstance(item, str):
                raise SmokeError("INVALID_CASES", "Smoke case strings are invalid.")
        expectation = raw.get("expectation")
        valid_expectations = (
            {"ephemeral_browser", "gallery-dl"}
            if suite == "download"
            else {"cpu_only", "gpu_fallback", "sigkill_resume"}
        )
        if expectation is not None and expectation not in valid_expectations:
            raise SmokeError("INVALID_CASES", "Smoke case expectation is invalid.")
        required_tools = raw.get("required_tools")
        if (
            not isinstance(required_tools, list)
            or not required_tools
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+ -]{0,127}", item)
                is None
                for item in required_tools
            )
            or len(required_tools) != len(set(required_tools))
        ):
            raise SmokeError(
                "INVALID_CASES",
                "Smoke case required tools are invalid.",
            )
        cases.append(dict(raw))
    return cases


def load_cases() -> list[dict[str, Any]]:
    try:
        raw = CASES_PATH.read_bytes()
    except OSError as exc:
        raise SmokeError("INVALID_CASES", "Cannot read the preregistered smoke cases.") from exc
    try:
        value = loads_strict(raw, max_bytes=1024 * 1024)
    except ContractError as exc:
        raise SmokeError("INVALID_CASES", "Smoke cases JSON is invalid.") from exc
    return _strict_cases(value)


def select_case(case_id: str) -> dict[str, Any]:
    if CASE_ID_PATTERN.fullmatch(case_id) is None:
        raise SmokeError("UNKNOWN_CASE", "Smoke case alias is invalid.")
    matches = [case for case in load_cases() if case["case_id"] == case_id]
    if len(matches) != 1:
        raise SmokeError("UNKNOWN_CASE", "Smoke case alias is not preregistered.")
    return matches[0]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_version(value: str) -> str:
    first = value.replace("\0", "").splitlines()[0].strip() if value else ""
    if not first or PRIVATE_STRING_PATTERN.search(first):
        return "unavailable"
    normalized = re.sub(r"[^A-Za-z0-9 ._+():-]+", " ", first)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or SAFE_VERSION_PATTERN.fullmatch(normalized) is None:
        return "unavailable"
    return normalized


def _tool_version(name: str, command: Sequence[str]) -> dict[str, str]:
    executable = shutil.which(command[0])
    if not executable:
        return {"name": name, "version": "unavailable"}
    try:
        process = subprocess.run(
            [executable, *command[1:]],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"name": name, "version": "unavailable"}
    return {"name": name, "version": _safe_version(process.stdout or process.stderr)}


def _package_tool_version(name: str, distribution: str) -> dict[str, str]:
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        version = "unavailable"
    return {"name": name, "version": _safe_version(version)}


def _chromium_tool_version() -> dict[str, str]:
    script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        " b=p.chromium.launch(headless=True)\n"
        " try: print(b.version)\n"
        " finally: b.close()\n"
    )
    return _tool_version(
        "chromium",
        (sys.executable, "-c", script),
    )


def collect_tools(extra: Sequence[dict[str, str]] = ()) -> list[dict[str, str]]:
    tools = [
        {"name": "python", "version": platform.python_version()},
        _tool_version("ffmpeg", ("ffmpeg", "-version")),
        _tool_version("ffprobe", ("ffprobe", "-version")),
    ]
    for item in extra:
        candidate_name = str(item.get("name") or "")[:128]
        name = (
            candidate_name
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+ -]{0,127}", candidate_name)
            else "unknown-tool"
        )
        version = _safe_version(str(item.get("version") or ""))
        if name and not any(existing["name"] == name for existing in tools):
            tools.append({"name": name, "version": version})
    return tools


def _commit_sha() -> str:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    value = process.stdout.strip().lower()
    if process.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is None:
        raise SmokeError("GIT_IDENTITY_UNAVAILABLE", "Cannot resolve the tested commit.")
    return value


def _environment() -> dict[str, str]:
    system = platform.system()
    os_name = "linux" if system == "Linux" else "macos" if system == "Darwin" else ""
    python = platform.python_version()
    if not os_name or re.fullmatch(r"3\.(?:11|12|13|14)(?:\.[0-9]+)?", python) is None:
        raise SmokeError("UNSUPPORTED_PLATFORM", "Smoke harness requires supported POSIX Python.")
    architecture = _safe_version(platform.machine())[:64]
    return {"os": os_name, "arch": architecture, "python": python}


def _run_json(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[int, dict[str, Any] | None, str]:
    try:
        process = runner(
            list(command),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=7200,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 5, None, exc.__class__.__name__
    payload: dict[str, Any] | None = None
    if process.stdout.strip():
        try:
            parsed = loads_strict(process.stdout, max_bytes=4 * 1024 * 1024)
            payload = parsed if isinstance(parsed, dict) else None
        except ContractError:
            payload = None
    error_code = ""
    error_payload_valid = False
    if process.stderr.strip():
        try:
            parsed_error = loads_strict(process.stderr, max_bytes=4 * 1024 * 1024)
            if isinstance(parsed_error, dict):
                error = parsed_error.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    candidate = error["code"][:128]
                    if re.fullmatch(r"[A-Z][A-Z0-9_]*", candidate):
                        error_code = candidate
                        error_payload_valid = parsed_error.get("status") == "error"
        except ContractError:
            pass
    if process.returncode == 0:
        if process.stderr != "" or payload is None:
            return 7, None, "CLI_PROTOCOL_VIOLATION"
    elif process.stdout != "" or not error_payload_valid:
        return process.returncode or 7, None, "CLI_PROTOCOL_VIOLATION"
    return process.returncode, payload, error_code or "COMMAND_FAILED"


def _detect_download_source(
    source: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str, str, bool]:
    return_code, payload, unused_error = _run_json(
        [sys.executable, str(DOWNLOAD_SCRIPT), "detect", source],
        runner=runner,
    )
    del unused_error
    if (
        return_code == 0
        and isinstance(payload, dict)
        and isinstance(payload.get("platform"), str)
        and isinstance(payload.get("source_fingerprint"), str)
        and SHA_PATTERN.fullmatch(payload["source_fingerprint"])
    ):
        return payload["platform"], payload["source_fingerprint"], True
    return "", hashlib.sha256(source.encode("utf-8")).hexdigest(), False


def _reverify_video_media(
    artifact: Mapping[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    """Independently probe current media bytes instead of trusting artifact facts."""

    media = artifact.get("media")
    if not isinstance(media, dict) or not isinstance(media.get("path"), str):
        return False
    try:
        process = runner(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name:stream=codec_type",
                "-of",
                "json",
                media["path"],
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if process.returncode != 0 or process.stderr:
            return False
        probed = loads_strict(process.stdout, max_bytes=1024 * 1024)
        if not isinstance(probed, dict):
            return False
        streams = probed.get("streams")
        format_value = probed.get("format")
        if not isinstance(streams, list) or not isinstance(format_value, dict):
            return False
        duration = float(format_value.get("duration"))
        if not (duration > 0 and duration < float("inf")):
            return False
        video_streams = sum(
            isinstance(item, dict) and item.get("codec_type") == "video"
            for item in streams
        )
        audio_streams = sum(
            isinstance(item, dict) and item.get("codec_type") == "audio"
            for item in streams
        )
        observed = {
            "duration_ms": round(duration * 1000),
            "has_video": video_streams > 0,
            "has_audio": audio_streams > 0,
            "container": str(format_value.get("format_name") or ""),
            "video_streams": video_streams,
            "audio_streams": audio_streams,
        }
        return all(media.get(key) == value for key, value in observed.items())
    except (ContractError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        return False


def _download_case(
    case: Mapping[str, Any],
    source: str,
    work_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    detected_platform, fingerprint, detected = _detect_download_source(
        source, runner=runner
    )
    assertions = [
        {"name": "registered-source-detected", "passed": detected},
        {
            "name": "registered-platform-matches",
            "passed": detected_platform == case["platform"],
        },
    ]
    base = {
        "source": {
            "platform": case["platform"],
            "fingerprint": fingerprint,
            "auth_mode": "anonymous",
            "fallback": None,
        },
        "engine": None,
        "artifacts": [],
        "assertions": assertions,
        "warnings": [],
        "tools": collect_tools(
            [_tool_version("yt-dlp", ("yt-dlp", "--version"))]
        ),
    }
    if not all(assertion["passed"] for assertion in assertions):
        base["warnings"].append("registered-source-detection-failed")
        return base
    return_code, payload, error_code = _run_json(
        [
            sys.executable,
            str(DOWNLOAD_SCRIPT),
            "download",
            source,
            "--output-dir",
            str(work_dir / "download"),
            "--lock-timeout",
            "30",
        ],
        runner=runner,
    )
    assertions.append({"name": "download-command-succeeded", "passed": return_code == 0})
    if return_code != 0 or not isinstance(payload, dict):
        base["warnings"].append(f"download-error-{error_code.lower()}")
        return base
    try:
        artifact_path = Path(str(payload["artifact_path"]))
        artifact = read_json_strict(artifact_path, expected="video-artifact")
        validate_file_context(artifact)
        artifact_valid = True
    except (KeyError, OSError, ContractError, ValueError):
        artifact_valid = False
        artifact = None
        artifact_path = Path()
    media_reverified = (
        artifact_valid
        and isinstance(artifact, dict)
        and _reverify_video_media(artifact, runner=runner)
    )
    assertions.append({"name": "video-artifact-v2-valid", "passed": artifact_valid})
    assertions.append(
        {"name": "video-media-reverified", "passed": media_reverified}
    )
    if artifact_valid and isinstance(artifact, dict):
        base["source"] = {
            "platform": artifact["source"]["platform"],
            "fingerprint": artifact["source"]["fingerprint"],
            "auth_mode": artifact["acquisition"]["auth_mode"],
            "fallback": artifact["acquisition"]["fallback"],
        }
        base["artifacts"] = [
            {"type": "video-artifact", "sha256": _sha256_file(artifact_path)}
        ]
        producer = artifact["producer"]
        route_tools: list[dict[str, str]] = [
            _tool_version("yt-dlp", ("yt-dlp", "--version")),
            {
                "name": str(producer.get("tool") or "download-video"),
                "version": str(producer.get("version") or "unknown"),
            },
        ]
        if artifact["source"]["platform"] == "youtube":
            route_tools.extend(
                [
                    _tool_version("deno", ("deno", "--version")),
                    _package_tool_version("yt-dlp-ejs", "yt-dlp-ejs"),
                ]
            )
        if artifact["acquisition"]["fallback"] == "gallery-dl":
            route_tools.append(
                _tool_version("gallery-dl", ("gallery-dl", "--version"))
            )
        if artifact["acquisition"]["auth_mode"] == "ephemeral_browser":
            route_tools.extend(
                [
                    _package_tool_version("playwright", "playwright"),
                    _chromium_tool_version(),
                ]
            )
        base["tools"] = collect_tools(
            route_tools
        )
        warning_count = len(artifact["acquisition"]["warnings"])
        if warning_count:
            base["warnings"].append(f"download-reported-{warning_count}-warnings")
    expectation = case.get("expectation")
    if expectation is None:
        observed = (
            isinstance(artifact, dict)
            and artifact["acquisition"]["auth_mode"] == "anonymous"
            and artifact["acquisition"]["fallback"] in {None, "none"}
        )
        assertions.append(
            {
                "name": "anonymous-route-observed",
                "passed": observed,
            }
        )
    else:
        observed = (
            isinstance(artifact, dict)
            and artifact["acquisition"]["fallback"] == expectation
            and (
                expectation != "ephemeral_browser"
                or artifact["acquisition"]["auth_mode"] == "ephemeral_browser"
            )
        )
        assertions.append(
            {
                "name": f"required-{expectation}-observed",
                "passed": observed,
            }
        )
    return base


def _transcription_command(
    case: Mapping[str, Any],
    *,
    source: str,
    model: str,
    binary: str | None,
    output_dir: Path,
) -> list[str]:
    case_id = case["case_id"]
    engine = case["engine"]
    command = [
        sys.executable,
        str(TRANSCRIBE_SCRIPT),
        "transcribe",
        source,
        "--output-dir",
        str(output_dir),
        "--engine",
        engine,
        "--model",
        model,
        "--lock-timeout",
        "30",
    ]
    if engine == "whisper-cpp":
        if not binary:
            raise SmokeError("SMOKE_ENV_MISSING", "Local whisper.cpp binary is not configured.")
        command.extend(["--whisper-cpp-bin", binary])
        if case_id == "whisper-cpp-cpu":
            command.append("--whisper-cpp-cpu-only")
    elif engine == "external":
        if not binary:
            raise SmokeError("SMOKE_ENV_MISSING", "Local external adapter is not configured.")
        command.extend(["--adapter", binary, "--trust-external-adapter"])
        if case_id == "external-long-resume":
            command.extend(["--chunk-seconds", "30"])
    return command


def _private_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _read_private_json_value(
    path: Path,
    *,
    maximum_bytes: int = 64 * 1024 * 1024,
) -> tuple[Any, os.stat_result, str]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > maximum_bytes
        ):
            raise SmokeError(
                "INVALID_PARTIAL_RESULT",
                "Partial transcription evidence is not a private managed file.",
                exit_code=7,
            )
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not block:
                break
            blocks.append(block)
            total += len(block)
            if total > maximum_bytes:
                raise SmokeError(
                    "INVALID_PARTIAL_RESULT",
                    "Partial transcription evidence exceeds its size limit.",
                    exit_code=7,
                )
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise SmokeError(
                "INVALID_PARTIAL_RESULT",
                "Partial transcription evidence changed while it was read.",
                exit_code=7,
            )
        raw = b"".join(blocks)
        try:
            value = loads_strict(raw, max_bytes=maximum_bytes)
        except ContractError as exc:
            raise SmokeError(
                "INVALID_PARTIAL_RESULT",
                "Partial transcription evidence is not strict JSON.",
                exit_code=7,
            ) from exc
        return value, after, hashlib.sha256(raw).hexdigest()
    except OSError as exc:
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Partial transcription evidence could not be read safely.",
            exit_code=7,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _chunk_reference(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
) -> dict[str, Any]:
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "count": manifest["count"],
        "timeline": [
            {
                "index": item["index"],
                "offset_ms": item["offset_ms"],
                "duration_ms": item["duration_ms"],
                "bytes": item["bytes"],
                "sha256": item["sha256"],
            }
            for item in manifest["chunks"]
        ],
    }


def _validated_chunk_result_snapshot(
    workspace: Path,
    *,
    completion: str,
) -> dict[str, Any]:
    if completion not in {"partial", "complete"}:
        raise ValueError("completion must be partial or complete")
    if (
        not _private_directory(workspace)
        or SHA_PATTERN.fullmatch(workspace.name) is None
    ):
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Transcription workspace identity is unsafe.",
            exit_code=7,
        )
    chunks_dir = workspace / "chunks"
    results_dir = workspace / "chunk-results"
    if not _private_directory(chunks_dir) or not _private_directory(results_dir):
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Chunk evidence directories are unsafe.",
            exit_code=7,
        )

    state, _, _ = _read_private_json_value(workspace / "state.json")
    manifest_path = chunks_dir / "chunks.manifest.json"
    manifest, _, manifest_sha256 = _read_private_json_value(manifest_path)
    try:
        validate_contract(state, expected="transcription-state")
        validate_contract(manifest, expected="chunk-set")
        validate_file_context(manifest, verify_chunks=True)
    except ContractError as exc:
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Partial transcription contract evidence is invalid.",
            exit_code=7,
        ) from exc
    if (
        state["job_id"] != workspace.name
        or manifest["job_id"] != workspace.name
        or manifest["source_sha256"] != state["settings"]["source_sha256"]
        or manifest["count"] <= 1
    ):
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Partial transcription identities do not match.",
            exit_code=7,
        )
    for item in manifest["chunks"]:
        if Path(item["path"]) != chunks_dir / item["name"]:
            raise SmokeError(
                "INVALID_PARTIAL_RESULT",
                "Chunk manifest escapes its transcription workspace.",
                exit_code=7,
            )

    expected = {
        f"{item['name']}.result.json": item
        for item in manifest["chunks"]
    }
    names = sorted(item.name for item in results_dir.iterdir())
    if any(name not in expected for name in names):
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Chunk result directory contains an unknown entry.",
            exit_code=7,
        )
    if completion == "partial":
        valid_count = 0 < len(names) < manifest["count"]
    else:
        valid_count = len(names) == manifest["count"]
    if not valid_count:
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Chunk result count does not match the requested completion state.",
            exit_code=7,
        )

    records: dict[str, Any] = {}
    evidence: dict[str, dict[str, Any]] = {}
    envelope_keys = {
        "schema_version",
        "job_id",
        "settings_sha256",
        "execution_guard_sha256",
        "chunk_manifest_sha256",
        "chunk_name",
        "result",
    }
    for name in names:
        path = results_dir / name
        envelope, metadata, result_sha256 = _read_private_json_value(path)
        chunk = expected[name]
        if (
            not isinstance(envelope, dict)
            or set(envelope) != envelope_keys
            or envelope.get("schema_version") != "awesome-capture.chunk-result/v1"
            or envelope.get("job_id") != workspace.name
            or envelope.get("settings_sha256") != state["settings_sha256"]
            or envelope.get("execution_guard_sha256")
            != state["execution_guard_sha256"]
            or envelope.get("chunk_manifest_sha256") != manifest_sha256
            or envelope.get("chunk_name") != chunk["name"]
        ):
            raise SmokeError(
                "INVALID_PARTIAL_RESULT",
                "Immutable chunk result identity is invalid.",
                exit_code=7,
            )
        records[chunk["name"]] = envelope["result"]
        evidence[chunk["name"]] = {
            "sha256": result_sha256,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
        }
    if sorted(item.name for item in results_dir.iterdir()) != names:
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Chunk result set changed while it was validated.",
            exit_code=7,
        )

    reference = _chunk_reference(manifest_path, manifest, manifest_sha256)
    synthetic_state = {
        **state,
        "status": "running",
        "chunk_set": reference,
        "chunks": records,
    }
    try:
        validate_contract(synthetic_state, expected="transcription-state")
    except ContractError as exc:
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Immutable chunk result content is invalid.",
            exit_code=7,
        ) from exc
    if state["status"] == "running":
        if state["chunk_set"] not in (None, reference) or any(
            records.get(name) != record
            for name, record in state["chunks"].items()
        ):
            raise SmokeError(
                "INVALID_PARTIAL_RESULT",
                "Running state contradicts immutable chunk results.",
                exit_code=7,
            )
    elif (
        completion != "complete"
        or state["status"] != "complete"
        or state["chunk_set"] != reference
        or state["chunks"] != records
    ):
        raise SmokeError(
            "INVALID_PARTIAL_RESULT",
            "Completed state contradicts immutable chunk results.",
            exit_code=7,
        )
    return {
        "workspace": workspace,
        "job_id": workspace.name,
        "manifest_sha256": manifest_sha256,
        "expected_count": manifest["count"],
        "results": evidence,
    }


def _find_partial_chunk_results(output_dir: Path) -> dict[str, Any] | None:
    transcriptions = (
        output_dir
        / ".awesome-capture-media"
        / "v2"
        / "transcriptions"
    )
    if not _private_directory(transcriptions):
        return None
    candidates: list[dict[str, Any]] = []
    for workspace in sorted(transcriptions.iterdir(), key=lambda item: item.name):
        try:
            candidates.append(
                _validated_chunk_result_snapshot(
                    workspace,
                    completion="partial",
                )
            )
        except (OSError, SmokeError, ContractError, KeyError, TypeError, ValueError):
            continue
    return candidates[0] if len(candidates) == 1 else None


def _completed_run_reused_partial_results(
    partial: Mapping[str, Any] | None,
    artifact: Mapping[str, Any],
) -> bool:
    if partial is None:
        return False
    try:
        workspace = partial["workspace"]
        if not isinstance(workspace, Path):
            return False
        completed = _validated_chunk_result_snapshot(
            workspace,
            completion="complete",
        )
        chunk_set = artifact["transcription"]["chunk_set"]
        if (
            completed["job_id"] != partial["job_id"]
            or completed["manifest_sha256"] != partial["manifest_sha256"]
            or completed["expected_count"] != partial["expected_count"]
            or not isinstance(chunk_set, dict)
            or chunk_set["manifest_sha256"] != partial["manifest_sha256"]
            or chunk_set["count"] != partial["expected_count"]
        ):
            return False
        current_results = completed["results"]
        return all(
            current_results.get(name) == identity
            for name, identity in partial["results"].items()
        )
    except (OSError, SmokeError, ContractError, KeyError, TypeError, ValueError):
        return False


def _kill_after_partial_results(
    command: Sequence[str],
    output_dir: Path,
    *,
    timeout_seconds: float = 20 * 60,
    poll_seconds: float = 0.1,
) -> tuple[dict[str, Any] | None, bool]:
    """SIGKILL only after a strict, durable, incomplete result set exists."""

    process = subprocess.Popen(
        list(command),
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=False,
    )
    observed_partial: dict[str, Any] | None = None
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    try:
        while process.poll() is None and time.monotonic() < deadline:
            observed_partial = _find_partial_chunk_results(output_dir)
            if observed_partial is not None:
                try:
                    process.send_signal(signal.SIGKILL)
                except ProcessLookupError:
                    observed_partial = None
                break
            time.sleep(max(0.01, poll_seconds))
        if process.poll() is None:
            process.send_signal(signal.SIGKILL)
        return_code = process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    return observed_partial, return_code == -signal.SIGKILL


def _transcription_case(
    case: Mapping[str, Any],
    source: str,
    model: str,
    binary: str | None,
    work_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    source_path = Path(source)
    fingerprint = (
        _sha256_file(source_path)
        if source_path.is_file() and not source_path.is_symlink()
        else "0" * 64
    )
    assertions = [
        {
            "name": "registered-local-media-exists",
            "passed": source_path.is_file() and not source_path.is_symlink(),
        },
        {
            "name": "explicit-local-model-exists",
            "passed": Path(model).exists() and not Path(model).is_symlink(),
        },
    ]
    if "binary_env" in case:
        binary_path = Path(binary or "")
        assertions.append(
            {
                "name": "explicit-local-binary-exists",
                "passed": bool(binary)
                and binary_path.is_file()
                and not binary_path.is_symlink(),
            }
        )
    base = {
        "source": {
            "platform": "local",
            "fingerprint": fingerprint,
            "auth_mode": "not-applicable",
            "fallback": None,
        },
        "engine": None,
        "artifacts": [],
        "assertions": assertions,
        "warnings": [],
        "tools": collect_tools(),
    }
    if not all(assertion["passed"] for assertion in assertions):
        base["warnings"].append("local-smoke-input-missing-or-unsafe")
        return base
    command = _transcription_command(
        case,
        source=source,
        model=model,
        binary=binary,
        output_dir=work_dir / "transcription",
    )
    expectation = case.get("expectation")
    partial_results: dict[str, Any] | None = None
    if expectation == "sigkill_resume":
        partial_results, killed = _kill_after_partial_results(
            command,
            work_dir / "transcription",
        )
        assertions.extend(
            [
                {
                    "name": "partial-chunk-state-observed",
                    "passed": partial_results is not None,
                },
                {"name": "transcription-process-sigkilled", "passed": killed},
            ]
        )
    return_code, payload, error_code = _run_json(command, runner=runner)
    assertions.append(
        {"name": "transcription-command-succeeded", "passed": return_code == 0}
    )
    if return_code != 0 or not isinstance(payload, dict):
        base["warnings"].append(f"transcription-error-{error_code.lower()}")
        return base
    try:
        transcript_path = Path(str(payload["transcript_path"]))
        artifact = read_json_strict(transcript_path, expected="transcript-artifact")
        validate_file_context(
            artifact,
            verify_source=True,
            verify_outputs=True,
            verify_chunks=True,
        )
        chunk_reference = artifact["transcription"]["chunk_set"]
        if chunk_reference is not None:
            chunk_manifest_path = Path(chunk_reference["manifest_path"])
            chunk_manifest = read_json_strict(
                chunk_manifest_path, expected="chunk-set"
            )
            validate_file_context(chunk_manifest, verify_chunks=True)
            if (
                _sha256_file(chunk_manifest_path)
                != chunk_reference["manifest_sha256"]
                or chunk_manifest["count"] != chunk_reference["count"]
            ):
                raise ContractError(
                    "FILE_CONTEXT_MISMATCH",
                    "Transcript chunk-set reference changed.",
                )
        artifact_valid = True
    except (KeyError, OSError, ContractError, ValueError):
        artifact_valid = False
        artifact = None
        transcript_path = Path()
    assertions.append(
        {"name": "transcript-artifact-v2-valid", "passed": artifact_valid}
    )
    assertions.append(
        {"name": "transcript-evidence-reverified", "passed": artifact_valid}
    )
    if artifact_valid and isinstance(artifact, dict):
        transcription = artifact["transcription"]
        identity = transcription["engine_identity"]
        model_identity = identity["model"]
        adapter_identity = identity["adapter"]
        base["source"]["fingerprint"] = artifact["source"]["sha256"]
        base["engine"] = {
            "name": transcription["engine"],
            "identity_sha256": identity["identity_sha256"],
            "model_sha256": model_identity["sha256"] if model_identity else None,
            "adapter_sha256": adapter_identity["sha256"] if adapter_identity else None,
        }
        base["artifacts"] = [
            {"type": "transcript-artifact", "sha256": _sha256_file(transcript_path)}
        ]
        extras = list(identity["packages"])
        executable = identity.get("executable")
        adapter = identity.get("adapter")
        for name, component in (("whisper-cpp", executable), ("external-adapter", adapter)):
            if component:
                extras.append(
                    {
                        "name": name,
                        "version": component.get("version")
                        or f"sha256-{component['sha256'][:12]}",
                    }
                )
        base["tools"] = collect_tools(extras)
        warning_count = len(artifact["warnings"])
        if warning_count:
            base["warnings"].append(
                f"transcription-reported-{warning_count}-warnings"
            )
        if expectation == "cpu_only":
            assertions.append(
                {
                    "name": "whisper-cpp-cpu-only-observed",
                    "passed": transcription["gpu_fallback_count"] == 0
                    and transcription["devices_used"] == ["cpu"],
                }
            )
        elif expectation == "gpu_fallback":
            assertions.append(
                {
                    "name": "whisper-cpp-gpu-fallback-observed",
                    "passed": transcription["gpu_fallback_count"] > 0
                    and "cpu" in transcription["devices_used"],
                }
            )
        elif expectation == "sigkill_resume":
            partial_reused = _completed_run_reused_partial_results(
                partial_results,
                artifact,
            )
            assertions.append(
                {
                    "name": "long-transcription-resumed-after-sigkill",
                    "passed": payload.get("result") in {"created", "recovered"}
                    and partial_reused,
                }
            )
            assertions.append(
                {
                    "name": "partial-chunk-results-reused",
                    "passed": partial_reused,
                }
            )
    return base


def _receipt_path(receipt_dir: Path, case_id: str, created_at: str) -> Path:
    stamp = created_at.replace("+00:00", "Z").replace("-", "").replace(":", "")
    return receipt_dir / f"{case_id}-{stamp}-{uuid.uuid4().hex[:12]}.json"


def _write_receipt(path: Path, value: dict[str, Any]) -> None:
    validate_contract(value, expected="smoke-receipt")
    validate_case_evidence(value)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    try:
        ensure_dir(path.parent, 0o700, private=True)
        atomic_write_noclobber(path, payload, 0o600)
    except PosixRuntimeError as exc:
        code = "RECEIPT_COLLISION" if exc.code == "PATH_COLLISION" else exc.code
        raise SmokeError(code, "Smoke receipt could not be published safely.", exit_code=4) from exc
    validate_smoke_receipt(
        path,
        require_pass=value["outcome"] == "pass",
        require_current_digest=False,
    )


def run_case(
    case_id: str,
    *,
    receipt_dir: Path,
    environ: Mapping[str, str] = os.environ,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], Path]:
    case = select_case(case_id)
    source = environ.get(case["source_env"], "")
    created_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    with tempfile.TemporaryDirectory(prefix=f"awesome-capture-smoke-{case_id}-") as temporary:
        try:
            # Keep the harness path spelling aligned with the managed media
            # runtime. On macOS, tempfile may return the fixed /var or /tmp
            # aliases while published manifests use /private/var or
            # /private/tmp. secure_mkdirs performs the POSIX no-follow checks
            # and returns the runtime's canonical spelling for those aliases.
            work_dir = secure_mkdirs(Path(temporary))
        except SafeRuntimeError as exc:
            raise SmokeError(
                exc.code,
                "Smoke working directory is unsafe.",
                exit_code=exc.exit_code,
            ) from exc
        if not source:
            details: dict[str, Any] = {
                "source": {
                    "platform": case["platform"],
                    "fingerprint": "0" * 64,
                    "auth_mode": "anonymous"
                    if case["suite"] == "download"
                    else "not-applicable",
                    "fallback": None,
                },
                "engine": None,
                "artifacts": [],
                "assertions": [
                    {"name": "registered-source-configured", "passed": False}
                ],
                "warnings": ["registered-source-environment-missing"],
                "tools": collect_tools(),
            }
        elif case["suite"] == "download":
            details = _download_case(case, source, work_dir, runner=runner)
        else:
            model = environ.get(case["model_env"], "")
            binary_env = case.get("binary_env")
            binary = environ.get(binary_env, "") if binary_env else None
            if not model:
                details = {
                    "source": {
                        "platform": "local",
                        "fingerprint": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                        "auth_mode": "not-applicable",
                        "fallback": None,
                    },
                    "engine": None,
                    "artifacts": [],
                    "assertions": [
                        {"name": "explicit-local-model-configured", "passed": False}
                    ],
                    "warnings": ["local-model-environment-missing"],
                    "tools": collect_tools(),
                }
            else:
                details = _transcription_case(
                    case,
                    source,
                    model,
                    binary,
                    work_dir,
                    runner=runner,
                )
    tool_versions = {
        item["name"]: item["version"]
        for item in details["tools"]
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("version"), str)
    }
    details["assertions"].append(
        {
            "name": "required-tools-observed",
            "passed": all(
                tool_versions.get(name) not in {None, "unavailable"}
                for name in case["required_tools"]
            ),
        }
    )
    outcome = (
        "pass"
        if details["assertions"]
        and all(assertion["passed"] for assertion in details["assertions"])
        else "fail"
    )
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "case_id": case_id,
        "created_at": created_at,
        "outcome": outcome,
        "commit_sha": _commit_sha(),
        "implementation_digest": implementation_digest(),
        "environment": _environment(),
        "tools": details["tools"],
        "source": details["source"],
        "engine": details["engine"],
        "artifacts": details["artifacts"],
        "assertions": details["assertions"],
        "warnings": details["warnings"],
    }
    # Canonical validation includes the recursive URL/private-path secret scan.
    validate_contract(receipt, expected="smoke-receipt")
    validate_case_evidence(receipt)
    destination = _receipt_path(receipt_dir, case_id, created_at)
    _write_receipt(destination, receipt)
    return receipt, destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("case_id", help="Exact case_id from smoke/cases.json")
    run_parser.add_argument(
        "--receipt-dir",
        type=Path,
        default=ROOT / "smoke" / "receipts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            result = {
                "status": "ok",
                "cases": [
                    {
                        "case_id": case["case_id"],
                        "suite": case["suite"],
                        "platform": case["platform"],
                    }
                    for case in load_cases()
                ],
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        receipt, path = run_case(args.case_id, receipt_dir=args.receipt_dir)
        result = {
            "status": "ok" if receipt["outcome"] == "pass" else "error",
            "case_id": receipt["case_id"],
            "outcome": receipt["outcome"],
            "receipt": path.name,
            "receipt_sha256": _sha256_file(path),
        }
        stream = sys.stdout if receipt["outcome"] == "pass" else sys.stderr
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=stream)
        return 0 if receipt["outcome"] == "pass" else 1
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "code": "INTERRUPTED",
                        "message": "Smoke harness was interrupted.",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 130
    except (SmokeError, ContractError, OSError, ValueError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, (SmokeError, ContractError)) else "SMOKE_HARNESS_FAILED"
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "code": code,
                        "message": "Smoke harness rejected the request.",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return exc.exit_code if isinstance(exc, SmokeError) else 2


if __name__ == "__main__":
    raise SystemExit(main())
