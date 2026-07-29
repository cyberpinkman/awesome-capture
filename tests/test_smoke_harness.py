from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "run_smoke.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_smoke_test_module", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_smoke = load_module()
from contracts.contract_runtime import (
    _engine_identity_projection,
    _transcription_settings_identity,
    canonical_json_sha256,
    contract_digest,
)
from tools import smoke_receipts


class SmokeHarnessTests(unittest.TestCase):
    def write_private_json(self, path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)

    def partial_chunk_result_fixture(
        self,
        root: Path,
    ) -> tuple[
        Path,
        Path,
        dict[str, object],
        dict[str, object],
        dict[str, dict[str, object]],
    ]:
        output_dir = root / "output"
        transcriptions = (
            output_dir
            / ".awesome-capture-media"
            / "v2"
            / "transcriptions"
        )
        for directory in (
            output_dir,
            output_dir / ".awesome-capture-media",
            output_dir / ".awesome-capture-media" / "v2",
            transcriptions,
        ):
            directory.mkdir(exist_ok=True)
            os.chmod(directory, 0o700)

        identity_core = {
            "model": {
                "kind": "file",
                "path": str(root / "model.bin"),
                "sha256": "a" * 64,
                "bytes": 1,
            },
            "executable": None,
            "adapter": {
                "kind": "file",
                "path": str(root / "adapter"),
                "sha256": "b" * 64,
                "bytes": 1,
            },
            "packages": [],
        }
        settings = {
            "contract_digest": contract_digest(),
            "algorithm": {
                "version": "awesome-capture.transcription-algorithm/v1",
                "sha256": "c" * 64,
            },
            "source_path": str(root / "source.wav"),
            "source_sha256": "d" * 64,
            "source_bytes": 128,
            "upstream_artifact_sha256": None,
            "engine": "external",
            "engine_identity": {
                "identity_sha256": canonical_json_sha256(
                    _engine_identity_projection(identity_core)
                ),
                **identity_core,
            },
            "requested_language": "en",
            "chunk_seconds": 30,
            "whisper_cpp_cpu_only": False,
            "sidecar_sha256": None,
        }
        settings_sha256 = canonical_json_sha256(
            _transcription_settings_identity(settings)
        )
        job_id = hashlib.sha256(
            b"awesome-capture.transcription-job/v2\0"
            + settings_sha256.encode("ascii")
        ).hexdigest()
        workspace = transcriptions / job_id
        chunks_dir = workspace / "chunks"
        results_dir = workspace / "chunk-results"
        for directory in (workspace, chunks_dir, results_dir):
            directory.mkdir()
            os.chmod(directory, 0o700)

        chunk_values = (b"A" * 64, b"B" * 64)
        chunks: list[dict[str, object]] = []
        for index, raw in enumerate(chunk_values):
            name = f"chunk-{index:05d}.wav"
            chunk_path = chunks_dir / name
            chunk_path.write_bytes(raw)
            os.chmod(chunk_path, 0o600)
            chunks.append(
                {
                    "index": index,
                    "name": name,
                    "path": str(chunk_path),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "sample_frames": 16000,
                    "sample_rate": 16000,
                    "offset_ms": index * 1000,
                    "duration_ms": 1000,
                }
            )
        manifest: dict[str, object] = {
            "schema_version": "awesome-capture.chunk-set/v1",
            "job_id": job_id,
            "source_sha256": settings["source_sha256"],
            "chunk_seconds": 30,
            "audio": {
                "sample_rate": 16000,
                "channels": 1,
                "sample_width": 2,
            },
            "count": 2,
            "total_duration_ms": 2000,
            "chunks": chunks,
        }
        manifest_path = chunks_dir / "chunks.manifest.json"
        self.write_private_json(manifest_path, manifest)
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        state: dict[str, object] = {
            "schema_version": "awesome-capture.transcription-state/v1",
            "status": "running",
            "job_id": job_id,
            "settings_sha256": settings_sha256,
            "execution_guard_sha256": "e" * 64,
            "settings": settings,
            "chunk_set": None,
            "chunks": {},
        }
        self.write_private_json(workspace / "state.json", state)

        records: dict[str, dict[str, object]] = {}
        for chunk in chunks:
            name = str(chunk["name"])
            records[name] = {
                "status": "complete",
                "language": "en",
                "silent": True,
                "chunk_sha256": chunk["sha256"],
                "offset_ms": chunk["offset_ms"],
                "duration_ms": chunk["duration_ms"],
                "raw_output_sha256": "f" * 64,
                "runtime": None,
                "segments": [],
            }

        def write_result(name: str) -> None:
            envelope = {
                "schema_version": "awesome-capture.chunk-result/v1",
                "job_id": job_id,
                "settings_sha256": settings_sha256,
                "execution_guard_sha256": state["execution_guard_sha256"],
                "chunk_manifest_sha256": manifest_sha256,
                "chunk_name": name,
                "result": records[name],
            }
            self.write_private_json(results_dir / f"{name}.result.json", envelope)

        write_result("chunk-00000.wav")
        return output_dir, workspace, state, manifest, records

    def registered_download_receipt(self) -> dict[str, object]:
        return {
            "schema_version": "awesome-capture.smoke-receipt/v1",
            "case_id": "youtube-anonymous",
            "created_at": dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "outcome": "pass",
            "commit_sha": "a" * 40,
            "implementation_digest": "b" * 64,
            "environment": {
                "os": "linux",
                "arch": "x86_64",
                "python": "3.14.0",
            },
            "tools": [
                {"name": "python", "version": "3.14.0"},
                {"name": "ffmpeg", "version": "8.1"},
                {"name": "ffprobe", "version": "8.1"},
                {"name": "yt-dlp", "version": "2026.7.4"},
                {"name": "deno", "version": "2.9.4"},
                {"name": "yt-dlp-ejs", "version": "1.0.0"},
            ],
            "source": {
                "platform": "youtube",
                "fingerprint": "41056884ffb08d869d53c54933c31dce7df1b1567a780face2bb065d9d62dae1",
                "auth_mode": "anonymous",
                "fallback": None,
            },
            "engine": None,
            "artifacts": [
                {"type": "video-artifact", "sha256": "d" * 64},
            ],
            "assertions": [
                {"name": name, "passed": True}
                for name in sorted(
                    {
                        "registered-source-detected",
                        "registered-platform-matches",
                        "registered-source-fingerprint-matches",
                        "registered-source-is-canonical",
                        "required-tools-observed",
                        "anonymous-route-observed",
                        "download-command-succeeded",
                        "video-artifact-v2-valid",
                        "video-media-reverified",
                    }
                )
            ],
            "warnings": [],
        }

    def registered_controlled_fallback_receipt(self) -> dict[str, object]:
        receipt = self.registered_download_receipt()
        receipt["case_id"] = "twitter-gallery-fallback"
        receipt["source"] = {
            "platform": "twitter",
            "fingerprint": "272c3cd8f41e281f00689741caf02f4ba2bb149d5b2d0644016c9d997221a7d1",
            "auth_mode": "anonymous",
            "fallback": "gallery-dl",
        }
        receipt["tools"] = [
            {"name": "python", "version": "3.14.0"},
            {"name": "ffmpeg", "version": "8.1"},
            {"name": "ffprobe", "version": "8.1"},
            {"name": "yt-dlp", "version": "2026.7.4"},
            {"name": "gallery-dl", "version": "1.32.8"},
            {
                "name": run_smoke.CONTROLLED_FAULT_TOOL,
                "version": run_smoke.CONTROLLED_FAULT_VERSION,
            },
        ]
        receipt["assertions"] = [
            {"name": name, "passed": True}
            for name in sorted(
                {
                    "registered-source-detected",
                    "registered-platform-matches",
                    "registered-source-fingerprint-matches",
                    "registered-source-is-canonical",
                    "required-tools-observed",
                    "download-command-succeeded",
                    "video-artifact-v2-valid",
                    "video-media-reverified",
                    "required-gallery-dl-observed",
                    *run_smoke.CONTROLLED_FAULT_ASSERTIONS,
                }
            )
        ]
        receipt["warnings"] = [run_smoke.CONTROLLED_FAULT_WARNING]
        return receipt

    def registered_transcription_receipt(self) -> dict[str, object]:
        return {
            "schema_version": "awesome-capture.smoke-receipt/v1",
            "case_id": "whisper-cpp-local",
            "created_at": dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "outcome": "pass",
            "commit_sha": "a" * 40,
            "implementation_digest": "b" * 64,
            "environment": {
                "os": "linux",
                "arch": "x86_64",
                "python": "3.14.0",
            },
            "tools": [
                {"name": "python", "version": "3.14.0"},
                {"name": "ffmpeg", "version": "8.1"},
                {"name": "ffprobe", "version": "8.1"},
                {"name": "whisper-cpp", "version": "1.9.1"},
            ],
            "source": {
                "platform": "local",
                "fingerprint": "c" * 64,
                "auth_mode": "not-applicable",
                "fallback": None,
            },
            "engine": {
                "name": "whisper-cpp",
                "identity_sha256": "d" * 64,
                "model_sha256": "e" * 64,
                "adapter_sha256": None,
            },
            "artifacts": [
                {"type": "transcript-artifact", "sha256": "f" * 64},
            ],
            "assertions": [
                {"name": name, "passed": True}
                for name in sorted(
                    {
                        "registered-local-media-exists",
                        "explicit-local-model-exists",
                        "explicit-local-binary-exists",
                        "required-tools-observed",
                        "transcription-command-succeeded",
                        "transcript-artifact-v2-valid",
                        "transcript-evidence-reverified",
                    }
                )
            ],
            "warnings": [],
        }

    def validate_receipt_value(self, value: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            path.write_text(
                json.dumps(value, ensure_ascii=False),
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            return smoke_receipts.validate_receipt(
                path,
                require_pass=True,
                require_current_digest=False,
            )

    def assert_case_registry_rejected(
        self,
        cases: list[dict[str, object]],
        *,
        schema_version: str | None = None,
    ) -> None:
        value = {
            "schema_version": schema_version or run_smoke.CASES_SCHEMA,
            "cases": cases,
        }
        with self.assertRaises(run_smoke.SmokeError) as raised:
            run_smoke._strict_cases(value)
        self.assertEqual(raised.exception.code, "INVALID_CASES")

        with tempfile.TemporaryDirectory() as temporary:
            cases_path = Path(temporary) / "cases.json"
            self.write_private_json(cases_path, value)
            with mock.patch.object(smoke_receipts, "CASES_PATH", cases_path):
                with self.assertRaises(
                    smoke_receipts.ContractError
                ) as receipt_raised:
                    smoke_receipts.load_case_registry()
        self.assertEqual(
            receipt_raised.exception.code,
            "SMOKE_CASES_INVALID",
        )

    def test_cases_are_alias_only_and_unknown_alias_is_rejected(self):
        cases = run_smoke.load_cases()
        self.assertEqual(run_smoke.CASES_SCHEMA, "awesome-capture.smoke-cases/v2")
        self.assertEqual(len(cases), 13)
        self.assertEqual(len({case["case_id"] for case in cases}), len(cases))
        download_cases = {
            case["case_id"]: case
            for case in cases
            if case["suite"] == "download"
        }
        self.assertEqual(
            set(download_cases),
            {
                "bilibili-anonymous",
                "youtube-anonymous",
                "twitter-anonymous",
                "douyin-ephemeral-browser",
                "tiktok-gallery-fallback",
                "twitter-gallery-fallback",
            },
        )
        self.assertEqual(
            {case["platform"] for case in download_cases.values()},
            {"bilibili", "youtube", "twitter", "douyin", "tiktok"},
        )
        self.assertEqual(
            download_cases["douyin-ephemeral-browser"]["expectation"],
            "ephemeral_browser",
        )
        self.assertEqual(
            download_cases["tiktok-gallery-fallback"]["expectation"],
            "gallery-dl",
        )
        for case in download_cases.values():
            self.assertRegex(case["source_fingerprint"], r"^[0-9a-f]{64}$")
        controlled = download_cases["twitter-gallery-fallback"]
        self.assertEqual(
            controlled["fault_profile"],
            run_smoke.CONTROLLED_FAULT_PROFILE,
        )
        self.assertIn(
            run_smoke.CONTROLLED_FAULT_TOOL,
            controlled["required_tools"],
        )
        self.assertFalse(
            any(
                case.get("fault_profile") is not None
                for case_id, case in download_cases.items()
                if case_id != "twitter-gallery-fallback"
            )
        )
        for case in cases:
            self.assertNotIn("url", case)
            self.assertNotIn("path", case)
            for key, value in case.items():
                if key.endswith("_env"):
                    self.assertRegex(value, r"^AWESOME_CAPTURE_SMOKE_[A-Z0-9_]+$")
        with self.assertRaises(run_smoke.SmokeError) as raised:
            run_smoke.select_case("https://example.invalid/video")
        self.assertEqual(raised.exception.code, "UNKNOWN_CASE")

    def test_controlled_fault_producer_verifier_constants_are_identical(self):
        self.assertEqual(run_smoke.CASES_SCHEMA, smoke_receipts.CASES_SCHEMA)
        self.assertEqual(
            run_smoke.CONTROLLED_FAULT_PROFILE,
            smoke_receipts.CONTROLLED_FAULT_PROFILE,
        )
        self.assertEqual(
            run_smoke.CONTROLLED_FAULT_TOOL,
            smoke_receipts.CONTROLLED_FAULT_TOOL,
        )
        self.assertEqual(
            run_smoke.CONTROLLED_FAULT_VERSION,
            smoke_receipts.CONTROLLED_FAULT_VERSION,
        )
        self.assertEqual(
            run_smoke.CONTROLLED_FAULT_WARNING,
            smoke_receipts.CONTROLLED_FAULT_WARNING,
        )
        self.assertEqual(
            run_smoke.CONTROLLED_FAULT_ASSERTIONS,
            smoke_receipts.CONTROLLED_FAULT_ASSERTIONS,
        )

    def test_cases_validator_rejects_literal_source_and_unknown_fields(self):
        invalid = dict(run_smoke.select_case("youtube-anonymous"))
        invalid["source_url"] = "https://example.invalid/video"
        self.assert_case_registry_rejected([invalid])

    def test_cases_v2_rejects_malformed_fingerprints_and_fault_bindings(self):
        registered = run_smoke.select_case("twitter-gallery-fallback")
        invalid_cases: dict[str, dict[str, object]] = {}

        missing_fingerprint = dict(registered)
        missing_fingerprint.pop("source_fingerprint")
        invalid_cases["missing-fingerprint"] = missing_fingerprint

        malformed_fingerprint = dict(registered)
        malformed_fingerprint["source_fingerprint"] = "A" * 64
        invalid_cases["malformed-fingerprint"] = malformed_fingerprint

        missing_fault = dict(registered)
        missing_fault.pop("fault_profile")
        invalid_cases["missing-fault"] = missing_fault

        unknown_fault = dict(registered)
        unknown_fault["fault_profile"] = "unregistered-fault/v1"
        invalid_cases["unknown-fault"] = unknown_fault

        misbound_fault = dict(registered)
        misbound_fault["case_id"] = "twitter-anonymous"
        invalid_cases["misbound-fault"] = misbound_fault

        wrong_platform = dict(registered)
        wrong_platform["platform"] = "tiktok"
        invalid_cases["wrong-platform"] = wrong_platform

        wrong_expectation = dict(registered)
        wrong_expectation["expectation"] = "ephemeral_browser"
        invalid_cases["wrong-expectation"] = wrong_expectation

        missing_fault_tool = dict(registered)
        missing_fault_tool["required_tools"] = [
            item
            for item in registered["required_tools"]
            if item != run_smoke.CONTROLLED_FAULT_TOOL
        ]
        invalid_cases["missing-fault-tool"] = missing_fault_tool

        unbound_fault_tool = dict(run_smoke.select_case("twitter-anonymous"))
        unbound_fault_tool["required_tools"] = [
            *unbound_fault_tool["required_tools"],
            run_smoke.CONTROLLED_FAULT_TOOL,
        ]
        invalid_cases["unbound-fault-tool"] = unbound_fault_tool

        invalid_env = dict(run_smoke.select_case("youtube-anonymous"))
        invalid_env["source_env"] = "YOUTUBE_URL"
        invalid_cases["invalid-env"] = invalid_env

        invalid_platform = dict(run_smoke.select_case("youtube-anonymous"))
        invalid_platform["platform"] = "vimeo"
        invalid_cases["invalid-download-platform"] = invalid_platform

        invalid_engine = dict(
            run_smoke.select_case("faster-whisper-local")
        )
        invalid_engine["engine"] = "remote"
        invalid_cases["invalid-transcription-engine"] = invalid_engine

        invalid_expectation = dict(
            run_smoke.select_case("tiktok-gallery-fallback")
        )
        invalid_expectation["expectation"] = "unbounded"
        invalid_cases["invalid-expectation"] = invalid_expectation

        invalid_tool = dict(run_smoke.select_case("youtube-anonymous"))
        invalid_tool["required_tools"] = [
            *invalid_tool["required_tools"],
            "invalid tool name",
        ]
        invalid_cases["invalid-required-tool"] = invalid_tool

        irrelevant_fingerprint = dict(
            run_smoke.select_case("faster-whisper-local")
        )
        irrelevant_fingerprint["source_fingerprint"] = None
        invalid_cases["irrelevant-null-fingerprint"] = irrelevant_fingerprint

        irrelevant_fault = dict(
            run_smoke.select_case("faster-whisper-local")
        )
        irrelevant_fault["fault_profile"] = None
        invalid_cases["irrelevant-null-fault"] = irrelevant_fault

        unknown_field = dict(run_smoke.select_case("youtube-anonymous"))
        unknown_field["unknown_field"] = "otherwise-valid"
        invalid_cases["unknown-field"] = unknown_field

        for name, case in invalid_cases.items():
            with self.subTest(name=name):
                self.assert_case_registry_rejected([case])

        first = dict(run_smoke.select_case("youtube-anonymous"))
        second = dict(run_smoke.select_case("bilibili-anonymous"))
        second["source_fingerprint"] = first["source_fingerprint"]
        with self.subTest(name="duplicate-download-fingerprint"):
            self.assert_case_registry_rejected([first, second])

        self.assert_case_registry_rejected(
            [registered],
            schema_version="awesome-capture.smoke-cases/v1",
        )

    def test_subprocess_json_protocol_is_fail_closed(self):
        def runner(command, **unused):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"status":"ok"}',
                stderr="leaked-warning",
            )

        code, payload, error = run_smoke._run_json(["fixture"], runner=runner)
        self.assertEqual(code, 7)
        self.assertIsNone(payload)
        self.assertEqual(error, "CLI_PROTOCOL_VIOLATION")

    def test_tool_versions_are_whitelisted_not_copied_from_raw_output(self):
        self.assertEqual(run_smoke._safe_version("ffmpeg 8.1"), "ffmpeg 8.1")
        self.assertEqual(
            run_smoke._safe_version(
                "deno 2.9.4 (stable, release, aarch64-apple-darwin)"
            ),
            "deno 2.9.4 (stable release aarch64-apple-darwin)",
        )
        for leaked in (
            "built on alice at /Volumes/secret/movie.mp4",
            "host=alice.local",
            r"C:\Users\alice\movie.mp4",
            "tool 1.0\nraw log line",
        ):
            with self.subTest(leaked=leaked):
                sanitized = run_smoke._safe_version(leaked)
                if "\n" in leaked:
                    self.assertEqual(sanitized, "tool 1.0")
                else:
                    self.assertEqual(sanitized, "unavailable")

        def invalid_failure(command, **unused):
            return subprocess.CompletedProcess(
                command,
                4,
                stdout='{"unexpected":true}',
                stderr="not-json",
            )

        code, payload, error = run_smoke._run_json(
            ["fixture"],
            runner=invalid_failure,
        )
        self.assertEqual(code, 4)
        self.assertIsNone(payload)
        self.assertEqual(error, "CLI_PROTOCOL_VIOLATION")

    def test_implementation_digest_includes_untracked_contract_surface(self):
        paths = set(smoke_receipts.tracked_files())
        self.assertIn(Path("contracts/contract_runtime.py"), paths)
        self.assertIn(Path("tools/run_smoke.py"), paths)
        self.assertIn(Path("smoke/cases.json"), paths)
        self.assertFalse(
            any(path.parts[:2] == ("smoke", "receipts") for path in paths)
        )
        self.assertRegex(smoke_receipts.implementation_digest(), r"^[0-9a-f]{64}$")

    def test_registered_passing_receipt_satisfies_release_evidence_contract(self):
        validated = self.validate_receipt_value(
            self.registered_download_receipt()
        )
        self.assertEqual(validated["case_id"], "youtube-anonymous")
        self.assertEqual(validated["outcome"], "pass")

    def test_registered_controlled_fallback_receipt_is_accepted(self):
        validated = self.validate_receipt_value(
            self.registered_controlled_fallback_receipt()
        )
        self.assertEqual(validated["case_id"], "twitter-gallery-fallback")
        self.assertEqual(validated["outcome"], "pass")

    def test_controlled_fallback_receipt_requires_complete_bound_evidence(self):
        missing_warning = self.registered_controlled_fallback_receipt()
        missing_warning["warnings"] = []

        missing_tool = self.registered_controlled_fallback_receipt()
        missing_tool["tools"] = [
            item
            for item in missing_tool["tools"]
            if item["name"] != run_smoke.CONTROLLED_FAULT_TOOL
        ]

        missing_assertion = self.registered_controlled_fallback_receipt()
        missing_assertion["assertions"] = [
            item
            for item in missing_assertion["assertions"]
            if item["name"] != "controlled-ytdlp-network-error-observed"
        ]

        wrong_tool_version = self.registered_controlled_fallback_receipt()
        for item in wrong_tool_version["tools"]:
            if item["name"] == run_smoke.CONTROLLED_FAULT_TOOL:
                item["version"] = "different-fault-v1"

        unknown_controlled_claim = self.registered_controlled_fallback_receipt()
        unknown_controlled_claim["assertions"].append(
            {"name": "controlled-unregistered-claim", "passed": True}
        )

        misleading_natural_claim = self.registered_controlled_fallback_receipt()
        misleading_natural_claim["assertions"].append(
            {"name": "x-natural-failure-observed", "passed": True}
        )

        for name, receipt in (
            ("warning", missing_warning),
            ("tool", missing_tool),
            ("assertion", missing_assertion),
            ("tool-version", wrong_tool_version),
            ("unknown-controlled-claim", unknown_controlled_claim),
            ("misleading-natural-claim", misleading_natural_claim),
        ):
            with self.subTest(missing=name):
                with self.assertRaises(smoke_receipts.ContractError) as raised:
                    self.validate_receipt_value(receipt)
                self.assertEqual(raised.exception.code, "SMOKE_CASE_MISMATCH")

    def test_nonfault_case_cannot_spoof_controlled_fallback_evidence(self):
        receipt = self.registered_download_receipt()
        receipt["warnings"].append(run_smoke.CONTROLLED_FAULT_WARNING)
        receipt["tools"].append(
            {
                "name": run_smoke.CONTROLLED_FAULT_TOOL,
                "version": run_smoke.CONTROLLED_FAULT_VERSION,
            }
        )
        receipt["assertions"].extend(
            {"name": name, "passed": True}
            for name in sorted(run_smoke.CONTROLLED_FAULT_ASSERTIONS)
        )

        with self.assertRaises(smoke_receipts.ContractError) as raised:
            self.validate_receipt_value(receipt)
        self.assertEqual(raised.exception.code, "SMOKE_CASE_MISMATCH")

    def test_release_receipt_rejects_duplicate_tool_names(self):
        receipt = self.registered_download_receipt()
        receipt["tools"].append(
            {"name": "ffprobe", "version": "different-version"}
        )
        with self.assertRaises(smoke_receipts.ContractError) as raised:
            self.validate_receipt_value(receipt)
        self.assertEqual(raised.exception.code, "SMOKE_CASE_MISMATCH")

    def test_release_receipt_rejects_missing_case_required_tool(self):
        receipt = self.registered_download_receipt()
        receipt["tools"] = [
            item for item in receipt["tools"] if item["name"] != "deno"
        ]
        with self.assertRaises(smoke_receipts.ContractError) as raised:
            self.validate_receipt_value(receipt)
        self.assertEqual(raised.exception.code, "SMOKE_CASE_MISMATCH")

    def test_future_receipt_is_rejected(self):
        receipt = self.registered_download_receipt()
        receipt["created_at"] = (
            dt.datetime.now(dt.timezone.utc)
            + smoke_receipts.FUTURE_SKEW
            + dt.timedelta(minutes=1)
        ).replace(microsecond=0).isoformat()
        with self.assertRaises(smoke_receipts.ContractError) as raised:
            self.validate_receipt_value(receipt)
        self.assertEqual(raised.exception.code, "FUTURE_SMOKE_RECEIPT")

    def test_old_receipt_remains_valid_when_digest_and_outcome_match(self):
        receipt = self.registered_download_receipt()
        receipt["created_at"] = "2000-01-01T00:00:00+00:00"
        validated = self.validate_receipt_value(receipt)
        self.assertEqual(validated["outcome"], "pass")
        self.assertEqual(
            validated["implementation_digest"],
            receipt["implementation_digest"],
        )

    def test_validate_existing_release_flags_enforce_outcome_and_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = Path(smoke_receipts.__file__).resolve()
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"

            passing_dir = root / "passing"
            passing_dir.mkdir()
            passing = self.registered_download_receipt()
            passing["implementation_digest"] = smoke_receipts.implementation_digest()
            self.write_private_json(passing_dir / "receipt.json", passing)
            accepted = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate-existing",
                    "--directory",
                    str(passing_dir),
                    "--require-pass",
                    "--require-current-digest",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(json.loads(accepted.stdout)["receipt_count"], 1)

            failed_dir = root / "failed"
            failed_dir.mkdir()
            failed = self.registered_download_receipt()
            failed["outcome"] = "fail"
            failed["assertions"][0]["passed"] = False
            self.write_private_json(failed_dir / "receipt.json", failed)
            rejected_outcome = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate-existing",
                    "--directory",
                    str(failed_dir),
                    "--require-pass",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(rejected_outcome.returncode, 2)
            self.assertEqual(rejected_outcome.stdout, "")
            self.assertEqual(
                json.loads(rejected_outcome.stderr)["error"]["code"],
                "SMOKE_FAILED",
            )

            stale_dir = root / "stale"
            stale_dir.mkdir()
            stale = self.registered_download_receipt()
            stale["implementation_digest"] = "0" * 64
            self.write_private_json(stale_dir / "receipt.json", stale)
            rejected_digest = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate-existing",
                    "--directory",
                    str(stale_dir),
                    "--require-current-digest",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(rejected_digest.returncode, 2)
            self.assertEqual(rejected_digest.stdout, "")
            self.assertEqual(
                json.loads(rejected_digest.stderr)["error"]["code"],
                "STALE_SMOKE_RECEIPT",
            )

            empty_dir = root / "empty"
            empty_dir.mkdir()
            rejected_empty = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate-existing",
                    "--directory",
                    str(empty_dir),
                    "--require-pass",
                    "--require-current-digest",
                    "--require-all-cases",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(rejected_empty.returncode, 2)
            self.assertEqual(rejected_empty.stdout, "")
            self.assertEqual(
                json.loads(rejected_empty.stderr)["error"]["code"],
                "SMOKE_EVIDENCE_MISSING",
            )

            rejected_partial = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate-existing",
                    "--directory",
                    str(passing_dir),
                    "--require-pass",
                    "--require-current-digest",
                    "--require-all-cases",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(rejected_partial.returncode, 2)
            self.assertEqual(rejected_partial.stdout, "")
            self.assertEqual(
                json.loads(rejected_partial.stderr)["error"]["code"],
                "SMOKE_EVIDENCE_MISSING",
            )

    def test_validate_cli_binds_one_receipt_to_the_requested_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = Path(smoke_receipts.__file__).resolve()
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            receipt = self.registered_download_receipt()
            receipt["implementation_digest"] = smoke_receipts.implementation_digest()
            first = root / "first.json"
            second = root / "second.json"
            self.write_private_json(first, receipt)
            self.write_private_json(second, receipt)

            accepted = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate",
                    str(first),
                    "--require-current-digest",
                    "--require-single",
                    "--require-case",
                    "youtube-anonymous",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            wrong_case = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate",
                    str(first),
                    "--require-single",
                    "--require-case",
                    "bilibili-anonymous",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(wrong_case.returncode, 2)
            self.assertEqual(wrong_case.stdout, "")
            self.assertEqual(
                json.loads(wrong_case.stderr)["error"]["code"],
                "SMOKE_CASE_MISMATCH",
            )

            multiple = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate",
                    str(first),
                    str(second),
                    "--require-single",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(multiple.returncode, 2)
            self.assertEqual(multiple.stdout, "")
            self.assertEqual(
                json.loads(multiple.stderr)["error"]["code"],
                "SMOKE_RECEIPT_SET_INVALID",
            )

    def test_release_coverage_requires_every_registered_case(self):
        case_ids = set(smoke_receipts.load_case_registry())
        complete = [
            {
                "path": f"/private/{case_id}.json",
                "case_id": case_id,
                "outcome": "pass",
                "implementation_digest": "a" * 64,
            }
            for case_id in sorted(case_ids)
        ]
        coverage = smoke_receipts.validate_required_case_coverage(complete)
        self.assertEqual(coverage["required_case_count"], len(case_ids))
        self.assertEqual(coverage["covered_case_count"], len(case_ids))

        without_twitter_fallback = [
            receipt
            for receipt in complete
            if receipt["case_id"] != "twitter-gallery-fallback"
        ]
        self.assertEqual(len(without_twitter_fallback), len(complete) - 1)
        with self.assertRaises(smoke_receipts.ContractError) as raised:
            smoke_receipts.validate_required_case_coverage(
                without_twitter_fallback
            )
        self.assertEqual(raised.exception.code, "SMOKE_EVIDENCE_MISSING")

    def test_registered_case_platform_and_engine_mismatches_are_rejected(self):
        platform_mismatch = self.registered_download_receipt()
        platform_mismatch["source"]["platform"] = "tiktok"
        engine_mismatch = self.registered_transcription_receipt()
        engine_mismatch["engine"]["name"] = "faster-whisper"
        for name, receipt in (
            ("platform", platform_mismatch),
            ("engine", engine_mismatch),
        ):
            with self.subTest(mismatch=name):
                with self.assertRaises(smoke_receipts.ContractError) as raised:
                    self.validate_receipt_value(receipt)
                self.assertEqual(raised.exception.code, "SMOKE_CASE_MISMATCH")

    def test_passing_receipt_requires_registered_assertions_tools_and_artifacts(self):
        cases: dict[str, dict[str, object]] = {}
        missing_assertion = self.registered_download_receipt()
        missing_assertion["assertions"] = missing_assertion["assertions"][1:]
        cases["assertion"] = missing_assertion
        missing_tool = self.registered_download_receipt()
        missing_tool["tools"] = [
            item for item in missing_tool["tools"] if item["name"] != "ffprobe"
        ]
        cases["tool"] = missing_tool
        missing_artifact = self.registered_download_receipt()
        missing_artifact["artifacts"] = []
        cases["artifact"] = missing_artifact
        expected_codes = {
            "assertion": {"SMOKE_CASE_MISMATCH"},
            "tool": {"SMOKE_CASE_MISMATCH"},
            # The canonical semantic layer may reject this before the stricter
            # registered-case evidence check is reached.
            "artifact": {
                "SEMANTIC_VALIDATION_FAILED",
                "SMOKE_CASE_MISMATCH",
            },
        }
        for evidence, receipt in cases.items():
            with self.subTest(evidence=evidence):
                with self.assertRaises(smoke_receipts.ContractError) as raised:
                    self.validate_receipt_value(receipt)
                self.assertIn(raised.exception.code, expected_codes[evidence])

    def test_missing_environment_still_writes_canonical_sanitized_failure_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt_dir = Path(temporary)
            with (
                mock.patch.object(run_smoke, "_commit_sha", return_value="b" * 40),
                mock.patch.object(
                    run_smoke, "implementation_digest", return_value="c" * 64
                ),
                mock.patch.object(
                    run_smoke,
                    "_environment",
                    return_value={"os": "linux", "arch": "x86_64", "python": "3.14.0"},
                ),
                mock.patch.object(
                    run_smoke,
                    "collect_tools",
                    return_value=[{"name": "python", "version": "3.14.0"}],
                ),
            ):
                receipt, path = run_smoke.run_case(
                    "youtube-anonymous",
                    receipt_dir=receipt_dir,
                    environ={},
                )
            self.assertEqual(receipt["outcome"], "fail")
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertNotIn("://", path.read_text(encoding="utf-8"))
            loaded = run_smoke.read_json_strict(path, expected="smoke-receipt")
            self.assertEqual(loaded["source"]["fingerprint"], "0" * 64)
            self.assertEqual(loaded["warnings"], ["registered-source-environment-missing"])

    def test_long_resume_uses_secure_canonical_temporary_path_identity(self):
        alias = Path(
            "/var/folders/fixture/awesome-capture-smoke-external-long-resume"
        )
        canonical = Path(
            "/private/var/folders/fixture/"
            "awesome-capture-smoke-external-long-resume"
        )
        temporary_context = mock.MagicMock()
        temporary_context.__enter__.return_value = str(alias)
        temporary_context.__exit__.return_value = False
        details = {
            "source": {
                "platform": "local",
                "fingerprint": "0" * 64,
                "auth_mode": "not-applicable",
                "fallback": None,
            },
            "engine": None,
            "artifacts": [],
            "assertions": [
                {"name": "partial-chunk-state-observed", "passed": False}
            ],
            "warnings": ["partial-chunk-state-not-observed"],
            "tools": [],
        }

        with tempfile.TemporaryDirectory() as receipt_temporary:
            receipt_dir = Path(receipt_temporary) / "receipts"
            with (
                mock.patch.object(
                    run_smoke.tempfile,
                    "TemporaryDirectory",
                    return_value=temporary_context,
                ),
                mock.patch.object(
                    run_smoke,
                    "secure_mkdirs",
                    return_value=canonical,
                ) as secure_work_dir,
                mock.patch.object(
                    run_smoke,
                    "_transcription_case",
                    return_value=details,
                ) as transcription_case,
                mock.patch.object(run_smoke, "_commit_sha", return_value="b" * 40),
                mock.patch.object(
                    run_smoke, "implementation_digest", return_value="c" * 64
                ),
                mock.patch.object(
                    run_smoke,
                    "_environment",
                    return_value={
                        "os": "macos",
                        "arch": "arm64",
                        "python": "3.14.0",
                    },
                ),
            ):
                receipt, path = run_smoke.run_case(
                    "external-long-resume",
                    receipt_dir=receipt_dir,
                    environ={
                        "AWESOME_CAPTURE_SMOKE_LONG_MEDIA": "/local/source.wav",
                        "AWESOME_CAPTURE_SMOKE_EXTERNAL_MODEL": "/local/model",
                        "AWESOME_CAPTURE_SMOKE_EXTERNAL_ADAPTER": "/local/adapter",
                    },
                )
            self.assertTrue(path.is_file())

        self.assertEqual(receipt["outcome"], "fail")
        secure_work_dir.assert_called_once_with(alias)
        self.assertEqual(transcription_case.call_args.args[4], canonical)

    def test_download_error_code_is_normalized_in_failure_receipt(self):
        canonical_url = "https://www.youtube.com/watch?v=4biXYSNkn9Y"
        fingerprint = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()

        def fake_runner(command, **unused):
            if "detect" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "status": "ok",
                            "operation": "detect",
                            "platform": "youtube",
                            "sanitized_url": canonical_url,
                            "source_fingerprint": fingerprint,
                        }
                    ),
                    stderr="",
                )
            if "download" in command:
                return subprocess.CompletedProcess(
                    command,
                    5,
                    stdout="",
                    stderr=json.dumps(
                        {
                            "status": "error",
                            "error": {
                                "code": "DOWNLOAD_FAILED",
                                "message": "Sanitized failure.",
                            },
                        }
                    ),
                )
            raise AssertionError(f"unexpected command: {command}")

        with tempfile.TemporaryDirectory() as temporary:
            receipt_dir = Path(temporary) / "receipts"
            with (
                mock.patch.object(run_smoke, "_commit_sha", return_value="b" * 40),
                mock.patch.object(
                    run_smoke, "implementation_digest", return_value="c" * 64
                ),
                mock.patch.object(
                    run_smoke,
                    "_environment",
                    return_value={"os": "linux", "arch": "x86_64", "python": "3.14.0"},
                ),
                mock.patch.object(
                    run_smoke,
                    "collect_tools",
                    return_value=[{"name": "python", "version": "3.14.0"}],
                ),
            ):
                receipt, path = run_smoke.run_case(
                    "youtube-anonymous",
                    receipt_dir=receipt_dir,
                    environ={
                        "AWESOME_CAPTURE_SMOKE_YOUTUBE_URL": canonical_url
                    },
                    runner=fake_runner,
                )

            self.assertEqual(receipt["outcome"], "fail")
            self.assertEqual(receipt["warnings"], ["download-error-download_failed"])
            self.assertTrue(path.is_file())
            assertions = {
                item["name"]: item["passed"] for item in receipt["assertions"]
            }
            self.assertFalse(assertions["required-tools-observed"])

    def test_anonymous_case_records_unexpected_fallback_as_failed_assertion(self):
        case = run_smoke.select_case("twitter-anonymous")
        canonical_url = (
            "https://x.com/maomao_acrypto/status/2081741144799498330"
        )
        artifact = {
            "source": {
                "platform": "twitter",
                "fingerprint": "a" * 64,
            },
            "acquisition": {
                "auth_mode": "anonymous",
                "fallback": "gallery-dl",
                "warnings": [],
            },
            "producer": {
                "tool": "yt-dlp",
                "version": "2026.07.04",
            },
        }
        with (
            mock.patch.object(
                run_smoke,
                "_detect_download_source",
                return_value=(
                    "twitter",
                    canonical_url,
                    case["source_fingerprint"],
                    True,
                ),
            ),
            mock.patch.object(
                run_smoke,
                "_run_json",
                return_value=(
                    0,
                    {"artifact_path": "/private/tmp/fixture-artifact.json"},
                    "",
                ),
            ),
            mock.patch.object(run_smoke, "read_json_strict", return_value=artifact),
            mock.patch.object(run_smoke, "validate_file_context"),
            mock.patch.object(run_smoke, "_reverify_video_media", return_value=True),
            mock.patch.object(run_smoke, "_sha256_file", return_value="b" * 64),
            mock.patch.object(
                run_smoke,
                "collect_tools",
                return_value=[
                    {"name": "python", "version": "3.14.0"},
                    {"name": "ffmpeg", "version": "8.1"},
                    {"name": "ffprobe", "version": "8.1"},
                    {"name": "yt-dlp", "version": "2026.07.04"},
                ],
            ),
        ):
            details = run_smoke._download_case(
                case,
                canonical_url,
                Path("/private/tmp/unused-smoke-work"),
                runner=mock.Mock(),
            )

        assertions = {
            item["name"]: item["passed"] for item in details["assertions"]
        }
        self.assertFalse(assertions["anonymous-route-observed"])

    def test_registered_source_mismatch_stops_before_any_download(self):
        case = run_smoke.select_case("twitter-gallery-fallback")
        canonical_source = (
            "https://twitter.com/video/status/745240047289458688"
        )
        different_source = (
            "https://twitter.com/video/status/745240047289458689"
        )
        scenarios = {
            "fingerprint": {
                "source": different_source,
                "sanitized_source": different_source,
                "fingerprint": hashlib.sha256(
                    different_source.encode("utf-8")
                ).hexdigest(),
                "failed_assertion": "registered-source-fingerprint-matches",
                "warning": "registered-source-fingerprint-mismatch",
            },
            "canonical-source": {
                "source": f"{canonical_source}?s=20",
                "sanitized_source": canonical_source,
                "fingerprint": case["source_fingerprint"],
                "failed_assertion": "registered-source-is-canonical",
                "warning": "registered-source-not-canonical",
            },
            "inconsistent-detector-fingerprint": {
                "source": canonical_source,
                "sanitized_source": canonical_source,
                "fingerprint": "f" * 64,
                "failed_assertion": "registered-source-detected",
                "warning": "registered-source-detection-failed",
            },
        }

        for name, scenario in scenarios.items():
            calls: list[list[str]] = []

            def detector_only_runner(command, **unused):
                calls.append(list(command))
                if "detect" not in command:
                    raise AssertionError(
                        "download boundary was reached after source mismatch"
                    )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "status": "ok",
                            "operation": "detect",
                            "platform": "twitter",
                            "sanitized_url": scenario["sanitized_source"],
                            "source_fingerprint": scenario["fingerprint"],
                        }
                    ),
                    stderr="",
                )

            with self.subTest(name=name):
                with (
                    mock.patch.object(run_smoke, "collect_tools", return_value=[]),
                    mock.patch.object(
                        run_smoke,
                        "_tool_version",
                        return_value={"name": "yt-dlp", "version": "fixture"},
                    ),
                    mock.patch.object(
                        run_smoke,
                        "_controlled_fallback_runner",
                        side_effect=AssertionError(
                            "controlled downloader must not be constructed"
                        ),
                    ) as controlled_runner,
                ):
                    details = run_smoke._download_case(
                        case,
                        str(scenario["source"]),
                        Path("/private/tmp/unused-smoke-work"),
                        runner=detector_only_runner,
                    )

                assertions = {
                    item["name"]: item["passed"]
                    for item in details["assertions"]
                }
                self.assertFalse(assertions[str(scenario["failed_assertion"])])
                self.assertEqual(
                    details["warnings"],
                    [scenario["warning"]],
                )
                self.assertEqual(details["artifacts"], [])
                self.assertEqual(len(calls), 1)
                self.assertIn("detect", calls[0])
                controlled_runner.assert_not_called()

    def test_controlled_tool_snapshot_rejects_env_shebang_and_hardlink(self):
        original_which = shutil.which
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_script = root / "env-tool"
            env_script.write_text(
                "#!/usr/bin/env python3\nprint('unsafe')\n",
                encoding="utf-8",
            )
            os.chmod(env_script, 0o755)

            base_script = root / "base-tool"
            base_script.write_text(
                f"#!{sys.executable}\nprint('unsafe')\n",
                encoding="utf-8",
            )
            os.chmod(base_script, 0o755)
            linked_script = root / "linked-tool"
            os.link(base_script, linked_script)

            non_executable = root / "non-executable-tool"
            non_executable.write_text(
                f"#!{sys.executable}\nprint('unsafe')\n",
                encoding="utf-8",
            )
            os.chmod(non_executable, 0o600)

            for name, candidate in (
                ("env-shebang", env_script),
                ("hardlink", linked_script),
                ("non-executable", non_executable),
            ):
                with self.subTest(name=name):
                    with mock.patch.object(
                        run_smoke.shutil,
                        "which",
                        side_effect=lambda requested, path=str(candidate): (
                            path
                            if requested == "gallery-dl"
                            else original_which(requested)
                        ),
                    ):
                        with self.assertRaises(run_smoke.SmokeError) as raised:
                            run_smoke._snapshot_controlled_tool("gallery-dl")
                    self.assertEqual(
                        raised.exception.code,
                        "UNSAFE_SMOKE_TOOL",
                    )

    def test_controlled_ytdlp_command_rejects_sensitive_option_injection(self):
        source = "https://twitter.com/video/status/745240047289458688"
        executable = Path("/private/controlled-tools/yt-dlp")
        with tempfile.TemporaryDirectory() as temporary:
            root = run_smoke.secure_mkdirs(Path(temporary))
            output_dir = root / "output"
            staging_parent = (
                output_dir
                / ".awesome-capture-media"
                / "v2"
                / "staging"
            )
            staging_parent.mkdir(parents=True, mode=0o700)
            for parent in (
                output_dir,
                output_dir / ".awesome-capture-media",
                output_dir / ".awesome-capture-media" / "v2",
                staging_parent,
            ):
                os.chmod(parent, 0o700)
            staging = staging_parent / "registered.0123456789abcdef"
            staging.mkdir(mode=0o700)

            valid = [
                str(executable),
                "--ignore-config",
                "--no-playlist",
                "--use-extractors",
                "Twitter",
                "--socket-timeout",
                "20",
                "--retries",
                "3",
                "--fragment-retries",
                "3",
                "--no-warnings",
                "--part",
                "--no-overwrites",
                "--write-info-json",
                "--format",
                "bv*+ba/b",
                "--paths",
                "temp:.",
                "--output",
                "%(id)s--%(title).120B.%(ext)s",
                "--print",
                "after_move:%(filepath)j",
                source,
            ]
            self.assertTrue(
                run_smoke._controlled_ytdlp_command_valid(
                    valid,
                    source=source,
                    output_dir=output_dir,
                    executable=executable,
                    pinned_cwd=staging,
                )
            )
            injections = {
                "netrc": ["--netrc"],
                "header": ["--add-header", "Authorization: redacted"],
                "exec": ["--exec", "false"],
                "cookies-equals": ["--cookies=/private/fixture-cookies.txt"],
            }
            for name, injected in injections.items():
                with self.subTest(name=name):
                    command = [*valid[:-1], *injected, valid[-1]]
                    self.assertFalse(
                        run_smoke._controlled_ytdlp_command_valid(
                            command,
                            source=source,
                            output_dir=output_dir,
                            executable=executable,
                            pinned_cwd=staging,
                        )
                    )

    def test_controlled_fallback_runs_real_production_download_offline(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        self.assertIsNotNone(ffmpeg, "ffmpeg is required by the no-skip suite")
        self.assertIsNotNone(ffprobe, "ffprobe is required by the no-skip suite")
        assert ffmpeg is not None
        assert ffprobe is not None

        source = "https://twitter.com/video/status/745240047289458688"
        case = run_smoke.select_case("twitter-gallery-fallback")
        self.assertEqual(
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
            case["source_fingerprint"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = run_smoke.secure_mkdirs(Path(temporary))
            fixture = root / "fixture.mp4"
            generated = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=32x32:d=0.25",
                    "-an",
                    "-c:v",
                    "mpeg4",
                    "-y",
                    str(fixture),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertGreater(fixture.stat().st_size, 0)
            os.chmod(fixture, 0o600)

            tool_dir = root / "tools"
            tool_dir.mkdir(mode=0o700)
            yt_dlp = tool_dir / "yt-dlp"
            yt_dlp.write_text(
                (
                    f"#!{sys.executable}\n"
                    "import sys\n"
                    "if sys.argv[1:] == ['--version']:\n"
                    "    print('2026.07.04')\n"
                    "    raise SystemExit(0)\n"
                    "raise SystemExit(92)\n"
                ),
                encoding="utf-8",
            )
            os.chmod(yt_dlp, 0o755)

            gallery_dl = tool_dir / "gallery-dl"
            gallery_dl.write_text(
                (
                    f"#!{sys.executable}\n"
                    "import shutil\n"
                    "import sys\n"
                    "from pathlib import Path\n"
                    "if sys.argv[1:] == ['--version']:\n"
                    "    print('1.32.8')\n"
                    "    raise SystemExit(0)\n"
                    f"source = {source!r}\n"
                    "expected = [\n"
                    "    '--config-ignore', '--no-input', '--range', '1',\n"
                    "    '-D', '.', '-f', 'download.{extension}', source,\n"
                    "]\n"
                    "if sys.argv[1:] != expected:\n"
                    "    raise SystemExit(91)\n"
                    f"shutil.copyfile({str(fixture)!r}, "
                    "Path.cwd() / 'download.mp4')\n"
                ),
                encoding="utf-8",
            )
            os.chmod(gallery_dl, 0o755)

            original_which = shutil.which

            def controlled_which(name):
                if name == "yt-dlp":
                    return str(yt_dlp)
                if name == "gallery-dl":
                    return str(gallery_dl)
                return original_which(name)

            output_dir = root / "output"
            state = {
                "fault_profile_applied": False,
                "fault_trigger_count": 0,
                "yt_dlp_command_verified": False,
                "network_error_observed": False,
                "fallback_gate_count": 0,
                "fallback_gate_accepted": False,
                "gallery_execution_count": 0,
                "gallery_command_verified": False,
                "tool_identities_stable": False,
            }
            command = [
                sys.executable,
                str(run_smoke.DOWNLOAD_SCRIPT),
                "download",
                source,
                "--output-dir",
                str(output_dir),
                "--lock-timeout",
                "30",
            ]
            leaked_stdout = io.StringIO()
            leaked_stderr = io.StringIO()
            with (
                mock.patch.object(
                    run_smoke.shutil,
                    "which",
                    side_effect=controlled_which,
                ),
                mock.patch.object(sys, "stdout", leaked_stdout),
                mock.patch.object(sys, "stderr", leaked_stderr),
            ):
                runner = run_smoke._controlled_fallback_runner(
                    source=source,
                    output_dir=output_dir,
                    state=state,
                )
                process = runner(command)

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stderr, "")
            self.assertEqual(leaked_stdout.getvalue(), "")
            self.assertEqual(leaked_stderr.getvalue(), "")
            payload = run_smoke.loads_strict(
                process.stdout,
                max_bytes=4 * 1024 * 1024,
            )
            self.assertIsInstance(payload, dict)
            artifact_path = Path(str(payload["artifact_path"]))
            artifact = run_smoke.read_json_strict(
                artifact_path,
                expected="video-artifact",
            )
            run_smoke.validate_file_context(artifact)
            self.assertTrue(
                run_smoke._reverify_video_media(
                    artifact,
                    runner=subprocess.run,
                )
            )
            self.assertEqual(artifact["producer"]["tool"], "gallery-dl")
            self.assertEqual(artifact["acquisition"]["fallback"], "gallery-dl")
            self.assertEqual(
                artifact["source"]["fingerprint"],
                case["source_fingerprint"],
            )
            self.assertIn(
                "yt-dlp failed with NETWORK_ERROR; "
                "used the bounded gallery-dl fallback.",
                artifact["acquisition"]["warnings"],
            )
            self.assertEqual(state["fault_trigger_count"], 1)
            self.assertEqual(state["gallery_execution_count"], 1)
            assertion_rows = run_smoke._controlled_fault_assertion_rows(
                case=case,
                state=state,
                artifact=artifact,
                artifact_path=artifact_path,
                output_dir=output_dir,
            )
            self.assertEqual(
                {item["name"] for item in assertion_rows},
                run_smoke.CONTROLLED_FAULT_ASSERTIONS,
            )
            self.assertTrue(all(item["passed"] for item in assertion_rows))

            def local_production_runner(command, **options):
                if "detect" in command or command[0] == "ffprobe":
                    return subprocess.run(command, **options)
                raise AssertionError(
                    f"unexpected outer smoke command: {command[0]}"
                )

            versions = {
                "ffmpeg": "8.1",
                "ffprobe": "8.1",
                "yt-dlp": "2026.07.04",
                "gallery-dl": "1.32.8",
            }

            def fixture_tool_version(name, unused_command):
                return {
                    "name": name,
                    "version": versions.get(name, "fixture"),
                }

            receipt_dir = root / "receipts"
            with (
                mock.patch.object(
                    run_smoke.shutil,
                    "which",
                    side_effect=controlled_which,
                ),
                mock.patch.object(
                    run_smoke,
                    "_tool_version",
                    side_effect=fixture_tool_version,
                ),
                mock.patch.object(
                    run_smoke,
                    "_commit_sha",
                    return_value="a" * 40,
                ),
                mock.patch.object(
                    run_smoke,
                    "implementation_digest",
                    return_value="b" * 64,
                ),
                mock.patch.object(
                    run_smoke,
                    "_environment",
                    return_value={
                        "os": "linux",
                        "arch": "x86_64",
                        "python": "3.14.0",
                    },
                ),
            ):
                receipt, receipt_path = run_smoke.run_case(
                    "twitter-gallery-fallback",
                    receipt_dir=receipt_dir,
                    environ={
                        "AWESOME_CAPTURE_SMOKE_TWITTER_GALLERY_URL": source
                    },
                    runner=local_production_runner,
                )

            self.assertEqual(receipt["outcome"], "pass")
            self.assertTrue(receipt_path.is_file())
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
            self.assertEqual(
                receipt["source"]["fingerprint"],
                case["source_fingerprint"],
            )
            receipt_assertions = {
                item["name"]: item["passed"]
                for item in receipt["assertions"]
            }
            self.assertTrue(
                all(
                    receipt_assertions[name]
                    for name in run_smoke.CONTROLLED_FAULT_ASSERTIONS
                )
            )
            validated_receipt = smoke_receipts.validate_receipt(
                receipt_path,
                require_pass=True,
                require_current_digest=False,
            )
            self.assertEqual(
                validated_receipt["case_id"],
                "twitter-gallery-fallback",
            )
            serialized_receipt = receipt_path.read_text(encoding="utf-8")
            for private_value in (
                source,
                str(root),
                str(yt_dlp),
                str(gallery_dl),
                "download.mp4",
            ):
                with self.subTest(private_value=private_value):
                    self.assertNotIn(private_value, serialized_receipt)

    def test_cli_interrupt_uses_sanitized_json_protocol(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(run_smoke, "run_case", side_effect=KeyboardInterrupt),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(sys, "stderr", stderr),
        ):
            exit_code = run_smoke.main(
                [
                    "run",
                    "youtube-anonymous",
                    "--receipt-dir",
                    "/private/tmp/unused-smoke-receipts",
                ]
            )

        self.assertEqual(exit_code, 130)
        self.assertEqual(stdout.getvalue(), "")
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["code"], "INTERRUPTED")
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_successful_fake_download_generates_content_hash_only_receipt(self):
        canonical_url = "https://www.youtube.com/watch?v=4biXYSNkn9Y"
        fingerprint = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()

        def fake_runner(command, **unused):
            if "detect" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "status": "ok",
                            "operation": "detect",
                            "platform": "youtube",
                            "sanitized_url": canonical_url,
                            "source_fingerprint": fingerprint,
                        }
                    ),
                    stderr="",
                )
            if "download" in command:
                output_dir = Path(command[command.index("--output-dir") + 1])
                final = output_dir / ".awesome-capture-media/v2/downloads/youtube/fake/hash"
                final.mkdir(parents=True)
                media = final / "media.mp4"
                media.write_bytes(b"fixture-video")
                media.chmod(0o600)
                media_hash = hashlib.sha256(media.read_bytes()).hexdigest()
                artifact = {
                    "schema_version": "awesome-capture.artifact/v2",
                    "artifact_type": "video",
                    "status": "complete",
                    "created_at": "2026-07-27T00:00:00+00:00",
                    "source": {
                        "platform": "youtube",
                        "fingerprint": fingerprint,
                        "url": canonical_url,
                        "webpage_url": canonical_url,
                        "id": "public",
                        "title": "",
                        "author": "",
                        "extractor": "fixture",
                    },
                    "media": {
                        "path": str(media),
                        "bytes": media.stat().st_size,
                        "sha256": media_hash,
                        "duration_ms": 1000,
                        "has_video": True,
                        "has_audio": False,
                        "container": "mp4",
                        "video_streams": 1,
                        "audio_streams": 0,
                    },
                    "acquisition": {
                        "auth_mode": "anonymous",
                        "fallback": "none",
                        "warnings": [],
                    },
                    "producer": {
                        "skill": "download-video",
                        "tool": "fixture",
                        "version": "1",
                        "contract_digest": "",
                    },
                }
                # run_smoke imports functions, not the contracts module object.
                from contracts.contract_runtime import (
                    contract_digest,
                    video_probe_evidence_sha256,
                )

                artifact["media"]["ffprobe"] = {
                    "tool": "ffprobe",
                    "version": "fixture-1",
                    "evidence_sha256": video_probe_evidence_sha256(
                        artifact["media"]
                    ),
                }
                artifact["producer"]["contract_digest"] = contract_digest()
                artifact_path = final / "artifact.json"
                artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "status": "ok",
                            "operation": "download",
                            "artifact_path": str(artifact_path),
                        }
                    ),
                    stderr="",
                )
            if command[0] == "ffprobe":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "format": {
                                "duration": "1.0",
                                "format_name": "mp4",
                            },
                            "streams": [{"codec_type": "video"}],
                        }
                    ),
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {command}")

        with tempfile.TemporaryDirectory() as temporary:
            receipt_dir = Path(temporary) / "receipts"
            with (
                mock.patch.object(run_smoke, "_commit_sha", return_value="d" * 40),
                mock.patch.object(
                    run_smoke, "implementation_digest", return_value="e" * 64
                ),
                mock.patch.object(
                    run_smoke,
                    "_environment",
                    return_value={"os": "linux", "arch": "x86_64", "python": "3.14.0"},
                ),
                mock.patch.object(
                    run_smoke,
                    "collect_tools",
                    return_value=[
                        {"name": "python", "version": "3.14.0"},
                        {"name": "ffmpeg", "version": "8.1"},
                        {"name": "ffprobe", "version": "8.1"},
                        {"name": "yt-dlp", "version": "2026.7.4"},
                        {"name": "deno", "version": "2.9.4"},
                        {"name": "yt-dlp-ejs", "version": "1.0.0"},
                    ],
                ),
            ):
                receipt, path = run_smoke.run_case(
                    "youtube-anonymous",
                    receipt_dir=receipt_dir,
                    environ={"AWESOME_CAPTURE_SMOKE_YOUTUBE_URL": canonical_url},
                    runner=fake_runner,
                )
            self.assertEqual(receipt["outcome"], "pass")
            self.assertEqual(receipt["source"]["fingerprint"], fingerprint)
            self.assertEqual(receipt["artifacts"][0]["type"], "video-artifact")
            assertions = {
                item["name"]: item["passed"] for item in receipt["assertions"]
            }
            self.assertTrue(assertions["anonymous-route-observed"])
            self.assertTrue(assertions["required-tools-observed"])
            serialized = path.read_text(encoding="utf-8")
            self.assertNotIn(canonical_url, serialized)
            self.assertNotIn(str(Path(temporary)), serialized)
            run_smoke.validate_contract(receipt, expected="smoke-receipt")

    def test_independent_ffprobe_mismatch_fails_media_assertion_and_receipt(self):
        canonical_url = "https://www.youtube.com/watch?v=4biXYSNkn9Y"
        fingerprint = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()

        def fake_runner(command, **unused):
            if "detect" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "status": "ok",
                            "operation": "detect",
                            "platform": "youtube",
                            "sanitized_url": canonical_url,
                            "source_fingerprint": fingerprint,
                        }
                    ),
                    stderr="",
                )
            if "download" in command:
                output_dir = Path(command[command.index("--output-dir") + 1])
                final = (
                    output_dir
                    / ".awesome-capture-media/v2/downloads/youtube/fake/hash"
                )
                final.mkdir(parents=True)
                media = final / "media.mp4"
                media.write_bytes(b"fixture-video")
                media.chmod(0o600)
                media_hash = hashlib.sha256(media.read_bytes()).hexdigest()
                artifact = {
                    "schema_version": "awesome-capture.artifact/v2",
                    "artifact_type": "video",
                    "status": "complete",
                    "created_at": "2026-07-27T00:00:00+00:00",
                    "source": {
                        "platform": "youtube",
                        "fingerprint": fingerprint,
                    },
                    "media": {
                        "path": str(media),
                        "bytes": media.stat().st_size,
                        "sha256": media_hash,
                        "duration_ms": 1000,
                        "has_video": True,
                        "has_audio": False,
                        "container": "mp4",
                        "video_streams": 1,
                        "audio_streams": 0,
                    },
                    "acquisition": {
                        "auth_mode": "anonymous",
                        "fallback": "none",
                        "warnings": [],
                    },
                    "producer": {
                        "skill": "download-video",
                        "tool": "fixture",
                        "version": "1",
                        "contract_digest": "",
                    },
                }
                from contracts.contract_runtime import (
                    contract_digest,
                    video_probe_evidence_sha256,
                )

                artifact["media"]["ffprobe"] = {
                    "tool": "ffprobe",
                    "version": "fixture-1",
                    "evidence_sha256": video_probe_evidence_sha256(
                        artifact["media"]
                    ),
                }
                artifact["producer"]["contract_digest"] = contract_digest()
                run_smoke.validate_contract(
                    artifact,
                    expected="video-artifact",
                )
                artifact_path = final / "artifact.json"
                artifact_path.write_text(
                    json.dumps(artifact),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "status": "ok",
                            "operation": "download",
                            "artifact_path": str(artifact_path),
                        }
                    ),
                    stderr="",
                )
            if command[0] == "ffprobe":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "format": {
                                "duration": "2.0",
                                "format_name": "mp4",
                            },
                            "streams": [{"codec_type": "video"}],
                        }
                    ),
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {command}")

        with tempfile.TemporaryDirectory() as temporary:
            receipt_dir = Path(temporary) / "receipts"
            with (
                mock.patch.object(run_smoke, "_commit_sha", return_value="d" * 40),
                mock.patch.object(
                    run_smoke, "implementation_digest", return_value="e" * 64
                ),
                mock.patch.object(
                    run_smoke,
                    "_environment",
                    return_value={
                        "os": "linux",
                        "arch": "x86_64",
                        "python": "3.14.0",
                    },
                ),
                mock.patch.object(
                    run_smoke,
                    "collect_tools",
                    return_value=[
                        {"name": "python", "version": "3.14.0"},
                        {"name": "ffmpeg", "version": "8.1"},
                        {"name": "ffprobe", "version": "8.1"},
                        {"name": "yt-dlp", "version": "2026.7.4"},
                        {"name": "deno", "version": "2.9.4"},
                        {"name": "yt-dlp-ejs", "version": "1.0.0"},
                    ],
                ),
            ):
                receipt, path = run_smoke.run_case(
                    "youtube-anonymous",
                    receipt_dir=receipt_dir,
                    environ={
                        "AWESOME_CAPTURE_SMOKE_YOUTUBE_URL": canonical_url
                    },
                    runner=fake_runner,
                )

            assertions = {
                item["name"]: item["passed"]
                for item in receipt["assertions"]
            }
            self.assertTrue(assertions["video-artifact-v2-valid"])
            self.assertFalse(assertions["video-media-reverified"])
            self.assertEqual(receipt["outcome"], "fail")
            validated = smoke_receipts.validate_receipt(
                path,
                require_pass=False,
                require_current_digest=False,
            )
            self.assertEqual(validated["outcome"], "fail")

    def test_transcription_commands_require_explicit_local_models(self):
        whisper = run_smoke._transcription_command(
            run_smoke.select_case("whisper-cpp-local"),
            source="/local/media.wav",
            model="/local/model.bin",
            binary="/local/whisper-cli",
            output_dir=Path("/local/output"),
        )
        self.assertIn("--model", whisper)
        self.assertIn("--whisper-cpp-bin", whisper)
        self.assertNotIn("http", " ".join(whisper).lower())
        cpu = run_smoke._transcription_command(
            run_smoke.select_case("whisper-cpp-cpu"),
            source="/local/media.wav",
            model="/local/model.bin",
            binary="/local/whisper-cli",
            output_dir=Path("/local/output"),
        )
        self.assertIn("--whisper-cpp-cpu-only", cpu)
        fallback = run_smoke._transcription_command(
            run_smoke.select_case("whisper-cpp-gpu-fallback"),
            source="/local/media.wav",
            model="/local/model.bin",
            binary="/local/fallback-whisper-cli",
            output_dir=Path("/local/output"),
        )
        self.assertNotIn("--whisper-cpp-cpu-only", fallback)
        external = run_smoke._transcription_command(
            run_smoke.select_case("external-local"),
            source="/local/media.wav",
            model="/local/model",
            binary="/local/adapter",
            output_dir=Path("/local/output"),
        )
        self.assertIn("--trust-external-adapter", external)
        self.assertIn("--adapter", external)
        long_resume = run_smoke._transcription_command(
            run_smoke.select_case("external-long-resume"),
            source="/local/long-media.wav",
            model="/local/model",
            binary="/local/adapter",
            output_dir=Path("/local/output"),
        )
        self.assertEqual(
            long_resume[long_resume.index("--chunk-seconds") + 1],
            "30",
        )

    def test_long_resume_kills_only_after_strict_partial_results_and_reuses_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                output_dir,
                workspace,
                state,
                manifest,
                records,
            ) = self.partial_chunk_result_fixture(Path(temporary))
            result_path = (
                workspace
                / "chunk-results"
                / "chunk-00000.wav.result.json"
            )

            invalid = json.loads(result_path.read_text(encoding="utf-8"))
            invalid["unknown"] = True
            self.write_private_json(result_path, invalid)
            self.assertIsNone(
                run_smoke._find_partial_chunk_results(output_dir)
            )
            invalid.pop("unknown")
            self.write_private_json(result_path, invalid)

            partial, killed = run_smoke._kill_after_partial_results(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(60)",
                ],
                output_dir,
                timeout_seconds=2,
                poll_seconds=0.01,
            )
            self.assertTrue(killed)
            self.assertIsNotNone(partial)
            assert partial is not None
            self.assertEqual(
                set(partial["results"]),
                {"chunk-00000.wav"},
            )
            self.assertEqual(partial["expected_count"], 2)

            manifest_path = workspace / "chunks" / "chunks.manifest.json"
            manifest_sha256 = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
            second_name = "chunk-00001.wav"
            second_envelope = {
                **invalid,
                "chunk_name": second_name,
                "result": records[second_name],
            }
            self.write_private_json(
                workspace
                / "chunk-results"
                / f"{second_name}.result.json",
                second_envelope,
            )
            reference = run_smoke._chunk_reference(
                manifest_path,
                manifest,
                manifest_sha256,
            )
            completed_state = {
                **state,
                "status": "complete",
                "chunk_set": reference,
                "chunks": records,
            }
            self.write_private_json(workspace / "state.json", completed_state)
            artifact = {
                "transcription": {
                    "chunk_set": reference,
                }
            }
            self.assertTrue(
                run_smoke._completed_run_reused_partial_results(
                    partial,
                    artifact,
                )
            )

            replacement = result_path.with_suffix(".replacement")
            replacement.write_bytes(result_path.read_bytes())
            os.chmod(replacement, 0o600)
            os.replace(replacement, result_path)
            self.assertFalse(
                run_smoke._completed_run_reused_partial_results(
                    partial,
                    artifact,
                )
            )

    def test_long_resume_assertion_requires_preserved_partial_result_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            model = root / "model"
            adapter = root / "adapter"
            transcript = root / "transcript.json"
            manifest = root / "chunks.manifest.json"
            for path in (source, model, adapter, transcript, manifest):
                path.write_bytes(b"fixture")
            manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
            artifact = {
                "source": {"sha256": "a" * 64},
                "transcription": {
                    "engine": "external",
                    "chunk_set": {
                        "manifest_path": str(manifest),
                        "manifest_sha256": manifest_sha256,
                        "count": 2,
                    },
                    "engine_identity": {
                        "identity_sha256": "b" * 64,
                        "model": {"sha256": "c" * 64},
                        "adapter": {"sha256": "d" * 64},
                        "executable": None,
                        "packages": [],
                    },
                },
                "warnings": [],
            }
            chunk_manifest = {"count": 2}
            partial = {
                "workspace": root,
                "job_id": "e" * 64,
                "manifest_sha256": manifest_sha256,
                "expected_count": 2,
                "results": {"chunk-00000.wav": {"inode": 1}},
            }

            def fake_runner(command, **unused):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "result": "created",
                            "transcript_path": str(transcript),
                        }
                    ),
                    stderr="",
                )

            def fake_read(path, *, expected):
                return artifact if expected == "transcript-artifact" else chunk_manifest

            with (
                mock.patch.object(
                    run_smoke,
                    "_transcription_command",
                    return_value=["transcribe"],
                ),
                mock.patch.object(
                    run_smoke,
                    "_kill_after_partial_results",
                    return_value=(partial, True),
                ),
                mock.patch.object(
                    run_smoke,
                    "read_json_strict",
                    side_effect=fake_read,
                ),
                mock.patch.object(run_smoke, "validate_file_context"),
                mock.patch.object(run_smoke, "collect_tools", return_value=[]),
                mock.patch.object(
                    run_smoke,
                    "_completed_run_reused_partial_results",
                    return_value=True,
                ) as reused,
            ):
                evidence = run_smoke._transcription_case(
                    run_smoke.select_case("external-long-resume"),
                    str(source),
                    str(model),
                    str(adapter),
                    root / "work",
                    runner=fake_runner,
                )
            assertions = {
                item["name"]: item["passed"]
                for item in evidence["assertions"]
            }
            self.assertTrue(assertions["partial-chunk-state-observed"])
            self.assertTrue(assertions["transcription-process-sigkilled"])
            self.assertTrue(
                assertions["long-transcription-resumed-after-sigkill"]
            )
            self.assertTrue(assertions["partial-chunk-results-reused"])
            reused.assert_called_once_with(partial, artifact)

    def test_workflow_has_only_case_input_and_no_credentials(self):
        workflow = (ROOT / ".github/workflows/smoke.yml").read_text(encoding="utf-8")
        inputs = workflow.split("permissions:", 1)[0]
        configured_cases = re.findall(r"(?m)^          - ([a-z0-9-]+)$", inputs)
        registered_cases = [case["case_id"] for case in run_smoke.load_cases()]
        self.assertIn("case:", inputs)
        self.assertNotIn("url:", inputs.lower())
        self.assertNotIn("cookie", workflow.lower())
        self.assertNotIn("cookies-from-browser", workflow.lower())
        self.assertNotIn("browser_path", workflow.lower())
        self.assertNotIn("download model", workflow.lower())
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("tools/run_smoke.py run \"$SMOKE_CASE\"", workflow)
        self.assertCountEqual(configured_cases, registered_cases)


if __name__ == "__main__":
    unittest.main()
