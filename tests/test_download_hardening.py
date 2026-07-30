from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/download-video/scripts/download_video.py"


def load_download_module():
    spec = importlib.util.spec_from_file_location("download_video_hardening", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


download_video = load_download_module()


class DownloadHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise AssertionError("download hardening tests require ffmpeg and ffprobe")

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
        path.chmod(0o600)

    def managed(self, temporary: str):
        output = Path(temporary) / "output"
        root = download_video.safe_output_root(str(output))
        return root, download_video.managed_layout(root)

    def source(self, url: str = "https://www.youtube.com/watch?v=abc"):
        public = download_video.sanitize_source_url(url, "youtube")
        fingerprint = download_video.hashlib.sha256(public.encode("utf-8")).hexdigest()
        return download_video._source_payload(
            {},
            platform_name="youtube",
            public_url=public,
            fingerprint=fingerprint,
            extractor="test",
        )

    def publish_video(self, layout, source):
        staging = download_video._new_staging_directory(layout, source["fingerprint"])
        media = staging / "input.mp4"
        self.make_video(media)
        return download_video._publish_staging(
            layout=layout,
            staging=staging,
            staged_media=media,
            platform_name="youtube",
            source=source,
            source_info=source,
            tool_name="fixture",
            tool_version="1",
            auth_mode="anonymous",
            fallback="none",
            warnings=[],
        )

    def completed_directory_snapshot(self, directory: Path):
        directory_metadata = directory.lstat()
        snapshot = {
            ".": (
                stat.S_IFMT(directory_metadata.st_mode),
                stat.S_IMODE(directory_metadata.st_mode),
                directory_metadata.st_dev,
                directory_metadata.st_ino,
            )
        }
        for entry in directory.iterdir():
            metadata = entry.lstat()
            if stat.S_ISREG(metadata.st_mode):
                payload = ("file", download_video.sha256_file(entry))
            elif stat.S_ISLNK(metadata.st_mode):
                payload = ("symlink", os.readlink(entry))
            else:
                payload = ("other", "")
            snapshot[entry.name] = (
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_nlink,
                metadata.st_size,
                payload,
            )
        return snapshot

    def test_capabilities_fail_closed(self):
        with mock.patch.object(download_video.os, "supports_dir_fd", set()):
            with self.assertRaises(download_video.DownloadError) as raised:
                download_video.require_posix_capabilities()
        self.assertEqual(raised.exception.code, "UNSUPPORTED_PLATFORM")
        self.assertEqual(raised.exception.exit_code, 3)

    def test_output_root_and_managed_tree_reject_symlinks_and_use_private_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            outside = parent / "outside"
            outside.mkdir()
            output_link = parent / "output"
            os.symlink(outside, output_link)
            with self.assertRaises(download_video.DownloadError) as raised:
                download_video.safe_output_root(str(output_link))
            self.assertEqual(raised.exception.code, "UNSAFE_OUTPUT_DIRECTORY")

            output_link.unlink()
            root = download_video.safe_output_root(str(output_link))
            layout = download_video.managed_layout(root)
            for path in layout.values():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(path.stat().st_uid, os.geteuid())

            downloads = layout["downloads"]
            downloads.rmdir()
            os.symlink(outside, downloads)
            with self.assertRaises(download_video.DownloadError) as managed:
                download_video.managed_layout(root)
            self.assertEqual(managed.exception.code, "UNSAFE_OUTPUT_DIRECTORY")

    def test_persistent_lock_times_out_and_is_mode_0600(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            lock = layout["locks"] / f"{'a' * 64}.lock"
            with download_video.source_lock(lock, 0):
                with self.assertRaises(download_video.DownloadError) as raised:
                    with download_video.source_lock(lock, 0):
                        pass
            self.assertEqual(raised.exception.code, "RESOURCE_BUSY")
            self.assertTrue(lock.exists())
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
            for timeout in (float("nan"), float("inf"), -1.0):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(download_video.DownloadError) as invalid:
                        with download_video.source_lock(lock, timeout):
                            pass
                    self.assertEqual(invalid.exception.code, "INVALID_ARGUMENT")

    def test_staging_requires_exactly_one_safe_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            staging = download_video._new_staging_directory(layout, "b" * 64)
            first = staging / "first.mp4"
            second = staging / "second.mp4"
            self.make_video(first)
            self.make_video(second)
            with self.assertRaises(download_video.DownloadError) as raised:
                download_video._validated_staging_media(staging, printed_path=first)
            self.assertEqual(raised.exception.code, "INTEGRITY_FAILED")

    def test_artifact_v2_is_last_commit_marker_and_only_strict_artifact_is_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, layout = self.managed(temporary)
            source = self.source()
            legacy_dir = root / "youtube"
            legacy_dir.mkdir()
            self.make_video(legacy_dir / "preseeded.mp4")

            result = self.publish_video(layout, source)
            artifact_path = Path(result["artifact_path"])
            media_path = Path(result["media_path"])
            self.assertEqual(result["result"], "created")
            self.assertEqual(result["manifest"]["schema_version"], "awesome-capture.artifact/v2")
            self.assertEqual(result["manifest"]["producer"]["contract_digest"], download_video.CONTRACT_DIGEST)
            self.assertEqual(artifact_path.name, "artifact.json")
            self.assertEqual(stat.S_IMODE(artifact_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(media_path.stat().st_mode), 0o600)
            self.assertEqual(media_path.stat().st_nlink, 1)
            self.assertFalse((artifact_path.parent / ".transaction.json").exists())

            reused = download_video._find_reusable(
                layout,
                platform_name="youtube",
                fingerprint=source["fingerprint"],
            )
            self.assertIsNotNone(reused)
            self.assertEqual(reused["result"], "reused")
            self.assertNotEqual(Path(reused["media_path"]), legacy_dir / "preseeded.mp4")

    def test_bilibili_bvid_and_page_query_survive_formal_video_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            public = download_video.sanitize_source_url(
                "https://www.bilibili.com/video/BV1fixture?bvid=BV1fixture&p=2",
                "bilibili",
            )
            fingerprint = download_video.hashlib.sha256(
                public.encode("utf-8")
            ).hexdigest()
            source = download_video._source_payload(
                {},
                platform_name="bilibili",
                public_url=public,
                fingerprint=fingerprint,
                extractor="BiliBili",
            )
            staging = download_video._new_staging_directory(layout, fingerprint)
            media = staging / "input.mp4"
            self.make_video(media)
            result = download_video._publish_staging(
                layout=layout,
                staging=staging,
                staged_media=media,
                platform_name="bilibili",
                source=source,
                source_info=source,
                tool_name="fixture",
                tool_version="1",
                auth_mode="anonymous",
                fallback="none",
                warnings=[],
            )
            artifact = json.loads(
                Path(result["artifact_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                artifact["source"]["url"],
                "https://www.bilibili.com/video/BV1fixture?bvid=BV1fixture&p=2",
            )

    def test_tampered_artifact_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            source = self.source()
            result = self.publish_video(layout, source)
            artifact_path = Path(result["artifact_path"])
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["source"]["url"] += "&token=secret"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            artifact_path.chmod(0o600)
            with self.assertRaises(download_video.DownloadError) as raised:
                download_video._find_reusable(
                    layout,
                    platform_name="youtube",
                    fingerprint=source["fingerprint"],
                )
            self.assertEqual(raised.exception.code, "INVALID_ARTIFACT")

    def test_reuse_rejects_private_file_and_final_directory_mode_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            source = self.source()
            result = self.publish_video(layout, source)
            artifact_path = Path(result["artifact_path"])
            targets = [
                artifact_path,
                Path(result["media_path"]),
                artifact_path.parent / "source.info.json",
            ]
            for target in targets:
                with self.subTest(target=target.name):
                    target.chmod(0o666)
                    with self.assertRaises(download_video.DownloadError):
                        download_video._find_reusable(
                            layout,
                            platform_name="youtube",
                            fingerprint=source["fingerprint"],
                        )
                    target.chmod(0o600)
            artifact_path.parent.chmod(0o777)
            with self.assertRaises(download_video.DownloadError) as raised:
                download_video._find_reusable(
                    layout,
                    platform_name="youtube",
                    fingerprint=source["fingerprint"],
                )
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            artifact_path.parent.chmod(0o700)

    def test_legacy_and_hardlinked_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, layout = self.managed(temporary)
            source = self.source()
            result = self.publish_video(layout, source)
            artifact_path = Path(result["artifact_path"])
            extra_link = root / "artifact-copy.json"
            os.link(artifact_path, extra_link)
            with self.assertRaises(download_video.DownloadError) as hardlinked:
                download_video._find_reusable(
                    layout,
                    platform_name="youtube",
                    fingerprint=source["fingerprint"],
                )
            self.assertEqual(hardlinked.exception.code, "INVALID_ARTIFACT")
            extra_link.unlink()

            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["schema_version"] = "awesome-capture.artifact/v1"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            artifact_path.chmod(0o600)
            with self.assertRaises(download_video.DownloadError) as legacy:
                download_video._find_reusable(
                    layout,
                    platform_name="youtube",
                    fingerprint=source["fingerprint"],
                )
            self.assertEqual(legacy.exception.code, "UNSUPPORTED_SCHEMA_VERSION")

    def test_symlink_at_final_hash_directory_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, layout = self.managed(temporary)
            source = self.source()
            staging = download_video._new_staging_directory(layout, source["fingerprint"])
            media = staging / "input.mp4"
            self.make_video(media)
            media_hash = download_video.sha256_file(media)
            platform = download_video._ensure_private_child(layout["downloads"], "youtube")
            source_dir = download_video._ensure_private_child(platform, source["fingerprint"])
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            os.symlink(outside, source_dir / media_hash)
            with self.assertRaises(download_video.DownloadError) as raised:
                download_video._publish_staging(
                    layout=layout,
                    staging=staging,
                    staged_media=media,
                    platform_name="youtube",
                    source=source,
                    source_info=source,
                    tool_name="fixture",
                    tool_version="1",
                    auth_mode="anonymous",
                    fallback="none",
                    warnings=[],
                )
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual({item.name for item in outside.iterdir()}, {"sentinel"})

    def test_staging_quarantine_source_swap_restores_foreign_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, layout = self.managed(temporary)
            staging = download_video._new_staging_directory(layout, "c" * 64)
            (staging / "journal.json").write_text("{}", encoding="utf-8")
            (staging / "journal.json").chmod(0o600)
            moved = staging.with_name(f"{staging.name}.moved")
            original = download_video.rename_noreplace
            swapped = False

            def swap_before_move(
                source_name: str,
                destination_name: str,
                *,
                source_dir_fd: int,
                destination_dir_fd: int,
            ) -> None:
                nonlocal swapped
                if not swapped:
                    os.rename(
                        source_name,
                        moved.name,
                        src_dir_fd=source_dir_fd,
                        dst_dir_fd=source_dir_fd,
                    )
                    os.mkdir(source_name, 0o700, dir_fd=source_dir_fd)
                    foreign_fd = os.open(
                        source_name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=source_dir_fd,
                    )
                    try:
                        sentinel_fd = os.open(
                            "sentinel",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=foreign_fd,
                        )
                        os.write(sentinel_fd, b"foreign")
                        os.close(sentinel_fd)
                    finally:
                        os.close(foreign_fd)
                    swapped = True
                original(
                    source_name,
                    destination_name,
                    source_dir_fd=source_dir_fd,
                    destination_dir_fd=destination_dir_fd,
                )

            with mock.patch.object(
                download_video,
                "rename_noreplace",
                side_effect=swap_before_move,
            ):
                with self.assertRaises(download_video.DownloadError) as raised:
                    download_video._quarantine_staging(layout, staging)
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertTrue(swapped)
            self.assertEqual((staging / "sentinel").read_bytes(), b"foreign")
            self.assertEqual((moved / "journal.json").read_text(encoding="utf-8"), "{}")
            self.assertEqual(list(layout["quarantine"].iterdir()), [])

    def test_quarantine_noreplace_preserves_racing_external_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            staging = download_video._new_staging_directory(layout, "a" * 64)
            partial = staging / "partial.bin"
            partial.write_bytes(b"partial")
            partial.chmod(0o600)
            original = download_video.rename_noreplace
            placeholder: dict[str, int] = {}

            def race(
                source_name: str,
                destination_name: str,
                *,
                source_dir_fd: int,
                destination_dir_fd: int,
            ) -> None:
                os.mkdir(destination_name, 0o700, dir_fd=destination_dir_fd)
                placeholder["inode"] = os.stat(
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
                download_video,
                "rename_noreplace",
                side_effect=race,
            ):
                with self.assertRaises(download_video.DownloadError) as raised:
                    download_video._quarantine_staging(layout, staging)
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertTrue(staging.is_dir())
            raced = next(layout["quarantine"].iterdir())
            self.assertEqual(raced.stat().st_ino, placeholder["inode"])

    def test_recover_rejects_unknown_download_root_objects(self):
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                output, layout = self.managed(temporary)
                platform = layout["downloads"] / "youtube"
                if kind == "file":
                    platform.write_bytes(b"foreign")
                    platform.chmod(0o600)
                else:
                    outside = Path(temporary).resolve() / "outside"
                    outside.mkdir(mode=0o700)
                    platform.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(download_video.DownloadError) as raised:
                    download_video.recover(
                        argparse.Namespace(
                            output_dir=str(output),
                            lock_timeout=1.0,
                        )
                    )
                self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")

    def test_crash_before_staging_journal_leaves_no_final_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            source = self.source()
            staging = download_video._new_staging_directory(layout, source["fingerprint"])
            media = staging / "input.mp4"
            self.make_video(media)
            media_hash = download_video.sha256_file(media)
            real_atomic = download_video.atomic_json_noclobber

            def crash_before_journal(path, value):
                if path.name == "source.info.pending.json":
                    raise OSError("simulated crash before staging journal")
                return real_atomic(path, value)

            with mock.patch.object(
                download_video,
                "atomic_json_noclobber",
                side_effect=crash_before_journal,
            ):
                with self.assertRaises(OSError):
                    download_video._publish_staging(
                        layout=layout,
                        staging=staging,
                        staged_media=media,
                        platform_name="youtube",
                        source=source,
                        source_info=source,
                        tool_name="fixture",
                        tool_version="1",
                        auth_mode="anonymous",
                        fallback="none",
                        warnings=[],
                    )
            final_dir = (
                layout["downloads"]
                / "youtube"
                / source["fingerprint"]
                / media_hash
            )
            self.assertFalse(final_dir.exists())
            recovery = download_video.recover_layout(
                layout,
                only_fingerprint=source["fingerprint"],
            )
            self.assertEqual(recovery["recovered"], [])
            self.assertEqual(len(recovery["quarantined"]), 1)
            self.assertIsNone(
                download_video._find_reusable(
                    layout,
                    platform_name="youtube",
                    fingerprint=source["fingerprint"],
                )
            )

    def test_recover_preserves_unjournaled_empty_final_as_external_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            source = self.source()
            platform_dir = download_video._ensure_private_child(
                layout["downloads"],
                "youtube",
            )
            source_dir = download_video._ensure_private_child(
                platform_dir,
                source["fingerprint"],
            )
            orphan = source_dir / ("d" * 64)
            orphan.mkdir(mode=0o700)

            with self.assertRaises(download_video.DownloadError) as raised:
                download_video.recover_layout(
                    layout,
                    only_fingerprint=source["fingerprint"],
                )
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertTrue(orphan.is_dir())
            with self.assertRaises(download_video.DownloadError) as blocked:
                download_video._find_reusable(
                    layout,
                    platform_name="youtube",
                    fingerprint=source["fingerprint"],
                )
            self.assertEqual(blocked.exception.code, "RECOVERY_CONFLICT")

    def test_explicit_recover_revalidates_unchanged_completed_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            output, layout = self.managed(temporary)
            source = self.source()
            result = self.publish_video(layout, source)
            final_dir = Path(result["artifact_path"]).parent
            before = self.completed_directory_snapshot(final_dir)

            recovery = download_video.recover(
                argparse.Namespace(
                    output_dir=str(output),
                    lock_timeout=1.0,
                )
            )

            self.assertEqual(recovery["recovered"], [])
            self.assertEqual(recovery["quarantined"], [])
            self.assertEqual(
                self.completed_directory_snapshot(final_dir),
                before,
            )

    def test_explicit_recover_rejects_invalid_completed_download_without_mutation(
        self,
    ):
        cases = (
            "tampered-media",
            "ffprobe-drift",
            "artifact-mode",
            "artifact-symlink",
            "artifact-hardlink",
            "source-info-mismatch",
            "wrong-hash-directory",
            "unknown-entry",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                output, layout = self.managed(temporary)
                source = self.source()
                result = self.publish_video(layout, source)
                artifact_path = Path(result["artifact_path"])
                media_path = Path(result["media_path"])
                final_dir = artifact_path.parent

                if case == "tampered-media":
                    with media_path.open("ab") as handle:
                        handle.write(b"tampered")
                elif case == "ffprobe-drift":
                    artifact = json.loads(
                        artifact_path.read_text(encoding="utf-8")
                    )
                    artifact["media"]["duration_ms"] += 1
                    artifact["media"]["ffprobe"]["evidence_sha256"] = (
                        download_video.video_probe_evidence_sha256(
                            artifact["media"]
                        )
                    )
                    artifact_path.write_text(
                        json.dumps(artifact),
                        encoding="utf-8",
                    )
                    artifact_path.chmod(0o600)
                elif case == "artifact-mode":
                    artifact_path.chmod(0o644)
                elif case == "artifact-symlink":
                    target = Path(temporary) / "artifact-target.json"
                    artifact_path.rename(target)
                    artifact_path.symlink_to(target)
                elif case == "artifact-hardlink":
                    os.link(
                        artifact_path,
                        Path(temporary) / "artifact-hardlink.json",
                    )
                elif case == "source-info-mismatch":
                    source_info_path = final_dir / "source.info.json"
                    source_info = json.loads(
                        source_info_path.read_text(encoding="utf-8")
                    )
                    source_info["title"] = "different"
                    source_info_path.write_text(
                        json.dumps(source_info),
                        encoding="utf-8",
                    )
                    source_info_path.chmod(0o600)
                elif case == "wrong-hash-directory":
                    renamed = final_dir.with_name("f" * 64)
                    final_dir.rename(renamed)
                    final_dir = renamed
                    artifact_path = final_dir / "artifact.json"
                    media_path = final_dir / media_path.name
                    artifact = json.loads(
                        artifact_path.read_text(encoding="utf-8")
                    )
                    artifact["media"]["path"] = str(media_path)
                    artifact_path.write_text(
                        json.dumps(artifact),
                        encoding="utf-8",
                    )
                    artifact_path.chmod(0o600)
                elif case == "unknown-entry":
                    unexpected = final_dir / "unexpected.bin"
                    unexpected.write_bytes(b"foreign")
                    unexpected.chmod(0o600)

                before = self.completed_directory_snapshot(final_dir)
                with self.assertRaises(download_video.DownloadError) as raised:
                    download_video.recover(
                        argparse.Namespace(
                            output_dir=str(output),
                            lock_timeout=1.0,
                        )
                    )
                self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
                self.assertEqual(
                    self.completed_directory_snapshot(final_dir),
                    before,
                )

    def test_recover_finishes_a_crash_before_artifact_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            source = self.source()
            staging = download_video._new_staging_directory(layout, source["fingerprint"])
            media = staging / "input.mp4"
            self.make_video(media)
            real_publish = download_video._publish_file_no_clobber

            def crash_before_commit(source_path, destination_path):
                if destination_path.name == "artifact.json":
                    raise OSError("simulated crash")
                return real_publish(source_path, destination_path)

            with mock.patch.object(
                download_video,
                "_publish_file_no_clobber",
                side_effect=crash_before_commit,
            ):
                with self.assertRaises(OSError):
                    download_video._publish_staging(
                        layout=layout,
                        staging=staging,
                        staged_media=media,
                        platform_name="youtube",
                        source=source,
                        source_info=source,
                        tool_name="fixture",
                        tool_version="1",
                        auth_mode="anonymous",
                        fallback="none",
                        warnings=[],
                    )
            self.assertFalse(any(layout["downloads"].rglob("artifact.json")))
            self.assertTrue(any(layout["downloads"].rglob(".transaction.json")))

            with mock.patch.object(download_video, "_publish_file_no_clobber", real_publish):
                recovery = download_video.recover_layout(
                    layout, only_fingerprint=source["fingerprint"]
                )
            self.assertEqual(len(recovery["recovered"]), 1)
            artifact_path = Path(recovery["recovered"][0])
            self.assertTrue(artifact_path.exists())
            artifact = download_video.strict_json_load(artifact_path)
            download_video.validate_video_artifact(
                artifact, artifact_path=artifact_path, revalidate_media=True
            )
            self.assertFalse(any(layout["downloads"].rglob(".transaction.json")))

    def test_recover_recreates_final_journal_after_early_crash(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            source = self.source()
            staging = download_video._new_staging_directory(layout, source["fingerprint"])
            media = staging / "input.mp4"
            self.make_video(media)
            real_atomic = download_video.atomic_json_noclobber

            def fail_final_journal(path, value):
                if path.name == ".transaction.json":
                    raise OSError("simulated crash before final journal")
                return real_atomic(path, value)

            with mock.patch.object(
                download_video,
                "atomic_json_noclobber",
                side_effect=fail_final_journal,
            ):
                with self.assertRaises(OSError):
                    download_video._publish_staging(
                        layout=layout,
                        staging=staging,
                        staged_media=media,
                        platform_name="youtube",
                        source=source,
                        source_info=source,
                        tool_name="fixture",
                        tool_version="1",
                        auth_mode="anonymous",
                        fallback="none",
                        warnings=[],
                    )
            self.assertTrue((staging / "journal.json").exists())
            with mock.patch.object(
                download_video,
                "atomic_json_noclobber",
                real_atomic,
            ):
                recovery = download_video.recover_layout(
                    layout, only_fingerprint=source["fingerprint"]
                )
            self.assertEqual(len(recovery["recovered"]), 1)
            self.assertTrue(Path(recovery["recovered"][0]).exists())

    def test_detect_cli_never_echoes_signed_query(self):
        process = subprocess.run(
            [
                os.fspath(Path(os.sys.executable)),
                str(SCRIPT),
                "detect",
                "https://www.youtube.com/watch?v=abc&token=super-secret",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn("super-secret", process.stdout + process.stderr)
        payload = json.loads(process.stdout)
        self.assertNotIn("url", payload)
        self.assertEqual(payload["sanitized_url"], "https://www.youtube.com/watch?v=abc")

    def test_doctor_dependency_failure_uses_error_channel_and_exit_three(self):
        environment = dict(os.environ)
        environment["PATH"] = ""
        process = subprocess.run(
            [
                os.fspath(Path(os.sys.executable)),
                str(SCRIPT),
                "doctor",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(process.returncode, 3)
        self.assertEqual(process.stdout, "")
        payload = json.loads(process.stderr)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["code"], "DEPENDENCY_MISSING")
        self.assertEqual(
            json.loads(payload["error"]["details"])["missing"],
            ["ffmpeg", "ffprobe", "yt-dlp"],
        )

    def test_ephemeral_cookie_publish_is_noclobber_and_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir(mode=0o700)
            sentinel = root / "sentinel"
            sentinel.write_bytes(b"unchanged")
            cookie_path = private / "cookies.txt"
            os.symlink(sentinel, cookie_path)

            with self.assertRaises(download_video.DownloadError) as captured:
                download_video.write_netscape_cookies(
                    cookie_path,
                    [
                        {
                            "domain": ".douyin.com",
                            "path": "/",
                            "secure": True,
                            "expires": 0,
                            "name": "session",
                            "value": "secret",
                        }
                    ],
                )

            self.assertEqual(captured.exception.code, "PATH_COLLISION")
            self.assertEqual(sentinel.read_bytes(), b"unchanged")
            self.assertTrue(cookie_path.is_symlink())

    def test_subprocess_cwd_is_pinned_to_open_staging_inode_during_path_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            staging = download_video._new_staging_directory(layout, "d" * 64)
            moved = staging.with_name(f"{staging.name}.moved")
            outside = Path(temporary) / "outside"
            outside.mkdir(mode=0o700)
            sentinel = outside / "sentinel"
            sentinel.write_bytes(b"unchanged")
            original_cwd_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)

            def simulated_subprocess(command, **options):
                self.assertEqual(command[command.index("--paths") + 1], "temp:.")
                output = command[command.index("--output") + 1]
                self.assertFalse(Path(output).is_absolute())
                self.assertIn("pass_fds", options)
                self.assertIn("preexec_fn", options)
                pinned_fd = options["pass_fds"][0]
                expected = os.fstat(pinned_fd)
                os.rename(staging, moved)
                os.symlink(outside, staging)
                options["preexec_fn"]()
                try:
                    actual = os.stat(".")
                    self.assertEqual(
                        (actual.st_dev, actual.st_ino),
                        (expected.st_dev, expected.st_ino),
                    )
                    Path("child-output").write_bytes(b"pinned")
                finally:
                    os.fchdir(original_cwd_fd)
                return subprocess.CompletedProcess(command, 0, "", "")

            try:
                with mock.patch.object(
                    download_video.subprocess,
                    "run",
                    side_effect=simulated_subprocess,
                ):
                    download_video._run_subprocess_pinned(
                        [
                            "/fake/yt-dlp",
                            "--paths",
                            "temp:.",
                            "--output",
                            "%(id)s.%(ext)s",
                        ],
                        timeout=1,
                        pinned_cwd=staging,
                    )
            finally:
                os.close(original_cwd_fd)

            self.assertEqual((moved / "child-output").read_bytes(), b"pinned")
            self.assertEqual(sentinel.read_bytes(), b"unchanged")
            self.assertFalse((outside / "child-output").exists())

    def test_gallery_fallback_uses_pinned_cwd_and_relative_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            args = argparse.Namespace(timeout=1)
            original_error = download_video.DownloadError(
                "DOWNLOAD_FAILED",
                "download failed",
                exit_code=5,
            )
            marker = RuntimeError("stop after command capture")
            with (
                mock.patch.object(
                    download_video,
                    "require_tool",
                    return_value="/fake/gallery-dl",
                ),
                mock.patch.object(
                    download_video,
                    "run_process_raw",
                    side_effect=marker,
                ) as runner,
            ):
                with self.assertRaisesRegex(RuntimeError, "command capture"):
                    download_video._gallery_download_locked(
                        args,
                        url="https://x.com/example/status/1",
                        platform_name="twitter",
                        layout=layout,
                        fingerprint="e" * 64,
                        original_error=original_error,
                    )

            command = runner.call_args.args[0]
            self.assertEqual(command.count("--force-ipv4"), 1)
            self.assertNotIn("--no-check-certificate", command)
            self.assertEqual(command[command.index("-D") + 1], ".")
            pinned_cwd = runner.call_args.kwargs["pinned_cwd"]
            self.assertFalse(pinned_cwd.is_symlink())
            self.assertEqual(pinned_cwd.parent, layout["staging"])

    def test_twitter_transport_is_ipv4_only_without_changing_other_platforms(self):
        args = argparse.Namespace(
            socket_timeout=20,
            retries=3,
            cookies=None,
            cookies_from_browser=None,
            impersonate=None,
        )
        with mock.patch.object(
            download_video,
            "require_tool",
            return_value="/fake/yt-dlp",
        ):
            twitter = download_video.base_ytdlp_args(args, "twitter")
            other_platforms = {
                platform: download_video.base_ytdlp_args(args, platform)
                for platform in ("douyin", "tiktok", "bilibili", "youtube")
            }

        self.assertEqual(twitter.count("--force-ipv4"), 1)
        self.assertNotIn("--no-check-certificates", twitter)
        for platform, command in other_platforms.items():
            with self.subTest(platform=platform):
                self.assertNotIn("--force-ipv4", command)
                self.assertNotIn("--no-check-certificates", command)

    def test_tiktok_gallery_fallback_keeps_default_address_family(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, layout = self.managed(temporary)
            args = argparse.Namespace(timeout=1)
            original_error = download_video.DownloadError(
                "DOWNLOAD_FAILED",
                "download failed",
                exit_code=5,
            )
            marker = RuntimeError("stop after command capture")
            with (
                mock.patch.object(
                    download_video,
                    "require_tool",
                    return_value="/fake/gallery-dl",
                ),
                mock.patch.object(
                    download_video,
                    "run_process_raw",
                    side_effect=marker,
                ) as runner,
            ):
                with self.assertRaisesRegex(RuntimeError, "command capture"):
                    download_video._gallery_download_locked(
                        args,
                        url="https://www.tiktok.com/@example/video/1",
                        platform_name="tiktok",
                        layout=layout,
                        fingerprint="f" * 64,
                        original_error=original_error,
                    )

            command = runner.call_args.args[0]
            self.assertNotIn("--force-ipv4", command)
            self.assertNotIn("--no-check-certificate", command)

    def test_download_cli_confines_external_tool_to_private_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = root / "fixture.mp4"
            self.make_video(fixture)
            tools = root / "bin"
            tools.mkdir()
            fake = tools / "yt-dlp"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, shutil, sys\n"
                "from pathlib import Path\n"
                f"fixture = Path({str(fixture)!r})\n"
                "if '--version' in sys.argv:\n"
                "    print('2026.07.04')\n"
                "    raise SystemExit(0)\n"
                "args = sys.argv[1:]\n"
                "template = Path(args[args.index('--output') + 1])\n"
                "assert not template.is_absolute()\n"
                "assert args[args.index('--paths') + 1] == 'temp:.'\n"
                "media = Path(str(template).replace('%(id)s', 'abc').replace('%(title).120B', 'title').replace('%(ext)s', 'mp4'))\n"
                "media.parent.mkdir(parents=True, exist_ok=True)\n"
                "shutil.copyfile(fixture, media)\n"
                "info = media.with_suffix('.info.json')\n"
                "info.write_text(json.dumps({'id':'abc','title':'title','webpage_url':'https://www.youtube.com/watch?v=abc','extractor_key':'Youtube'}), encoding='utf-8')\n"
                "print(json.dumps(str(media)))\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            output = root / "output"
            environment = dict(os.environ)
            environment["PATH"] = f"{tools}{os.pathsep}{environment['PATH']}"
            process = subprocess.run(
                [
                    os.fspath(Path(os.sys.executable)),
                    str(SCRIPT),
                    "download",
                    "https://www.youtube.com/watch?v=abc&token=secret",
                    "--output-dir",
                    str(output),
                    "--gallery-fallback",
                    "off",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stderr, "")
            self.assertNotIn("secret", process.stdout)
            payload = json.loads(process.stdout)
            artifact_path = Path(payload["artifact_path"])
            self.assertTrue(artifact_path.is_file())
            self.assertIn("/.awesome-capture-media/v2/downloads/youtube/", str(artifact_path))
            self.assertEqual(list((output / ".awesome-capture-media/v2/staging").iterdir()), [])
            self.assertFalse((output / "youtube").exists())
            final_names = {item.name for item in artifact_path.parent.iterdir()}
            self.assertEqual(final_names, {"artifact.json", "source.info.json", "media.mp4"})


if __name__ == "__main__":
    unittest.main()
