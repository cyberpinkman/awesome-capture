from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD = ROOT / "skills/download-video/scripts/download_video.py"
TRANSCRIBE = ROOT / "skills/transcribe-media/scripts/transcribe_media.py"
INGEST = ROOT / "skills/ingest-knowledge/scripts/knowledge_writer.py"
BUILDER = ROOT / "skills/build-obsidian-vault/scripts/vault_builder.py"


class FailureRedactionMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        missing = [
            executable
            for executable in ("ffmpeg", "ffprobe")
            if shutil.which(executable) is None
        ]
        if missing:
            raise AssertionError(
                "Failure redaction tests require the CI-preflighted tools: "
                + ", ".join(missing)
            )

    def run_cli(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env or {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def assert_failure_protocol(
        self,
        process: subprocess.CompletedProcess[str],
    ) -> dict[str, object]:
        self.assertNotEqual(process.returncode, 0, process)
        self.assertEqual(process.stdout, "")
        self.assertTrue(process.stderr.strip())
        try:
            payload = json.loads(process.stderr)
        except json.JSONDecodeError as exc:
            self.fail(
                "stderr was not exactly one JSON value: "
                f"{process.stderr!r}: {exc}"
            )
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("status"), "error")
        self.assertIsInstance(payload.get("error"), dict)
        return payload

    def write_tone(self, path: Path) -> None:
        frames = bytearray()
        for index in range(16_000):
            sample = round(12_000 * math.sin(2 * math.pi * 440 * index / 16_000))
            frames.extend(struct.pack("<h", sample))
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(frames)
        path.chmod(0o600)

    def test_expected_failure_json_protocol_and_download_redaction_matrix(self) -> None:
        secrets = {
            "URL_QUERY_SECRET",
            "COOKIE_VALUE_SECRET",
            "BEARER_TOKEN_SECRET",
            "RAW_DOWNLOADER_STDERR_SECRET",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tools = root / "bin"
            tools.mkdir()
            fake_ytdlp = tools / "yt-dlp"
            fake_ytdlp.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "print('RAW_DOWNLOADER_STDERR_SECRET', file=sys.stderr)\n"
                "print('Cookie: COOKIE_VALUE_SECRET', file=sys.stderr)\n"
                "print('Authorization: Bearer BEARER_TOKEN_SECRET', file=sys.stderr)\n"
                "print('failed to resolve host: ' + ' '.join(sys.argv[1:]), file=sys.stderr)\n"
                "raise SystemExit(9)\n",
                encoding="utf-8",
            )
            fake_ytdlp.chmod(0o700)
            cookie_file = root / "authorized-cookies.txt"
            cookie_file.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t0\tsession\tCOOKIE_VALUE_SECRET\n",
                encoding="utf-8",
            )
            cookie_file.chmod(0o600)
            missing_transcript = root / "missing-INGEST_TOKEN_SECRET.json"
            missing_config = root / "missing-BUILDER_TOKEN_SECRET.json"
            environment = {
                **os.environ,
                "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            cases = {
                "download": (
                    [
                        sys.executable,
                        str(DOWNLOAD),
                        "probe",
                        (
                            "https://www.youtube.com/watch?v=fixture"
                            "&token=URL_QUERY_SECRET"
                        ),
                        "--cookies",
                        str(cookie_file),
                    ],
                    5,
                    "NETWORK_ERROR",
                ),
                "transcribe": (
                    [
                        sys.executable,
                        str(TRANSCRIBE),
                        "inspect",
                        "https://example.invalid/media?token=TRANSCRIBE_TOKEN_SECRET",
                    ],
                    2,
                    "USE_DOWNLOAD_VIDEO",
                ),
                "ingest": (
                    [
                        sys.executable,
                        str(INGEST),
                        "validate-transcript",
                        str(missing_transcript),
                    ],
                    2,
                    "INPUT_NOT_FOUND",
                ),
                "builder": (
                    [
                        sys.executable,
                        str(BUILDER),
                        "validate-config",
                        str(missing_config),
                    ],
                    2,
                    "CONFIG_NOT_FOUND",
                ),
            }

            for name, (command, returncode, error_code) in cases.items():
                with self.subTest(cli=name):
                    process = self.run_cli(command, env=environment)
                    payload = self.assert_failure_protocol(process)
                    self.assertEqual(process.returncode, returncode, process.stderr)
                    self.assertEqual(payload["error"]["code"], error_code)
                    combined = process.stdout + process.stderr
                    for secret in secrets:
                        self.assertNotIn(secret, combined)
                    if name == "transcribe":
                        self.assertNotIn("TRANSCRIBE_TOKEN_SECRET", combined)
                    elif name == "ingest":
                        self.assertNotIn("INGEST_TOKEN_SECRET", combined)
                    elif name == "builder":
                        self.assertNotIn("BUILDER_TOKEN_SECRET", combined)

    def test_whisper_cpp_gpu_and_cpu_failure_is_nonzero_and_never_commits(self) -> None:
        child_secrets = {
            "RAW_ASR_STDERR_SECRET",
            "ASR_COOKIE_SECRET",
            "ASR_BEARER_TOKEN_SECRET",
            "ASR_QUERY_SECRET",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            media = root / "tone.wav"
            self.write_tone(media)
            model = root / "model.bin"
            model.write_bytes(b"local model identity")
            model.chmod(0o600)
            attempt_log = root / "whisper-attempts.jsonl"
            fake_whisper = root / "whisper-cli"
            fake_whisper.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"log = Path({str(attempt_log)!r})\n"
                "args = sys.argv[1:]\n"
                "if '--version' in args:\n"
                "    print('whisper.cpp version test-1.9.1')\n"
                "    raise SystemExit(0)\n"
                "with log.open('a', encoding='utf-8') as output:\n"
                "    output.write(json.dumps(args) + '\\n')\n"
                "print('RAW_ASR_STDERR_SECRET', file=sys.stderr)\n"
                "print('Cookie: ASR_COOKIE_SECRET', file=sys.stderr)\n"
                "print('Authorization: Bearer ASR_BEARER_TOKEN_SECRET', file=sys.stderr)\n"
                "print('https://asr.invalid/result?token=ASR_QUERY_SECRET', file=sys.stderr)\n"
                "raise SystemExit(9)\n",
                encoding="utf-8",
            )
            fake_whisper.chmod(0o700)
            output_dir = root / "output"

            process = self.run_cli(
                [
                    sys.executable,
                    str(TRANSCRIBE),
                    "transcribe",
                    str(media),
                    "--output-dir",
                    str(output_dir),
                    "--engine",
                    "whisper-cpp",
                    "--model",
                    str(model),
                    "--whisper-cpp-bin",
                    str(fake_whisper),
                    "--chunk-seconds",
                    "30",
                    "--timeout",
                    "30",
                    "--ignore-sidecar",
                ],
                timeout=90,
            )

            payload = self.assert_failure_protocol(process)
            self.assertEqual(process.returncode, 5, process.stderr)
            self.assertEqual(payload["error"]["code"], "TRANSCRIPTION_FAILED")
            for secret in child_secrets:
                self.assertNotIn(secret, process.stderr)

            attempts = [
                json.loads(line)
                for line in attempt_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(attempts), 2)
            self.assertNotIn("-ng", attempts[0])
            self.assertIn("-ng", attempts[1])

            self.assertEqual(list(output_dir.rglob("transcript.json")), [])
            self.assertEqual(list(output_dir.rglob("transcript.pending.json")), [])
            for json_path in output_dir.rglob("*.json"):
                value = json.loads(json_path.read_text(encoding="utf-8"))
                if value.get("artifact_type") == "transcript":
                    self.assertNotEqual(value.get("status"), "complete", json_path)
                if (
                    value.get("schema_version")
                    == "awesome-capture.transcription-state/v1"
                ):
                    self.assertNotEqual(value.get("status"), "complete", json_path)


if __name__ == "__main__":
    unittest.main()
