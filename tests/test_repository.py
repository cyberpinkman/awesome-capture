from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    "download-video",
    "transcribe-media",
    "ingest-knowledge",
    "build-obsidian-vault",
)


class RepositoryStructureTests(unittest.TestCase):
    def test_public_repository_documents_exist(self):
        for name in ("README.md", "AGENTS.md", "LICENSE", "SECURITY.md"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text(encoding="utf-8"))

    def test_skill_frontmatter_and_agent_metadata_exist(self):
        for name in SKILLS:
            with self.subTest(skill=name):
                skill_path = ROOT / "skills" / name / "SKILL.md"
                text = skill_path.read_text(encoding="utf-8")
                frontmatter = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
                self.assertIsNotNone(frontmatter)
                header = frontmatter.group(1) if frontmatter else ""
                self.assertRegex(header, rf"(?m)^name:\s*{re.escape(name)}\s*$")
                self.assertRegex(header, r"(?m)^description:\s*\S.+$")
                self.assertTrue(
                    (ROOT / "skills" / name / "agents" / "openai.yaml").is_file()
                )

    def test_agent_guide_routes_every_skill(self):
        guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for name in SKILLS:
            with self.subTest(skill=name):
                self.assertIn(name, guide)


if __name__ == "__main__":
    unittest.main()
