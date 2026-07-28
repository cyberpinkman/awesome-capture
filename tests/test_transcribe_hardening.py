from __future__ import annotations

import argparse
import hashlib
import importlib.util
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


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "transcribe-media" / "scripts" / "transcribe_media.py"
SPEC = importlib.util.spec_from_file_location("transcribe_hardening_module", SCRIPT)
assert SPEC and SPEC.loader
transcribe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transcribe)
from _contracts import media_runtime as safe_runtime  # type: ignore  # noqa: E402


class TranscribeHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise AssertionError("The hardening suite requires the repository ffmpeg/ffprobe baseline.")

    def root(self, temporary: str) -> Path:
        # macOS exposes /var as a root-owned alias. The runtime accepts it too,
        # but canonical paths keep fixture comparisons deterministic.
        return Path(temporary).resolve()

    def write_wav(self, path: Path, *, seconds: float = 2.0) -> None:
        frames = int(16000 * seconds)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x01\x00" * frames)

    def write_sidecar(self, media: Path, text: str = "严格转写") -> Path:
        sidecar = media.with_suffix(".srt")
        sidecar.write_text(
            f"1\n00:00:00,100 --> 00:00:01,000\n{text}\n\n",
            encoding="utf-8",
        )
        return sidecar

    def sidecar_args(self, media: Path, output: Path, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "media": str(media),
            "output_dir": str(output),
            "source_artifact": None,
            "ignore_sidecar": False,
            "engine": "auto",
            "model": None,
            "language": None,
            "adapter": None,
            "trust_external_adapter": False,
            "whisper_cpp_bin": None,
            "whisper_cpp_cpu_only": False,
            "chunk_seconds": 30,
            "timeout": 60,
            "lock_timeout": 1.0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def derive_sidecar_job_id(self, media: Path, root: Path) -> str:
        created = transcribe.transcribe(
            self.sidecar_args(media, root / "job-id-probe")
        )
        job_id = Path(created["transcript_path"]).parent.name
        self.assertEqual(len(job_id), 64)
        self.assertEqual(job_id, job_id.lower())
        int(job_id, 16)
        return job_id

    def precreate_workspace(
        self,
        output: Path,
        job_id: str,
    ) -> Path:
        transcriptions = transcribe.secure_mkdirs(
            output
            / transcribe.MANAGED_ROOT_NAME
            / transcribe.MANAGED_LAYOUT_VERSION
            / "transcriptions"
        )
        workspace = transcriptions / job_id
        workspace.mkdir(mode=0o700)
        return workspace

    def write_adapter(self, path: Path, body: str) -> Path:
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def test_auto_is_whisper_cpp_only_and_requires_explicit_model_file(self) -> None:
        with mock.patch.object(transcribe, "module_available", return_value=True):
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.select_engine("auto")
        self.assertEqual(raised.exception.code, "ENGINE_UNAVAILABLE")
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            model = root / "model.bin"
            model.write_bytes(b"model")
            with mock.patch.object(transcribe, "probe_whisper_cpp") as probe:
                with self.assertRaises(transcribe.TranscriptionError) as raised:
                    transcribe.select_engine("auto", str(model), None)
            self.assertEqual(raised.exception.code, "ENGINE_UNAVAILABLE")
            probe.assert_not_called()

    def test_whisper_version_probe_executes_the_content_verified_fd_not_a_swapped_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            binary = self.write_adapter(
                root / "whisper-cli",
                "print('whisper.cpp 9.9.9')\n",
            )
            sentinel = root / "foreign-was-executed"
            foreign = self.write_adapter(
                root / "foreign",
                "import pathlib\n"
                f"pathlib.Path({str(sentinel)!r}).write_text('unsafe')\n"
                "print('foreign 1.0')\n",
            )
            real_run_process = transcribe.run_process
            swapped = False

            def swap_after_fd_validation(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                nonlocal swapped
                if not swapped and "--version" in command:
                    swapped = True
                    os.replace(foreign, binary)
                return real_run_process(command, **kwargs)

            with mock.patch.object(
                transcribe,
                "run_process",
                side_effect=swap_after_fd_validation,
            ):
                with self.assertRaises(transcribe.TranscriptionError) as raised:
                    transcribe.probe_whisper_cpp(str(binary), timeout=5)
            self.assertTrue(swapped)
            self.assertEqual(raised.exception.code, "ENGINE_UNAVAILABLE")
            self.assertFalse(sentinel.exists())

    def test_whisper_runner_rejects_path_swap_without_executing_foreign_binary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            model = root / "model.bin"
            model.write_bytes(b"local-model")
            binary = self.write_adapter(
                root / "whisper-cli",
                "import json, pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('whisper.cpp 9.9.9')\n"
                "else:\n"
                "    prefix = sys.argv[sys.argv.index('-of') + 1]\n"
                "    value = {'transcription':[{'text':'safe',"
                "'offsets':{'from':100,'to':1000}}],"
                "'result':{'language':'en'}}\n"
                "    pathlib.Path(prefix + '.json').write_text(json.dumps(value))\n",
            )
            identity = transcribe.engine_identity_for(
                "whisper-cpp",
                str(model),
                None,
                str(binary),
                timeout=5,
            )
            sentinel = root / "foreign-was-executed"
            foreign = self.write_adapter(
                root / "foreign",
                "import pathlib\n"
                f"pathlib.Path({str(sentinel)!r}).write_text('unsafe')\n",
            )
            real_run_process = transcribe.run_process
            swapped = False

            def swap_after_fd_validation(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                nonlocal swapped
                if not swapped and "-ojf" in command:
                    swapped = True
                    os.replace(foreign, binary)
                return real_run_process(command, **kwargs)

            run = transcribe.whisper_cpp_runner(
                identity,
                None,
                5,
                cpu_only=True,
                gpu_previously_failed=False,
            )
            with mock.patch.object(
                transcribe,
                "run_process",
                side_effect=swap_after_fd_validation,
            ):
                with self.assertRaises(transcribe.TranscriptionError) as raised:
                    run(media)
            self.assertTrue(swapped)
            self.assertEqual(raised.exception.code, "IDENTITY_CHANGED")
            self.assertFalse(sentinel.exists())

    def test_content_identity_hashes_local_model_tree_and_rejects_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            model = root / "model"
            model.mkdir()
            (model / "a.bin").write_bytes(b"a")
            (model / "b.bin").write_bytes(b"b")
            first = transcribe.content_identity(model)
            self.assertEqual(first["kind"], "directory")
            self.assertEqual(first["file_count"], 2)
            (model / "b.bin").write_bytes(b"changed")
            second = transcribe.content_identity(model)
            self.assertNotEqual(first["sha256"], second["sha256"])
            os.symlink(model / "a.bin", model / "linked.bin")
            with self.assertRaises(transcribe.SafeRuntimeError) as raised:
                transcribe.content_identity(model)
            self.assertEqual(raised.exception.code, "UNSAFE_MODEL")
            (model / "linked.bin").unlink()
            single_model = root / "model.bin"
            single_model.write_bytes(b"weights")
            os.link(single_model, root / "model-hardlink.bin")
            with self.assertRaises(transcribe.SafeRuntimeError) as raised:
                transcribe.content_identity(single_model)
            self.assertEqual(raised.exception.code, "UNSAFE_MODEL")

    def test_job_identity_depends_on_content_not_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            models = (root / "model-a", root / "model-b")
            adapters = (root / "adapter-a", root / "adapter-b")
            for model in models:
                model.mkdir(mode=0o700)
                (model / "weights.bin").write_bytes(b"identical-local-model")
            adapter_body = (
                "import json\n"
                "print(json.dumps({'protocol':'awesome-capture.external-asr/v1',"
                "'language':'en','segments':[]}))\n"
            )
            for adapter in adapters:
                self.write_adapter(adapter, adapter_body)

            identities = [
                transcribe.engine_identity_for(
                    "external",
                    str(model),
                    str(adapter),
                    None,
                    timeout=5,
                    trust_external_adapter=True,
                )
                for model, adapter in zip(models, adapters, strict=True)
            ]
            self.assertNotEqual(
                identities[0]["model"]["path"],
                identities[1]["model"]["path"],
            )
            self.assertNotEqual(
                identities[0]["adapter"]["path"],
                identities[1]["adapter"]["path"],
            )
            self.assertEqual(
                identities[0]["identity_sha256"],
                identities[1]["identity_sha256"],
            )

            settings_values = []
            for index, identity in enumerate(identities):
                settings_values.append(
                    {
                        "contract_digest": transcribe.contract_digest(),
                        "algorithm": transcribe.transcription_algorithm_identity(),
                        "source_path": str(root / f"source-{index}.wav"),
                        "source_sha256": "a" * 64,
                        "source_bytes": 1024,
                        "upstream_artifact_sha256": "b" * 64,
                        "engine": "external",
                        "engine_identity": identity,
                        "requested_language": "en",
                        "chunk_seconds": 30,
                        "whisper_cpp_cpu_only": False,
                        "sidecar_sha256": None,
                    }
                )
            settings_hashes = [
                transcribe.canonical_json_sha256(
                    transcribe.transcription_settings_identity(settings)
                )
                for settings in settings_values
            ]
            self.assertEqual(settings_hashes[0], settings_hashes[1])
            job_ids = [
                hashlib.sha256(
                    b"awesome-capture.transcription-job/v2\0"
                    + digest.encode("ascii")
                ).hexdigest()
                for digest in settings_hashes
            ]
            self.assertEqual(job_ids[0], job_ids[1])

    def test_source_artifact_digest_is_bound_to_one_safe_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            artifact = root / "video.artifact.json"
            raw = b'{"status":"complete"}\n'
            artifact.write_bytes(raw)
            self.assertEqual(
                transcribe.current_source_artifact_sha256(artifact),
                hashlib.sha256(raw).hexdigest(),
            )

            duplicate = root / "artifact-hardlink.json"
            os.link(artifact, duplicate)
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.current_source_artifact_sha256(artifact)
            self.assertEqual(raised.exception.code, "IDENTITY_CHANGED")

            duplicate.unlink()
            artifact.unlink()
            target = root / "other.json"
            target.write_bytes(raw)
            os.symlink(target, artifact)
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.current_source_artifact_sha256(artifact)
            self.assertEqual(raised.exception.code, "IDENTITY_CHANGED")

    def test_external_requires_trust_adapter_and_local_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            adapter = self.write_adapter(root / "adapter", "print('{}')\n")
            model = root / "model"
            model.mkdir()
            (model / "weights").write_bytes(b"model")
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.engine_identity_for(
                    "external",
                    str(model),
                    str(adapter),
                    None,
                    timeout=1,
                )
            self.assertEqual(raised.exception.code, "EXTERNAL_ADAPTER_NOT_TRUSTED")
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.engine_identity_for(
                    "external",
                    "remote/repository-id",
                    str(adapter),
                    None,
                    timeout=1,
                    trust_external_adapter=True,
                )
            self.assertIn(raised.exception.code, {"MODEL_UNAVAILABLE", "UNSAFE_PATH"})

    def test_external_adapter_stdout_must_be_exactly_one_strict_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            model = root / "model"
            model.mkdir()
            (model / "weights").write_bytes(b"model")
            adapter = self.write_adapter(
                root / "adapter",
                "print('{\"protocol\":\"awesome-capture.external-asr/v1\","
                "\"language\":null,\"segments\":[]} trailing')\n",
            )
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.transcribe(
                    self.sidecar_args(
                        media,
                        root / "output",
                        ignore_sidecar=True,
                        engine="external",
                        model=str(model),
                        adapter=str(adapter),
                        trust_external_adapter=True,
                    )
                )
            self.assertEqual(raised.exception.code, "INVALID_ADAPTER_OUTPUT")
            self.assertEqual(
                list((root / "output").rglob("transcript.json")),
                [],
            )

    def test_external_adapter_path_swap_cannot_execute_foreign_or_publish(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            model = root / "model"
            model.mkdir()
            (model / "weights").write_bytes(b"model")
            adapter = self.write_adapter(
                root / "adapter",
                "import json\n"
                "print(json.dumps({'protocol':'awesome-capture.external-asr/v1',"
                "'language':'en','segments':["
                "{'start':0.1,'end':1.0,'text':'safe'}]}))\n",
            )
            sentinel = root / "foreign-was-executed"
            foreign = self.write_adapter(
                root / "foreign",
                "import json, pathlib\n"
                f"pathlib.Path({str(sentinel)!r}).write_text('unsafe')\n"
                "print(json.dumps({'protocol':'awesome-capture.external-asr/v1',"
                "'language':None,'segments':[]}))\n",
            )
            output = root / "output"
            real_run_process = transcribe.run_process
            swapped = False

            def swap_after_fd_validation(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                nonlocal swapped
                if not swapped and "--protocol" in command:
                    swapped = True
                    os.replace(foreign, adapter)
                return real_run_process(command, **kwargs)

            with mock.patch.object(
                transcribe,
                "run_process",
                side_effect=swap_after_fd_validation,
            ):
                with self.assertRaises(transcribe.TranscriptionError) as raised:
                    transcribe.transcribe(
                        self.sidecar_args(
                            media,
                            output,
                            ignore_sidecar=True,
                            engine="external",
                            model=str(model),
                            adapter=str(adapter),
                            trust_external_adapter=True,
                        )
                    )
            self.assertTrue(swapped)
            self.assertEqual(raised.exception.code, "IDENTITY_CHANGED")
            self.assertFalse(sentinel.exists())
            self.assertEqual(list(output.rglob("transcript.json")), [])

    def test_external_empty_segments_publish_consistent_no_speech(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            model = root / "model"
            model.mkdir()
            (model / "weights").write_bytes(b"model")
            adapter = self.write_adapter(
                root / "adapter",
                "import json\n"
                "print(json.dumps({'protocol':'awesome-capture.external-asr/v1',"
                "'language':None,'segments':[]}))\n",
            )
            result = transcribe.transcribe(
                self.sidecar_args(
                    media,
                    root / "output",
                    ignore_sidecar=True,
                    engine="external",
                    model=str(model),
                    adapter=str(adapter),
                    trust_external_adapter=True,
                )
            )
            artifact = json.loads(
                Path(result["transcript_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["segments"], [])
            self.assertEqual(artifact["text"], "")
            self.assertTrue(artifact["no_speech_detected"])

    def test_segment_validation_rejects_bool_nan_and_chunk_overflow(self) -> None:
        invalid = [
            [{"start": True, "end": 1.0, "text": "x"}],
            [{"start": float("nan"), "end": 1.0, "text": "x"}],
            [{"start": 0.0, "end": 2.0, "text": "x"}],
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(transcribe.TranscriptionError) as raised:
                    transcribe.normalize_engine_segments(
                        value,
                        chunk_index=0,
                        offset_ms=0,
                        chunk_duration_ms=1000,
                    )
                self.assertEqual(raised.exception.code, "INVALID_ENGINE_OUTPUT")
        with self.assertRaises(ValueError):
            transcribe.whisper_cpp_milliseconds(
                {"offsets": {"from": True}},
                "from",
            )

    def test_chunk_set_manifest_rejects_extra_missing_and_changed_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            source = root / "source.wav"
            self.write_wav(source)
            chunks_dir = root / "private" / "chunks"
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            transcribe.normalize_chunks(
                source,
                chunks_dir,
                30,
                60,
                job_id="a" * 64,
                source_sha256=source_hash,
            )
            manifest = transcribe.validate_chunk_set(
                chunks_dir,
                expected_job_id="a" * 64,
                expected_source_sha256=source_hash,
            )
            self.assertEqual(manifest["count"], 1)
            extra = chunks_dir / "unexpected.bin"
            extra.write_bytes(b"x")
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.validate_chunk_set(chunks_dir)
            self.assertEqual(raised.exception.code, "CHUNK_SET_CONFLICT")
            extra.unlink()
            chunk = chunks_dir / "chunk-00000.wav"
            chunk.write_bytes(chunk.read_bytes() + b"x")
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.validate_chunk_set(chunks_dir)
            self.assertEqual(raised.exception.code, "CHUNK_SET_CONFLICT")

    def test_chunk_staging_fifo_is_rejected_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            chunks = self.root(temporary) / "chunks"
            chunks.mkdir(mode=0o700)
            os.mkfifo(chunks / "chunk-00000.wav", mode=0o600)
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.secure_private_chunk_timeline(chunks)
            self.assertEqual(raised.exception.code, "CHUNK_SET_CONFLICT")

    def test_sidecar_uses_private_snapshot_v2_state_and_artifact_last(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            # A neighbouring legacy artifact must not be inspected or inherited.
            Path(f"{media}.artifact.json").write_text(
                '{"schema_version":"awesome-capture.artifact/v1"}',
                encoding="utf-8",
            )
            result = transcribe.transcribe(self.sidecar_args(media, root / "output"))
            artifact_path = Path(result["transcript_path"])
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(result["result"], "created")
            self.assertEqual(artifact["schema_version"], "awesome-capture.artifact/v2")
            self.assertIsNone(artifact["source"]["upstream"])
            self.assertIn("/.awesome-capture-media/v2/transcriptions/", str(artifact_path))
            self.assertEqual(
                stat.S_IMODE(artifact_path.stat().st_mode),
                0o600,
            )
            snapshot = Path(artifact["source"]["snapshot_path"])
            self.assertNotEqual(snapshot, media)
            self.assertEqual(snapshot.read_bytes(), media.read_bytes())
            state = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], transcribe.STATE_SCHEMA_VERSION)
            self.assertEqual(state["status"], "complete")
            self.assertRegex(state["execution_guard_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                state["execution_guard_sha256"],
                artifact["transcription"]["execution_guard_sha256"],
            )
            for descriptor in artifact["outputs"].values():
                if descriptor is not None:
                    path = Path(descriptor["path"])
                    self.assertEqual(descriptor["bytes"], path.stat().st_size)
                    self.assertEqual(
                        descriptor["sha256"],
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )

    def test_preexisting_empty_deterministic_workspace_is_recovery_conflict(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            job_id = self.derive_sidecar_job_id(media, root)
            output = root / "occupied-output"
            workspace = self.precreate_workspace(output, job_id)

            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.transcribe(self.sidecar_args(media, output))
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertEqual(list(workspace.iterdir()), [])

    def test_preseeded_allowed_workspace_entries_are_never_overwritten(
        self,
    ) -> None:
        allowed_names = (
            "source.snapshot",
            "sidecar.snapshot.srt",
            "sidecar.snapshot.vtt",
            "chunks",
            "chunk-results",
            "state.json",
            "transcript.pending.json",
            "transcript.json",
            "transcript.md",
            "transcript.txt",
            "transcript.srt",
            "transcript.vtt",
        )
        sentinel_bytes = b"foreign-workspace-placeholder"
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            job_id = self.derive_sidecar_job_id(media, root)

            for index, name in enumerate(allowed_names):
                with self.subTest(name=name):
                    output = root / f"occupied-output-{index}"
                    workspace = self.precreate_workspace(output, job_id)
                    placeholder = workspace / name
                    if name in {"chunks", "chunk-results"}:
                        placeholder.mkdir(mode=0o700)
                        sentinel = placeholder / "sentinel"
                    else:
                        sentinel = placeholder
                    sentinel.write_bytes(sentinel_bytes)
                    sentinel.chmod(0o600)
                    placeholder_identity = (
                        os.lstat(placeholder).st_dev,
                        os.lstat(placeholder).st_ino,
                    )
                    sentinel_identity = (
                        os.lstat(sentinel).st_dev,
                        os.lstat(sentinel).st_ino,
                    )

                    with self.assertRaises(transcribe.TranscriptionError):
                        transcribe.transcribe(self.sidecar_args(media, output))

                    self.assertEqual(
                        (
                            os.lstat(placeholder).st_dev,
                            os.lstat(placeholder).st_ino,
                        ),
                        placeholder_identity,
                    )
                    self.assertEqual(
                        (
                            os.lstat(sentinel).st_dev,
                            os.lstat(sentinel).st_ino,
                        ),
                        sentinel_identity,
                    )
                    self.assertEqual(sentinel.read_bytes(), sentinel_bytes)

    def test_algorithm_content_identity_creates_a_distinct_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            args = self.sidecar_args(media, root / "output")
            first = transcribe.transcribe(args)
            replacement = {
                "version": transcribe.ALGORITHM_VERSION,
                "sha256": "0" * 64,
            }
            with mock.patch.object(
                transcribe,
                "transcription_algorithm_identity",
                return_value=replacement,
            ):
                second = transcribe.transcribe(args)
            self.assertEqual(first["result"], "created")
            self.assertEqual(second["result"], "created")
            self.assertNotEqual(first["transcript_path"], second["transcript_path"])

    def test_explicit_legacy_source_artifact_is_rejected_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            artifact = root / "legacy.json"
            artifact.write_text(
                '{"schema_version":"awesome-capture.artifact/v1","artifact_type":"video"}',
                encoding="utf-8",
            )
            artifact.chmod(0o600)
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.transcribe(
                    self.sidecar_args(
                        media,
                        root / "output",
                        source_artifact=str(artifact),
                    )
                )
            self.assertEqual(raised.exception.code, "UNSUPPORTED_SCHEMA_VERSION")

    def test_external_protocol_chunk_set_reuse_and_model_change_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            model = root / "model"
            model.mkdir()
            (model / "weights").write_bytes(b"model")
            adapter = self.write_adapter(
                root / "adapter",
                "import json\n"
                "print(json.dumps({'protocol':'awesome-capture.external-asr/v1',"
                "'language':'zh','segments':[{'start':0.1,'end':1.0,'text':'内容'}]}))\n",
            )
            args = self.sidecar_args(
                media,
                root / "output",
                ignore_sidecar=True,
                engine="external",
                model=str(model),
                adapter=str(adapter),
                trust_external_adapter=True,
            )
            created = transcribe.transcribe(args)
            artifact = json.loads(Path(created["transcript_path"]).read_text(encoding="utf-8"))
            self.assertEqual(artifact["transcription"]["chunk_set"]["count"], 1)
            timeline = artifact["transcription"]["chunk_set"]["timeline"]
            self.assertEqual(len(timeline), 1)
            self.assertEqual(timeline[0]["offset_ms"], 0)
            self.assertEqual(
                timeline[-1]["offset_ms"] + timeline[-1]["duration_ms"],
                artifact["source"]["duration_ms"],
            )
            self.assertEqual(transcribe.transcribe(args)["result"], "reused")
            chunks_dir = (
                Path(created["transcript_path"]).parent / "chunks"
            )
            (chunks_dir / "extra").write_bytes(b"tamper")
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.transcribe(args)
            self.assertEqual(raised.exception.code, "CHUNK_SET_CONFLICT")

    def test_external_adapter_mutating_then_restoring_model_cannot_publish_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            model = root / "model"
            model.mkdir()
            weights = model / "weights"
            weights.write_bytes(b"model")
            adapter = self.write_adapter(
                root / "adapter",
                "import json, pathlib, sys\n"
                "model = pathlib.Path(sys.argv[sys.argv.index('--model') + 1])\n"
                "weights = model / 'weights'\n"
                "before = weights.stat()\n"
                "original = weights.read_bytes()\n"
                "weights.write_bytes(b'changed')\n"
                "weights.write_bytes(original)\n"
                "import os\n"
                "os.utime(weights, ns=(before.st_atime_ns, before.st_mtime_ns))\n"
                "print(json.dumps({'protocol':'awesome-capture.external-asr/v1',"
                "'language':None,'segments':[{'start':0.1,'end':1.0,'text':'x'}]}))\n",
            )
            args = self.sidecar_args(
                media,
                root / "output",
                ignore_sidecar=True,
                engine="external",
                model=str(model),
                adapter=str(adapter),
                trust_external_adapter=True,
            )
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.transcribe(args)
            self.assertEqual(raised.exception.code, "IDENTITY_CHANGED")
            self.assertEqual(
                list((root / "output").rglob("transcript.json")),
                [],
            )

    def test_external_adapter_mutating_then_restoring_snapshot_cannot_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            model = root / "model"
            model.mkdir()
            (model / "weights").write_bytes(b"model")
            adapter = self.write_adapter(
                root / "adapter",
                "import json, os, pathlib, sys\n"
                "source = pathlib.Path('../source.snapshot')\n"
                "before = source.stat()\n"
                "original = source.read_bytes()\n"
                "source.write_bytes(b'changed')\n"
                "source.write_bytes(original)\n"
                "os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))\n"
                "print(json.dumps({'protocol':'awesome-capture.external-asr/v1',"
                "'language':None,'segments':[]}))\n",
            )
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.transcribe(
                    self.sidecar_args(
                        media,
                        root / "output",
                        ignore_sidecar=True,
                        engine="external",
                        model=str(model),
                        adapter=str(adapter),
                        trust_external_adapter=True,
                    )
                )
            self.assertEqual(raised.exception.code, "IDENTITY_CHANGED")
            self.assertEqual(list((root / "output").rglob("transcript.json")), [])

    def test_recover_publishes_pending_artifact_and_symlink_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            output = root / "output"
            created = transcribe.transcribe(self.sidecar_args(media, output))
            transcript_path = Path(created["transcript_path"])
            transcript_path.unlink()
            recovered = transcribe.recover_output(str(output), lock_timeout=1)
            self.assertEqual(recovered["workspaces"][0]["status"], "recovered")
            self.assertTrue(transcript_path.is_file())

            outside = root / "outside"
            outside.mkdir()
            linked_output = root / "linked-output"
            os.symlink(outside, linked_output)
            with self.assertRaises((transcribe.TranscriptionError, transcribe.SafeRuntimeError)) as raised:
                transcribe.transcribe(self.sidecar_args(media, linked_output))
            self.assertIn(
                raised.exception.code,
                {"UNSAFE_PATH", "UNSAFE_DIRECTORY"},
            )
            self.assertEqual(list(outside.iterdir()), [])

    def test_atomic_writes_and_snapshot_copy_keep_pinned_parent_during_symlink_swap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            outside = root / "outside"
            outside.mkdir()

            managed = root / "managed"
            managed.mkdir(mode=0o700)
            pinned = root / "managed-pinned"
            original_write_all = safe_runtime._write_all
            swapped = False

            def swap_atomic_parent(descriptor: int, value: bytes) -> None:
                nonlocal swapped
                if not swapped:
                    managed.rename(pinned)
                    managed.symlink_to(outside, target_is_directory=True)
                    swapped = True
                original_write_all(descriptor, value)

            with mock.patch.object(
                safe_runtime,
                "_write_all",
                side_effect=swap_atomic_parent,
            ):
                safe_runtime.atomic_bytes(
                    managed / "artifact.json",
                    b'{"safe":true}\n',
                )
            self.assertEqual(
                (pinned / "artifact.json").read_bytes(),
                b'{"safe":true}\n',
            )
            self.assertEqual(list(outside.iterdir()), [])

            with self.assertRaises(safe_runtime.SafeRuntimeError) as raised:
                with safe_runtime.exclusive_lock(
                    root / "invalid-timeout.lock",
                    timeout=float("nan"),
                ):
                    pass
            self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")

            source = root / "source.bin"
            source.write_bytes(b"private media bytes")
            expected_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            snapshot_parent = root / "snapshot-parent"
            snapshot_parent.mkdir(mode=0o700)
            pinned_snapshot_parent = root / "snapshot-parent-pinned"
            snapshot_swapped = False

            def swap_snapshot_parent(descriptor: int, value: bytes) -> None:
                nonlocal snapshot_swapped
                if not snapshot_swapped:
                    snapshot_parent.rename(pinned_snapshot_parent)
                    snapshot_parent.symlink_to(outside, target_is_directory=True)
                    snapshot_swapped = True
                original_write_all(descriptor, value)

            with mock.patch.object(
                safe_runtime,
                "_write_all",
                side_effect=swap_snapshot_parent,
            ):
                safe_runtime.copy_private_snapshot(
                    source,
                    snapshot_parent / "source.snapshot",
                    expected_sha256=expected_sha256,
                    expected_bytes=source.stat().st_size,
                )
            self.assertEqual(
                (pinned_snapshot_parent / "source.snapshot").read_bytes(),
                source.read_bytes(),
            )
            self.assertEqual(list(outside.iterdir()), [])

    def test_atomic_noreplace_preserves_racing_publication_and_quarantine_targets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            original = safe_runtime.rename_noreplace

            source_directory = root / "chunks-staging"
            source_directory.mkdir(mode=0o700)
            chunk = source_directory / "chunk-00000.wav"
            chunk.write_bytes(b"chunk")
            chunk.chmod(0o600)
            target_directory = root / "chunks"
            directory_placeholder: dict[str, int] = {}

            def race_directory(
                source_name: str,
                destination_name: str,
                *,
                source_dir_fd: int,
                destination_dir_fd: int,
            ) -> None:
                os.mkdir(destination_name, 0o700, dir_fd=destination_dir_fd)
                directory_placeholder["inode"] = os.stat(
                    destination_name,
                    dir_fd=destination_dir_fd,
                    follow_symlinks=False,
                ).st_ino
                original(
                    source_name,
                    destination_name,
                    source_dir_fd=source_dir_fd,
                    destination_dir_fd=destination_dir_fd,
                )

            with mock.patch.object(
                safe_runtime,
                "rename_noreplace",
                side_effect=race_directory,
            ):
                with self.assertRaises(safe_runtime.SafeRuntimeError) as raised:
                    safe_runtime.publish_private_directory(
                        source_directory,
                        target_directory,
                    )
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertTrue(source_directory.is_dir())
            self.assertEqual(
                target_directory.stat().st_ino,
                directory_placeholder["inode"],
            )

            quarantine = root / "quarantine"
            quarantine.mkdir(mode=0o700)
            staged_directory = root / "staged-dir"
            staged_directory.mkdir(mode=0o700)
            (staged_directory / "partial").write_bytes(b"partial")
            (staged_directory / "partial").chmod(0o600)
            with mock.patch.object(
                safe_runtime,
                "rename_noreplace",
                side_effect=race_directory,
            ):
                with self.assertRaises(safe_runtime.SafeRuntimeError) as raised:
                    safe_runtime.quarantine_private_directory(
                        staged_directory,
                        quarantine,
                        target_name="directory-race",
                    )
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertTrue(staged_directory.is_dir())
            self.assertTrue((quarantine / "directory-race").is_dir())

            staged_file = root / "staged-file"
            staged_file.write_bytes(b"private")
            staged_file.chmod(0o600)
            file_placeholder: dict[str, int] = {}

            def race_file(
                source_name: str,
                destination_name: str,
                *,
                source_dir_fd: int,
                destination_dir_fd: int,
            ) -> None:
                descriptor = os.open(
                    destination_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_dir_fd,
                )
                try:
                    os.write(descriptor, b"foreign")
                finally:
                    os.close(descriptor)
                file_placeholder["inode"] = os.stat(
                    destination_name,
                    dir_fd=destination_dir_fd,
                    follow_symlinks=False,
                ).st_ino
                original(
                    source_name,
                    destination_name,
                    source_dir_fd=source_dir_fd,
                    destination_dir_fd=destination_dir_fd,
                )

            with mock.patch.object(
                safe_runtime,
                "rename_noreplace",
                side_effect=race_file,
            ):
                with self.assertRaises(safe_runtime.SafeRuntimeError) as raised:
                    safe_runtime.quarantine_private_file(
                        staged_file,
                        quarantine,
                        target_name="file-race",
                    )
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertTrue(staged_file.is_file())
            raced_file = quarantine / "file-race"
            self.assertEqual(raced_file.read_bytes(), b"foreign")
            self.assertEqual(raced_file.stat().st_ino, file_placeholder["inode"])

    def test_noreplace_capability_is_required(self) -> None:
        with mock.patch.object(
            safe_runtime,
            "rename_noreplace_available",
            return_value=False,
        ):
            with self.assertRaises(safe_runtime.SafeRuntimeError) as raised:
                safe_runtime.require_posix_security()
        self.assertEqual(raised.exception.code, "UNSUPPORTED_PLATFORM")

    def test_pending_recovery_finishes_state_transition_after_publish_boundary_crash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            sidecar = self.write_sidecar(media)
            upstream_path = root / "upstream-artifact.json"
            upstream_path.write_text("{}", encoding="utf-8")
            upstream_path.chmod(0o600)
            upstream = {
                "artifact_path": str(upstream_path),
                "artifact_sha256": "a" * 64,
                "platform": "youtube",
                "fingerprint": "b" * 64,
            }
            output = root / "output"
            original_write_state = transcribe._write_state

            def fail_before_complete(
                path: Path,
                state: dict[str, object],
                *,
                expected_previous: dict[str, object] | None = None,
            ) -> None:
                if state.get("status") == "complete":
                    raise transcribe.TranscriptionError(
                        "SIMULATED_CRASH",
                        "Simulated interruption before complete-state publication.",
                        exit_code=4,
                    )
                original_write_state(
                    path,
                    state,
                    expected_previous=expected_previous,
                )

            with (
                mock.patch.object(
                    transcribe,
                    "upstream_source",
                    return_value=(upstream, []),
                ),
                mock.patch.object(
                    transcribe,
                    "_write_state",
                    side_effect=fail_before_complete,
                ),
            ):
                with self.assertRaises(transcribe.TranscriptionError) as raised:
                    transcribe.transcribe(self.sidecar_args(media, output))
            self.assertEqual(raised.exception.code, "SIMULATED_CRASH")
            workspace = next(
                (
                    output
                    / ".awesome-capture-media"
                    / "v2"
                    / "transcriptions"
                ).iterdir()
            )
            self.assertTrue((workspace / "transcript.pending.json").is_file())
            self.assertFalse((workspace / "transcript.json").exists())
            interrupted_state = json.loads(
                (workspace / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(interrupted_state["status"], "ready_to_publish")

            media.unlink()
            sidecar.unlink()
            upstream_path.unlink()
            recovered = transcribe.recover_output(str(output), lock_timeout=1)
            self.assertEqual(recovered["workspaces"][0]["status"], "recovered")
            self.assertTrue((workspace / "transcript.json").is_file())
            completed_state = json.loads(
                (workspace / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(completed_state["status"], "complete")

    def test_running_state_with_durable_pending_intent_resumes_without_early_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            output = root / "output"
            args = self.sidecar_args(media, output)
            original_write_state = transcribe._write_state

            def fail_before_ready(
                path: Path,
                state: dict[str, object],
                *,
                expected_previous: dict[str, object] | None = None,
            ) -> None:
                if state.get("status") == "ready_to_publish":
                    raise transcribe.TranscriptionError(
                        "SIMULATED_CRASH",
                        "Simulated interruption before the ready marker.",
                        exit_code=4,
                    )
                original_write_state(
                    path,
                    state,
                    expected_previous=expected_previous,
                )

            with mock.patch.object(
                transcribe,
                "_write_state",
                side_effect=fail_before_ready,
            ):
                with self.assertRaises(transcribe.TranscriptionError) as raised:
                    transcribe.transcribe(args)
            self.assertEqual(raised.exception.code, "SIMULATED_CRASH")
            workspace = next(
                (
                    output
                    / ".awesome-capture-media"
                    / "v2"
                    / "transcriptions"
                ).iterdir()
            )
            self.assertTrue((workspace / "transcript.pending.json").is_file())
            state = json.loads(
                (workspace / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "running")
            recovery = transcribe.recover_output(str(output), lock_timeout=1)
            self.assertEqual(recovery["workspaces"][0]["status"], "pending")
            self.assertFalse((workspace / "transcript.json").exists())
            resumed = transcribe.transcribe(args)
            self.assertEqual(resumed["result"], "created")
            self.assertTrue((workspace / "transcript.json").is_file())

    def test_recovery_rechecks_snapshot_identity_before_publishing_pending_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            output = root / "output"
            created = transcribe.transcribe(self.sidecar_args(media, output))
            transcript_path = Path(created["transcript_path"])
            artifact = json.loads(transcript_path.read_text(encoding="utf-8"))
            transcript_path.unlink()
            Path(artifact["source"]["snapshot_path"]).write_bytes(b"tampered")

            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.recover_output(str(output), lock_timeout=1)
            self.assertEqual(raised.exception.code, "IDENTITY_CHANGED")
            self.assertFalse(transcript_path.exists())

    def test_legacy_complete_state_without_artifact_is_resumed_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            output = root / "output"
            args = self.sidecar_args(media, output)
            created = transcribe.transcribe(args)
            transcript_path = Path(created["transcript_path"])
            workspace = transcript_path.parent
            transcript_path.unlink()
            (workspace / "transcript.pending.json").unlink()

            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.transcribe(args)
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertFalse(transcript_path.exists())

    def test_recovery_quarantines_proven_chunk_staging_residues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            output = root / "output"
            created = transcribe.transcribe(self.sidecar_args(media, output))
            workspace = Path(created["transcript_path"]).parent
            managed_root = workspace.parent.parent
            job_id = workspace.name

            legacy = workspace / "chunks.staging.deadbeef"
            legacy.mkdir(mode=0o700)
            (legacy / "chunk-00000.wav").write_bytes(b"partial")
            global_staging = safe_runtime.create_private_directory(
                managed_root / "staging",
                prefix=f"transcribe-{job_id}-chunks.",
            )
            (global_staging / "chunk-00000.wav").write_bytes(b"partial")

            recovered = transcribe.recover_output(str(output), lock_timeout=1)
            self.assertEqual(recovered["workspaces"][0]["status"], "complete")
            self.assertFalse(legacy.exists())
            self.assertFalse(global_staging.exists())
            quarantine = managed_root / "quarantine"
            self.assertTrue(
                (
                    quarantine
                    / f"transcribe-{job_id}-legacy-chunks.deadbeef"
                ).is_dir()
            )
            self.assertTrue((quarantine / global_staging.name).is_dir())

    def test_recovery_rejects_legacy_hardlink_without_deleting_either_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            media = root / "source.wav"
            self.write_wav(media)
            self.write_sidecar(media)
            output = root / "output"
            created = transcribe.transcribe(self.sidecar_args(media, output))
            transcript_path = Path(created["transcript_path"])
            workspace = transcript_path.parent
            managed_root = workspace.parent.parent

            duplicate_link = (
                workspace / f".transcript.json.{'a' * 32}.tmp"
            )
            os.link(transcript_path, duplicate_link)
            interrupted_replace = workspace / f".state.json.{'b' * 32}.tmp"
            interrupted_replace.write_bytes(b"partial state")
            interrupted_replace.chmod(0o600)

            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.recover_output(str(output), lock_timeout=1)
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertTrue(duplicate_link.exists())
            self.assertEqual(os.lstat(transcript_path).st_nlink, 2)
            self.assertFalse(interrupted_replace.exists())
            quarantine_name = (
                f"transcribe-{workspace.name}-atomic-{'b' * 32}-state-json"
            )
            self.assertTrue((managed_root / "quarantine" / quarantine_name).exists())

    def test_existing_broad_managed_root_is_rejected_without_tightening(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            output = root / "output"
            output.mkdir(mode=0o700)
            managed = output / ".awesome-capture-media"
            managed.mkdir(mode=0o700)
            managed.chmod(0o777)
            with self.assertRaises(
                (transcribe.TranscriptionError, transcribe.SafeRuntimeError)
            ) as raised:
                transcribe.recover_output(str(output), lock_timeout=1)
            self.assertIn(
                raised.exception.code,
                {"UNSAFE_PATH", "UNSAFE_DIRECTORY"},
            )
            self.assertEqual(managed.stat().st_mode & 0o777, 0o777)
            self.assertFalse((managed / "v2").exists())

    def test_strict_json_wrapper_rejects_hardlinks_and_contract_drift_is_integrity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(temporary)
            artifact_path = root / "video.json"
            value = json.loads(
                (
                    ROOT
                    / "contracts"
                    / "fixtures"
                    / "valid"
                    / "video-artifact.json"
                ).read_text(encoding="utf-8")
            )
            value["producer"]["contract_digest"] = "0" * 64
            artifact_path.write_text(json.dumps(value), encoding="utf-8")
            alias = root / "video-alias.json"
            os.link(artifact_path, alias)
            with self.assertRaises(transcribe.TranscriptionError) as unsafe:
                transcribe.strict_json_file(
                    artifact_path,
                    maximum_bytes=transcribe.JSON_LIMIT,
                    description="video artifact",
                )
            self.assertEqual(unsafe.exception.code, "UNSAFE_PATH")
            alias.unlink()
            artifact_path.chmod(0o644)
            with self.assertRaises(transcribe.TranscriptionError) as mode_drift:
                transcribe.validate_video_artifact(
                    artifact_path,
                    Path(value["media"]["path"]),
                    value["media"],
                    value["media"]["sha256"],
                )
            self.assertEqual(mode_drift.exception.code, "UNSAFE_PATH")
            artifact_path.chmod(0o600)
            with self.assertRaises(transcribe.TranscriptionError) as drift:
                transcribe.validate_video_artifact(
                    artifact_path,
                    Path(value["media"]["path"]),
                    value["media"],
                    value["media"]["sha256"],
                )
            self.assertEqual(drift.exception.code, "CONTRACT_BUILD_MISMATCH")
            self.assertEqual(drift.exception.exit_code, 7)

    def test_doctor_and_nonfinite_lock_timeout_use_json_failure_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            empty_path = Path(temporary).resolve()
            doctor = subprocess.run(
                [sys.executable, str(SCRIPT), "doctor"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": str(empty_path),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                text=True,
                capture_output=True,
            )
            self.assertEqual(doctor.returncode, 3)
            self.assertEqual(doctor.stdout, "")
            self.assertEqual(
                json.loads(doctor.stderr)["error"]["code"],
                "DEPENDENCY_MISSING",
            )

            invalid_timeout = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "transcribe",
                    str(empty_path / "missing.wav"),
                    "--output-dir",
                    str(empty_path / "output"),
                    "--lock-timeout",
                    "nan",
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                capture_output=True,
            )
            self.assertEqual(invalid_timeout.returncode, 2)
            self.assertEqual(invalid_timeout.stdout, "")
            self.assertEqual(
                json.loads(invalid_timeout.stderr)["error"]["code"],
                "INVALID_ARGUMENT",
            )


if __name__ == "__main__":
    unittest.main()
