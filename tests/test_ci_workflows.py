from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CiWorkflowTests(unittest.TestCase):
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
