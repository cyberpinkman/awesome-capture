from __future__ import annotations

import io
import re
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
PINNED_ACTION = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"(?:/[A-Za-z0-9_.-]+)?@[0-9a-f]{40}$"
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

        linux_update = "update &&"
        linux_install = "install --yes --no-install-recommends ffmpeg"
        macos_install = "brew install ffmpeg"
        preflight = "- name: Verify required POSIX media tools"

        self.assertIn("max-parallel: 2", linux)
        self.assertIn("timeout-minutes: 13", linux)
        self.assertIn("for attempt in 1 2", linux)
        self.assertIn("timeout --kill-after=10s 45s", linux)
        self.assertIn("timeout --kill-after=10s 300s", linux)
        self.assertNotIn("timeout --kill-after=10s 120s", linux)
        self.assertIn("Acquire::Retries=1", linux)
        self.assertIn("Acquire::http::Timeout=15", linux)
        self.assertIn("Acquire::https::Timeout=15", linux)
        self.assertIn("Dpkg::Lock::Timeout=30", linux)
        self.assertIn("dpkg --configure -a", linux)
        self.assertIn(linux_update, linux)
        self.assertIn(linux_install, linux)
        self.assertLess(linux.index(linux_update), linux.index(linux_install))
        self.assertLess(linux.index(linux_install), linux.index(preflight))
        self.assertIn(macos_install, macos)
        self.assertIn("timeout-minutes: 15", macos)
        self.assertIn('HOMEBREW_NO_AUTO_UPDATE: "1"', macos)
        self.assertLess(macos.index(macos_install), macos.index(preflight))
        self.assertEqual(workflow.count("--github-annotations"), 2)
        self.assertEqual(
            workflow.count("tools/smoke_receipts.py validate-scope"),
            2,
        )
        self.assertEqual(
            workflow.count("tools/smoke_receipts.py validate-existing"),
            2,
        )
        self.assertEqual(workflow.count("fetch-depth: 0"), 2)

    def test_required_check_aggregates_the_complete_matrix(self) -> None:
        workflow = (ROOT / ".github/workflows/tests.yml").read_text(
            encoding="utf-8"
        )
        required = workflow.split("  required:", 1)[1]

        self.assertIn("if: ${{ always() }}", required)
        self.assertIn("needs:\n      - linux\n      - macos-posix", required)
        self.assertIn("LINUX_RESULT: ${{ needs.linux.result }}", required)
        self.assertIn(
            "MACOS_RESULT: ${{ needs.macos-posix.result }}",
            required,
        )
        self.assertIn(
            'if [ "$LINUX_RESULT" != "success" ] || '
            '[ "$MACOS_RESULT" != "success" ]; then',
            required,
        )
        self.assertIn("exit 1", required)

    def test_all_remote_actions_are_pinned_by_sha_with_version_comments(
        self,
    ) -> None:
        workflow_root = ROOT / ".github" / "workflows"
        workflows = sorted(
            {
                *workflow_root.glob("*.yml"),
                *workflow_root.glob("*.yaml"),
            }
        )
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in workflows
        )

        for path in workflows:
            workflow = path.read_text(encoding="utf-8")
            uses_lines = re.findall(
                r"(?m)^\s*uses:\s*([^#\s]+)(?:\s+#\s*(\S.*))?$",
                workflow,
            )
            for action, comment in uses_lines:
                if action.startswith("./"):
                    continue
                with self.subTest(workflow=path.name, action=action):
                    self.assertRegex(action, PINNED_ACTION)
                    self.assertRegex(comment, r"^v[0-9]")

        self.assertIn(CHECKOUT, combined)
        self.assertIn(SETUP_PYTHON, combined)
        self.assertNotIn(
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            combined,
        )
        self.assertNotIn(
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
            combined,
        )

    def test_release_workflow_is_manual_fail_closed_and_idempotent(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        trigger = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
        policy, jobs = workflow.split("jobs:", 1)
        verify, publish = jobs.split("  publish:", 1)

        self.assertIn("workflow_dispatch:", trigger)
        self.assertIn("version:", trigger)
        self.assertNotIn("push:", trigger)
        self.assertNotIn("pull_request:", trigger)
        self.assertIn("permissions:\n  contents: read", policy)
        self.assertNotIn("contents: write", policy)
        self.assertIn("actions: read", verify)
        self.assertIn("contents: read", verify)
        self.assertNotIn("contents: write", verify)
        self.assertIn("contents: write", publish)
        self.assertNotIn("actions: write", workflow)
        self.assertEqual(workflow.count("contents: write"), 1)
        self.assertIn("group: awesome-capture-release", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("continue-on-error:", workflow)
        self.assertNotIn("always()", workflow)

        self.assertEqual(
            workflow.count(
                'if [ "$DISPATCH_REF" != "refs/heads/main" ]; then'
            ),
            2,
        )
        self.assertEqual(workflow.count("fetch-depth: 0"), 2)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        self.assertEqual(workflow.count("ref: ${{ github.sha }}"), 2)
        self.assertEqual(
            workflow.count(
                'if ! [[ "$REQUESTED_VERSION" =~ '
                r"^(0|[1-9][0-9]*)\."
                r"(0|[1-9][0-9]*)\."
                r"(0|[1-9][0-9]*)$ ]]; then"
            ),
            2,
        )

        local_gates = (
            'check-release \\\n            --requested-version "$REQUESTED_VERSION"',
            'notes --output "$RELEASE_NOTES"',
            "python tools/sync_vendored.py --check",
            "python tools/smoke_receipts.py validate-release",
            "python tools/check_repository_hygiene.py",
            "git diff --check",
            "git status --porcelain",
        )
        for gate in local_gates:
            with self.subTest(gate=gate):
                self.assertIn(gate, workflow)
        self.assertNotIn("--require-all-cases", workflow)
        self.assertNotIn("smoke_components:", workflow)
        self.assertNotIn("--scope", verify)

        self.assertIn(
            "actions/workflows/tests.yml/runs",
            workflow,
        )
        self.assertIn('.head_sha == \\"$GITHUB_SHA\\"', workflow)
        self.assertIn("-f head_sha=\"$GITHUB_SHA\"", workflow)
        self.assertGreaterEqual(
            workflow.count("git/ref/heads/main"),
            2,
        )
        self.assertEqual(
            workflow.count("X-GitHub-Api-Version: 2022-11-28"),
            6,
        )
        self.assertIn("state=reused", workflow)
        self.assertIn("state=tag-only", workflow)
        self.assertIn("state=new", workflow)
        self.assertIn(
            "The release tag is not the exact release commit.",
            workflow,
        )
        self.assertIn(
            "Existing release notes do not match CHANGELOG.md.",
            workflow,
        )
        self.assertIn('gh release create "$RELEASE_TAG"', workflow)
        self.assertIn('--target "$GITHUB_SHA"', workflow)
        self.assertIn('--notes-file "$RELEASE_NOTES"', workflow)
        self.assertIn("Verify the published tag and release", workflow)
        self.assertIn(
            "Published notes differ from CHANGELOG.md.",
            workflow,
        )

        token_lines = re.findall(
            r"(?m)^(\s*)GH_TOKEN:\s*\$\{\{ github\.token \}\}$",
            workflow,
        )
        self.assertEqual(len(token_lines), 4)
        self.assertTrue(
            all(indentation == "          " for indentation in token_lines)
        )
        self.assertEqual(workflow.count("${{ github.token }}"), 4)

    def test_public_smoke_installs_media_tools_before_preflight(self) -> None:
        workflow = (ROOT / ".github/workflows/smoke.yml").read_text(
            encoding="utf-8"
        )
        public_download, _ = workflow.split("  local-posix-asr:", 1)
        update = "update &&"
        install = "install --yes --no-install-recommends ffmpeg"
        preflight = "- name: Verify tools without installing credentials"

        self.assertIn("timeout-minutes: 13", public_download)
        self.assertIn("for attempt in 1 2", public_download)
        self.assertIn("timeout --kill-after=10s 45s", public_download)
        self.assertIn("timeout --kill-after=10s 300s", public_download)
        self.assertNotIn("timeout --kill-after=10s 120s", public_download)
        self.assertIn("Acquire::Retries=1", public_download)
        self.assertIn("Dpkg::Lock::Timeout=30", public_download)
        self.assertIn("dpkg --configure -a", public_download)
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

    def test_manual_smoke_is_trusted_ref_gated_and_uploads_only_valid_receipts(
        self,
    ) -> None:
        workflow = (ROOT / ".github/workflows/smoke.yml").read_text(
            encoding="utf-8"
        )
        authorize, smoke_jobs = workflow.split("  public-download:", 1)
        jobs = smoke_jobs.split("  local-posix-asr:", 1)
        public_download = jobs[0]
        local_posix, local_mlx = jobs[1].split("  local-mlx-asr:", 1)
        sections = (public_download, local_posix, local_mlx)

        self.assertIn(
            "Require an original-repository default-branch dispatch",
            authorize,
        )
        self.assertIn("runs-on: ubuntu-24.04", authorize)
        self.assertNotIn("self-hosted", authorize)
        self.assertIn('if [ "$REPOSITORY_IS_FORK" = "true" ]; then', authorize)
        self.assertIn(
            'if [ "$DISPATCH_REF" != "refs/heads/$DEFAULT_BRANCH" ]; then',
            authorize,
        )
        self.assertEqual(workflow.count("needs: authorize"), 3)
        self.assertEqual(
            workflow.count("environment: awesome-capture-smoke"),
            3,
        )
        self.assertEqual(workflow.count("ref: ${{ github.sha }}"), 3)
        self.assertEqual(workflow.count("persist-credentials: false"), 3)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("if: ${{ always() }}", workflow)
        self.assertEqual(workflow.count("if: ${{ !cancelled() }}"), 3)

        validation = (
            "- name: Validate receipt schema, digest, redaction, and outcome"
        )
        upload = "- name: Upload validated sanitized receipt"
        guarded_upload = (
            "if: ${{ !cancelled() && "
            "steps.validate_receipt.outcome == 'success' }}"
        )
        receipt_path = (
            "path: ${{ runner.temp }}/awesome-capture-smoke-receipts/"
            "${{ github.run_id }}-${{ github.run_attempt }}-"
            "${{ github.job }}/*.json"
        )
        self.assertEqual(workflow.count(validation), 3)
        self.assertEqual(workflow.count(upload), 3)
        self.assertEqual(workflow.count(receipt_path), 3)
        self.assertEqual(
            workflow.count(
                "name: smoke-receipt-${{ inputs.case }}-"
                "${{ github.run_id }}-${{ github.run_attempt }}"
            ),
            3,
        )
        self.assertEqual(workflow.count("--require-single"), 3)
        self.assertEqual(
            workflow.count('--require-case "$SMOKE_CASE"'),
            3,
        )
        self.assertEqual(
            workflow.count(
                "${{ github.run_id }}-${{ github.run_attempt }}-"
                "${{ github.job }}"
            ),
            9,
        )
        for section in sections:
            with self.subTest(job=section.splitlines()[0].strip()):
                self.assertIn(
                    'tools/run_smoke.py run "$SMOKE_CASE"',
                    section,
                )
                self.assertIn(validation, section)
                self.assertIn("id: validate_receipt", section)
                self.assertIn("--require-current-digest", section)
                self.assertNotIn("--require-pass", section)
                self.assertIn(upload, section)
                self.assertIn(guarded_upload, section)
                self.assertIn("if-no-files-found: error", section)
                self.assertLess(
                    section.index('tools/run_smoke.py run "$SMOKE_CASE"'),
                    section.index(validation),
                )
                self.assertLess(section.index(validation), section.index(upload))


if __name__ == "__main__":
    unittest.main()
