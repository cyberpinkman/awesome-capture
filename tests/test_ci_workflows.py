from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import run_tests  # noqa: E402

CHECKOUT = (
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    " # v7.0.1"
)
SETUP_PYTHON = (
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
    " # v7.0.0"
)


class CiWorkflowTests(unittest.TestCase):
    def test_github_annotations_expose_only_failed_test_identity(self) -> None:
        class SyntheticFailure(unittest.TestCase):
            def runTest(self) -> None:
                pass

        test = SyntheticFailure()
        result = unittest.TestResult()
        result.failures.append((test, "PRIVATE TRACEBACK"))
        stream = io.StringIO()

        run_tests.emit_github_annotations(
            result,
            fail_on_skip=True,
            stream=stream,
        )

        annotation = stream.getvalue()
        self.assertIn("::error title=Unit test failure::", annotation)
        self.assertIn(test.id(), annotation)
        self.assertNotIn("PRIVATE TRACEBACK", annotation)

    def test_test_jobs_install_media_tools_before_preflight(self) -> None:
        workflow = (ROOT / ".github/workflows/tests.yml").read_text(
            encoding="utf-8"
        )
        linux, macos = workflow.split("  macos-posix:", 1)

        linux_install = (
            "sudo apt-get install --yes --no-install-recommends ffmpeg"
        )
        linux_update = "sudo apt-get update"
        macos_install = "brew install ffmpeg"
        preflight = "- name: Verify required POSIX media tools"

        self.assertIn(linux_update, linux)
        self.assertIn(linux_install, linux)
        self.assertLess(linux.index(linux_update), linux.index(linux_install))
        self.assertLess(linux.index(linux_install), linux.index(preflight))
        self.assertIn(macos_install, macos)
        self.assertIn('HOMEBREW_NO_AUTO_UPDATE: "1"', macos)
        self.assertLess(macos.index(macos_install), macos.index(preflight))
        self.assertEqual(workflow.count("--github-annotations"), 2)

    def test_official_actions_use_node24_releases_pinned_by_sha(self) -> None:
        workflows = [
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / ".github/workflows").glob("*.yml"))
        ]
        combined = "\n".join(workflows)

        self.assertEqual(combined.count(CHECKOUT), 5)
        self.assertEqual(combined.count(SETUP_PYTHON), 5)
        self.assertNotIn(
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            combined,
        )
        self.assertNotIn(
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
            combined,
        )

    def test_public_smoke_installs_media_tools_before_preflight(self) -> None:
        workflow = (ROOT / ".github/workflows/smoke.yml").read_text(
            encoding="utf-8"
        )
        public_download, _ = workflow.split("  local-posix-asr:", 1)
        update = "sudo apt-get update"
        install = "sudo apt-get install --yes --no-install-recommends ffmpeg"
        preflight = "- name: Verify tools without installing credentials"

        self.assertIn(update, public_download)
        self.assertIn(install, public_download)
        self.assertLess(
            public_download.index(update),
            public_download.index(install),
        )
        self.assertLess(
            public_download.index(install),
            public_download.index(preflight),
        )


if __name__ == "__main__":
    unittest.main()
