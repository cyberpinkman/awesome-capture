from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD = ROOT / "skills/download-video/scripts/download_video.py"
TRANSCRIBE = ROOT / "skills/transcribe-media/scripts/transcribe_media.py"
BUILDER = ROOT / "skills/build-obsidian-vault/scripts/vault_builder.py"
INGEST = ROOT / "skills/ingest-knowledge/scripts/knowledge_writer.py"
CONFIG = (
    ROOT
    / "skills"
    / "build-obsidian-vault"
    / "assets"
    / "vault-config.example.json"
)
TRANSCRIPT = (
    ROOT
    / "contracts"
    / "fixtures"
    / "valid"
    / "transcript-artifact.json"
)

sys.path.insert(0, str(ROOT))
from contracts import posix_runtime  # noqa: E402


class FaultInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise AssertionError(
                "Fault injection requires the CI-preflighted ffmpeg tools."
            )

    def run_json(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        expected: int = 0,
    ) -> dict:
        process = subprocess.run(
            command,
            cwd=ROOT,
            env=env or {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(process.returncode, expected, process.stderr)
        if expected == 0:
            self.assertEqual(process.stderr, "")
            return json.loads(process.stdout)
        self.assertEqual(process.stdout, "")
        return json.loads(process.stderr)

    def killed_environment(self, failpoint: str) -> dict[str, str]:
        return {
            **os.environ,
            "AWESOME_CAPTURE_ENABLE_TEST_FAILPOINTS": "1",
            "AWESOME_CAPTURE_TEST_FAILPOINT": failpoint,
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def assert_sigkill(self, command: list[str], failpoint: str, env=None) -> None:
        environment = self.killed_environment(failpoint)
        if env:
            environment.update(env)
        process = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(process.returncode, -signal.SIGKILL, process.stderr)

    def write_wav(self, path: Path) -> None:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x01\x00" * 16000)

    def make_video(self, path: Path) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=0.25",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )

    def test_transcribe_sigkill_boundaries_recover_without_early_commit(self):
        failpoints = (
            "transcribe.after-pending",
            "transcribe.after-ready",
            "transcribe.after-complete-state",
            "transcribe.after-pending-refresh",
            "transcribe.after-artifact",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            media = root / "source.wav"
            self.write_wav(media)
            media.with_suffix(".srt").write_text(
                "1\n00:00:00,100 --> 00:00:00,900\nrecoverable\n\n",
                encoding="utf-8",
            )
            for index, failpoint in enumerate(failpoints):
                with self.subTest(failpoint=failpoint):
                    output = root / f"output-{index}"
                    command = [
                        sys.executable,
                        str(TRANSCRIBE),
                        "transcribe",
                        str(media),
                        "--output-dir",
                        str(output),
                        "--chunk-seconds",
                        "30",
                    ]
                    self.assert_sigkill(command, failpoint)
                    recovered = self.run_json(
                        [
                            sys.executable,
                            str(TRANSCRIBE),
                            "recover",
                            "--output-dir",
                            str(output),
                        ]
                    )
                    workspace_result = recovered["workspaces"][0]["status"]
                    self.assertIn(
                        workspace_result,
                        {"pending", "recovered", "complete"},
                    )
                    if workspace_result == "pending":
                        self.run_json(command)
                    artifacts = list(
                        output.rglob("transcript.json")
                    )
                    self.assertEqual(len(artifacts), 1)

    def builder_plan(self, vault: Path) -> str:
        return self.run_json(
            [
                sys.executable,
                str(BUILDER),
                "plan",
                str(CONFIG),
                "--vault",
                str(vault),
            ]
        )["plan_sha256"]

    def build_vault(self, vault: Path) -> None:
        expected = self.builder_plan(vault)
        self.run_json(
            [
                sys.executable,
                str(BUILDER),
                "build",
                str(CONFIG),
                "--vault",
                str(vault),
                "--apply",
                "--expected-plan-sha256",
                expected,
            ]
        )

    def test_vault_build_sigkill_boundaries_recover_and_audit(self):
        failpoints = (
            "vault-build.after-journal",
            "vault-build.after-publish-0",
            "vault-build.after-publish-1",
            "vault-build.after-publish-2",
            "vault-build.after-publish-3",
            "vault-build.after-publish-4",
            "vault-build.after-complete-journal",
            "vault-build.before-cleanup",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for index, failpoint in enumerate(failpoints):
                with self.subTest(failpoint=failpoint):
                    vault = root / f"Vault-{index}"
                    expected = self.builder_plan(vault)
                    command = [
                        sys.executable,
                        str(BUILDER),
                        "build",
                        str(CONFIG),
                        "--vault",
                        str(vault),
                        "--apply",
                        "--expected-plan-sha256",
                        expected,
                    ]
                    self.assert_sigkill(command, failpoint)
                    self.run_json(
                        [
                            sys.executable,
                            str(BUILDER),
                            "recover",
                            "--vault",
                            str(vault),
                        ]
                    )
                    audited = self.run_json(
                        [
                            sys.executable,
                            str(BUILDER),
                            "audit",
                            "--vault",
                            str(vault),
                            "--require-build-receipt",
                        ]
                    )
                    self.assertTrue(audited["healthy"], audited)

    def write_draft(self, path: Path) -> None:
        path.write_text(
            "# Fault recovery\n\n"
            "## Evidence\n\n"
            "- durable transaction [00:00:00.100–00:00:01.000]\n\n"
            "## Pending checks\n\n"
            "- none\n",
            encoding="utf-8",
        )

    def ingest_command(
        self,
        vault: Path,
        draft: Path,
        expected_plan: str | None = None,
        *,
        transcript: Path = TRANSCRIPT,
    ) -> list[str]:
        command = [
            sys.executable,
            str(INGEST),
            "commit",
            "--transcript",
            str(transcript),
            "--document",
            str(draft),
            "--vault",
            str(vault),
            "--title",
            "Fault recovery",
        ]
        if expected_plan is None:
            command.append("--dry-run")
        else:
            command.extend(
                [
                    "--expected-plan-sha256",
                    expected_plan,
                ]
            )
        return command

    def test_ingest_sigkill_boundaries_recover_and_audit(self):
        failpoints = (
            "ingest.after-journal",
            "ingest.after-publish-0",
            "ingest.after-publish-1",
            "ingest.after-publish-2",
            "ingest.after-complete-journal",
            "ingest.before-cleanup",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            draft = root / "draft.md"
            self.write_draft(draft)
            transcript = root / "transcript.json"
            shutil.copyfile(TRANSCRIPT, transcript)
            transcript.chmod(0o600)
            for index, failpoint in enumerate(failpoints):
                with self.subTest(failpoint=failpoint):
                    vault = root / f"Vault-{index}"
                    self.build_vault(vault)
                    plan = self.run_json(
                        self.ingest_command(vault, draft, transcript=transcript)
                    )["plan_sha256"]
                    self.assert_sigkill(
                        self.ingest_command(
                            vault,
                            draft,
                            plan,
                            transcript=transcript,
                        ),
                        failpoint,
                    )
                    self.run_json(
                        [
                            sys.executable,
                            str(INGEST),
                            "recover",
                            "--vault",
                            str(vault),
                        ]
                    )
                    audited = self.run_json(
                        [
                            sys.executable,
                            str(INGEST),
                            "audit",
                            "--vault",
                            str(vault),
                        ]
                    )
                    self.assertTrue(audited["healthy"], audited)

    def fake_downloader(self, root: Path, fixture: Path) -> Path:
        tools = root / "bin"
        tools.mkdir()
        fake = tools / "yt-dlp"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, shutil, sys\n"
            "from pathlib import Path\n"
            f"fixture = Path({str(fixture)!r})\n"
            "if '--version' in sys.argv:\n"
            " print('2026.07.04'); raise SystemExit(0)\n"
            "args=sys.argv[1:]\n"
            "template=Path(args[args.index('--output')+1])\n"
            "media=Path(str(template).replace('%(id)s','abc')"
            ".replace('%(title).120B','title').replace('%(ext)s','mp4'))\n"
            "media.parent.mkdir(parents=True,exist_ok=True)\n"
            "shutil.copyfile(fixture,media)\n"
            "media.with_suffix('.info.json').write_text(json.dumps({"
            "'id':'abc','title':'fixture','webpage_url':"
            "'https://www.youtube.com/watch?v=abc','extractor_key':'Youtube'}),"
            "encoding='utf-8')\n"
            "print(json.dumps(str(media)))\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        return tools

    def test_download_sigkill_publish_boundaries_recover(self):
        failpoints = (
            "download.after-staging-journal",
            "download.after-final-journal",
            "download.after-publish-0",
            "download.after-publish-1",
            "download.after-publish-2",
            "download.before-cleanup",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = root / "fixture-video.mp4"
            self.make_video(fixture)
            tools = self.fake_downloader(root, fixture)
            path_env = f"{tools}{os.pathsep}{os.environ['PATH']}"
            for index, failpoint in enumerate(failpoints):
                with self.subTest(failpoint=failpoint):
                    output = root / f"output-{index}"
                    command = [
                        sys.executable,
                        str(DOWNLOAD),
                        "download",
                        "https://www.youtube.com/watch?v=abc",
                        "--output-dir",
                        str(output),
                        "--gallery-fallback",
                        "off",
                    ]
                    self.assert_sigkill(
                        command,
                        failpoint,
                        env={"PATH": path_env},
                    )
                    self.run_json(
                        [
                            sys.executable,
                            str(DOWNLOAD),
                            "recover",
                            "--output-dir",
                            str(output),
                        ],
                        env={
                            **os.environ,
                            "PATH": path_env,
                            "PYTHONDONTWRITEBYTECODE": "1",
                        },
                    )
                    artifacts = list(output.rglob("artifact.json"))
                    self.assertEqual(len(artifacts), 1)

    def test_enospc_fsync_link_and_permission_fail_without_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            managed = posix_runtime.ensure_dir(
                root / "managed",
                0o700,
                private=True,
            )

            with mock.patch.object(
                posix_runtime.os,
                "write",
                side_effect=OSError(errno.ENOSPC, "no space"),
            ):
                with self.assertRaises(posix_runtime.PosixRuntimeError):
                    posix_runtime.atomic_write_noclobber(
                        managed / "enospc.bin",
                        b"value",
                    )
            self.assertFalse((managed / "enospc.bin").exists())

            actual_fsync = posix_runtime.os.fsync
            fsync_calls = 0

            def fail_data_fsync(descriptor: int) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError(errno.EIO, "fsync failed")
                actual_fsync(descriptor)

            with mock.patch.object(
                posix_runtime.os,
                "fsync",
                side_effect=fail_data_fsync,
            ):
                with self.assertRaises(posix_runtime.PosixRuntimeError):
                    posix_runtime.atomic_write_noclobber(
                        managed / "fsync.bin",
                        b"value",
                    )
            self.assertFalse((managed / "fsync.bin").exists())

            with mock.patch.object(
                posix_runtime,
                "rename_noreplace",
                side_effect=PermissionError(errno.EACCES, "link denied"),
            ):
                with self.assertRaises(posix_runtime.PosixRuntimeError):
                    posix_runtime.atomic_write_noclobber(
                        managed / "link.bin",
                        b"value",
                    )
            self.assertFalse((managed / "link.bin").exists())

            actual_open = posix_runtime.os.open

            def deny_staging(path, flags, *args, **kwargs):
                if flags & os.O_CREAT:
                    raise PermissionError(errno.EACCES, "permission denied")
                return actual_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                posix_runtime.os,
                "open",
                side_effect=deny_staging,
            ):
                with self.assertRaises(posix_runtime.PosixRuntimeError):
                    posix_runtime.atomic_write_noclobber(
                        managed / "permission.bin",
                        b"value",
                    )
            self.assertFalse((managed / "permission.bin").exists())
            residues = list(managed.iterdir())
            self.assertTrue(
                all(
                    item.name == ".awesome-capture-quarantine"
                    or (
                        item.name.startswith(".link.bin.staging-")
                        and item.is_file()
                    )
                    for item in residues
                )
            )
            quarantine = managed / ".awesome-capture-quarantine"
            self.assertEqual(quarantine.stat().st_mode & 0o777, 0o700)
            for residue in quarantine.iterdir():
                self.assertTrue(residue.is_file())
                self.assertEqual(residue.stat().st_mode & 0o777, 0o600)
            for residue in residues:
                if residue != quarantine:
                    self.assertEqual(residue.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
