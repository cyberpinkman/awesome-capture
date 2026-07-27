from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


download_video = load_module(
    "download_video_skill", "skills/download-video/scripts/download_video.py"
)
transcribe_media = load_module(
    "transcribe_media_skill", "skills/transcribe-media/scripts/transcribe_media.py"
)
knowledge_writer = load_module(
    "knowledge_writer_skill", "skills/ingest-knowledge/scripts/knowledge_writer.py"
)
vault_builder = load_module(
    "vault_builder_skill", "skills/build-obsidian-vault/scripts/vault_builder.py"
)


class DownloadVideoTests(unittest.TestCase):
    def test_detects_all_supported_platforms_without_substring_spoofing(self):
        cases = {
            "https://v.douyin.com/abc/": "douyin",
            "https://www.tiktok.com/@u/video/1": "tiktok",
            "https://www.bilibili.com/video/BV1x": "bilibili",
            "https://youtu.be/abc": "youtube",
            "https://x.com/user/status/1": "twitter",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                _, platform = download_video.normalize_and_detect(url)
                self.assertEqual(platform, expected)
        with self.assertRaises(download_video.DownloadError) as raised:
            download_video.normalize_and_detect("https://youtube.com.evil.example/watch?v=x")
        self.assertEqual(raised.exception.code, "UNSUPPORTED_URL")

    def test_classifies_antibot_failures(self):
        cases = {
            "Fresh cookies (not necessarily logged in) are needed": "FRESH_COOKIES_REQUIRED",
            "HTTP Error 412: Precondition Failed": "SESSION_REQUIRED",
            "Your IP address is blocked from accessing this post": "IP_BLOCKED",
            "Failed to resolve host": "NETWORK_ERROR",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(download_video.classify_ytdlp_error(message).code, expected)

    def test_redacts_credentials_and_signed_query_strings(self):
        redacted = download_video.redact_sensitive(
            "Cookie: secret\n"
            "Authorization: Bearer secret\n"
            "https://cdn.example/video.mp4?token=secret&expires=1"
        )
        self.assertNotIn("secret", redacted)
        self.assertIn("https://cdn.example/video.mp4?<redacted>", redacted)

    def test_persisted_source_urls_drop_sensitive_query_parameters(self):
        youtube = download_video.sanitize_source_url(
            "https://www.youtube.com/watch?v=abc&token=secret&expires=1&utm_source=test",
            "youtube",
        )
        self.assertEqual(youtube, "https://www.youtube.com/watch?v=abc")
        douyin = download_video.sanitize_source_url(
            "https://www.douyin.com/?modal_id=123&signature=secret",
            "douyin",
        )
        self.assertEqual(douyin, "https://www.douyin.com/?modal_id=123")
        metadata = download_video.safe_metadata(
            {
                "id": "abc",
                "webpage_url": "https://youtu.be/abc?si=secret&token=secret",
            },
            "youtube",
            "https://www.youtube.com/watch?v=abc&token=secret",
        )
        self.assertEqual(metadata["webpage_url"], "https://youtu.be/abc")
        self.assertNotIn("secret", json.dumps(metadata))

    def test_rejects_home_and_symlinked_platform_output(self):
        with self.assertRaises(download_video.DownloadError) as raised:
            download_video.safe_output_root(str(Path.home()))
        self.assertEqual(raised.exception.code, "UNSAFE_OUTPUT_DIRECTORY")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            outside = root / "outside"
            output.mkdir()
            outside.mkdir()
            os.symlink(outside, output / "tiktok")
            with self.assertRaises(download_video.DownloadError) as symlinked:
                download_video.safe_platform_directory(output, "tiktok")
            self.assertEqual(symlinked.exception.code, "UNSAFE_OUTPUT_DIRECTORY")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_ffprobe_accepts_a_valid_small_video_instead_of_using_a_size_threshold(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "tiny.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(video),
                ],
                check=True,
            )
            self.assertLess(video.stat().st_size, 100_000)
            probed = download_video.ffprobe(video)
            self.assertTrue(probed["has_video"])
            self.assertTrue(probed["has_audio"])
            self.assertGreater(probed["duration_seconds"], 0)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_gallery_fallback_reuses_a_verified_existing_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            url = "https://www.tiktok.com/@u/video/1"
            source_id = hashlib.sha256(url.encode()).hexdigest()[:16]
            platform_dir = root / "tiktok"
            platform_dir.mkdir()
            video = platform_dir / f"{source_id}--gallery-dl.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:d=1",
                    "-c:v",
                    "libx264",
                    str(video),
                ],
                check=True,
            )
            args = argparse.Namespace(timeout=30)
            original = download_video.DownloadError("IP_BLOCKED", "blocked")
            real_require = download_video.require_tool

            def fake_require(name):
                return "/usr/bin/true" if name == "gallery-dl" else real_require(name)

            with mock.patch.object(download_video, "require_tool", side_effect=fake_require):
                with mock.patch.object(download_video, "version_of", return_value="1.32.8"):
                    result = download_video.gallery_download(
                        args,
                        url=url,
                        platform_name="tiktok",
                        output_root=root,
                        original_error=original,
                    )
            self.assertEqual(Path(result["media_path"]), video)
            self.assertIn("reused", result["manifest"]["acquisition"]["warnings"][0])


class TranscribeMediaTests(unittest.TestCase):
    def write_wav(self, path: Path, *, frames: int, sample_rate: int = 16000):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(b"\x00\x00" * frames)

    def fake_whisper_cpp(self, root: Path) -> Path:
        executable = root / "fake-whisper-cli"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "args = sys.argv[1:]\n"
            "if '--version' in args:\n"
            "    print('whisper.cpp version: test-1.9.1')\n"
            "    raise SystemExit(0)\n"
            "log = Path(sys.argv[0]).with_suffix('.log')\n"
            "with log.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(args) + '\\n')\n"
            "if '-ng' not in args:\n"
            "    print('simulated GPU allocation failure', file=sys.stderr)\n"
            "    raise SystemExit(9)\n"
            "audio = Path(args[args.index('-f') + 1])\n"
            "prefix = args[args.index('-of') + 1]\n"
            "payload = {\n"
            "    'result': {'language': 'zh'},\n"
            "    'params': {'language': args[args.index('-l') + 1]},\n"
            "    'transcription': [{\n"
            "        'offsets': {'from': 100, 'to': 900},\n"
            "        'text': ' ' + audio.stem,\n"
            "        'tokens': [{'p': 0.8}, {'p': 1.0}],\n"
            "    }],\n"
            "}\n"
            "Path(prefix + '.json').write_text(\n"
            "    json.dumps(payload), encoding='utf-8'\n"
            ")\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def test_parses_srt_and_vtt_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            srt = Path(temporary) / "sample.srt"
            srt.write_text(
                "1\n00:00:00,100 --> 00:00:01,200\n你好 <b>世界</b>\n\n",
                encoding="utf-8",
            )
            vtt = Path(temporary) / "sample.vtt"
            vtt.write_text(
                "WEBVTT\n\n00:00.500 --> 00:02.000\nSecond line\n\n",
                encoding="utf-8",
            )
            self.assertEqual(
                transcribe_media.parse_sidecar(srt)[0],
                {
                    "start_ms": 100,
                    "end_ms": 1200,
                    "text": "你好 世界",
                    "chunk_index": 0,
                },
            )
            self.assertEqual(transcribe_media.parse_sidecar(vtt)[0]["start_ms"], 500)

    def test_rejects_url_input_and_delegates_to_download_skill(self):
        with self.assertRaises(transcribe_media.TranscriptionError) as raised:
            transcribe_media.media_path("https://youtu.be/example")
        self.assertEqual(raised.exception.code, "USE_DOWNLOAD_VIDEO")

    def test_auto_never_selects_mlx_without_an_explicit_request(self):
        with mock.patch.object(
            transcribe_media,
            "module_available",
            side_effect=lambda name: name == "mlx_whisper",
        ):
            with self.assertRaises(transcribe_media.TranscriptionError) as raised:
                transcribe_media.select_engine("auto")
        self.assertEqual(raised.exception.code, "ENGINE_UNAVAILABLE")

        with mock.patch.object(
            transcribe_media,
            "module_available",
            side_effect=lambda name: name == "faster_whisper",
        ):
            self.assertEqual(
                transcribe_media.select_engine("auto"),
                "faster-whisper",
            )

    def test_python_engine_identity_records_runtime_package_versions(self):
        versions = {
            "faster-whisper": "1.2.3",
            "ctranslate2": "4.5.6",
            "mlx-whisper": "0.4.2",
            "mlx": "0.30.0",
        }
        with mock.patch.object(
            transcribe_media,
            "package_version",
            side_effect=versions.get,
        ):
            faster = transcribe_media.engine_identity_for(
                "faster-whisper",
                "small",
                None,
                None,
                timeout=10,
            )
            mlx = transcribe_media.engine_identity_for(
                "mlx-whisper",
                "mlx-community/whisper-small",
                None,
                None,
                timeout=10,
            )
        self.assertEqual(
            faster["package_versions"],
            {"faster-whisper": "1.2.3", "ctranslate2": "4.5.6"},
        )
        self.assertEqual(
            mlx["package_versions"],
            {"mlx-whisper": "0.4.2", "mlx": "0.30.0"},
        )

    def test_whisper_cpp_retries_cpu_and_uses_actual_cumulative_chunk_offsets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "source.media"
            media.write_bytes(b"local media source")
            model = root / "ggml-small.bin"
            model.write_bytes(b"pinned local model")
            executable = self.fake_whisper_cpp(root)
            prepared = root / "prepared"
            prepared.mkdir()
            first_chunk = prepared / "chunk-00000.wav"
            second_chunk = prepared / "chunk-00001.wav"
            self.write_wav(first_chunk, frames=20_000)
            self.write_wav(second_chunk, frames=40_000)
            args = argparse.Namespace(
                media=str(media),
                output_dir=str(root / "output"),
                ignore_sidecar=True,
                engine="auto",
                model=str(model),
                language=None,
                adapter=None,
                whisper_cpp_bin=str(executable),
                whisper_cpp_cpu_only=False,
                chunk_seconds=600,
                timeout=10,
            )
            media_metadata = {
                "path": str(media),
                "bytes": media.stat().st_size,
                "duration_seconds": 3.75,
                "container": "test",
                "has_audio": True,
                "has_video": False,
                "streams": [{"codec_type": "audio"}],
            }
            with (
                mock.patch.object(
                    transcribe_media,
                    "inspect_media",
                    return_value=media_metadata,
                ),
                mock.patch.object(
                    transcribe_media,
                    "normalize_chunks",
                    return_value=[first_chunk, second_chunk],
                ),
                mock.patch.object(
                    transcribe_media,
                    "chunk_has_signal",
                    return_value=True,
                ),
            ):
                result = transcribe_media.transcribe(args)

            artifact = json.loads(
                Path(result["transcript_path"]).read_text(encoding="utf-8")
            )
            transcription = artifact["transcription"]
            timeline = transcription["chunk_timeline"]
            self.assertEqual(transcription["engine"], "whisper-cpp")
            self.assertEqual([item["offset_ms"] for item in timeline], [0, 1250])
            self.assertEqual([item["duration_ms"] for item in timeline], [1250, 2500])
            self.assertEqual(
                [item["start_ms"] for item in artifact["segments"]],
                [100, 1350],
            )
            self.assertEqual(transcription["devices_used"], ["cpu"])
            self.assertEqual(transcription["gpu_fallback_count"], 1)
            identity = transcription["engine_identity"]
            self.assertEqual(identity["binary_path"], str(executable.resolve()))
            self.assertEqual(
                identity["binary_sha256"],
                hashlib.sha256(executable.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                identity["model_sha256"],
                hashlib.sha256(model.read_bytes()).hexdigest(),
            )
            state = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))
            self.assertEqual(
                state["chunks"]["chunk-00001.wav"]["offset_ms"],
                1250,
            )
            self.assertTrue(
                state["chunks"]["chunk-00000.wav"]["runtime"]["gpu_fallback"]
            )
            invocations = [
                json.loads(line)
                for line in executable.with_suffix(".log")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(invocations), 3)
            self.assertEqual(
                ["-ng" in invocation for invocation in invocations],
                [False, True, True],
            )

            state["chunks"].pop("chunk-00001.wav")
            Path(result["state_path"]).write_text(
                json.dumps(state),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    transcribe_media,
                    "inspect_media",
                    return_value=media_metadata,
                ),
                mock.patch.object(
                    transcribe_media,
                    "normalize_chunks",
                    return_value=[first_chunk, second_chunk],
                ),
                mock.patch.object(
                    transcribe_media,
                    "chunk_has_signal",
                    return_value=True,
                ),
            ):
                resumed = transcribe_media.transcribe(args)
            resumed_state = json.loads(
                Path(resumed["state_path"]).read_text(encoding="utf-8")
            )
            resumed_runtime = resumed_state["chunks"]["chunk-00001.wav"]["runtime"]
            self.assertFalse(resumed_runtime["gpu_attempted"])
            self.assertTrue(resumed_runtime["gpu_disabled_after_failure"])
            resumed_invocations = [
                json.loads(line)
                for line in executable.with_suffix(".log")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(resumed_invocations), 4)
            self.assertIn("-ng", resumed_invocations[-1])
            resumed_artifact = json.loads(
                Path(resumed["transcript_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                resumed_artifact["transcription"]["gpu_fallback_count"],
                1,
            )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_sidecar_transcription_emits_all_artifacts_and_propagates_download_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "sample.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(audio),
                ],
                check=True,
            )
            audio.with_suffix(".srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,500\n这是测试内容\n\n",
                encoding="utf-8",
            )
            source_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
            Path(f"{audio}.artifact.json").write_text(
                json.dumps(
                    {
                        "schema_version": transcribe_media.SCHEMA_VERSION,
                        "artifact_type": "video",
                        "status": "complete",
                        "source": {
                            "url": "https://www.youtube.com/watch?v=test",
                            "platform": "youtube",
                        },
                        "media": {"path": str(audio), "sha256": source_hash},
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                media=str(audio),
                output_dir=str(root / "output"),
                ignore_sidecar=False,
                engine="auto",
                model=None,
                language=None,
                adapter=None,
                chunk_seconds=600,
                timeout=60,
            )
            result = transcribe_media.transcribe(args)
            artifact = json.loads(Path(result["transcript_path"]).read_text(encoding="utf-8"))
            self.assertEqual(result["segment_count"], 1)
            self.assertEqual(artifact["transcription"]["engine"], "sidecar-subtitle")
            self.assertEqual(
                artifact["source"]["upstream"]["url"],
                "https://www.youtube.com/watch?v=test",
            )
            for key in ("markdown_path", "text_path", "srt_path", "vtt_path", "state_path"):
                self.assertTrue(Path(artifact[key]).is_file(), key)


class VaultAndIngestIntegrationTests(unittest.TestCase):
    def config(self):
        return vault_builder.read_config(
            ROOT
            / "skills"
            / "build-obsidian-vault"
            / "assets"
            / "vault-config.example.json"
        )

    def transcript(self, path: Path) -> Path:
        value = {
            "schema_version": knowledge_writer.SCHEMA_VERSION,
            "artifact_type": "transcript",
            "status": "complete",
            "source": {
                "path": "/tmp/source.mp4",
                "sha256": "a" * 64,
                "bytes": 1234,
                "duration_seconds": 12.0,
                "upstream": {
                    "url": "https://www.youtube.com/watch?v=source",
                    "platform": "youtube",
                },
            },
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 3200,
                    "text": "可核查的原始观点",
                    "chunk_index": 0,
                }
            ],
            "text": "可核查的原始观点",
        }
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def draft(self, path: Path) -> Path:
        path.write_text(
            "# 测试知识\n\n"
            "## 核心结论\n\n"
            "- 原材料提出一个可核查观点。证据：[00:00:00.000–00:00:03.200]\n\n"
            "## 待验证\n\n"
            "- 该观点的外部有效性仍需验证。\n",
            encoding="utf-8",
        )
        return path

    def test_build_is_idempotent_and_auditable(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "Vault"
            first = vault_builder.build(
                self.config(), vault, apply=True, extend_existing=False
            )
            self.assertEqual(first["result"], "created")
            second = vault_builder.build(
                self.config(), vault, apply=True, extend_existing=False
            )
            self.assertEqual(second["result"], "unchanged")
            self.assertTrue(vault_builder.audit(vault)["healthy"])
            self.assertEqual(list((vault / ".obsidian").glob("*.json")), [])
            (vault / "Home.md").write_text("changed", encoding="utf-8")
            with self.assertRaises(vault_builder.VaultError) as raised:
                vault_builder.build(
                    self.config(), vault, apply=True, extend_existing=False
                )
            self.assertEqual(raised.exception.code, "BUILD_CONFLICT")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_build_rejects_symlinked_managed_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "Vault"
            outside = root / "outside"
            vault.mkdir()
            outside.mkdir()
            os.symlink(outside, vault / "00 Inbox")
            with self.assertRaises(vault_builder.VaultError) as raised:
                vault_builder.build(
                    self.config(), vault, apply=True, extend_existing=True
                )
            self.assertEqual(raised.exception.code, "BUILD_CONFLICT")

    def test_build_rejects_home_as_vault_target(self):
        with self.assertRaises(vault_builder.VaultError) as raised:
            vault_builder.build_plan(self.config(), Path.home())
        self.assertEqual(raised.exception.code, "UNSAFE_VAULT_TARGET")

    def test_ingest_dry_run_commit_and_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "Vault"
            vault_builder.build(self.config(), vault, apply=True, extend_existing=False)
            transcript = self.transcript(root / "transcript.json")
            draft = self.draft(root / "draft.md")
            base = dict(
                transcript=str(transcript),
                document=str(draft),
                vault=str(vault),
                title="测试：知识",
                collection="00 Inbox",
                sources_dir="90 Sources",
                tag=["测试"],
                allow_plain_folder=False,
            )
            dry_run = knowledge_writer.commit(
                argparse.Namespace(**base, dry_run=True)
            )
            self.assertEqual(dry_run["result"], "dry-run")
            self.assertFalse(Path(dry_run["knowledge_note"]).exists())
            created = knowledge_writer.commit(
                argparse.Namespace(**base, dry_run=False)
            )
            self.assertEqual(created["result"], "created")
            note_path = Path(created["knowledge_note"])
            source_path = Path(created["source_note"])
            self.assertTrue(note_path.is_file())
            self.assertTrue(source_path.is_file())
            note = note_path.read_text(encoding="utf-8")
            self.assertIn("source_url: \"https://www.youtube.com/watch?v=source\"", note)
            self.assertIn("[[90 Sources/", note)
            self.assertIn("\n# 测试：知识\n", note)
            self.assertNotIn("\n# 测试知识\n", note)
            reused = knowledge_writer.commit(
                argparse.Namespace(**base, dry_run=False)
            )
            self.assertEqual(reused["result"], "reused")
            self.assertEqual(reused["knowledge_note"], created["knowledge_note"])
            self.assertEqual(reused["title"], "测试：知识")
            self.assertEqual(reused["link_style"], "wikilink")

    def test_ingest_uses_portable_markdown_links_without_a_build_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "ExistingVault"
            (vault / ".obsidian").mkdir(parents=True)
            transcript = self.transcript(root / "transcript.json")
            draft = self.draft(root / "draft.md")
            result = knowledge_writer.commit(
                argparse.Namespace(
                    transcript=str(transcript),
                    document=str(draft),
                    vault=str(vault),
                    title="Portable link",
                    collection="00 Inbox",
                    sources_dir="90 Sources",
                    tag=[],
                    allow_plain_folder=False,
                    dry_run=False,
                    link_style="auto",
                )
            )
            note = Path(result["knowledge_note"]).read_text(encoding="utf-8")
            self.assertEqual(result["link_style"], "markdown")
            self.assertIn(
                "[原始转写](../90%20Sources/Portable%20link--",
                note,
            )

    def test_ingest_rejects_inconsistent_transcript_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript_path = self.transcript(Path(temporary) / "transcript.json")
            base = json.loads(transcript_path.read_text(encoding="utf-8"))
            cases = []

            mismatched_text = json.loads(json.dumps(base))
            mismatched_text["text"] = "与分段不一致"
            cases.append(mismatched_text)

            missing_whisper_identity = json.loads(json.dumps(base))
            missing_whisper_identity["transcription"] = {
                "engine": "whisper-cpp",
                "engine_identity": {},
            }
            cases.append(missing_whisper_identity)

            invalid_timeline = json.loads(json.dumps(base))
            invalid_timeline["transcription"] = {
                "engine": "faster-whisper",
                "chunk_timeline": [
                    {
                        "offset_ms": 10,
                        "duration_ms": 100,
                        "sha256": "b" * 64,
                    }
                ],
            }
            cases.append(invalid_timeline)

            for value in cases:
                with self.subTest(transcription=value.get("transcription")):
                    with self.assertRaises(knowledge_writer.IngestError) as raised:
                        knowledge_writer.validate_transcript(value)
                    self.assertEqual(raised.exception.code, "INVALID_TRANSCRIPT")


if __name__ == "__main__":
    unittest.main()
