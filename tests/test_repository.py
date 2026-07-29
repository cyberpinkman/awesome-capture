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
        for name in (
            "README.md",
            "AGENTS.md",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "LICENSE",
            "RELEASING.md",
            "SECURITY.md",
            "VERSION",
            "VERSIONING.md",
        ):
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
                self.assertEqual(
                    (ROOT / "skills" / name / "VERSION").read_text(
                        encoding="utf-8"
                    ),
                    (ROOT / "VERSION").read_text(encoding="utf-8"),
                )

    def test_agent_guide_routes_every_skill(self):
        guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for name in SKILLS:
            with self.subTest(skill=name):
                self.assertIn(name, guide)

    def test_pull_request_governance_is_public_and_complete(self):
        contributing_path = ROOT / "CONTRIBUTING.md"
        template_path = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
        self.assertTrue(contributing_path.is_file())
        self.assertTrue(template_path.is_file())

        contributing = contributing_path.read_text(encoding="utf-8")
        for heading in (
            "## PR 工作流",
            "## 变更证据矩阵",
            "## 安全与公开仓库边界",
            "## Review 与合并规则",
        ):
            with self.subTest(contributing_heading=heading):
                self.assertIn(heading, contributing)

        for required_text in (
            "python3 tools/release.py check",
            "python3 tools/sync_vendored.py --check",
            "python3 tools/run_tests.py --fail-on-skip",
            "python3 tools/check_repository_hygiene.py",
            "git diff --check",
            "不得通过反复 rerun",
            "最终 head SHA",
            "awesome-capture-smoke",
            "self-hosted runner",
            "CHANGELOG.md",
            "VERSIONING.md",
            "RELEASING.md",
            "SECURITY.md",
            "MIT License",
        ):
            with self.subTest(contributing_text=required_text):
                self.assertIn(required_text, contributing)

        template = template_path.read_text(encoding="utf-8")
        for heading in (
            "## 变更摘要",
            "## 验证证据",
            "## 真实 Smoke 证据",
            "## 安全与公开性",
            "## 提交前检查",
        ):
            with self.subTest(template_heading=heading):
                self.assertIn(heading, template)
        self.assertIn("CONTRIBUTING.md", template)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        releasing = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
        self.assertIn("[贡献指南](CONTRIBUTING.md)", readme)
        self.assertIn("CONTRIBUTING.md", agents)
        self.assertIn("CONTRIBUTING.md", releasing)

        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("pull_request:", workflow)


if __name__ == "__main__":
    unittest.main()
