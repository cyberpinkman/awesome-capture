from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "release.py"
SKILLS = (
    "download-video",
    "transcribe-media",
    "ingest-knowledge",
    "build-obsidian-vault",
)


def changelog(
    *,
    version: str = "0.1.0",
    released_on: str = "2026-07-28",
    unreleased: str = "",
    current_notes: str = "### Added\n\n- Initial public release.",
    history: str = "",
) -> str:
    unreleased_body = f"\n{unreleased.strip()}\n" if unreleased.strip() else "\n"
    history_body = f"\n{history.strip()}\n" if history.strip() else ""
    return (
        "# Changelog\n\n"
        "All notable user-visible changes are recorded here.\n\n"
        "## [Unreleased]\n"
        f"{unreleased_body}\n"
        f"## [{version}] - {released_on}\n\n"
        f"{current_notes.strip()}\n"
        f"{history_body}\n"
        f"[Unreleased]: https://github.com/cyberpinkman/awesome-capture/compare/v{version}...HEAD\n"
        f"[{version}]: https://github.com/cyberpinkman/awesome-capture/releases/tag/v{version}\n"
    )


class ReleaseMetadataTests(unittest.TestCase):
    maxDiff = None

    def make_repository(
        self,
        parent: Path,
        *,
        version_bytes: bytes = b"0.1.0\n",
        changelog_text: str | None = None,
    ) -> Path:
        repository = parent / "repository"
        (repository / "tools").mkdir(parents=True)
        shutil.copyfile(SCRIPT, repository / "tools" / "release.py")
        (repository / "VERSION").write_bytes(version_bytes)
        for skill in SKILLS:
            directory = repository / "skills" / skill
            directory.mkdir(parents=True)
            (directory / "VERSION").write_bytes(version_bytes)
        (repository / "CHANGELOG.md").write_text(
            changelog() if changelog_text is None else changelog_text,
            encoding="utf-8",
        )
        return repository

    def run_cli(
        self,
        repository: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
            }
        )
        return subprocess.run(
            [
                sys.executable,
                str(repository / "tools" / "release.py"),
                *arguments,
            ],
            cwd=repository,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def parse_single_object(self, raw: str) -> dict[str, Any]:
        self.assertTrue(raw)
        value = json.loads(raw)
        self.assertIsInstance(value, dict)
        return value

    def assert_success(
        self,
        completed: subprocess.CompletedProcess[str],
        *,
        operation: str,
    ) -> dict[str, Any]:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        value = self.parse_single_object(completed.stdout)
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["operation"], operation)
        return value

    def assert_error(
        self,
        completed: subprocess.CompletedProcess[str],
        *,
        code: str,
        exit_code: int = 2,
        private_root: Path,
    ) -> dict[str, Any]:
        self.assertEqual(completed.returncode, exit_code, completed.stderr)
        self.assertEqual(completed.stdout, "")
        value = self.parse_single_object(completed.stderr)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"]["code"], code)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertNotIn(str(private_root), completed.stderr)
        return value

    def test_check_accepts_canonical_metadata_and_json_protocol(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-valid-"
        ) as temporary:
            repository = self.make_repository(Path(temporary))

            completed = self.run_cli(repository, "check")

            value = self.assert_success(completed, operation="check")
            self.assertEqual(
                value,
                {
                    "copies": 4,
                    "operation": "check",
                    "release_count": 1,
                    "status": "ok",
                    "tag": "v0.1.0",
                    "version": "0.1.0",
                },
            )

    def test_version_rejects_nonstable_prefix_whitespace_leading_zero_and_extra_line(
        self,
    ) -> None:
        cases = (
            b"v0.1.0\n",
            b"0.1.0-rc.1\n",
            b"0.1.0+build.1\n",
            b" 0.1.0\n",
            b"0.1.0 \n",
            b"01.2.3\n",
            b"0.1.0\n\n",
        )
        for index, raw in enumerate(cases):
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory(
                    prefix=f"awesome-capture-release-version-{index}-"
                ) as temporary:
                    root = Path(temporary)
                    repository = self.make_repository(
                        root,
                        version_bytes=raw,
                    )

                    completed = self.run_cli(repository, "check")

                    self.assert_error(
                        completed,
                        code="VERSION_INVALID",
                        private_root=root,
                    )

    def test_check_rejects_missing_and_drifted_skill_versions(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-copies-"
        ) as temporary:
            root = Path(temporary)
            repository = self.make_repository(root)
            missing = repository / "skills" / SKILLS[0] / "VERSION"
            missing.unlink()

            missing_result = self.run_cli(repository, "check")

            self.assert_error(
                missing_result,
                code="VERSION_COPY_MISSING",
                private_root=root,
            )
            missing.write_text("0.1.0\n", encoding="utf-8")
            drifted = repository / "skills" / SKILLS[1] / "VERSION"
            drifted.write_text("0.1.1\n", encoding="utf-8")

            drift_result = self.run_cli(repository, "check")

            self.assert_error(
                drift_result,
                code="VERSION_COPY_MISMATCH",
                private_root=root,
            )

    def test_sync_repairs_missing_and_drifted_copies_idempotently(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-sync-"
        ) as temporary:
            repository = self.make_repository(Path(temporary))
            (repository / "skills" / SKILLS[0] / "VERSION").unlink()
            (repository / "skills" / SKILLS[1] / "VERSION").write_text(
                "9.9.9\n",
                encoding="utf-8",
            )

            first = self.run_cli(repository, "sync")
            second = self.run_cli(repository, "sync")
            checked = self.run_cli(repository, "check")

            first_value = self.assert_success(first, operation="sync")
            self.assertEqual(
                first_value["updated"],
                [
                    f"skills/{SKILLS[0]}/VERSION",
                    f"skills/{SKILLS[1]}/VERSION",
                ],
            )
            second_value = self.assert_success(second, operation="sync")
            self.assertEqual(second_value["updated"], [])
            self.assert_success(checked, operation="check")

    def test_sync_rejects_matching_symlink_copy_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-sync-symlink-"
        ) as temporary:
            root = Path(temporary)
            repository = self.make_repository(root)
            sentinel = root / "sentinel"
            sentinel.write_text("0.1.0\n", encoding="utf-8")
            destination = repository / "skills" / SKILLS[0] / "VERSION"
            destination.unlink()
            destination.symlink_to(sentinel)

            completed = self.run_cli(repository, "sync")

            self.assert_error(
                completed,
                code="VERSION_COPY_INVALID",
                private_root=root,
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "0.1.0\n")
            self.assertTrue(destination.is_symlink())

    def test_changelog_rejects_duplicate_versions(self) -> None:
        duplicate = changelog(
            history=(
                "## [0.1.0] - 2026-07-27\n\n"
                "### Fixed\n\n"
                "- Duplicate release."
            )
        )
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-duplicate-"
        ) as temporary:
            root = Path(temporary)
            repository = self.make_repository(
                root,
                changelog_text=duplicate,
            )

            completed = self.run_cli(repository, "check")

            self.assert_error(
                completed,
                code="CHANGELOG_INVALID",
                private_root=root,
            )

    def test_changelog_rejects_invalid_calendar_date(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-date-"
        ) as temporary:
            root = Path(temporary)
            repository = self.make_repository(
                root,
                changelog_text=changelog(released_on="2026-02-30"),
            )

            completed = self.run_cli(repository, "check")

            self.assert_error(
                completed,
                code="CHANGELOG_INVALID",
                private_root=root,
            )

    def test_changelog_rejects_version_and_date_ordering_errors(self) -> None:
        cases = (
            changelog(
                history=(
                    "## [0.2.0] - 2026-07-27\n\n"
                    "### Added\n\n"
                    "- Incorrectly newer historical version."
                )
            ),
            changelog(
                version="0.2.0",
                released_on="2026-07-01",
                history=(
                    "## [0.1.0] - 2026-07-28\n\n"
                    "### Added\n\n"
                    "- Incorrectly newer historical date."
                ),
            ),
        )
        for index, changelog_text in enumerate(cases):
            with self.subTest(index=index):
                with tempfile.TemporaryDirectory(
                    prefix=f"awesome-capture-release-order-{index}-"
                ) as temporary:
                    root = Path(temporary)
                    version = b"0.2.0\n" if index else b"0.1.0\n"
                    repository = self.make_repository(
                        root,
                        version_bytes=version,
                        changelog_text=changelog_text,
                    )

                    completed = self.run_cli(repository, "check")

                    self.assert_error(
                        completed,
                        code="CHANGELOG_INVALID",
                        private_root=root,
                    )

    def test_check_release_requires_empty_unreleased_section(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-unreleased-"
        ) as temporary:
            root = Path(temporary)
            repository = self.make_repository(
                root,
                changelog_text=changelog(
                    unreleased="### Added\n\n- Pending work."
                ),
            )

            ordinary_check = self.run_cli(repository, "check")
            release_check = self.run_cli(
                repository,
                "check-release",
                "--requested-version",
                "0.1.0",
            )

            self.assert_success(ordinary_check, operation="check")
            self.assert_error(
                release_check,
                code="RELEASE_NOT_READY",
                private_root=root,
            )

    def test_check_release_rejects_requested_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-requested-"
        ) as temporary:
            root = Path(temporary)
            repository = self.make_repository(root)

            completed = self.run_cli(
                repository,
                "check-release",
                "--requested-version",
                "0.1.1",
            )

            self.assert_error(
                completed,
                code="REQUESTED_VERSION_MISMATCH",
                private_root=root,
            )

    def test_check_release_accepts_current_version_above_history(self) -> None:
        text = changelog(
            version="0.2.0",
            history=(
                "## [0.1.0] - 2026-07-27\n\n"
                "### Added\n\n"
                "- Earlier release."
            ),
        )
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-history-"
        ) as temporary:
            repository = self.make_repository(
                Path(temporary),
                version_bytes=b"0.2.0\n",
                changelog_text=text,
            )

            completed = self.run_cli(
                repository,
                "check-release",
                "--requested-version",
                "0.2.0",
            )

            value = self.assert_success(
                completed,
                operation="check-release",
            )
            self.assertEqual(value["tag"], "v0.2.0")

    def test_notes_writes_only_current_body_and_excludes_footer(self) -> None:
        text = changelog(
            version="0.2.0",
            current_notes="### Added\n\n- Current release only.",
            history=(
                "## [0.1.0] - 2026-07-27\n\n"
                "### Added\n\n"
                "- Historical release only.\n\n"
                "[0.1.0]: https://github.com/cyberpinkman/awesome-capture/releases/tag/v0.1.0"
            ),
        )
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-notes-"
        ) as temporary:
            root = Path(temporary).resolve()
            repository = self.make_repository(
                root,
                version_bytes=b"0.2.0\n",
                changelog_text=text,
            )
            output = root / "release-notes.md"

            completed = self.run_cli(
                repository,
                "notes",
                "--output",
                str(output),
            )

            value = self.assert_success(completed, operation="notes")
            expected = "### Added\n\n- Current release only.\n"
            self.assertEqual(output.read_text(encoding="utf-8"), expected)
            self.assertEqual(value["bytes"], len(expected.encode("utf-8")))
            self.assertNotIn("Unreleased", output.read_text(encoding="utf-8"))
            self.assertNotIn("0.1.0", output.read_text(encoding="utf-8"))
            self.assertNotIn("github.com", output.read_text(encoding="utf-8"))

    def test_notes_is_no_clobber_and_rejects_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-safe-notes-"
        ) as temporary:
            root = Path(temporary).resolve()
            repository = self.make_repository(root)
            existing = root / "existing.md"
            existing.write_text("sentinel\n", encoding="utf-8")

            collision = self.run_cli(
                repository,
                "notes",
                "--output",
                str(existing),
            )

            self.assert_error(
                collision,
                code="OUTPUT_EXISTS",
                exit_code=4,
                private_root=root,
            )
            self.assertEqual(existing.read_text(encoding="utf-8"), "sentinel\n")

            real_directory = root / "real"
            real_directory.mkdir()
            linked_directory = root / "linked"
            linked_directory.symlink_to(real_directory, target_is_directory=True)
            unsafe_output = linked_directory / "notes.md"

            unsafe = self.run_cli(
                repository,
                "notes",
                "--output",
                str(unsafe_output),
            )

            self.assert_error(
                unsafe,
                code="OUTPUT_PATH_UNSAFE",
                private_root=root,
            )
            self.assertFalse((real_directory / "notes.md").exists())

    def test_invalid_arguments_also_follow_json_error_protocol(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-arguments-"
        ) as temporary:
            root = Path(temporary)
            repository = self.make_repository(root)

            completed = self.run_cli(repository, "check-release")

            self.assert_error(
                completed,
                code="INVALID_ARGUMENTS",
                private_root=root,
            )

    def test_missing_command_is_rejected_before_repository_reads(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-release-no-command-"
        ) as temporary:
            root = Path(temporary)
            repository = root / "repository"
            (repository / "tools").mkdir(parents=True)
            shutil.copyfile(SCRIPT, repository / "tools" / "release.py")

            completed = self.run_cli(repository)

            self.assert_error(
                completed,
                code="INVALID_ARGUMENTS",
                private_root=root,
            )


if __name__ == "__main__":
    unittest.main()
