from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD = (
    ROOT / "skills" / "download-video" / "scripts" / "download_video.py"
)
TRANSCRIBE = (
    ROOT
    / "skills"
    / "transcribe-media"
    / "scripts"
    / "transcribe_media.py"
)
VAULT_BUILDER = (
    ROOT
    / "skills"
    / "build-obsidian-vault"
    / "scripts"
    / "vault_builder.py"
)
KNOWLEDGE_WRITER = (
    ROOT
    / "skills"
    / "ingest-knowledge"
    / "scripts"
    / "knowledge_writer.py"
)
VAULT_CONFIG = (
    ROOT
    / "skills"
    / "build-obsidian-vault"
    / "assets"
    / "vault-config.example.json"
)


class ContractPipelineE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        missing = [
            executable
            for executable in ("ffmpeg", "ffprobe")
            if shutil.which(executable) is None
        ]
        if missing:
            raise AssertionError(
                "The contract E2E suite requires: " + ", ".join(missing)
            )

    def run_json(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int = 120,
    ) -> dict[str, object]:
        process = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        self.assertEqual(
            process.returncode,
            0,
            msg=(
                f"command failed ({process.returncode}): {command}\n"
                f"stdout={process.stdout!r}\nstderr={process.stderr!r}"
            ),
        )
        self.assertEqual(process.stderr, "", msg=command)
        self.assertTrue(process.stdout.strip(), msg=command)
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            self.fail(
                f"stdout was not exactly one JSON value: {command}\n"
                f"stdout={process.stdout!r}\n{exc}"
            )
        self.assertIsInstance(value, dict, msg=command)
        self.assertEqual(value.get("status"), "ok", msg=value)
        return value

    def make_media(self, path: Path) -> None:
        process = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=1.2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1.2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            process.returncode,
            0,
            msg=f"ffmpeg failed: {process.stderr}",
        )
        self.assertTrue(path.is_file())

    def write_fake_ytdlp(self, path: Path) -> None:
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "import shutil\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "if '--version' in sys.argv[1:]:\n"
            "    print('2026.07.04')\n"
            "    raise SystemExit(0)\n"
            "\n"
            "args = sys.argv[1:]\n"
            "template = args[args.index('--output') + 1]\n"
            "media = Path(\n"
            "    template.replace('%(id)s', 'fixture')\n"
            "    .replace('%(title).120B', 'contract-e2e')\n"
            "    .replace('%(ext)s', 'mp4')\n"
            ")\n"
            "media.parent.mkdir(parents=True, exist_ok=True)\n"
            "shutil.copyfile(Path(os.environ['AWESOME_CAPTURE_E2E_MEDIA']), media)\n"
            "source_url = args[-1]\n"
            "info = {\n"
            "    'id': 'fixture',\n"
            "    'title': 'contract-e2e',\n"
            "    'uploader': 'fixture',\n"
            "    'duration': 1.2,\n"
            "    'webpage_url': source_url,\n"
            "    'extractor_key': 'Youtube',\n"
            "}\n"
            "media.with_suffix('.info.json').write_text(\n"
            "    json.dumps(info), encoding='utf-8'\n"
            ")\n"
            "print(json.dumps(str(media.resolve())))\n",
            encoding="utf-8",
        )
        path.chmod(0o700)

    def write_external_adapter(self, path: Path) -> None:
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "args = sys.argv[1:]\n"
            "protocol = args[args.index('--protocol') + 1]\n"
            "model = Path(args[args.index('--model') + 1])\n"
            "media = Path(args[args.index('--input') + 1])\n"
            "if (\n"
            "    protocol != 'awesome-capture.external-asr/v1'\n"
            "    or not model.is_file()\n"
            "    or not media.is_file()\n"
            "):\n"
            "    raise SystemExit(9)\n"
            "print(json.dumps({\n"
            "    'protocol': protocol,\n"
            "    'language': 'zh',\n"
            "    'segments': [\n"
            "        {'start': 0.1, 'end': 0.5, 'text': '离线契约链路'}\n"
            "    ],\n"
            "}))\n",
            encoding="utf-8",
        )
        path.chmod(0o700)

    def test_video_to_transcript_to_vault_is_strict_and_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            arbitrary_cwd = root / "arbitrary-cwd"
            fake_bin = root / "fake-bin"
            arbitrary_cwd.mkdir()
            fake_bin.mkdir()

            fixture_media = root / "fixture-source.mp4"
            self.make_media(fixture_media)
            self.write_fake_ytdlp(fake_bin / "yt-dlp")
            adapter = root / "external-asr"
            self.write_external_adapter(adapter)
            model = root / "local-model.bin"
            model.write_bytes(b"offline fixture model")
            model.chmod(0o600)

            config = root / "vault-config.json"
            shutil.copyfile(VAULT_CONFIG, config)
            draft = root / "draft.md"
            draft.write_text(
                "# 离线契约链路\n\n"
                "## 核心结论\n\n"
                "- 四个 skill 通过显式契约交接。"
                "证据：[00:00:00.100–00:00:00.500]\n\n"
                "## 待验证\n\n"
                "- 真实平台获取质量仍需独立 smoke 验证。\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), env.get("PATH", "")]
            )
            env["AWESOME_CAPTURE_E2E_MEDIA"] = str(fixture_media)
            env["PYTHONDONTWRITEBYTECODE"] = "1"

            download_output = root / "download-output"
            download_command = [
                sys.executable,
                str(DOWNLOAD),
                "download",
                "https://www.youtube.com/watch?v=contract-e2e",
                "--output-dir",
                str(download_output),
                "--douyin-browser-fallback",
                "off",
                "--gallery-fallback",
                "off",
                "--timeout",
                "60",
                "--lock-timeout",
                "5",
            ]
            downloaded = self.run_json(
                download_command,
                cwd=arbitrary_cwd,
                env=env,
            )
            self.assertEqual(downloaded["result"], "created")
            video_artifact_path = Path(str(downloaded["artifact_path"]))
            video_media_path = Path(str(downloaded["media_path"]))
            video_artifact = json.loads(
                video_artifact_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                video_artifact["schema_version"],
                "awesome-capture.artifact/v2",
            )
            self.assertEqual(video_artifact["artifact_type"], "video")
            self.assertTrue(video_artifact["media"]["has_video"])
            self.assertTrue(video_artifact["media"]["has_audio"])
            self.assertEqual(
                self.run_json(
                    download_command,
                    cwd=arbitrary_cwd,
                    env=env,
                )["result"],
                "reused",
            )

            transcription_output = root / "transcription-output"
            transcribe_command = [
                sys.executable,
                str(TRANSCRIBE),
                "transcribe",
                str(video_media_path),
                "--source-artifact",
                str(video_artifact_path),
                "--output-dir",
                str(transcription_output),
                "--engine",
                "external",
                "--model",
                str(model),
                "--adapter",
                str(adapter),
                "--trust-external-adapter",
                "--language",
                "zh",
                "--chunk-seconds",
                "30",
                "--timeout",
                "60",
                "--lock-timeout",
                "5",
            ]
            transcribed = self.run_json(
                transcribe_command,
                cwd=arbitrary_cwd,
                env=env,
            )
            self.assertEqual(transcribed["result"], "created")
            transcript_path = Path(str(transcribed["transcript_path"]))
            transcript = json.loads(
                transcript_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                transcript["schema_version"],
                "awesome-capture.artifact/v2",
            )
            self.assertEqual(transcript["artifact_type"], "transcript")
            self.assertEqual(
                transcript["source"]["upstream"]["artifact_path"],
                str(video_artifact_path),
            )
            self.assertEqual(
                transcript["transcription"]["engine"],
                "external",
            )
            self.assertEqual(
                transcript["transcription"]["chunk_set"]["count"],
                1,
            )
            self.assertEqual(transcript["text"], "离线契约链路")
            state = json.loads(
                Path(str(transcribed["state_path"])).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                state["schema_version"],
                "awesome-capture.transcription-state/v1",
            )
            chunk_manifest = json.loads(
                Path(
                    transcript["transcription"]["chunk_set"][
                        "manifest_path"
                    ]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                chunk_manifest["schema_version"],
                "awesome-capture.chunk-set/v1",
            )
            self.assertEqual(
                self.run_json(
                    transcribe_command,
                    cwd=arbitrary_cwd,
                    env=env,
                )["result"],
                "reused",
            )

            vault = root / "Vault"
            build_plan = self.run_json(
                [
                    sys.executable,
                    str(VAULT_BUILDER),
                    "plan",
                    str(config),
                    "--vault",
                    str(vault),
                ],
                cwd=arbitrary_cwd,
                env=env,
            )
            built = self.run_json(
                [
                    sys.executable,
                    str(VAULT_BUILDER),
                    "build",
                    str(config),
                    "--vault",
                    str(vault),
                    "--apply",
                    "--expected-plan-sha256",
                    str(build_plan["plan_sha256"]),
                    "--lock-timeout",
                    "5",
                ],
                cwd=arbitrary_cwd,
                env=env,
            )
            self.assertEqual(built["result"], "created")
            build_receipt = json.loads(
                Path(str(built["receipt_path"])).read_text(encoding="utf-8")
            )
            self.assertEqual(
                build_receipt["schema_version"],
                "awesome-capture.vault-build-receipt/v1",
            )

            video_media_path.unlink()
            video_artifact_path.unlink()
            self.assertFalse(video_media_path.exists())
            self.assertFalse(video_artifact_path.exists())
            self.assertTrue(
                Path(transcript["source"]["snapshot_path"]).is_file()
            )
            validated = self.run_json(
                [
                    sys.executable,
                    str(KNOWLEDGE_WRITER),
                    "validate-transcript",
                    str(transcript_path),
                ],
                cwd=arbitrary_cwd,
                env=env,
            )
            self.assertEqual(validated["segment_count"], 1)

            ingest_base = [
                sys.executable,
                str(KNOWLEDGE_WRITER),
                "commit",
                "--transcript",
                str(transcript_path),
                "--document",
                str(draft),
                "--vault",
                str(vault),
                "--title",
                "离线契约链路",
                "--collection",
                "00 Inbox",
                "--sources-dir",
                "90 Sources",
                "--tag",
                "e2e",
                "--lock-timeout",
                "5",
            ]
            ingest_plan = self.run_json(
                [*ingest_base, "--dry-run"],
                cwd=arbitrary_cwd,
                env=env,
            )
            self.assertEqual(ingest_plan["result"], "dry-run")
            ingested = self.run_json(
                [
                    *ingest_base,
                    "--expected-plan-sha256",
                    str(ingest_plan["plan_sha256"]),
                ],
                cwd=arbitrary_cwd,
                env=env,
            )
            self.assertEqual(ingested["result"], "created")
            ingest_receipt = json.loads(
                Path(str(ingested["receipt_path"])).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                ingest_receipt["schema_version"],
                "awesome-capture.ingest-receipt/v1",
            )
            self.assertEqual(
                ingest_receipt["source_media_verification"],
                "not_checked",
            )

            builder_audit_command = [
                sys.executable,
                str(VAULT_BUILDER),
                "audit",
                "--vault",
                str(vault),
                "--require-build-receipt",
                "--lock-timeout",
                "5",
            ]
            ingest_audit_command = [
                sys.executable,
                str(KNOWLEDGE_WRITER),
                "audit",
                "--vault",
                str(vault),
                "--lock-timeout",
                "5",
            ]
            self.assertTrue(
                self.run_json(
                    builder_audit_command,
                    cwd=arbitrary_cwd,
                    env=env,
                )["healthy"]
            )
            self.assertTrue(
                self.run_json(
                    ingest_audit_command,
                    cwd=arbitrary_cwd,
                    env=env,
                )["healthy"]
            )

            repeated_build_plan = self.run_json(
                [
                    sys.executable,
                    str(VAULT_BUILDER),
                    "plan",
                    str(config),
                    "--vault",
                    str(vault),
                ],
                cwd=arbitrary_cwd,
                env=env,
            )
            repeated_build = self.run_json(
                [
                    sys.executable,
                    str(VAULT_BUILDER),
                    "build",
                    str(config),
                    "--vault",
                    str(vault),
                    "--apply",
                    "--expected-plan-sha256",
                    str(repeated_build_plan["plan_sha256"]),
                    "--lock-timeout",
                    "5",
                ],
                cwd=arbitrary_cwd,
                env=env,
            )
            self.assertEqual(repeated_build["result"], "unchanged")

            repeated_ingest_plan = self.run_json(
                [*ingest_base, "--dry-run"],
                cwd=arbitrary_cwd,
                env=env,
            )
            self.assertEqual(
                repeated_ingest_plan["plan_sha256"],
                ingest_plan["plan_sha256"],
            )
            repeated_ingest = self.run_json(
                [
                    *ingest_base,
                    "--expected-plan-sha256",
                    str(repeated_ingest_plan["plan_sha256"]),
                ],
                cwd=arbitrary_cwd,
                env=env,
            )
            self.assertEqual(repeated_ingest["result"], "reused")
            self.assertEqual(
                repeated_ingest["receipt_path"],
                ingested["receipt_path"],
            )
            self.assertTrue(
                self.run_json(
                    builder_audit_command,
                    cwd=arbitrary_cwd,
                    env=env,
                )["healthy"]
            )
            self.assertTrue(
                self.run_json(
                    ingest_audit_command,
                    cwd=arbitrary_cwd,
                    env=env,
                )["healthy"]
            )


if __name__ == "__main__":
    unittest.main()
