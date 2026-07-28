from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CLI_SCRIPTS = {
    "download-video": ROOT / "skills/download-video/scripts/download_video.py",
    "transcribe-media": ROOT / "skills/transcribe-media/scripts/transcribe_media.py",
    "build-obsidian-vault": (
        ROOT / "skills/build-obsidian-vault/scripts/vault_builder.py"
    ),
    "ingest-knowledge": (
        ROOT / "skills/ingest-knowledge/scripts/knowledge_writer.py"
    ),
}


class CliContractHardeningTests(unittest.TestCase):
    maxDiff = None

    def run_cli(
        self,
        script: Path,
        arguments: list[str],
        *,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
            }
        )
        return subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )

    def single_json_object(self, raw: str, *, channel: str) -> dict[str, Any]:
        self.assertTrue(raw, f"{channel} was empty")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.fail(f"{channel} was not exactly one JSON value: {exc}")
        self.assertIsInstance(value, dict, f"{channel} JSON was not an object")
        return value

    def assert_json_error(
        self,
        process: subprocess.CompletedProcess[str],
        *,
        code: str,
        exit_code: int,
        private_root: Path,
    ) -> dict[str, Any]:
        self.assertEqual(process.returncode, exit_code, process.stderr)
        self.assertEqual(process.stdout, "")
        value = self.single_json_object(process.stderr, channel="stderr")
        self.assertEqual(value.get("status"), "error")
        self.assertIsInstance(value.get("error"), dict)
        self.assertEqual(value["error"].get("code"), code)
        self.assertNotIn("Traceback", process.stderr)
        self.assertNotIn(str(private_root), process.stderr)
        return value

    def private_directory(self, path: Path) -> Path:
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        return path

    def copy_skill(self, root: Path, name: str) -> Path:
        source = ROOT / "skills" / name
        destination = root / f"standalone-{name}"
        shutil.copytree(source, destination)
        return destination

    def tamper_contract_manifest(self, skill: Path) -> None:
        manifest = skill / "scripts" / "_contracts" / "manifest.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        original = value.get("contract_digest")
        replacement = "0" * 64 if original != "0" * 64 else "f" * 64
        value["contract_digest"] = replacement
        manifest.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def write_ingest_inputs(self, root: Path) -> tuple[Path, Path]:
        transcript = root / "transcript.json"
        shutil.copyfile(
            ROOT / "contracts/fixtures/valid/transcript-artifact.json",
            transcript,
        )
        transcript.chmod(0o600)
        draft = root / "draft.md"
        draft.write_text(
            "# 测试知识\n\n"
            "## 核心结论\n\n"
            "- 原材料提出一个可核查观点。证据：[00:00:00.100–00:00:01.000]\n\n"
            "## 待验证\n\n"
            "- 该观点的外部有效性仍需验证。\n",
            encoding="utf-8",
        )
        draft.chmod(0o600)
        return transcript, draft

    def ingest_arguments(
        self,
        transcript: Path,
        draft: Path,
        vault: Path,
        *,
        dry_run: bool,
        expected_plan_sha256: str | None = None,
    ) -> list[str]:
        arguments = [
            "commit",
            "--transcript",
            str(transcript),
            "--document",
            str(draft),
            "--vault",
            str(vault),
            "--title",
            "测试知识",
            "--lock-timeout",
            "2",
        ]
        if dry_run:
            arguments.append("--dry-run")
        if expected_plan_sha256 is not None:
            arguments.extend(
                ["--expected-plan-sha256", expected_plan_sha256]
            )
        return arguments

    def tree_snapshot(self, root: Path) -> dict[str, tuple[Any, ...]]:
        snapshot: dict[str, tuple[Any, ...]] = {}
        for path in sorted(root.rglob("*")):
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                snapshot[relative] = (
                    "directory",
                    stat.S_IMODE(metadata.st_mode),
                )
            elif stat.S_ISREG(metadata.st_mode):
                snapshot[relative] = (
                    "file",
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_nlink,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            elif stat.S_ISLNK(metadata.st_mode):
                snapshot[relative] = ("symlink", os.readlink(path))
            else:
                snapshot[relative] = (
                    "special",
                    stat.S_IFMT(metadata.st_mode),
                )
        return snapshot

    def file_snapshot(self, path: Path) -> tuple[bytes, int, int]:
        metadata = path.stat()
        return (
            path.read_bytes(),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_nlink,
        )

    def test_all_cli_help_is_one_stdout_json_object(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-cli-help-"
        ) as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            cwd = self.private_directory(root / "arbitrary-cwd")
            for name, script in CLI_SCRIPTS.items():
                with self.subTest(skill=name):
                    process = self.run_cli(script, ["--help"], cwd=cwd)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertEqual(process.stderr, "")
                    value = self.single_json_object(
                        process.stdout,
                        channel="stdout",
                    )
                    self.assertEqual(value.get("status"), "ok")
                    self.assertEqual(value.get("operation"), "help")

    def test_download_copy_rejects_tampered_contract_manifest(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-contract-download-"
        ) as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            skill = self.copy_skill(root, "download-video")
            cwd = self.private_directory(root / "arbitrary-cwd")
            self.tamper_contract_manifest(skill)

            process = self.run_cli(
                skill / "scripts/download_video.py",
                ["detect", "https://www.youtube.com/watch?v=public"],
                cwd=cwd,
            )

            self.assert_json_error(
                process,
                code="CONTRACT_BUILD_MISMATCH",
                exit_code=7,
                private_root=root,
            )

    def test_transcribe_copy_rejects_tampered_contract_manifest(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-contract-transcribe-"
        ) as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            skill = self.copy_skill(root, "transcribe-media")
            cwd = self.private_directory(root / "arbitrary-cwd")
            self.tamper_contract_manifest(skill)

            process = self.run_cli(
                skill / "scripts/transcribe_media.py",
                ["doctor"],
                cwd=cwd,
            )

            self.assert_json_error(
                process,
                code="CONTRACT_BUILD_MISMATCH",
                exit_code=7,
                private_root=root,
            )

    def test_builder_copy_rejects_tamper_before_target_write(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-contract-builder-"
        ) as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            skill = self.copy_skill(root, "build-obsidian-vault")
            cwd = self.private_directory(root / "arbitrary-cwd")
            script = skill / "scripts/vault_builder.py"
            config = skill / "assets/vault-config.example.json"
            vault = root / "Vault"
            planned = self.run_cli(
                script,
                ["plan", str(config), "--vault", str(vault)],
                cwd=cwd,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan = self.single_json_object(planned.stdout, channel="stdout")
            self.assertFalse(vault.exists())
            self.tamper_contract_manifest(skill)

            process = self.run_cli(
                script,
                [
                    "build",
                    str(config),
                    "--vault",
                    str(vault),
                    "--apply",
                    "--expected-plan-sha256",
                    str(plan["plan_sha256"]),
                ],
                cwd=cwd,
            )

            self.assert_json_error(
                process,
                code="CONTRACT_BUILD_MISMATCH",
                exit_code=7,
                private_root=root,
            )
            self.assertFalse(
                (vault / ".awesome-capture/transactions").exists()
            )
            self.assertFalse(vault.exists(), "builder created its target")

    def test_ingest_copy_rejects_tamper_before_vault_write(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-contract-ingest-"
        ) as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            skill = self.copy_skill(root, "ingest-knowledge")
            cwd = self.private_directory(root / "arbitrary-cwd")
            vault = self.private_directory(root / "Vault")
            self.private_directory(vault / ".obsidian")
            transcript, draft = self.write_ingest_inputs(root)
            script = skill / "scripts/knowledge_writer.py"
            planned = self.run_cli(
                script,
                self.ingest_arguments(
                    transcript,
                    draft,
                    vault,
                    dry_run=True,
                ),
                cwd=cwd,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan = self.single_json_object(planned.stdout, channel="stdout")
            before = self.tree_snapshot(vault)
            self.tamper_contract_manifest(skill)

            process = self.run_cli(
                script,
                self.ingest_arguments(
                    transcript,
                    draft,
                    vault,
                    dry_run=False,
                    expected_plan_sha256=str(plan["plan_sha256"]),
                ),
                cwd=cwd,
            )

            self.assert_json_error(
                process,
                code="CONTRACT_BUILD_MISMATCH",
                exit_code=7,
                private_root=root,
            )
            self.assertEqual(self.tree_snapshot(vault), before)
            self.assertFalse((vault / ".awesome-capture").exists())
            self.assertFalse(Path(plan["knowledge_note"]).exists())
            self.assertFalse(Path(plan["source_note"]).exists())
            self.assertFalse(Path(plan["receipt_path"]).exists())

    def test_ingest_target_change_makes_confirmed_plan_stale(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="awesome-capture-ingest-stale-plan-"
        ) as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            cwd = self.private_directory(root / "arbitrary-cwd")
            vault = self.private_directory(root / "Vault")
            self.private_directory(vault / ".obsidian")
            transcript, draft = self.write_ingest_inputs(root)
            script = CLI_SCRIPTS["ingest-knowledge"]
            arguments = self.ingest_arguments(
                transcript,
                draft,
                vault,
                dry_run=True,
            )
            first_process = self.run_cli(script, arguments, cwd=cwd)
            self.assertEqual(
                first_process.returncode,
                0,
                first_process.stderr,
            )
            first_plan = self.single_json_object(
                first_process.stdout,
                channel="stdout",
            )
            placeholder = Path(first_plan["knowledge_note"])
            placeholder.parent.mkdir(parents=True, mode=0o700)
            placeholder.parent.chmod(0o700)
            placeholder.write_text(
                "foreign occupant must not be overwritten\n",
                encoding="utf-8",
            )
            placeholder.chmod(0o644)
            before = self.file_snapshot(placeholder)

            changed_process = self.run_cli(script, arguments, cwd=cwd)
            self.assertEqual(
                changed_process.returncode,
                0,
                changed_process.stderr,
            )
            changed_plan = self.single_json_object(
                changed_process.stdout,
                channel="stdout",
            )
            self.assertNotEqual(
                first_plan["plan_sha256"],
                changed_plan["plan_sha256"],
            )

            committed = self.run_cli(
                script,
                self.ingest_arguments(
                    transcript,
                    draft,
                    vault,
                    dry_run=False,
                    expected_plan_sha256=str(first_plan["plan_sha256"]),
                ),
                cwd=cwd,
            )

            self.assert_json_error(
                committed,
                code="STALE_PLAN",
                exit_code=4,
                private_root=root,
            )
            self.assertEqual(self.file_snapshot(placeholder), before)
            self.assertFalse(Path(first_plan["source_note"]).exists())
            self.assertFalse(Path(first_plan["receipt_path"]).exists())
            self.assertFalse(
                (vault / ".awesome-capture/transactions").exists()
            )


if __name__ == "__main__":
    unittest.main()
