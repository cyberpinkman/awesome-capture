from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONCURRENT_LOCK_TIMEOUT = "30"
CONCURRENT_PROCESS_TIMEOUT = 45


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vault_builder = load_module(
    "vault_builder_v2",
    "skills/build-obsidian-vault/scripts/vault_builder.py",
)
knowledge_writer = load_module(
    "knowledge_writer_v2",
    "skills/ingest-knowledge/scripts/knowledge_writer.py",
)


class VaultReceiptV1Tests(unittest.TestCase):
    def config(self):
        return vault_builder.read_config(
            ROOT
            / "skills"
            / "build-obsidian-vault"
            / "assets"
            / "vault-config.example.json"
        )

    def build(self, vault: Path):
        plan = vault_builder.build_plan(self.config(), vault)
        return vault_builder.build(
            self.config(),
            vault,
            apply=True,
            extend_existing=False,
            expected_plan_sha256=plan["plan_sha256"],
        )

    def test_build_receipt_is_versioned_cross_day_stable_and_auditable(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            first = self.build(vault)
            self.assertEqual(first["result"], "created")
            self.assertEqual(vault.stat().st_mode & 0o777, 0o700)
            home = (vault / "Home.md").read_text(encoding="utf-8")
            self.assertNotRegex(home, r"created: \d{4}-\d{2}-\d{2}")
            receipt = json.loads(
                (vault / ".awesome-capture" / "vault-build.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                receipt["schema_version"],
                "awesome-capture.vault-build-receipt/v1",
            )
            second_plan = vault_builder.build_plan(self.config(), vault)
            second = vault_builder.build(
                self.config(),
                vault,
                apply=True,
                extend_existing=False,
                expected_plan_sha256=second_plan["plan_sha256"],
            )
            self.assertEqual(second["result"], "unchanged")
            audit = vault_builder.audit(vault, require_build_receipt=True)
            self.assertTrue(audit["healthy"], audit)

            (vault / "_Templates" / "Knowledge Note.md").unlink()
            audit = vault_builder.audit(vault, require_build_receipt=True)
            self.assertFalse(audit["healthy"])
            self.assertIn(
                "MISSING_MANAGED_FILE",
                {item["code"] for item in audit["findings"]},
            )

    def test_builder_audit_fails_closed_when_vault_path_is_swapped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            moved = root / "Vault-original"
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            sentinel = outside / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            self.build(vault)
            real_verify = vault_builder.verify_managed_content
            swapped = False

            def verify_then_swap(path, receipt):
                nonlocal swapped
                result = real_verify(path, receipt)
                vault.rename(moved)
                vault.symlink_to(outside, target_is_directory=True)
                swapped = True
                return result

            with mock.patch.object(
                vault_builder,
                "verify_managed_content",
                side_effect=verify_then_swap,
            ):
                result = vault_builder.audit(
                    vault,
                    require_build_receipt=True,
                )

            self.assertTrue(swapped)
            self.assertFalse(result["healthy"])
            self.assertIn(
                "UNSAFE_VAULT_ROOT",
                {item["code"] for item in result["findings"]},
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_existing_nonwritable_vault_root_remains_usable(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            vault.mkdir(mode=0o755)
            vault.chmod(0o755)
            plan = vault_builder.build_plan(self.config(), vault)
            result = vault_builder.build(
                self.config(),
                vault,
                apply=True,
                extend_existing=False,
                expected_plan_sha256=plan["plan_sha256"],
            )
            self.assertEqual(result["result"], "created")
            self.assertEqual(vault.stat().st_mode & 0o777, 0o755)
            self.assertTrue(
                vault_builder.audit(vault, require_build_receipt=True)["healthy"]
            )

    def test_builder_rejects_group_or_world_writable_vault_roots(self):
        operations = ("plan", "build", "recover")
        for mode in (0o770, 0o777):
            for operation in operations:
                with (
                    self.subTest(mode=oct(mode), operation=operation),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary).resolve()
                    vault = root / "Vault"
                    outside = root / "outside"
                    vault.mkdir(mode=0o700)
                    vault.chmod(mode)
                    outside.mkdir(mode=0o700)
                    sentinel = outside / "sentinel"
                    sentinel.write_text("unchanged", encoding="utf-8")

                    with self.assertRaises(vault_builder.VaultError) as raised:
                        if operation == "plan":
                            vault_builder.build_plan(self.config(), vault)
                        elif operation == "build":
                            vault_builder.build(
                                self.config(),
                                vault,
                                apply=True,
                                extend_existing=True,
                                expected_plan_sha256="0" * 64,
                            )
                        else:
                            vault_builder.recover(vault)

                    self.assertEqual(
                        raised.exception.code,
                        "UNSAFE_VAULT_TARGET",
                    )
                    self.assertEqual(
                        sentinel.read_text(encoding="utf-8"),
                        "unchanged",
                    )
                    self.assertEqual(list(vault.iterdir()), [])

            with (
                self.subTest(mode=oct(mode), operation="audit"),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                vault = root / "Vault"
                outside = root / "outside"
                vault.mkdir(mode=0o700)
                vault.chmod(mode)
                outside.mkdir(mode=0o700)
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged", encoding="utf-8")
                result = vault_builder.audit(
                    vault,
                    require_build_receipt=True,
                )
                self.assertFalse(result["healthy"])
                self.assertIn(
                    "UNSAFE_VAULT_ROOT",
                    {item["code"] for item in result["findings"]},
                )
                self.assertEqual(
                    sentinel.read_text(encoding="utf-8"),
                    "unchanged",
                )
                self.assertEqual(list(vault.iterdir()), [])

    def test_builder_rejects_vault_root_not_owned_by_current_user(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            vault.mkdir(mode=0o700)
            config = self.config()
            current_uid = os.geteuid()
            with mock.patch.object(
                vault_builder.os,
                "geteuid",
                return_value=current_uid + 1,
            ):
                with self.assertRaises(vault_builder.VaultError) as raised:
                    vault_builder.build_plan(config, vault)
            self.assertEqual(raised.exception.code, "UNSAFE_VAULT_TARGET")

    def test_apply_rejects_stale_plan_and_legacy_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            with self.assertRaises(vault_builder.VaultError) as raised:
                vault_builder.build(
                    self.config(),
                    vault,
                    apply=True,
                    extend_existing=False,
                    expected_plan_sha256="0" * 64,
                )
            self.assertEqual(raised.exception.code, "STALE_PLAN")

            vault.mkdir()
            metadata = vault / ".awesome-capture"
            metadata.mkdir(mode=0o700)
            legacy_receipt = metadata / "vault-build.json"
            legacy_receipt.write_text(
                json.dumps(
                    {
                        "schema_version": "awesome-capture.vault-config/v1",
                        "config_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(legacy_receipt, 0o600)
            plan = vault_builder.build_plan(self.config(), vault)
            with self.assertRaises(vault_builder.VaultError) as raised:
                vault_builder.build(
                    self.config(),
                    vault,
                    apply=True,
                    extend_existing=True,
                    expected_plan_sha256=plan["plan_sha256"],
                )
            self.assertEqual(raised.exception.code, "UNSUPPORTED_RECEIPT_SCHEMA")

    def test_matching_hardlinked_markdown_is_a_prepublication_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            vault.mkdir()
            outside = root / "outside-home.md"
            content = vault_builder.desired_files(self.config())[Path("Home.md")]
            outside.write_text(content, encoding="utf-8")
            os.chmod(outside, 0o644)
            os.link(outside, vault / "Home.md")
            plan = vault_builder.build_plan(self.config(), vault)
            self.assertIn("Home.md", plan["conflicts"])
            with self.assertRaises(vault_builder.VaultError) as raised:
                vault_builder.build(
                    self.config(),
                    vault,
                    apply=True,
                    extend_existing=True,
                    expected_plan_sha256=plan["plan_sha256"],
                )
            self.assertEqual(raised.exception.code, "BUILD_CONFLICT")
            self.assertFalse(
                (vault / ".awesome-capture" / "vault-build.json").exists()
            )
            self.assertEqual(outside.read_text(encoding="utf-8"), content)

    def test_symlinked_managed_directory_never_touches_outside(self):
        if not hasattr(os, "symlink"):
            self.fail("POSIX suite requires symlink support")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            outside = root / "outside"
            sentinel = outside / "sentinel"
            vault.mkdir()
            outside.mkdir()
            sentinel.write_text("unchanged", encoding="utf-8")
            os.symlink(outside, vault / "00 Inbox")
            plan = vault_builder.build_plan(self.config(), vault)
            with self.assertRaises(vault_builder.VaultError):
                vault_builder.build(
                    self.config(),
                    vault,
                    apply=True,
                    extend_existing=True,
                    expected_plan_sha256=plan["plan_sha256"],
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_concurrent_identical_build_creates_once_then_reuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(self.config(), ensure_ascii=False),
                encoding="utf-8",
            )
            plan = vault_builder.build_plan(self.config(), vault)
            command = [
                sys.executable,
                str(
                    ROOT
                    / "skills"
                    / "build-obsidian-vault"
                    / "scripts"
                    / "vault_builder.py"
                ),
                "build",
                str(config_path),
                "--vault",
                str(vault),
                "--apply",
                "--expected-plan-sha256",
                plan["plan_sha256"],
                "--lock-timeout",
                CONCURRENT_LOCK_TIMEOUT,
            ]
            processes = [
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)
            ]
            results = [
                process.communicate(timeout=CONCURRENT_PROCESS_TIMEOUT)
                for process in processes
            ]
            self.assertEqual([process.returncode for process in processes], [0, 0], results)
            payloads = [json.loads(stdout) for stdout, _ in results]
            self.assertEqual(
                sorted(payload["result"] for payload in payloads),
                ["created", "unchanged"],
            )

    def test_lock_scaffolding_is_not_existing_vault_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            vault.mkdir(mode=0o700)
            metadata = vault / ".awesome-capture"
            metadata.mkdir(mode=0o700)
            lock = metadata / "vault.lock"
            lock.write_bytes(b"")
            lock.chmod(0o600)

            plan = vault_builder.build_plan(self.config(), vault)
            self.assertEqual(plan["root_state"], "empty")
            result = vault_builder.build(
                self.config(),
                vault,
                apply=True,
                extend_existing=False,
                expected_plan_sha256=plan["plan_sha256"],
            )
            self.assertEqual(result["result"], "created")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            vault.mkdir(mode=0o700)
            metadata = vault / ".awesome-capture"
            metadata.mkdir(mode=0o700)
            lock = metadata / "vault.lock"
            lock.write_bytes(b"")
            lock.chmod(0o600)
            unknown = metadata / "unknown"
            unknown.write_bytes(b"external")
            unknown.chmod(0o600)

            plan = vault_builder.build_plan(self.config(), vault)
            self.assertEqual(plan["root_state"], "existing")
            with self.assertRaises(vault_builder.VaultError) as raised:
                vault_builder.build(
                    self.config(),
                    vault,
                    apply=True,
                    extend_existing=False,
                    expected_plan_sha256=plan["plan_sha256"],
                )
            self.assertEqual(
                raised.exception.code,
                "EXISTING_VAULT_REQUIRES_OPT_IN",
            )
            self.assertEqual(unknown.read_bytes(), b"external")

    def test_concurrent_different_build_configs_create_one_consistent_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            first_config = self.config()
            second_config = json.loads(json.dumps(first_config))
            second_config["name"] = "Conflicting Knowledge Base"
            config_paths = [root / "first-config.json", root / "second-config.json"]
            configs = [first_config, second_config]
            for path, config in zip(config_paths, configs):
                path.write_text(
                    json.dumps(config, ensure_ascii=False),
                    encoding="utf-8",
                )
            plans = [
                vault_builder.build_plan(config, vault)
                for config in configs
            ]

            def command(config_path: Path, plan_sha256: str) -> list[str]:
                return [
                    sys.executable,
                    str(
                        ROOT
                        / "skills"
                        / "build-obsidian-vault"
                        / "scripts"
                        / "vault_builder.py"
                    ),
                    "build",
                    str(config_path),
                    "--vault",
                    str(vault),
                    "--apply",
                    "--expected-plan-sha256",
                    plan_sha256,
                    "--lock-timeout",
                    CONCURRENT_LOCK_TIMEOUT,
                ]

            processes = [
                subprocess.Popen(
                    command(path, plan["plan_sha256"]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                for path, plan in zip(config_paths, plans)
            ]
            results = [
                process.communicate(timeout=CONCURRENT_PROCESS_TIMEOUT)
                for process in processes
            ]
            self.assertEqual(
                sorted(process.returncode for process in processes),
                [0, 4],
                results,
            )
            successful = [
                json.loads(stdout)
                for process, (stdout, stderr) in zip(processes, results)
                if process.returncode == 0
                and not stderr
            ]
            failed = [
                json.loads(stderr)
                for process, (stdout, stderr) in zip(processes, results)
                if process.returncode == 4
                and not stdout
            ]
            self.assertEqual(len(successful), 1, results)
            self.assertEqual(len(failed), 1, results)
            self.assertEqual(successful[0]["result"], "created")
            self.assertEqual(failed[0]["error"]["code"], "BUILD_CONFLICT")

            receipt_path = vault / ".awesome-capture" / "vault-build.json"
            receipt = vault_builder.read_build_receipt(receipt_path)
            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertEqual(
                receipt["config_sha256"],
                successful[0]["config_sha256"],
            )
            self.assertEqual(
                receipt["plan_sha256"],
                successful[0]["plan_sha256"],
            )
            self.assertIn(
                receipt["config_sha256"],
                {vault_builder.config_digest(config) for config in configs},
            )
            self.assertTrue(
                vault_builder.audit(vault, require_build_receipt=True)["healthy"]
            )
            self.assertEqual(
                list((vault / ".awesome-capture").glob("vault-build.json")),
                [receipt_path],
            )
            transactions = vault / ".awesome-capture" / "transactions"
            self.assertFalse(transactions.exists() and any(transactions.iterdir()))

    def test_crash_after_partial_publish_is_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            plan = vault_builder.build_plan(self.config(), vault)
            original = vault_builder.publish_relative
            calls = 0

            def fail_after_first(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected publish failure")
                return original(*args, **kwargs)

            with mock.patch.object(
                vault_builder,
                "publish_relative",
                side_effect=fail_after_first,
            ):
                with self.assertRaises(OSError):
                    vault_builder.build(
                        self.config(),
                        vault,
                        apply=True,
                        extend_existing=False,
                        expected_plan_sha256=plan["plan_sha256"],
                    )
            self.assertFalse(vault_builder.audit(vault)["healthy"])
            recovered = vault_builder.recover(vault)
            self.assertTrue(recovered["recovered"])
            self.assertTrue(
                vault_builder.audit(vault, require_build_receipt=True)["healthy"]
            )

    def test_build_crash_during_partial_cleanup_resumes_from_complete_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            plan = vault_builder.build_plan(self.config(), vault)

            def fail_before_quarantine(root, transaction, journal, completed):
                raise OSError("injected cleanup failure")

            with mock.patch.object(
                vault_builder,
                "_cleanup_completed_transaction",
                side_effect=fail_before_quarantine,
            ):
                with self.assertRaises(OSError):
                    vault_builder.build(
                        self.config(),
                        vault,
                        apply=True,
                        extend_existing=False,
                        expected_plan_sha256=plan["plan_sha256"],
                    )

            transactions = list(
                (vault / ".awesome-capture" / "transactions").glob("build-*")
            )
            self.assertEqual(len(transactions), 1)
            journal = json.loads(
                (transactions[0] / "journal.json").read_text(encoding="utf-8")
            )
            completed = json.loads(
                (transactions[0] / ".journal-complete.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(journal["status"], "publishing")
            self.assertEqual(completed["status"], "complete")
            self.assertTrue(
                all(step["status"] == "published" for step in completed["steps"])
            )
            self.assertEqual(
                {item.name for item in transactions[0].iterdir()},
                {"journal.json", ".journal-complete.json"},
            )

            recovered = vault_builder.recover(vault)
            self.assertEqual(recovered["recovered"], [transactions[0].name])
            self.assertFalse(transactions[0].exists())
            self.assertTrue(
                (
                    vault
                    / ".awesome-capture"
                    / "quarantine"
                    / f"completed-{transactions[0].name}"
                ).is_dir()
            )
            self.assertTrue(
                vault_builder.audit(vault, require_build_receipt=True)["healthy"]
            )

    def test_build_cleanup_recovery_rejects_symlink_without_touching_outside(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            plan = vault_builder.build_plan(self.config(), vault)

            def fail_before_cleanup(root_path, transaction, journal, completed):
                raise OSError("injected cleanup failure")

            with mock.patch.object(
                vault_builder,
                "_cleanup_completed_transaction",
                side_effect=fail_before_cleanup,
            ):
                with self.assertRaises(OSError):
                    vault_builder.build(
                        self.config(),
                        vault,
                        apply=True,
                        extend_existing=False,
                        expected_plan_sha256=plan["plan_sha256"],
                    )

            transaction = next(
                (vault / ".awesome-capture" / "transactions").glob("build-*")
            )
            completion = transaction / ".journal-complete.json"
            completion.unlink()
            sentinel = root / "outside-sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            completion.symlink_to(sentinel)

            with self.assertRaises(vault_builder.VaultError) as raised:
                vault_builder.recover(vault)
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertTrue(completion.is_symlink())

    def test_build_receipt_symlink_hardlink_and_forged_digest_are_unhealthy(self):
        cases = ("symlink", "hardlink", "mode-0400", "mode-0700", "forged-digest")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                vault = root / "Vault"
                self.build(vault)
                receipt = vault / ".awesome-capture" / "vault-build.json"
                if case == "symlink":
                    outside = root / "outside-build-receipt.json"
                    receipt.replace(outside)
                    os.symlink(outside, receipt)
                    expected_code = "UNSAFE_BUILD_RECEIPT"
                elif case == "hardlink":
                    os.link(receipt, root / "outside-build-receipt.json")
                    expected_code = "UNSAFE_BUILD_RECEIPT"
                elif case.startswith("mode-"):
                    receipt.chmod(int(case.removeprefix("mode-"), 8))
                    expected_code = "UNSAFE_BUILD_RECEIPT"
                else:
                    value = json.loads(receipt.read_text(encoding="utf-8"))
                    value["producer"]["contract_digest"] = "0" * 64
                    receipt.write_text(
                        json.dumps(value, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    expected_code = "CONTRACT_BUILD_MISMATCH"

                audit = vault_builder.audit(vault, require_build_receipt=True)
                self.assertFalse(audit["healthy"], audit)
                self.assertIn(
                    expected_code,
                    {item["code"] for item in audit["findings"]},
                    audit,
                )

    def test_recover_rejects_unknown_transaction_file_without_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            self.build(vault)
            transaction_id = "123e4567-e89b-12d3-a456-426614174000"
            transaction = (
                vault
                / ".awesome-capture"
                / "transactions"
                / f"build-{transaction_id}"
            )
            transaction.mkdir(mode=0o700)
            staged = transaction / "pending.md"
            staged.write_bytes(b"pending content")
            journal = {
                "schema_version": "awesome-capture.transaction/v1",
                "transaction_id": transaction_id,
                "kind": "vault-build",
                "status": "publishing",
                "created_at": "2026-07-27T00:00:00+00:00",
                "job_id": "adversarial-recovery",
                "root": str(vault),
                "staging_root": str(transaction),
                "steps": [
                    {
                        "index": 0,
                        "operation": "publish-file",
                        "source": staged.name,
                        "destination": "Pending.md",
                        "bytes": staged.stat().st_size,
                        "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                        "status": "pending",
                    }
                ],
            }
            (transaction / "journal.json").write_text(
                json.dumps(journal, ensure_ascii=False),
                encoding="utf-8",
            )
            rogue = transaction / "unknown.bin"
            rogue.write_bytes(b"must not be removed")

            with self.assertRaises(vault_builder.VaultError) as raised:
                vault_builder.recover(vault)
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertFalse((vault / "Pending.md").exists())
            self.assertEqual(rogue.read_bytes(), b"must not be removed")
            self.assertTrue(transaction.is_dir())

    def test_ingest_audit_times_out_behind_shared_vault_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            self.build(vault)
            command = [
                sys.executable,
                str(
                    ROOT
                    / "skills"
                    / "ingest-knowledge"
                    / "scripts"
                    / "knowledge_writer.py"
                ),
                "audit",
                "--vault",
                str(vault),
                "--lock-timeout",
                "0",
            ]
            with vault_builder.vault_lock(
                vault,
                exclusive=True,
                timeout=1,
                create=False,
            ):
                process = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    timeout=10,
                )
            self.assertEqual(process.returncode, 4, process)
            self.assertEqual(process.stdout, "")
            error = json.loads(process.stderr)
            self.assertEqual(error["error"]["code"], "VAULT_BUSY")

    def test_audits_report_pending_transaction_without_repairing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            self.build(vault)
            pending = (
                vault
                / ".awesome-capture"
                / "transactions"
                / "ingest-123e4567-e89b-12d3-a456-426614174000"
            )
            pending.mkdir(mode=0o700)
            marker = pending / "unknown.bin"
            marker.write_bytes(b"pending")

            build_audit = vault_builder.audit(vault, require_build_receipt=True)
            ingest_audit = knowledge_writer.audit(vault)
            self.assertFalse(build_audit["healthy"], build_audit)
            self.assertFalse(ingest_audit["healthy"], ingest_audit)
            self.assertIn(
                "RECOVERY_REQUIRED",
                {item["code"] for item in build_audit["findings"]},
            )
            self.assertIn(
                "RECOVERY_REQUIRED",
                {item["code"] for item in ingest_audit["findings"]},
            )
            self.assertEqual(marker.read_bytes(), b"pending")

    def test_audits_do_not_repair_lock_mode_and_reject_metadata_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build(vault)
            lock = vault / ".awesome-capture" / "vault.lock"
            os.chmod(lock, 0o644)
            build_audit = vault_builder.audit(vault, require_build_receipt=True)
            ingest_audit = knowledge_writer.audit(vault)
            self.assertFalse(build_audit["healthy"])
            self.assertFalse(ingest_audit["healthy"])
            self.assertEqual(lock.stat().st_mode & 0o777, 0o644)

            other_vault = root / "Other"
            outside = root / "outside-metadata"
            other_vault.mkdir()
            (other_vault / ".obsidian").mkdir()
            outside.mkdir()
            (other_vault / ".awesome-capture").symlink_to(
                outside,
                target_is_directory=True,
            )
            self.assertFalse(vault_builder.audit(other_vault)["healthy"])
            self.assertFalse(knowledge_writer.audit(other_vault)["healthy"])

    def test_nested_managed_directories_are_private_and_intermediate_symlink_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            config = json.loads(json.dumps(self.config()))
            config["folders"].append("Nested/Child")
            plan = vault_builder.build_plan(config, vault)
            vault_builder.build(
                config,
                vault,
                apply=True,
                extend_existing=False,
                expected_plan_sha256=plan["plan_sha256"],
            )
            for directory in (vault / "Nested", vault / "Nested" / "Child"):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

            outside = root / "outside"
            outside.mkdir(mode=0o700)
            sentinel = outside / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            (vault / "Nested").rename(vault / "Nested-original")
            (vault / "Nested").symlink_to(outside, target_is_directory=True)
            audited = vault_builder.audit(vault, require_build_receipt=True)
            self.assertFalse(audited["healthy"])
            self.assertIn(
                "UNSAFE_MANAGED_DIRECTORY",
                {item["code"] for item in audited["findings"]},
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_audits_report_missing_lock_and_markdown_mode_without_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            self.build(vault)
            home = vault / "Home.md"
            home.chmod(0o666)
            mode_audit = vault_builder.audit(vault, require_build_receipt=True)
            self.assertFalse(mode_audit["healthy"])
            self.assertIn(
                "UNSAFE_MANAGED_FILE",
                {item["code"] for item in mode_audit["findings"]},
            )
            self.assertEqual(home.stat().st_mode & 0o777, 0o666)

            lock = vault / ".awesome-capture" / "vault.lock"
            lock.unlink()
            builder_audit = vault_builder.audit(vault, require_build_receipt=True)
            ingest_audit = knowledge_writer.audit(vault)
            self.assertFalse(builder_audit["healthy"])
            self.assertFalse(ingest_audit["healthy"])
            self.assertIn(
                "MISSING_LOCK",
                {item["code"] for item in builder_audit["findings"]},
            )
            self.assertIn(
                "MISSING_LOCK",
                {item["code"] for item in ingest_audit["findings"]},
            )
            self.assertFalse(lock.exists())

    def test_vault_locks_reject_nonfinite_timeouts(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve() / "Vault"
            vault.mkdir(mode=0o700)
            for module, error_type in (
                (vault_builder, vault_builder.VaultError),
                (knowledge_writer, knowledge_writer.IngestError),
            ):
                for timeout in (float("nan"), float("inf"), -1.0):
                    with self.subTest(module=module.__name__, timeout=timeout):
                        with self.assertRaises(error_type) as raised:
                            with module.vault_lock(
                                vault,
                                exclusive=True,
                                timeout=timeout,
                                create=True,
                            ):
                                pass
                        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")


class IngestReceiptV1Tests(unittest.TestCase):
    def config(self):
        return vault_builder.read_config(
            ROOT
            / "skills"
            / "build-obsidian-vault"
            / "assets"
            / "vault-config.example.json"
        )

    def transcript(self, path: Path) -> Path:
        value = json.loads(
            (ROOT / "contracts" / "fixtures" / "valid" / "transcript-artifact.json").read_text(
                encoding="utf-8"
            )
        )
        value["source"].update(
            {
                "path": "/media/may-be-deleted.mp4",
                "snapshot_path": "/private/snapshot.mp4",
                "bytes": 123,
                "duration_ms": 12_000,
            }
        )
        value["segments"] = [
            {
                "start_ms": 0,
                "end_ms": 3200,
                "text": "可核查的原始观点",
                "chunk_index": 0,
            }
        ]
        value["text"] = "可核查的原始观点"
        value["producer"]["contract_digest"] = knowledge_writer.contract_digest()
        self.refresh_transcript_identity(value)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
        return path

    def refresh_transcript_identity(self, value: dict) -> None:
        source = value["source"]
        transcription = value["transcription"]
        settings = {
            "contract_digest": value["producer"]["contract_digest"],
            "algorithm": transcription["algorithm"],
            "source_path": source["path"],
            "source_sha256": source["sha256"],
            "source_bytes": source["bytes"],
            "upstream_artifact_sha256": (
                source["upstream"]["artifact_sha256"]
                if source["upstream"] is not None
                else None
            ),
            "engine": transcription["engine"],
            "engine_identity": transcription["engine_identity"],
            "requested_language": transcription["requested_language"],
            "chunk_seconds": transcription["chunk_seconds"],
            "whisper_cpp_cpu_only": transcription["whisper_cpp_cpu_only"],
            "sidecar_sha256": (
                source["sidecar"]["sha256"]
                if source["sidecar"] is not None
                else None
            ),
        }
        identity_settings = {
            "contract_digest": settings["contract_digest"],
            "algorithm": settings["algorithm"],
            "source_sha256": settings["source_sha256"],
            "source_bytes": settings["source_bytes"],
            "upstream_artifact_sha256": settings["upstream_artifact_sha256"],
            "engine": settings["engine"],
            "engine_identity_sha256": settings["engine_identity"][
                "identity_sha256"
            ],
            "requested_language": settings["requested_language"],
            "chunk_seconds": settings["chunk_seconds"],
            "whisper_cpp_cpu_only": settings["whisper_cpp_cpu_only"],
            "sidecar_sha256": settings["sidecar_sha256"],
        }
        encoded = json.dumps(
            identity_settings,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        settings_sha256 = hashlib.sha256(encoded).hexdigest()
        transcription["settings_sha256"] = settings_sha256
        transcription["job_id"] = hashlib.sha256(
            b"awesome-capture.transcription-job/v2\0"
            + settings_sha256.encode("ascii")
        ).hexdigest()

    def test_ingest_rejects_nonprivate_transcript_artifact_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript = self.transcript(root / "transcript.json")
            transcript.chmod(0o644)
            draft = self.draft(root / "draft.md")
            with self.assertRaises(knowledge_writer.IngestError) as raised:
                knowledge_writer.commit(self.args(transcript, draft, vault))
            self.assertEqual(raised.exception.code, "UNSAFE_INPUT")

    def test_ingest_rejects_group_or_world_writable_vault_roots(self):
        for mode in (0o770, 0o777):
            for operation in ("commit", "recover"):
                with (
                    self.subTest(mode=oct(mode), operation=operation),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary).resolve()
                    vault = root / "Vault"
                    outside = root / "outside"
                    vault.mkdir(mode=0o700)
                    vault.chmod(mode)
                    outside.mkdir(mode=0o700)
                    sentinel = outside / "sentinel"
                    sentinel.write_text("unchanged", encoding="utf-8")
                    transcript = self.transcript(root / "transcript.json")
                    draft = self.draft(root / "draft.md")

                    with self.assertRaises(
                        knowledge_writer.IngestError
                    ) as raised:
                        if operation == "commit":
                            knowledge_writer.commit(
                                self.args(
                                    transcript,
                                    draft,
                                    vault,
                                    allow_plain_folder=True,
                                )
                            )
                        else:
                            knowledge_writer.recover(vault)

                    self.assertEqual(
                        raised.exception.code,
                        "UNSAFE_VAULT_TARGET",
                    )
                    self.assertEqual(
                        sentinel.read_text(encoding="utf-8"),
                        "unchanged",
                    )
                    self.assertEqual(list(vault.iterdir()), [])

            with (
                self.subTest(mode=oct(mode), operation="audit"),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                vault = root / "Vault"
                outside = root / "outside"
                vault.mkdir(mode=0o700)
                vault.chmod(mode)
                outside.mkdir(mode=0o700)
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged", encoding="utf-8")
                result = knowledge_writer.audit(vault)
                self.assertFalse(result["healthy"])
                self.assertIn(
                    "UNSAFE_VAULT_ROOT",
                    {item["code"] for item in result["findings"]},
                )
                self.assertEqual(
                    sentinel.read_text(encoding="utf-8"),
                    "unchanged",
                )
                self.assertEqual(list(vault.iterdir()), [])

    def draft(self, path: Path) -> Path:
        path.write_text(
            "# 测试知识\n\n"
            "## 核心结论\n\n"
            "- 原材料提出一个可核查观点。证据：[00:00:00.000–00:00:03.200]\n\n"
            "## 待验证\n\n"
            "- 该观点的外部有效性仍需验证。\n",
            encoding="utf-8",
        )
        return path

    def build_vault(self, vault: Path):
        plan = vault_builder.build_plan(self.config(), vault)
        vault_builder.build(
            self.config(),
            vault,
            apply=True,
            extend_existing=False,
            expected_plan_sha256=plan["plan_sha256"],
        )

    def test_ingest_audit_fails_closed_when_vault_path_is_swapped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            moved = root / "Vault-original"
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            sentinel = outside / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            self.build_vault(vault)
            real_discover = knowledge_writer.managed_note_identities
            swapped = False

            def discover_after_swap(path):
                nonlocal swapped
                vault.rename(moved)
                vault.symlink_to(outside, target_is_directory=True)
                swapped = True
                return real_discover(path)

            with mock.patch.object(
                knowledge_writer,
                "managed_note_identities",
                side_effect=discover_after_swap,
            ):
                result = knowledge_writer.audit(vault)

            self.assertTrue(swapped)
            self.assertFalse(result["healthy"])
            self.assertIn(
                "UNSAFE_VAULT_ROOT",
                {item["code"] for item in result["findings"]},
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def args(self, transcript: Path, draft: Path, vault: Path, **overrides):
        values = {
            "transcript": str(transcript),
            "document": str(draft),
            "vault": str(vault),
            "title": "测试知识",
            "collection": "00 Inbox",
            "sources_dir": "90 Sources",
            "tag": ["测试"],
            "allow_plain_folder": False,
            "link_style": "auto",
            "dry_run": True,
            "expected_plan_sha256": None,
            "verify_source_media": False,
            "lock_timeout": 2.0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_deleted_source_ingests_reuses_and_reports_user_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript = self.transcript(root / "transcript.json")
            draft = self.draft(root / "draft.md")
            dry = knowledge_writer.commit(self.args(transcript, draft, vault))
            created = knowledge_writer.commit(
                self.args(
                    transcript,
                    draft,
                    vault,
                    dry_run=False,
                    expected_plan_sha256=dry["plan_sha256"],
                )
            )
            self.assertEqual(created["result"], "created")
            receipt = json.loads(Path(created["receipt_path"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], "awesome-capture.ingest-receipt/v1")
            self.assertEqual(len(receipt["ingest_id"]), 64)
            self.assertEqual(receipt["source_media_verification"], "not_checked")

            dry_again = knowledge_writer.commit(self.args(transcript, draft, vault))
            reused = knowledge_writer.commit(
                self.args(
                    transcript,
                    draft,
                    vault,
                    dry_run=False,
                    expected_plan_sha256=dry_again["plan_sha256"],
                )
            )
            self.assertEqual(reused["result"], "reused")
            note = Path(reused["knowledge_note"])
            note.write_text(note.read_text(encoding="utf-8") + "\n用户补充\n", encoding="utf-8")
            reused_modified = knowledge_writer.commit(
                self.args(
                    transcript,
                    draft,
                    vault,
                    dry_run=False,
                    expected_plan_sha256=dry_again["plan_sha256"],
                )
            )
            self.assertTrue(
                any(item.startswith("CONTENT_MODIFIED:") for item in reused_modified["warnings"])
            )
            audit = knowledge_writer.audit(vault)
            self.assertTrue(audit["healthy"])
            self.assertFalse(audit["clean"])

    def test_nested_ingest_directories_are_private_and_note_mode_drift_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript = self.transcript(root / "transcript.json")
            draft = self.draft(root / "draft.md")
            dry = knowledge_writer.commit(
                self.args(
                    transcript,
                    draft,
                    vault,
                    collection="Nested/Knowledge",
                    sources_dir="Nested/Sources",
                )
            )
            created = knowledge_writer.commit(
                self.args(
                    transcript,
                    draft,
                    vault,
                    collection="Nested/Knowledge",
                    sources_dir="Nested/Sources",
                    dry_run=False,
                    expected_plan_sha256=dry["plan_sha256"],
                )
            )
            for directory in (
                vault / "Nested",
                vault / "Nested" / "Knowledge",
                vault / "Nested" / "Sources",
            ):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            note = Path(created["knowledge_note"])
            note.chmod(0o666)
            audited = knowledge_writer.audit(vault)
            self.assertFalse(audited["healthy"])
            self.assertIn(
                "UNSAFE_NOTE_MODE",
                {item["code"] for item in audited["findings"]},
            )
            self.assertEqual(note.stat().st_mode & 0o777, 0o666)

    def test_reused_ingest_honors_explicit_source_media_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript_path = self.transcript(root / "transcript.json")
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            media = root / "source.bin"
            media.write_bytes(b"original source media")
            transcript["source"]["path"] = str(media)
            transcript["source"]["bytes"] = media.stat().st_size
            transcript["source"]["sha256"] = hashlib.sha256(
                media.read_bytes()
            ).hexdigest()
            self.refresh_transcript_identity(transcript)
            transcript_path.write_text(
                json.dumps(transcript, ensure_ascii=False),
                encoding="utf-8",
            )
            draft = self.draft(root / "draft.md")
            dry = knowledge_writer.commit(
                self.args(transcript_path, draft, vault)
            )
            knowledge_writer.commit(
                self.args(
                    transcript_path,
                    draft,
                    vault,
                    dry_run=False,
                    expected_plan_sha256=dry["plan_sha256"],
                )
            )
            media.write_bytes(b"changed source media")
            reuse_plan = knowledge_writer.commit(
                self.args(
                    transcript_path,
                    draft,
                    vault,
                    verify_source_media=True,
                )
            )
            with self.assertRaises(knowledge_writer.IngestError) as raised:
                knowledge_writer.commit(
                    self.args(
                        transcript_path,
                        draft,
                        vault,
                        dry_run=False,
                        expected_plan_sha256=reuse_plan["plan_sha256"],
                        verify_source_media=True,
                    )
                )
            self.assertEqual(raised.exception.code, "SOURCE_INTEGRITY_FAILED")

    def test_audit_requires_identity_in_real_frontmatter_and_rejects_parent_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for case in ("body-only-identity", "parent-symlink"):
                with self.subTest(case=case):
                    vault = root / f"Vault-{case}"
                    self.build_vault(vault)
                    transcript = self.transcript(root / f"{case}-transcript.json")
                    draft = self.draft(root / f"{case}-draft.md")
                    dry = knowledge_writer.commit(self.args(transcript, draft, vault))
                    created = knowledge_writer.commit(
                        self.args(
                            transcript,
                            draft,
                            vault,
                            dry_run=False,
                            expected_plan_sha256=dry["plan_sha256"],
                        )
                    )
                    receipt = json.loads(
                        Path(created["receipt_path"]).read_text(encoding="utf-8")
                    )
                    note = Path(created["knowledge_note"])
                    if case == "body-only-identity":
                        lines = note.read_text(encoding="utf-8").splitlines()
                        identity_lines = [
                            line
                            for line in lines
                            if line.startswith(("awesome_capture_id:", "source_sha256:"))
                        ]
                        lines = [
                            line
                            for line in lines
                            if not line.startswith(("awesome_capture_id:", "source_sha256:"))
                        ]
                        note.write_text(
                            "\n".join([*lines, "", *identity_lines, ""]),
                            encoding="utf-8",
                        )
                    else:
                        collection = vault / "00 Inbox"
                        outside = root / f"outside-{case}"
                        shutil.move(str(collection), str(outside))
                        collection.symlink_to(outside, target_is_directory=True)
                    audit = knowledge_writer.audit(vault)
                    self.assertFalse(audit["healthy"], (case, audit, receipt))
                    self.assertIn(
                        "BROKEN_RECEIPT_TARGET",
                        {item["code"] for item in audit["findings"]},
                    )

    def test_ingest_crash_during_partial_cleanup_resumes_from_complete_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript = self.transcript(root / "transcript.json")
            draft = self.draft(root / "draft.md")
            dry = knowledge_writer.commit(self.args(transcript, draft, vault))

            def fail_before_quarantine(
                root_path,
                transaction,
                journal,
                completed,
            ):
                raise OSError("injected cleanup failure")

            with mock.patch.object(
                knowledge_writer,
                "_cleanup_completed_transaction",
                side_effect=fail_before_quarantine,
            ):
                with self.assertRaises(OSError):
                    knowledge_writer.commit(
                        self.args(
                            transcript,
                            draft,
                            vault,
                            dry_run=False,
                            expected_plan_sha256=dry["plan_sha256"],
                        )
                    )

            transactions = list(
                (vault / ".awesome-capture" / "transactions").glob("ingest-*")
            )
            self.assertEqual(len(transactions), 1)
            journal = json.loads(
                (transactions[0] / "journal.json").read_text(encoding="utf-8")
            )
            completed = json.loads(
                (transactions[0] / ".journal-complete.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(journal["status"], "publishing")
            self.assertEqual(completed["status"], "complete")
            self.assertTrue(
                all(step["status"] == "published" for step in completed["steps"])
            )
            self.assertEqual(
                {item.name for item in transactions[0].iterdir()},
                {"journal.json", ".journal-complete.json"},
            )

            recovered = knowledge_writer.recover(vault)
            self.assertEqual(recovered["recovered"], [transactions[0].name])
            self.assertFalse(transactions[0].exists())
            self.assertTrue(
                (
                    vault
                    / ".awesome-capture"
                    / "quarantine"
                    / f"completed-{transactions[0].name}"
                ).is_dir()
            )
            self.assertTrue(knowledge_writer.audit(vault)["healthy"])

    def test_ingest_cleanup_recovery_rejects_changed_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript = self.transcript(root / "transcript.json")
            draft = self.draft(root / "draft.md")
            dry = knowledge_writer.commit(self.args(transcript, draft, vault))

            with mock.patch.object(
                knowledge_writer,
                "_cleanup_completed_transaction",
                side_effect=OSError("injected cleanup failure"),
            ):
                with self.assertRaises(OSError):
                    knowledge_writer.commit(
                        self.args(
                            transcript,
                            draft,
                            vault,
                            dry_run=False,
                            expected_plan_sha256=dry["plan_sha256"],
                        )
                    )

            transaction = next(
                (vault / ".awesome-capture" / "transactions").glob("ingest-*")
            )
            journal = json.loads(
                (transaction / "journal.json").read_text(encoding="utf-8")
            )
            destination = vault / journal["steps"][0]["destination"]
            destination.write_text("tampered", encoding="utf-8")

            with self.assertRaises(knowledge_writer.IngestError) as raised:
                knowledge_writer.recover(vault)
            self.assertEqual(raised.exception.code, "RECOVERY_CONFLICT")
            self.assertTrue(transaction.is_dir())

    def test_stable_id_is_transcript_artifact_content_not_source_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first_path = self.transcript(root / "first.json")
            first = json.loads(first_path.read_text(encoding="utf-8"))
            second = json.loads(json.dumps(first))
            second["transcription"]["requested_language"] = "en"
            self.refresh_transcript_identity(second)
            second_path = root / "second.json"
            second_path.write_text(json.dumps(second), encoding="utf-8")
            first_summary = knowledge_writer.validate_transcript(first)
            second_summary = knowledge_writer.validate_transcript(second)
            self.assertNotEqual(
                knowledge_writer.receipt_id(first_summary["artifact_sha256"]),
                knowledge_writer.receipt_id(second_summary["artifact_sha256"]),
            )
            self.assertEqual(first_summary["source_sha256"], second_summary["source_sha256"])

    def test_concurrent_identical_ingest_creates_once_then_reuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript = self.transcript(root / "transcript.json")
            draft = self.draft(root / "draft.md")
            dry = knowledge_writer.commit(self.args(transcript, draft, vault))
            command = [
                sys.executable,
                str(
                    ROOT
                    / "skills"
                    / "ingest-knowledge"
                    / "scripts"
                    / "knowledge_writer.py"
                ),
                "commit",
                "--transcript",
                str(transcript),
                "--document",
                str(draft),
                "--vault",
                str(vault),
                "--title",
                "测试知识",
                "--collection",
                "00 Inbox",
                "--sources-dir",
                "90 Sources",
                "--tag",
                "测试",
                "--expected-plan-sha256",
                dry["plan_sha256"],
                "--lock-timeout",
                CONCURRENT_LOCK_TIMEOUT,
            ]
            processes = [
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)
            ]
            results = [
                process.communicate(timeout=CONCURRENT_PROCESS_TIMEOUT)
                for process in processes
            ]
            self.assertEqual([process.returncode for process in processes], [0, 0], results)
            payloads = [json.loads(stdout) for stdout, _ in results]
            self.assertEqual(
                sorted(payload["result"] for payload in payloads),
                ["created", "reused"],
            )

    def test_ingest_receipt_symlink_hardlink_and_forged_digest_are_unhealthy(self):
        cases = ("symlink", "hardlink", "mode-0400", "mode-0700", "forged-digest")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                vault = root / "Vault"
                self.build_vault(vault)
                transcript = self.transcript(root / "transcript.json")
                draft = self.draft(root / "draft.md")
                dry = knowledge_writer.commit(self.args(transcript, draft, vault))
                created = knowledge_writer.commit(
                    self.args(
                        transcript,
                        draft,
                        vault,
                        dry_run=False,
                        expected_plan_sha256=dry["plan_sha256"],
                    )
                )
                receipt = Path(created["receipt_path"])
                if case == "symlink":
                    outside = root / "outside-ingest-receipt.json"
                    receipt.replace(outside)
                    os.symlink(outside, receipt)
                elif case == "hardlink":
                    os.link(receipt, root / "outside-ingest-receipt.json")
                elif case.startswith("mode-"):
                    receipt.chmod(int(case.removeprefix("mode-"), 8))
                else:
                    value = json.loads(receipt.read_text(encoding="utf-8"))
                    value["producer"]["contract_digest"] = "0" * 64
                    receipt.write_text(
                        json.dumps(value, ensure_ascii=False),
                        encoding="utf-8",
                    )

                audit = knowledge_writer.audit(vault)
                self.assertFalse(audit["healthy"], audit)
                receipt_findings = [
                    item
                    for item in audit["findings"]
                    if item["path"].endswith(f"{receipt.stem}.json")
                ]
                self.assertTrue(receipt_findings, audit)
                if case.startswith("mode-"):
                    self.assertIn(
                        "UNSAFE_RECEIPT",
                        {item["code"] for item in receipt_findings},
                    )
                elif case == "forged-digest":
                    self.assertIn(
                        "CONTRACT_BUILD_MISMATCH",
                        {item["code"] for item in receipt_findings},
                    )

    def test_audit_detects_a_deleted_ingest_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript = self.transcript(root / "transcript.json")
            draft = self.draft(root / "draft.md")
            dry = knowledge_writer.commit(self.args(transcript, draft, vault))
            created = knowledge_writer.commit(
                self.args(
                    transcript,
                    draft,
                    vault,
                    dry_run=False,
                    expected_plan_sha256=dry["plan_sha256"],
                )
            )
            receipt_path = Path(created["receipt_path"])
            receipt_path.unlink()

            audit = knowledge_writer.audit(vault)
            self.assertFalse(audit["healthy"], audit)
            self.assertFalse(audit["clean"], audit)
            self.assertIn(
                "MISSING_INGEST_RECEIPT",
                {item["code"] for item in audit["findings"]},
            )
            self.assertTrue(receipt_path.parent.is_dir())
            self.assertFalse(receipt_path.exists())

    def test_audit_detects_a_deleted_ingest_receipts_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript = self.transcript(root / "transcript.json")
            draft = self.draft(root / "draft.md")
            dry = knowledge_writer.commit(self.args(transcript, draft, vault))
            created = knowledge_writer.commit(
                self.args(
                    transcript,
                    draft,
                    vault,
                    dry_run=False,
                    expected_plan_sha256=dry["plan_sha256"],
                )
            )
            receipt_directory = Path(created["receipt_path"]).parent
            shutil.rmtree(receipt_directory)

            audit = knowledge_writer.audit(vault)
            self.assertFalse(audit["healthy"], audit)
            self.assertFalse(audit["clean"], audit)
            self.assertIn(
                "MISSING_INGEST_RECEIPT",
                {item["code"] for item in audit["findings"]},
            )
            self.assertFalse(receipt_directory.exists())

    def test_concurrent_different_drafts_create_once_then_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            self.build_vault(vault)
            transcript = self.transcript(root / "transcript.json")
            first_draft = self.draft(root / "first.md")
            second_draft = self.draft(root / "second.md")
            second_draft.write_text(
                second_draft.read_text(encoding="utf-8")
                + "\n- 推断：第二份草稿采用不同结构。\n",
                encoding="utf-8",
            )
            first_plan = knowledge_writer.commit(
                self.args(transcript, first_draft, vault)
            )
            second_plan = knowledge_writer.commit(
                self.args(transcript, second_draft, vault)
            )

            def command(draft: Path, plan_sha256: str) -> list[str]:
                return [
                    sys.executable,
                    str(
                        ROOT
                        / "skills"
                        / "ingest-knowledge"
                        / "scripts"
                        / "knowledge_writer.py"
                    ),
                    "commit",
                    "--transcript",
                    str(transcript),
                    "--document",
                    str(draft),
                    "--vault",
                    str(vault),
                    "--title",
                    "测试知识",
                    "--collection",
                    "00 Inbox",
                    "--sources-dir",
                    "90 Sources",
                    "--tag",
                    "测试",
                    "--expected-plan-sha256",
                    plan_sha256,
                    "--lock-timeout",
                    CONCURRENT_LOCK_TIMEOUT,
                ]

            processes = [
                subprocess.Popen(
                    command(first_draft, first_plan["plan_sha256"]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                ),
                subprocess.Popen(
                    command(second_draft, second_plan["plan_sha256"]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                ),
            ]
            results = [
                process.communicate(timeout=CONCURRENT_PROCESS_TIMEOUT)
                for process in processes
            ]
            self.assertEqual(
                sorted(process.returncode for process in processes),
                [0, 4],
                results,
            )
            successful = [
                json.loads(stdout)
                for process, (stdout, _) in zip(processes, results)
                if process.returncode == 0
            ]
            failed = [
                json.loads(stderr)
                for process, (_, stderr) in zip(processes, results)
                if process.returncode == 4
            ]
            self.assertEqual(successful[0]["result"], "created")
            self.assertEqual(failed[0]["error"]["code"], "INGEST_ID_CONFLICT")
            receipts = list(
                (vault / ".awesome-capture" / "receipts").glob("*.json")
            )
            self.assertEqual(len(receipts), 1)


class VaultRuntimePathRaceTests(unittest.TestCase):
    @staticmethod
    def runtime_module(function):
        return sys.modules[function.__module__]

    def test_macos_fixed_tmp_alias_is_normalized_before_nofollow_walk(self):
        runtime = self.runtime_module(vault_builder.runtime_open_root)
        with (
            mock.patch.object(runtime.sys, "platform", "darwin"),
            mock.patch.object(
                runtime.os.path,
                "realpath",
                return_value="/private/tmp",
            ) as realpath,
        ):
            normalized = runtime._absolute_path(
                Path("/tmp/awesome-capture/vault")
            )
        self.assertEqual(
            normalized,
            Path("/private/tmp/awesome-capture/vault"),
        )
        realpath.assert_called_once_with("/tmp")

    def test_builder_and_ingest_ensure_directory_lstat_swap_never_chmods_outside(
        self,
    ):
        cases = (
            ("builder", vault_builder.ensure_directory),
            ("ingest", knowledge_writer.ensure_directory),
        )
        for label, ensure in cases:
            with self.subTest(runtime=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                target = root / f"{label}-managed"
                moved = root / f"{label}-managed-original"
                outside = root / f"{label}-outside"
                outside.mkdir(mode=0o755)
                outside.chmod(0o755)
                runtime = self.runtime_module(
                    vault_builder.runtime_ensure_directory_path
                    if label == "builder"
                    else knowledge_writer.runtime_ensure_directory_path
                )
                real_fchmod = runtime.os.fchmod
                swapped = False

                def swap_before_descriptor_chmod(descriptor, mode):
                    nonlocal swapped
                    if not swapped:
                        target.rename(moved)
                        target.symlink_to(outside, target_is_directory=True)
                        swapped = True
                    return real_fchmod(descriptor, mode)

                with mock.patch.object(
                    runtime.os,
                    "fchmod",
                    side_effect=swap_before_descriptor_chmod,
                ):
                    ensure(target, mode=0o700)

                self.assertTrue(swapped)
                self.assertTrue(target.is_symlink())
                self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
                self.assertEqual(moved.stat().st_mode & 0o777, 0o700)

    def test_ingest_transaction_parent_swap_cannot_create_outside_directory(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            outside = root / "outside"
            vault.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            runtime = self.runtime_module(
                knowledge_writer.runtime_create_transaction_directory
            )
            real_ensure = runtime.ensure_relative_directory
            transactions = vault / ".awesome-capture" / "transactions"
            moved = transactions.with_name("transactions-original")
            swapped = False

            def ensure_then_swap(vault_path, relative, *, mode):
                nonlocal swapped
                real_ensure(vault_path, relative, mode=mode)
                transactions.rename(moved)
                transactions.symlink_to(outside, target_is_directory=True)
                swapped = True

            with mock.patch.object(
                runtime,
                "ensure_relative_directory",
                side_effect=ensure_then_swap,
            ):
                with self.assertRaises(
                    knowledge_writer.IngestError
                ) as raised:
                    knowledge_writer.transaction_directory(vault)

            self.assertTrue(swapped)
            self.assertEqual(raised.exception.code, "INVALID_DESTINATION")
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(list(moved.iterdir()), [])

    def test_vault_lock_stays_on_held_root_when_path_is_swapped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault = root / "Vault"
            moved = root / "Vault-original"
            outside = root / "outside"
            vault.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            sentinel = outside / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            runtime = self.runtime_module(vault_builder.runtime_vault_lock)
            real_mkdir = runtime.os.mkdir
            swapped = False

            def swap_before_metadata_create(path, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == ".awesome-capture" and not swapped:
                    vault.rename(moved)
                    vault.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return real_mkdir(path, mode, dir_fd=dir_fd)

            with (
                mock.patch.object(runtime, "require_posix"),
                mock.patch.object(
                    runtime.os,
                    "mkdir",
                    side_effect=swap_before_metadata_create,
                ),
            ):
                with vault_builder.vault_lock(
                    vault,
                    exclusive=True,
                    timeout=1.0,
                    create=True,
                ):
                    pass

            self.assertTrue(swapped)
            self.assertTrue(vault.is_symlink())
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"),
                "unchanged",
            )
            self.assertFalse((outside / ".awesome-capture").exists())
            self.assertTrue(
                (moved / ".awesome-capture" / "vault.lock").is_file()
            )

    def test_staging_writers_reject_renamed_transaction_symlink_without_escape(
        self,
    ):
        cases = (
            (
                "builder",
                lambda path: vault_builder.stage_file(
                    path,
                    b"builder payload",
                    mode=0o600,
                ),
                vault_builder.VaultError,
                "UNSAFE_PATH",
            ),
            (
                "ingest",
                lambda path: knowledge_writer.fsync_write(
                    path,
                    "ingest payload",
                ),
                knowledge_writer.IngestError,
                "INVALID_DESTINATION",
            ),
        )
        for label, write, error_type, expected_code in cases:
            with self.subTest(runtime=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                vault = root / "Vault"
                transactions = vault / ".awesome-capture" / "transactions"
                transaction = transactions / f"{label}-transaction"
                outside = root / f"{label}-outside"
                transaction.mkdir(mode=0o700, parents=True)
                transaction.chmod(0o700)
                outside.mkdir(mode=0o700)
                moved = transaction.with_name(f"{transaction.name}-original")
                transaction.rename(moved)
                transaction.symlink_to(outside, target_is_directory=True)

                with self.assertRaises(error_type) as raised:
                    write(transaction / "payload")

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertEqual(list(moved.iterdir()), [])
                self.assertTrue(transaction.is_symlink())


if __name__ == "__main__":
    unittest.main()
