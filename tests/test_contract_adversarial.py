from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contracts import contract_runtime as canonical  # noqa: E402


VALID = ROOT / "contracts" / "fixtures" / "valid"
RUNTIME_PATHS = (
    ("canonical", ROOT / "contracts" / "contract_runtime.py"),
    (
        "download-video",
        ROOT
        / "skills"
        / "download-video"
        / "scripts"
        / "_contracts"
        / "contract_runtime.py",
    ),
    (
        "transcribe-media",
        ROOT
        / "skills"
        / "transcribe-media"
        / "scripts"
        / "_contracts"
        / "contract_runtime.py",
    ),
    (
        "build-obsidian-vault",
        ROOT
        / "skills"
        / "build-obsidian-vault"
        / "scripts"
        / "_contracts"
        / "contract_runtime.py",
    ),
    (
        "ingest-knowledge",
        ROOT
        / "skills"
        / "ingest-knowledge"
        / "scripts"
        / "_contracts"
        / "contract_runtime.py",
    ),
)


def load_runtime(label: str, path: Path) -> ModuleType:
    module_name = f"_awesome_capture_adversarial_{label.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load contract runtime: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_valid(name: str) -> dict[str, Any]:
    value = json.loads((VALID / name).read_text(encoding="utf-8"))
    if "producer" in value:
        value["producer"]["contract_digest"] = canonical.contract_digest()
    if "settings" in value:
        value["settings"]["contract_digest"] = canonical.contract_digest()
        value["settings_sha256"] = canonical.canonical_json_sha256(
            canonical._transcription_settings_identity(value["settings"])
        )
        value["job_id"] = hashlib.sha256(
            b"awesome-capture.transcription-job/v2\0"
            + value["settings_sha256"].encode("ascii")
        ).hexdigest()
    return value


class StrictLoaderAdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtimes = [
            (label, load_runtime(label, path))
            for label, path in RUNTIME_PATHS
        ]

    def test_every_runtime_rejects_hostile_json_scalars_and_truncation(self):
        cases = (
            ("positive-overflow", b'{"value":1e999}', "NON_FINITE_NUMBER"),
            ("negative-overflow", b'{"value":-1e999}', "NON_FINITE_NUMBER"),
            (
                "oversized-integer",
                b'{"value":' + (b"9" * 5000) + b"}",
                "INVALID_JSON",
            ),
            ("lone-surrogate", b'{"value":"\\ud800"}', "INVALID_UNICODE"),
            ("truncated", b'{"value":', "INVALID_JSON"),
        )
        for runtime_name, runtime in self.runtimes:
            for case_name, raw, expected_code in cases:
                with self.subTest(runtime=runtime_name, case=case_name):
                    with self.assertRaises(runtime.ContractError) as raised:
                        runtime.loads_strict(raw)
                    self.assertEqual(raised.exception.code, expected_code)

    def test_every_runtime_bounds_integers_when_interpreter_limit_is_disabled(
        self,
    ):
        original_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(0)
            raw = b'{"value":' + (b"9" * 5000) + b"}"
            for runtime_name, runtime in self.runtimes:
                with self.subTest(runtime=runtime_name):
                    with self.assertRaises(runtime.ContractError) as raised:
                        runtime.loads_strict(raw)
                    self.assertEqual(raised.exception.code, "INVALID_JSON")
        finally:
            sys.set_int_max_str_digits(original_limit)

    def test_every_runtime_enforces_the_four_mib_boundary(self):
        limit = 4 * 1024 * 1024
        prefix = b'{"value":"'
        suffix = b'"}'
        exactly_at_limit = (
            prefix
            + (b"a" * (limit - len(prefix) - len(suffix)))
            + suffix
        )
        one_byte_over = exactly_at_limit + b" "
        self.assertEqual(len(exactly_at_limit), limit)
        self.assertEqual(len(one_byte_over), limit + 1)

        for runtime_name, runtime in self.runtimes:
            with self.subTest(runtime=runtime_name, boundary="exact"):
                parsed = runtime.loads_strict(exactly_at_limit)
                self.assertEqual(
                    len(parsed["value"]),
                    limit - len(prefix) - len(suffix),
                )
            with self.subTest(runtime=runtime_name, boundary="over"):
                with self.assertRaises(runtime.ContractError) as raised:
                    runtime.loads_strict(one_byte_over)
                self.assertEqual(raised.exception.code, "JSON_TOO_LARGE")

    def test_every_runtime_hashes_the_exact_bytes_parsed_from_one_descriptor(self):
        raw = b'{\n  "value": "stable"\n}\n'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "input.json"
            path.write_bytes(raw)
            for runtime_name, runtime in self.runtimes:
                with self.subTest(runtime=runtime_name):
                    value, digest = runtime.read_json_strict_with_sha256(
                        path,
                        validate=False,
                    )
                    self.assertEqual(value, {"value": "stable"})
                    self.assertEqual(digest, hashlib.sha256(raw).hexdigest())


class SchemaParityAdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtimes = [
            (label, load_runtime(f"schema_{label}", path))
            for label, path in RUNTIME_PATHS
        ]

    def test_bool_integer_const_and_type_match_draft_2020_12(self):
        cases = (
            ("const-true-true", {"const": True}, True),
            ("const-true-one", {"const": True}, 1),
            ("const-false-false", {"const": False}, False),
            ("const-false-zero", {"const": False}, 0),
            ("const-one-one", {"const": 1}, 1),
            ("const-one-float-one", {"const": 1}, 1.0),
            ("const-one-true", {"const": 1}, True),
            ("integer-one", {"type": "integer"}, 1),
            ("integer-true", {"type": "integer"}, True),
            ("integer-false", {"type": "integer"}, False),
            ("boolean-true", {"type": "boolean"}, True),
            ("boolean-one", {"type": "boolean"}, 1),
            ("number-zero", {"type": "number"}, 0),
            ("number-false", {"type": "number"}, False),
        )
        for runtime_name, runtime in self.runtimes:
            for case_name, schema, instance in cases:
                expected = Draft202012Validator(schema).is_valid(instance)
                actual = True
                try:
                    runtime._validate_schema_node(
                        instance,
                        schema,
                        schema,
                        "$",
                    )
                except runtime.ContractError:
                    actual = False
                with self.subTest(runtime=runtime_name, case=case_name):
                    self.assertEqual(actual, expected)


class SemanticAdversarialTests(unittest.TestCase):
    def assert_semantic_rejected(self, value: dict[str, Any]) -> None:
        with self.assertRaises(canonical.ContractError) as raised:
            canonical.validate_contract(value)
        self.assertEqual(raised.exception.code, "SEMANTIC_VALIDATION_FAILED")

    def test_chunk_name_and_path_must_match_the_contiguous_index(self):
        wrong_name = load_valid("chunk-set.json")
        wrong_name["chunks"][0]["name"] = "chunk-00001.wav"
        self.assert_semantic_rejected(wrong_name)

        wrong_path = load_valid("chunk-set.json")
        wrong_path["chunks"][0]["path"] = "/private/stage/renamed.wav"
        self.assert_semantic_rejected(wrong_path)

    def test_state_silent_flag_must_exactly_match_segments(self):
        state = load_valid("transcription-state.json")
        state["chunks"]["sidecar"]["silent"] = True
        self.assert_semantic_rejected(state)

    def test_transaction_staging_prefix_and_receipt_last_invariants(self):
        outside_staging = load_valid("transaction.json")
        outside_staging["staging_root"] = (
            "/private/vault-other/transactions/job-1"
        )

        non_contiguous_publish_prefix = load_valid("transaction.json")
        non_contiguous_publish_prefix["status"] = "publishing"
        non_contiguous_publish_prefix["steps"][0]["status"] = "pending"
        second = copy.deepcopy(non_contiguous_publish_prefix["steps"][0])
        second.update(
            {
                "index": 1,
                "source": "source-2.md",
                "destination": "30 Resources/note-2.md",
                "status": "published",
            }
        )
        non_contiguous_publish_prefix["steps"].append(second)

        receipt_not_last = load_valid("transaction.json")
        receipt_not_last["steps"][0]["operation"] = "publish-receipt"
        following = copy.deepcopy(receipt_not_last["steps"][0])
        following.update(
            {
                "index": 1,
                "operation": "publish-file",
                "source": "source-2.md",
                "destination": "30 Resources/note-2.md",
            }
        )
        receipt_not_last["steps"].append(following)

        zero_receipt = load_valid("transaction.json")
        zero_receipt["steps"][0]["operation"] = "publish-file"

        for case_name, value in (
            ("staging-outside-root", outside_staging),
            ("published-after-pending", non_contiguous_publish_prefix),
            ("receipt-not-last", receipt_not_last),
            ("zero-receipt", zero_receipt),
        ):
            with self.subTest(case=case_name):
                self.assert_semantic_rejected(value)

    def test_ingest_notes_are_distinct_and_markers_are_exact(self):
        duplicate_note = load_valid("ingest-receipt.json")
        duplicate_note["source_note"] = duplicate_note["knowledge_note"]

        inexact_marker = load_valid("ingest-receipt.json")
        inexact_marker["initial_files"][0]["identity_marker"] += "-suffix"

        for case_name, value in (
            ("duplicate-note-path", duplicate_note),
            ("inexact-identity-marker", inexact_marker),
        ):
            with self.subTest(case=case_name):
                self.assert_semantic_rejected(value)

    def test_asr_engine_content_identity_kind_matrix_is_fail_closed(self):
        def component(kind: str, name: str) -> dict[str, Any]:
            value: dict[str, Any] = {
                "kind": kind,
                "path": f"/private/models/{name}",
                "sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                "bytes": 1,
                "version": None,
            }
            if kind == "directory":
                value["file_count"] = 1
            return value

        def identity(
            *,
            model: dict[str, Any] | None,
            executable: dict[str, Any] | None,
            adapter: dict[str, Any] | None,
            packages: list[dict[str, str]],
        ) -> dict[str, Any]:
            value = {
                "identity_sha256": "",
                "model": model,
                "executable": executable,
                "adapter": adapter,
                "packages": packages,
            }
            value["identity_sha256"] = canonical.canonical_json_sha256(
                canonical._engine_identity_projection(value)
            )
            return value

        valid = {
            "whisper-cpp": identity(
                model=component("file", "whisper-model.bin"),
                executable=component("file", "whisper-cli"),
                adapter=None,
                packages=[],
            ),
            "faster-whisper": identity(
                model=component("directory", "faster-model"),
                executable=None,
                adapter=None,
                packages=[
                    {"name": "faster-whisper", "version": "1"},
                    {"name": "ctranslate2", "version": "1"},
                ],
            ),
            "mlx-whisper": identity(
                model=component("directory", "mlx-model"),
                executable=None,
                adapter=None,
                packages=[
                    {"name": "mlx-whisper", "version": "1"},
                    {"name": "mlx", "version": "1"},
                ],
            ),
            "external": identity(
                model=component("file", "external-model.bin"),
                executable=None,
                adapter=component("file", "external-adapter"),
                packages=[],
            ),
            "sidecar-subtitle": identity(
                model=None,
                executable=None,
                adapter=component("file", "source.srt"),
                packages=[],
            ),
        }
        for engine, value in valid.items():
            with self.subTest(engine=engine, validity="baseline"):
                canonical._validate_engine_identity(
                    value,
                    engine,
                    path="$.engine_identity",
                    sidecar_present=engine == "sidecar-subtitle",
                )

        invalid = {
            "whisper-cpp": identity(
                model=component("directory", "whisper-model"),
                executable=component("file", "whisper-cli"),
                adapter=None,
                packages=[],
            ),
            "faster-whisper": identity(
                model=component("file", "faster-model.bin"),
                executable=None,
                adapter=None,
                packages=[{"name": "faster-whisper", "version": "1"}],
            ),
            "mlx-whisper": identity(
                model=component("directory", "mlx-model"),
                executable=component("file", "python"),
                adapter=None,
                packages=[{"name": "mlx-whisper", "version": "1"}],
            ),
            "external": identity(
                model=component("file", "external-model.bin"),
                executable=None,
                adapter=component("directory", "external-adapter"),
                packages=[],
            ),
            "sidecar-subtitle": identity(
                model=component("file", "unexpected-model.bin"),
                executable=None,
                adapter=component("file", "source.srt"),
                packages=[],
            ),
        }
        for engine, value in invalid.items():
            with self.subTest(engine=engine, validity="invalid"):
                with self.assertRaises(canonical.ContractError) as raised:
                    canonical._validate_engine_identity(
                        value,
                        engine,
                        path="$.engine_identity",
                        sidecar_present=engine == "sidecar-subtitle",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "SEMANTIC_VALIDATION_FAILED",
                )

        wrong_packages = identity(
            model=component("directory", "faster-model"),
            executable=None,
            adapter=None,
            packages=[
                {"name": "faster-whisper", "version": "1"},
                {"name": "not-ctranslate2", "version": "1"},
            ],
        )
        with self.assertRaises(canonical.ContractError):
            canonical._validate_engine_identity(
                wrong_packages,
                "faster-whisper",
                path="$.engine_identity",
                sidecar_present=False,
            )

    def test_public_url_and_state_sidecar_identity_are_fail_closed(self):
        video = load_valid("video-artifact.json")
        hostile_urls = (
            "https://[broken",
            "https://www.youtube.com/watch?v=one&v=two",
            "https://www.youtube.com/watch?v=token%3Dsecret",
            "https://www.youtube.com:444/watch?v=public",
        )
        for url in hostile_urls:
            with self.subTest(url=url):
                candidate = copy.deepcopy(video)
                candidate["source"]["url"] = url
                candidate["source"]["webpage_url"] = url
                with self.assertRaises(canonical.ContractError) as raised:
                    canonical.validate_contract(candidate)
                self.assertIn(
                    raised.exception.code,
                    {"SEMANTIC_VALIDATION_FAILED", "SENSITIVE_DATA_FORBIDDEN"},
                )

        state = load_valid("transcription-state.json")
        state["settings"]["engine"] = "faster-whisper"
        state["settings"]["sidecar_sha256"] = "a" * 64
        with self.assertRaises(canonical.ContractError) as raised:
            canonical._validate_state(state)
        self.assertEqual(
            raised.exception.code,
            "SEMANTIC_VALIDATION_FAILED",
        )

    def test_controlled_metadata_rejects_secret_assignments(self):
        video = load_valid("video-artifact.json")
        video["acquisition"]["warnings"] = ["token=redacted"]
        with self.assertRaises(canonical.ContractError) as raised:
            canonical.validate_contract(video)
        self.assertEqual(
            raised.exception.code,
            "SENSITIVE_DATA_FORBIDDEN",
        )

    def test_video_source_metadata_rejects_secret_assignments(self):
        for field in ("id", "title", "author", "extractor"):
            for assignment in (
                "token=redacted",
                "secret: redacted",
                "authorization=Bearer redacted",
            ):
                with self.subTest(field=field, assignment=assignment):
                    video = load_valid("video-artifact.json")
                    video["source"][field] = assignment
                    with self.assertRaises(canonical.ContractError) as raised:
                        canonical.validate_contract(video)
                    self.assertEqual(
                        raised.exception.code,
                        "SENSITIVE_DATA_FORBIDDEN",
                    )

    def test_video_ffprobe_proof_binds_normalized_media_facts(self):
        video = load_valid("video-artifact.json")
        video["media"]["duration_ms"] += 1
        self.assert_semantic_rejected(video)

    def test_chunk_file_context_rejects_an_extra_directory_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            chunk_directory = Path(temporary).resolve() / "chunks"
            chunk_directory.mkdir(mode=0o700)
            os.chmod(chunk_directory, 0o700)

            chunk_set = load_valid("chunk-set.json")
            chunk = chunk_set["chunks"][0]
            chunk_path = chunk_directory / chunk["name"]
            payload = b"\0" * 32044
            chunk_path.write_bytes(payload)
            os.chmod(chunk_path, 0o600)
            chunk["path"] = str(chunk_path)
            chunk["bytes"] = len(payload)
            chunk["sha256"] = hashlib.sha256(payload).hexdigest()

            manifest = chunk_directory / "chunks.manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            os.chmod(manifest, 0o600)

            canonical.validate_file_context(
                chunk_set,
                verify_chunks=True,
            )

            unexpected = chunk_directory / "unexpected.tmp"
            unexpected.write_bytes(b"foreign")
            os.chmod(unexpected, 0o600)
            with self.assertRaises(canonical.ContractError) as raised:
                canonical.validate_file_context(
                    chunk_set,
                    verify_chunks=True,
                )
            self.assertEqual(
                raised.exception.code,
                "FILE_CONTEXT_MISMATCH",
            )


if __name__ == "__main__":
    unittest.main()
