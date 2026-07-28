from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contracts.contract_runtime import (  # noqa: E402
    ContractError,
    _contract_manifest_entries,
    contract_digest,
    loads_strict,
    read_json_strict,
    validate_contract,
    validate_file_context,
    verify_contract_bundle,
)
from contracts.bundle_runtime import (  # noqa: E402
    RUNTIME_FILES as BUNDLE_RUNTIME_FILES,
    _runtime_manifest_entries,
    runtime_digest,
)
from contracts.posix_runtime import (  # noqa: E402
    FileLock,
    PosixRuntimeError,
    atomic_write_noclobber,
    ensure_dir,
    read_regular_file,
    strict_read_json,
    validate_relative_path,
)
from tools import sync_vendored  # noqa: E402


VALID = ROOT / "contracts" / "fixtures" / "valid"
INVALID = ROOT / "contracts" / "fixtures" / "invalid"
SCHEMAS = ROOT / "contracts" / "schemas"


class ContractSchemaTests(unittest.TestCase):
    def load_valid(self, name: str) -> dict:
        value = read_json_strict(VALID / name)
        self.assertIsInstance(value, dict)
        return value

    def test_every_schema_is_valid_draft_2020_12(self):
        expected = {
            "artifact-transcript-v2.schema.json",
            "artifact-video-v2.schema.json",
            "chunk-set-v1.schema.json",
            "ingest-receipt-v1.schema.json",
            "smoke-receipt-v1.schema.json",
            "transaction-v1.schema.json",
            "transcription-state-v1.schema.json",
            "vault-build-receipt-v1.schema.json",
            "vault-config-v1.schema.json",
        }
        self.assertEqual({path.name for path in SCHEMAS.glob("*.json")}, expected)
        for path in sorted(SCHEMAS.glob("*.json")):
            with self.subTest(schema=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    schema["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )
                Draft202012Validator.check_schema(schema)

    def test_all_positive_fixtures_pass_and_route_to_expected_contract(self):
        expected = {
            "video-artifact.json": "video-artifact",
            "transcript-artifact.json": "transcript-artifact",
            "chunk-set.json": "chunk-set",
            "transcription-state.json": "transcription-state",
            "transaction.json": "transaction",
            "vault-config.json": "vault-config",
            "vault-build-receipt.json": "vault-build-receipt",
            "ingest-receipt.json": "ingest-receipt",
            "smoke-receipt.json": "smoke-receipt",
        }
        for filename, contract_name in expected.items():
            with self.subTest(fixture=filename):
                value = self.load_valid(filename)
                self.assertEqual(validate_contract(value, expected=contract_name), contract_name)

    def test_strict_loader_rejects_duplicate_keys_and_nonfinite_numbers(self):
        cases = {
            "duplicate-key.json": "DUPLICATE_JSON_KEY",
            "non-finite.json": "INVALID_JSON",
        }
        for filename, code in cases.items():
            with self.subTest(fixture=filename):
                with self.assertRaises(ContractError) as raised:
                    loads_strict((INVALID / filename).read_bytes())
                self.assertEqual(raised.exception.code, code)

    def test_each_formal_contract_has_a_negative_fixture(self):
        expected = {
            "video-artifact-invalid.json": "video-artifact",
            "transcript-artifact-invalid.json": "transcript-artifact",
            "transcription-state-invalid.json": "transcription-state",
            "chunk-set-invalid.json": "chunk-set",
            "transaction-invalid.json": "transaction",
            "vault-config-invalid.json": "vault-config",
            "vault-build-receipt-invalid.json": "vault-build-receipt",
            "ingest-receipt-invalid.json": "ingest-receipt",
            "smoke-receipt-invalid.json": "smoke-receipt",
        }
        for filename, contract_name in expected.items():
            with self.subTest(fixture=filename):
                value = read_json_strict(
                    INVALID / filename,
                    validate=False,
                )
                with self.assertRaises(ContractError):
                    validate_contract(value, expected=contract_name)

    def test_legacy_unknown_and_absent_versions_are_rejected(self):
        with self.assertRaises(ContractError) as legacy:
            read_json_strict(INVALID / "legacy-artifact.json")
        self.assertEqual(legacy.exception.code, "UNSUPPORTED_SCHEMA_VERSION")
        with self.assertRaises(ContractError) as absent:
            validate_contract({"artifact_type": "video"})
        self.assertEqual(absent.exception.code, "UNSUPPORTED_SCHEMA_VERSION")
        with self.assertRaises(ContractError) as unknown:
            validate_contract(
                {
                    "schema_version": "awesome-capture.artifact/v99",
                    "artifact_type": "video",
                }
            )
        self.assertEqual(unknown.exception.code, "UNSUPPORTED_SCHEMA_VERSION")

    def test_unknown_properties_wrong_plain_integer_and_contract_drift_fail(self):
        video = self.load_valid("video-artifact.json")
        unknown = copy.deepcopy(video)
        unknown["source"]["token"] = "secret"
        with self.assertRaises(ContractError) as extra:
            validate_contract(unknown)
        self.assertIn(extra.exception.code, {"SCHEMA_VALIDATION_FAILED", "SENSITIVE_DATA_FORBIDDEN"})

        boolean_integer = copy.deepcopy(video)
        boolean_integer["media"]["bytes"] = True
        with self.assertRaises(ContractError) as wrong_type:
            validate_contract(boolean_integer)
        self.assertEqual(wrong_type.exception.code, "SCHEMA_VALIDATION_FAILED")

        drifted = copy.deepcopy(video)
        drifted["producer"]["contract_digest"] = "0" * 64
        with self.assertRaises(ContractError) as drift:
            validate_contract(drifted)
        self.assertEqual(drift.exception.code, "CONTRACT_BUILD_MISMATCH")

    def test_transcript_semantics_are_self_contained_and_strict(self):
        transcript = self.load_valid("transcript-artifact.json")
        self.assertFalse(Path(transcript["source"]["path"]).exists())
        self.assertEqual(validate_contract(transcript), "transcript-artifact")

        overlap = copy.deepcopy(transcript)
        overlap["segments"].append(
            {"start_ms": 900, "end_ms": 1100, "text": "重叠", "chunk_index": 0}
        )
        overlap["text"] += "\n重叠"
        with self.assertRaises(ContractError) as invalid_timeline:
            validate_contract(overlap)
        self.assertEqual(invalid_timeline.exception.code, "SEMANTIC_VALIDATION_FAILED")

        bad_text = copy.deepcopy(transcript)
        bad_text["text"] = "重建文本"
        with self.assertRaises(ContractError):
            validate_contract(bad_text)

        bad_chunk_index = copy.deepcopy(transcript)
        bad_chunk_index["segments"][0]["chunk_index"] = 999
        with self.assertRaises(ContractError) as invalid_chunk:
            validate_contract(bad_chunk_index)
        self.assertEqual(invalid_chunk.exception.code, "SEMANTIC_VALIDATION_FAILED")

        bad_job = copy.deepcopy(transcript)
        bad_job["transcription"]["job_id"] = "f" * 64
        with self.assertRaises(ContractError) as invalid_job:
            validate_contract(bad_job)
        self.assertEqual(invalid_job.exception.code, "SEMANTIC_VALIDATION_FAILED")

        state = self.load_valid("transcription-state.json")
        state["job_id"] = "f" * 64
        with self.assertRaises(ContractError) as invalid_state_job:
            validate_contract(state)
        self.assertEqual(
            invalid_state_job.exception.code,
            "SEMANTIC_VALIDATION_FAILED",
        )

    def test_ingest_id_and_smoke_redaction_semantics_are_enforced(self):
        receipt = self.load_valid("ingest-receipt.json")
        receipt["ingest_id"] = "0" * 64
        with self.assertRaises(ContractError):
            validate_contract(receipt)

        smoke = self.load_valid("smoke-receipt.json")
        smoke["warnings"] = ["log at https://private.example/path"]
        with self.assertRaises(ContractError) as sensitive:
            validate_contract(smoke)
        self.assertIn(
            sensitive.exception.code,
            {"SCHEMA_VALIDATION_FAILED", "SENSITIVE_DATA_FORBIDDEN"},
        )
        for leaked in (
            "built on alice at /Volumes/secret/movie.mp4",
            "host=alice.local",
            r"C:\Users\alice\movie.mp4",
        ):
            with self.subTest(leaked=leaked):
                leaked_receipt = self.load_valid("smoke-receipt.json")
                leaked_receipt["tools"][0]["version"] = leaked
                with self.assertRaises(ContractError):
                    validate_contract(leaked_receipt)

    def test_invalid_calendar_timestamp_is_rejected(self):
        video = self.load_valid("video-artifact.json")
        for timestamp in (
            "2026-99-99T99:99:99+99:99",
            "2026-02-30T12:00:00Z",
            "2026-01-01T24:01:00Z",
        ):
            with self.subTest(timestamp=timestamp):
                invalid = copy.deepcopy(video)
                invalid["created_at"] = timestamp
                with self.assertRaises(ContractError) as raised:
                    validate_contract(invalid)
                self.assertEqual(
                    raised.exception.code,
                    "SCHEMA_VALIDATION_FAILED",
                )

    def test_required_enum_unknown_version_path_hash_timestamp_matrix_runs_in_all_bundles(self):
        fixture_contracts = {
            "video-artifact.json": "video-artifact",
            "transcript-artifact.json": "transcript-artifact",
            "transcription-state.json": "transcription-state",
            "chunk-set.json": "chunk-set",
            "transaction.json": "transaction",
            "vault-config.json": "vault-config",
            "vault-build-receipt.json": "vault-build-receipt",
            "ingest-receipt.json": "ingest-receipt",
            "smoke-receipt.json": "smoke-receipt",
        }

        def resolve(schema: dict, root: dict) -> dict:
            while "$ref" in schema:
                pointer = schema["$ref"]
                self.assertTrue(pointer.startswith("#/"))
                resolved: object = root
                for component in pointer[2:].split("/"):
                    resolved = resolved[
                        component.replace("~1", "/").replace("~0", "~")
                    ]
                self.assertIsInstance(resolved, dict)
                schema = resolved
            return schema

        def type_matches(value: object, schema: dict, root: dict) -> bool:
            schema = resolve(schema, root)
            expected = schema.get("type")
            if isinstance(expected, list):
                return any(
                    type_matches(value, {"type": item}, root)
                    for item in expected
                )
            return {
                "null": value is None,
                "object": isinstance(value, dict),
                "array": isinstance(value, list),
                "string": isinstance(value, str),
                "integer": isinstance(value, int)
                and not isinstance(value, bool),
                "number": isinstance(value, (int, float))
                and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
            }.get(expected, True)

        def mutate_at(
            original: dict,
            path: tuple[object, ...],
            *,
            delete: bool = False,
            add_unknown: bool = False,
            replacement: object = None,
        ) -> dict:
            mutated = copy.deepcopy(original)
            cursor: object = mutated
            if add_unknown:
                for component in path:
                    cursor = cursor[component]
                cursor["__unexpected_contract_field__"] = True
                return mutated
            for component in path[:-1]:
                cursor = cursor[component]
            final = path[-1]
            if delete:
                del cursor[final]
            else:
                cursor[final] = replacement
            return mutated

        def generated_mutations(instance: dict, schema: dict) -> list[dict]:
            mutations: list[dict] = []

            def visit(
                value: object,
                node: dict,
                path: tuple[object, ...],
            ) -> None:
                node = resolve(node, schema)
                if "oneOf" in node:
                    branches = [
                        branch
                        for branch in node["oneOf"]
                        if type_matches(value, branch, schema)
                    ]
                    if branches:
                        visit(value, branches[0], path)
                    return
                if path and "const" in node:
                    mutations.append(
                        mutate_at(
                            instance,
                            path,
                            replacement="__invalid_const__",
                        )
                    )
                if path and "enum" in node:
                    mutations.append(
                        mutate_at(
                            instance,
                            path,
                            replacement="__invalid_enum__",
                        )
                    )
                if isinstance(value, dict):
                    for required in node.get("required", []):
                        if required in value:
                            mutations.append(
                                mutate_at(
                                    instance,
                                    (*path, required),
                                    delete=True,
                                )
                            )
                    if node.get("additionalProperties") is False:
                        mutations.append(
                            mutate_at(
                                instance,
                                path,
                                add_unknown=True,
                            )
                        )
                    for key, child in value.items():
                        child_schema = node.get("properties", {}).get(key)
                        if isinstance(child_schema, dict):
                            visit(child, child_schema, (*path, key))
                elif isinstance(value, list):
                    item_schema = node.get("items")
                    if isinstance(item_schema, dict):
                        for index, child in enumerate(value):
                            visit(child, item_schema, (*path, index))

            visit(instance, schema, ())
            return mutations

        def replace_path(
            value: dict,
            path: tuple[object, ...],
            replacement: object,
        ) -> dict:
            return mutate_at(value, path, replacement=replacement)

        category_mutations = {
            "video-artifact.json": [
                (("media", "path"), "relative.mp4"),
                (("media", "sha256"), "not-a-hash"),
                (("created_at",), "2026-99-99T99:99:99+99:99"),
                (("media", "bytes"), True),
            ],
            "transcript-artifact.json": [
                (("source", "path"), "relative.mp4"),
                (("source", "sha256"), "not-a-hash"),
                (("created_at",), "2026-02-30T00:00:00Z"),
                (("segments", 0, "start_ms"), True),
            ],
            "transcription-state.json": [
                (("settings", "source_path"), "../escape"),
                (("settings_sha256",), "not-a-hash"),
                (("settings", "source_bytes"), True),
            ],
            "chunk-set.json": [
                (("chunks", 0, "path"), "../escape.wav"),
                (("source_sha256",), "not-a-hash"),
                (("chunks", 0, "duration_ms"), True),
            ],
            "transaction.json": [
                (("root",), "relative"),
                (("steps", 0, "sha256"), "not-a-hash"),
                (("created_at",), "2026-13-01T00:00:00Z"),
                (("steps", 0, "bytes"), True),
            ],
            "vault-config.json": [
                (("folders", 0), "../escape"),
                (("daily_notes", "enabled"), 1),
            ],
            "vault-build-receipt.json": [
                (("managed_directories", 0), "../escape"),
                (("config_sha256",), "not-a-hash"),
                (("created_at",), "2026-01-32T00:00:00Z"),
                (("managed_files", 0, "bytes"), True),
            ],
            "ingest-receipt.json": [
                (("knowledge_note",), "../escape.md"),
                (("draft_sha256",), "not-a-hash"),
                (("created_at",), "2026-00-01T00:00:00Z"),
                (("segment_count",), True),
            ],
            "smoke-receipt.json": [
                (("tools", 0, "version"), "/private/movie.mp4"),
                (("implementation_digest",), "not-a-hash"),
                (("created_at",), "2026-01-01T25:00:00Z"),
            ],
        }

        invalid_cases: list[dict] = []
        valid_cases: list[dict] = []
        for filename, contract_name in fixture_contracts.items():
            value = self.load_valid(filename)
            schema = json.loads(
                (
                    SCHEMAS
                    / {
                        "video-artifact": "artifact-video-v2.schema.json",
                        "transcript-artifact": "artifact-transcript-v2.schema.json",
                        "transcription-state": "transcription-state-v1.schema.json",
                        "chunk-set": "chunk-set-v1.schema.json",
                        "transaction": "transaction-v1.schema.json",
                        "vault-config": "vault-config-v1.schema.json",
                        "vault-build-receipt": "vault-build-receipt-v1.schema.json",
                        "ingest-receipt": "ingest-receipt-v1.schema.json",
                        "smoke-receipt": "smoke-receipt-v1.schema.json",
                    }[contract_name]
                ).read_text(encoding="utf-8")
            )
            valid_cases.append(value)
            invalid_cases.extend(generated_mutations(value, schema))
            invalid_cases.extend(
                replace_path(value, path, replacement)
                for path, replacement in category_mutations[filename]
            )

        unique_invalid = {
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ): value
            for value in invalid_cases
        }
        self.assertGreaterEqual(len(unique_invalid), 100)
        for index, value in enumerate(unique_invalid.values()):
            with self.subTest(runtime="canonical", mutation=index):
                with self.assertRaises(ContractError):
                    validate_contract(value)

        runner = (
            "import json,sys;"
            "from _contracts.contract_runtime import ContractError,validate_contract;"
            "payload=json.load(open(sys.argv[1],encoding='utf-8'));"
            "assert all(validate_contract(v) for v in payload['valid']);"
            "accepted=[]\n"
            "for i,v in enumerate(payload['invalid']):\n"
            " try: validate_contract(v)\n"
            " except ContractError: continue\n"
            " accepted.append(i)\n"
            "assert not accepted, accepted\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            matrix = Path(temporary) / "matrix.json"
            matrix.write_text(
                json.dumps(
                    {
                        "valid": valid_cases,
                        "invalid": list(unique_invalid.values()),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            for skill in (
                "download-video",
                "transcribe-media",
                "build-obsidian-vault",
                "ingest-knowledge",
            ):
                with self.subTest(runtime=skill):
                    process = subprocess.run(
                        [sys.executable, "-c", runner, str(matrix)],
                        cwd=ROOT / "skills" / skill / "scripts",
                        env={
                            **os.environ,
                            "PYTHONDONTWRITEBYTECODE": "1",
                        },
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(process.returncode, 0, process.stderr)

    def test_video_file_context_rehashes_current_regular_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary).resolve() / "video.mp4"
            media.write_bytes(b"verified-video")
            media.chmod(0o600)
            video = self.load_valid("video-artifact.json")
            video["media"]["path"] = str(media)
            video["media"]["bytes"] = media.stat().st_size
            video["media"]["sha256"] = hashlib.sha256(media.read_bytes()).hexdigest()
            validate_file_context(video)
            media.write_bytes(b"changed")
            with self.assertRaises(ContractError) as changed:
                validate_file_context(video)
            self.assertEqual(changed.exception.code, "FILE_CONTEXT_MISMATCH")

    def test_contract_reader_rejects_hardlinked_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original = root / "artifact.json"
            original.write_bytes((VALID / "video-artifact.json").read_bytes())
            alias = root / "alias.json"
            os.link(original, alias)
            with self.assertRaises(ContractError) as raised:
                read_json_strict(original)
            self.assertEqual(raised.exception.code, "UNSAFE_JSON_FILE")

    def test_contract_reader_detects_final_path_swap_after_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original = root / "artifact.json"
            outside = root / "outside.json"
            original.write_bytes((VALID / "video-artifact.json").read_bytes())
            outside.write_bytes((VALID / "video-artifact.json").read_bytes())
            actual_read = os.read
            swapped = False

            def swap_then_read(descriptor: int, size: int) -> bytes:
                nonlocal swapped
                if not swapped:
                    original.unlink()
                    original.symlink_to(outside)
                    swapped = True
                return actual_read(descriptor, size)

            with mock.patch(
                "contracts.contract_runtime.os.read",
                side_effect=swap_then_read,
            ):
                with self.assertRaises(ContractError) as raised:
                    read_json_strict(original)
            self.assertEqual(raised.exception.code, "UNSAFE_JSON_FILE")

    def test_contract_manifest_and_all_standalone_copies_are_exact(self):
        digest = verify_contract_bundle()
        self.assertEqual(digest, contract_digest())
        self.assertEqual(len(runtime_digest()), 64)
        expected_contract_files = {
            *sync_vendored.CONTRACT_RUNTIME_FILES,
            *(
                f"schemas/{filename}"
                for filename in sync_vendored.SCHEMA_FILES
            ),
        }
        self.assertEqual(
            set(dict(_contract_manifest_entries())),
            expected_contract_files,
        )
        self.assertEqual(
            tuple(sync_vendored.RUNTIME_FILES),
            BUNDLE_RUNTIME_FILES,
        )
        self.assertEqual(
            set(dict(_runtime_manifest_entries())),
            set(BUNDLE_RUNTIME_FILES),
        )
        check = subprocess.run(
            [sys.executable, "tools/sync_vendored.py", "--check"],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        for skill in (
            "download-video",
            "transcribe-media",
            "ingest-knowledge",
            "build-obsidian-vault",
        ):
            scripts = ROOT / "skills" / skill / "scripts"
            process = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from _contracts.contract_runtime import "
                        "contract_digest,verify_contract_bundle;"
                        "from _contracts.bundle_runtime import runtime_digest;"
                        "assert contract_digest()==verify_contract_bundle();"
                        "assert len(runtime_digest())==64;"
                        "print(contract_digest())"
                    ),
                ],
                cwd=scripts,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                capture_output=True,
            )
            self.assertEqual(process.returncode, 0, f"{skill}: {process.stderr}")
            self.assertEqual(process.stdout.strip(), digest)

    def test_a_copied_skill_contract_bundle_has_no_repository_dependency(self):
        source = ROOT / "skills" / "download-video" / "scripts" / "_contracts"
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary) / "scripts"
            shutil.copytree(source, scripts / "_contracts")
            process = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from _contracts.contract_runtime import "
                        "contract_digest,validate_contract;"
                        "assert len(contract_digest())==64;"
                        "print('standalone-ok')"
                    ),
                ],
                cwd=scripts,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                capture_output=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout.strip(), "standalone-ok")

    def test_each_copied_skill_runs_from_an_arbitrary_working_directory(self):
        if shutil.which("ffprobe") is None:
            self.fail("The standalone contract test requires the CI-preflighted ffprobe.")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            copied = root / "copied"
            arbitrary_cwd = root / "arbitrary-cwd"
            copied.mkdir()
            arbitrary_cwd.mkdir()
            for skill in (
                "download-video",
                "transcribe-media",
                "build-obsidian-vault",
                "ingest-knowledge",
            ):
                shutil.copytree(ROOT / "skills" / skill, copied / skill)

            media = root / "fixture.wav"
            with wave.open(str(media), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\0\0" * 1600)
            transcript = root / "transcript.json"
            shutil.copyfile(VALID / "transcript-artifact.json", transcript)
            transcript.chmod(0o600)

            commands = [
                [
                    sys.executable,
                    str(copied / "download-video" / "scripts" / "download_video.py"),
                    "detect",
                    "https://www.youtube.com/watch?v=standalone",
                ],
                [
                    sys.executable,
                    str(copied / "transcribe-media" / "scripts" / "transcribe_media.py"),
                    "inspect",
                    str(media),
                ],
                [
                    sys.executable,
                    str(copied / "build-obsidian-vault" / "scripts" / "vault_builder.py"),
                    "validate-config",
                    str(copied / "build-obsidian-vault" / "assets" / "vault-config.example.json"),
                ],
                [
                    sys.executable,
                    str(copied / "ingest-knowledge" / "scripts" / "knowledge_writer.py"),
                    "validate-transcript",
                    str(transcript),
                ],
            ]
            for command in commands:
                with self.subTest(skill=Path(command[1]).parts[-3]):
                    process = subprocess.run(
                        command,
                        cwd=arbitrary_cwd,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                        text=True,
                        capture_output=True,
                        timeout=30,
                    )
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertEqual(process.stderr, "")
                    payload = json.loads(process.stdout)
                    self.assertIsInstance(payload, dict)
                    self.assertEqual(payload.get("status"), "ok")

    def test_all_skill_clis_use_the_json_error_channel(self):
        scripts = [
            ROOT / "skills" / "download-video" / "scripts" / "download_video.py",
            ROOT / "skills" / "transcribe-media" / "scripts" / "transcribe_media.py",
            ROOT / "skills" / "build-obsidian-vault" / "scripts" / "vault_builder.py",
            ROOT / "skills" / "ingest-knowledge" / "scripts" / "knowledge_writer.py",
        ]
        for script in scripts:
            with self.subTest(skill=script.parts[-3]):
                process = subprocess.run(
                    [sys.executable, str(script), "--not-a-real-option"],
                    cwd=ROOT,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(process.returncode, 2)
                self.assertEqual(process.stdout, "")
                error = json.loads(process.stderr)
                self.assertEqual(error.get("status"), "error")
                self.assertEqual(error.get("error", {}).get("code"), "INVALID_ARGUMENT")


class PosixRuntimeTests(unittest.TestCase):
    def test_relative_path_validation_rejects_escape_and_platform_separators(self):
        self.assertEqual(validate_relative_path("30 Resources/note.md"), Path("30 Resources/note.md"))
        for value in ("../escape", "/absolute", "folder/../../escape", r"folder\\escape"):
            with self.subTest(path=value):
                with self.assertRaises(PosixRuntimeError):
                    validate_relative_path(value)

    def test_atomic_noclobber_is_private_durable_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            managed = ensure_dir(Path(temporary).resolve() / "managed", 0o700, private=True)
            destination = managed / "receipt.json"
            atomic_write_noclobber(destination, b'{"ok":true}', 0o600)
            self.assertEqual(read_regular_file(destination, 100), b'{"ok":true}')
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            with self.assertRaises(PosixRuntimeError) as collision:
                atomic_write_noclobber(destination, b"replacement", 0o600)
            self.assertEqual(collision.exception.code, "PATH_COLLISION")
            self.assertEqual(destination.read_bytes(), b'{"ok":true}')

    def test_secure_reader_rejects_symlink_hardlink_duplicate_json_and_nan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original = root / "value.json"
            original.write_text('{"ok": true}', encoding="utf-8")
            symlink = root / "symlink.json"
            symlink.symlink_to(original)
            with self.assertRaises(PosixRuntimeError):
                read_regular_file(symlink, 100)
            hardlink = root / "hardlink.json"
            os.link(original, hardlink)
            with self.assertRaises(PosixRuntimeError):
                read_regular_file(original, 100)
            hardlink.unlink()
            original.write_text('{"a": 1, "a": 2}', encoding="utf-8")
            with self.assertRaises(PosixRuntimeError) as duplicate:
                strict_read_json(original, 100)
            self.assertEqual(duplicate.exception.code, "DUPLICATE_JSON_KEY")
            original.write_text('{"a": NaN}', encoding="utf-8")
            with self.assertRaises(PosixRuntimeError) as nonfinite:
                strict_read_json(original, 100)
            self.assertEqual(nonfinite.exception.code, "INVALID_JSON")

    def test_file_lock_has_bounded_contention_and_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            managed = ensure_dir(Path(temporary).resolve() / "managed", 0o700, private=True)
            lock_path = managed / "job.lock"
            child_code = (
                "import sys;"
                f"sys.path.insert(0,{str(ROOT)!r});"
                "from contracts.posix_runtime import FileLock,PosixRuntimeError;"
                f"p={str(lock_path)!r};"
                "\ntry:\n"
                "  with FileLock(p,timeout=0.05): pass\n"
                "except PosixRuntimeError as e:\n"
                "  assert e.code=='RESOURCE_BUSY'; print(e.code)\n"
                "else:\n"
                "  raise SystemExit('unexpected lock acquisition')\n"
            )
            with FileLock(lock_path, timeout=1):
                process = subprocess.run(
                    [sys.executable, "-c", child_code],
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    text=True,
                    capture_output=True,
                )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout.strip(), "RESOURCE_BUSY")
            self.assertTrue(lock_path.is_file())
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
