from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import stat
import tempfile
import unittest
import wave
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


download_video = load_module(
    "download_video_adversarial_hardlinks",
    "skills/download-video/scripts/download_video.py",
)
transcribe_media = load_module(
    "transcribe_media_adversarial_hardlinks",
    "skills/transcribe-media/scripts/transcribe_media.py",
)
safe_runtime = importlib.import_module("_contracts.media_runtime")
vault_builder = load_module(
    "vault_builder_adversarial_hardlinks",
    "skills/build-obsidian-vault/scripts/vault_builder.py",
)
knowledge_writer = load_module(
    "knowledge_writer_adversarial_hardlinks",
    "skills/ingest-knowledge/scripts/knowledge_writer.py",
)


class AdversarialHardlinkTests(unittest.TestCase):
    def sentinel(
        self,
        root: Path,
        *,
        name: str,
        content: bytes,
        mode: int,
    ) -> Path:
        path = root / name
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def snapshot(self, path: Path) -> tuple[bytes, int, int]:
        metadata = path.stat()
        return (
            path.read_bytes(),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_nlink,
        )

    def assert_sentinel_unchanged(
        self,
        path: Path,
        expected: tuple[bytes, int, int],
    ) -> None:
        self.assertTrue(path.exists(), "outside sentinel was removed")
        self.assertEqual(
            self.snapshot(path),
            expected,
            "outside sentinel content, mode, or link count changed",
        )

    def private_directory(self, path: Path) -> Path:
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        return path

    def transaction_staging(
        self,
        vault: Path,
        *,
        name: str,
    ) -> Path:
        metadata = self.private_directory(vault / ".awesome-capture")
        transactions = self.private_directory(metadata / "transactions")
        return self.private_directory(transactions / name)

    def test_download_staging_rejects_hardlink_before_chmod(self) -> None:
        with tempfile.TemporaryDirectory(prefix="awesome-capture-hardlink-download-") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            staging = self.private_directory(root / "staging")
            sentinel = self.sentinel(
                root,
                name="outside-sentinel",
                content=b"outside download sentinel\n",
                mode=0o644,
            )
            os.link(sentinel, staging / "download.mp4")
            expected = self.snapshot(sentinel)

            rejected = None
            try:
                download_video._secure_staging_tree(staging)
            except download_video.DownloadError as exc:
                rejected = exc

            self.assert_sentinel_unchanged(sentinel, expected)
            self.assertIsNotNone(rejected, "hard-linked downloader output was not rejected")

    def test_transcribe_lock_rejects_hardlink_before_chmod(self) -> None:
        with tempfile.TemporaryDirectory(prefix="awesome-capture-hardlink-lock-") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            locks = self.private_directory(root / "locks")
            sentinel = self.sentinel(
                root,
                name="outside-sentinel",
                content=b"outside lock sentinel\n",
                mode=0o644,
            )
            lock_path = locks / "transcribe-job.lock"
            os.link(sentinel, lock_path)
            expected = self.snapshot(sentinel)

            rejected = None
            try:
                with safe_runtime.exclusive_lock(lock_path, timeout=0.0):
                    pass
            except safe_runtime.SafeRuntimeError as exc:
                rejected = exc

            self.assert_sentinel_unchanged(sentinel, expected)
            self.assertIsNotNone(rejected, "hard-linked transcription lock was not rejected")

    def test_ffmpeg_chunk_hardlink_is_rejected_before_chmod(self) -> None:
        with tempfile.TemporaryDirectory(prefix="awesome-capture-hardlink-chunk-") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            sentinel = root / "outside-sentinel.wav"
            with wave.open(str(sentinel), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\x01\x00" * 16000)
            sentinel.chmod(0o644)
            staging = self.private_directory(root / "staging")
            quarantine = self.private_directory(root / "quarantine")
            linked_snapshot: list[tuple[bytes, int, int]] = []

            def fake_ffmpeg(command: list[str], *, timeout: int, **unused: object):
                del timeout
                output = (
                    Path(str(unused["cwd"]))
                    / command[-1].replace("%05d", "00000")
                )
                os.link(sentinel, output)
                linked_snapshot.append(self.snapshot(sentinel))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(
                transcribe_media,
                "require_tool",
                return_value="/fake/ffmpeg",
            ), mock.patch.object(
                transcribe_media,
                "run_process",
                side_effect=fake_ffmpeg,
            ):
                with self.assertRaises(transcribe_media.TranscriptionError) as raised:
                    transcribe_media.normalize_chunks(
                        sentinel,
                        root / "chunks",
                        30,
                        30,
                        job_id="a" * 64,
                        source_sha256=hashlib.sha256(sentinel.read_bytes()).hexdigest(),
                        expected_duration_ms=1000,
                        staging_root=staging,
                        quarantine_root=quarantine,
                    )

            self.assertEqual(raised.exception.code, "CHUNK_SET_CONFLICT")
            self.assertEqual(len(linked_snapshot), 1)
            self.assert_sentinel_unchanged(sentinel, linked_snapshot[0])

    def test_builder_publish_rejects_hardlinked_staged_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="awesome-capture-hardlink-builder-") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            vault = self.private_directory(root / "Vault")
            transaction = self.transaction_staging(
                vault,
                name="build-00000000-0000-0000-0000-000000000000",
            )
            sentinel = self.sentinel(
                root,
                name="outside-sentinel.md",
                content=b"# Outside builder sentinel\n",
                mode=0o644,
            )
            staged = transaction / "note.md"
            os.link(sentinel, staged)
            expected = self.snapshot(sentinel)
            digest = hashlib.sha256(expected[0]).hexdigest()

            rejected = None
            try:
                vault_builder.publish_relative(
                    staged,
                    vault,
                    "note.md",
                    digest,
                    mode=0o644,
                )
            except vault_builder.VaultError as exc:
                rejected = exc

            self.assert_sentinel_unchanged(sentinel, expected)
            self.assertIsNotNone(rejected, "hard-linked build staging file was published")
            self.assertFalse((vault / "note.md").exists())

    def test_ingest_publish_rejects_hardlinked_staged_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="awesome-capture-hardlink-ingest-") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            vault = self.private_directory(root / "Vault")
            notes = self.private_directory(vault / "Notes")
            transaction = self.transaction_staging(
                vault,
                name="ingest-00000000-0000-0000-0000-000000000000",
            )
            sentinel = self.sentinel(
                root,
                name="outside-sentinel.md",
                content=b"# Outside ingest sentinel\n",
                mode=0o644,
            )
            staged = transaction / "note.md"
            os.link(sentinel, staged)
            expected = self.snapshot(sentinel)
            digest = hashlib.sha256(expected[0]).hexdigest()

            rejected = None
            try:
                knowledge_writer.publish_relative(
                    staged,
                    vault,
                    Path("Notes/note.md"),
                    digest,
                    mode=0o644,
                )
            except knowledge_writer.IngestError as exc:
                rejected = exc

            self.assert_sentinel_unchanged(sentinel, expected)
            self.assertIsNotNone(rejected, "hard-linked ingest staging file was published")
            self.assertFalse((notes / "note.md").exists())


if __name__ == "__main__":
    unittest.main()
