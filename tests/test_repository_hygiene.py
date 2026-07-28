from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_repository_hygiene.py"


class RepositoryHygieneTests(unittest.TestCase):
    def run_scan(self, root: Path) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root)],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    def test_ignored_files_are_scanned_and_reported_deterministically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ignored = [
                "__pycache__/",
                "*.pyc",
                "*.mp4",
                "*.safetensors",
                "cookies.txt",
                "*.artifact.json",
                "*.state.json",
                ".awesome-capture/",
                ".awesome-capture-media/",
                ".env.*",
                "*.pem",
            ]
            (root / ".gitignore").write_text("\n".join(ignored), encoding="utf-8")
            paths = {
                ".awesome-capture/receipt.json": "{}",
                ".awesome-capture-media/v2/lock": "",
                ".env.production": "TOKEN=private",
                "capture.artifact.json": "{}",
                "clip.mp4": "media",
                "cookies.txt": "secret",
                "credentials.json": "{}",
                "ignored/__pycache__/module.cpython-314.pyc": "bytecode",
                "ignored/orphan.pyc": "bytecode",
                "model.safetensors": "weights",
                "private.pem": "key",
                "run.state.json": "{}",
            }
            for relative, content in paths.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            first = self.run_scan(root)
            second = self.run_scan(root)

            self.assertEqual(first.returncode, 1)
            self.assertEqual(first.stdout, "")
            self.assertEqual(first.stderr, second.stderr)
            payload = json.loads(first.stderr)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(
                payload["error"]["code"],
                "REPOSITORY_HYGIENE_FAILED",
            )
            reported = {
                item["path"]: item["rule"] for item in payload["violations"]
            }
            self.assertEqual(
                reported,
                {
                    ".awesome-capture": "awesome-capture-runtime-directory",
                    ".awesome-capture-media": "awesome-capture-runtime-directory",
                    ".env.production": "secret-environment-file",
                    "capture.artifact.json": "capture-artifact-or-state",
                    "clip.mp4": "media-file",
                    "cookies.txt": "cookie-file",
                    "credentials.json": "secret-file",
                    "ignored/__pycache__": "python-cache-directory",
                    "ignored/orphan.pyc": "python-bytecode",
                    "model.safetensors": "model-file",
                    "private.pem": "secret-file",
                    "run.state.json": "capture-artifact-or-state",
                },
            )
            self.assertEqual(
                payload["violations"],
                sorted(payload["violations"], key=lambda item: item["path"]),
            )

    def test_declared_contract_fixtures_are_the_only_artifact_state_exceptions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = (
                "contracts/fixtures/invalid/legacy-artifact.json",
                "contracts/fixtures/valid/transcript-artifact.json",
                "contracts/fixtures/valid/transcription-state.json",
                "contracts/fixtures/valid/video-artifact.json",
            )
            for relative in allowed:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}", encoding="utf-8")
            (root / ".env.example").write_text("TOKEN=\n", encoding="utf-8")

            clean = self.run_scan(root)

            self.assertEqual(clean.returncode, 0)
            self.assertEqual(clean.stderr, "")
            self.assertEqual(
                json.loads(clean.stdout),
                {"status": "ok", "violation_count": 0, "violations": []},
            )

            undeclared = (
                root / "contracts" / "fixtures" / "valid" / "new-artifact.json"
            )
            undeclared.write_text("{}", encoding="utf-8")
            dirty = self.run_scan(root)

            self.assertEqual(dirty.returncode, 1)
            self.assertEqual(
                json.loads(dirty.stderr)["violations"],
                [
                    {
                        "path": "contracts/fixtures/valid/new-artifact.json",
                        "rule": "capture-artifact-or-state",
                    }
                ],
            )

    def test_non_directory_root_is_a_sanitized_scan_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_root = root / "not-a-directory"
            file_root.write_text("x", encoding="utf-8")

            completed = self.run_scan(file_root)

            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(
                json.loads(completed.stderr),
                {
                    "error": {
                        "code": "REPOSITORY_SCAN_ERROR",
                        "message": "Repository hygiene scan could not be completed.",
                    },
                    "status": "error",
                },
            )
            self.assertNotIn(str(file_root), completed.stderr)


if __name__ == "__main__":
    unittest.main()
