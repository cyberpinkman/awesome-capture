#!/usr/bin/env python3
"""Plan, build, and audit a conservative Obsidian vault."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _contracts.contract_runtime import (  # noqa: E402
    ContractError,
    contract_digest,
    read_json_strict,
    validate_contract,
)
from _contracts.posix_runtime import (  # noqa: E402
    PosixRuntimeError,
    file_sha256 as secure_file_sha256,
    read_regular_file,
    require_posix as require_contract_posix,
    test_failpoint,
)
from _contracts.vault_runtime import (  # noqa: E402
    VaultRuntimeError,
    create_transaction_directory as runtime_create_transaction_directory,
    ensure_directory_path as runtime_ensure_directory_path,
    ensure_relative_directory as runtime_ensure_relative_directory,
    ensure_vault_root as runtime_ensure_vault_root,
    fsync_directory as runtime_fsync_directory,
    open_root as runtime_open_root,
    publish_relative as runtime_publish_relative,
    quarantine_completed_transaction as runtime_quarantine_completed_transaction,
    sha256_descriptor as runtime_sha256_descriptor,
    vault_lock as runtime_vault_lock,
    walk_directory as runtime_walk_directory,
    write_new_file as runtime_write_new_file,
)


SCHEMA_VERSION = "awesome-capture.vault-config/v1"
BUILD_RECEIPT_SCHEMA = "awesome-capture.vault-build-receipt/v1"
TRANSACTION_SCHEMA = "awesome-capture.transaction/v1"
LAYOUT_VERSION = "awesome-capture.vault-layout/v1"
ALLOWED_KEYS = {
    "schema_version",
    "name",
    "profile",
    "language",
    "folders",
    "inbox_folder",
    "sources_folder",
    "attachments_folder",
    "templates_folder",
    "link_style",
    "daily_notes",
}
PROFILES = {"general", "research", "creator", "projects", "custom"}
DEFAULT_LOCK_TIMEOUT = 30.0
VAULT_ID_SCHEMA = "awesome-capture.vault-id/v1"


class VaultError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: str = "", exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details.strip()[-4000:]
        return {"status": "error", "error": error}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise VaultError("INVALID_ARGUMENT", message, exit_code=2)


def json_print(value: Any, *, stream: Any = None) -> None:
    import sys

    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        file=stream or sys.stdout,
    )


def require_posix() -> None:
    try:
        require_contract_posix()
    except PosixRuntimeError as exc:
        raise VaultError(
            "UNSUPPORTED_PLATFORM",
            exc.message,
            exit_code=3,
        ) from exc


def reject_nonfinite(value: str) -> Any:
    raise ValueError(f"Non-finite JSON number is forbidden: {value}")


def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def strict_json_file(path: Path, *, maximum_bytes: int = 4 * 1024 * 1024) -> dict[str, Any]:
    try:
        value = read_json_strict(
            path,
            validate=False,
            maximum_bytes=maximum_bytes,
        )
    except ContractError as exc:
        code = "CONFIG_NOT_FOUND" if exc.code == "JSON_NOT_READABLE" else (
            "UNSAFE_INPUT" if exc.code == "UNSAFE_JSON_FILE" else "INVALID_JSON"
        )
        raise VaultError(code, "JSON input could not be read and validated safely.", exit_code=2) from exc
    if not isinstance(value, dict):
        raise VaultError("INVALID_JSON", "JSON input must be one object.", exit_code=2)
    if len(canonical_json(value)) > maximum_bytes:
        raise VaultError("INVALID_JSON", "JSON input exceeds the size limit.", exit_code=2)
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_error(exc: VaultRuntimeError) -> VaultError:
    return VaultError(
        exc.code,
        exc.message,
        exit_code=exc.exit_code,
    )


def fsync_directory(path: Path) -> None:
    try:
        runtime_fsync_directory(path)
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def ensure_directory(path: Path, *, mode: int) -> None:
    try:
        runtime_ensure_directory_path(path, mode=mode)
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def _open_root(path: Path, *, require_owner: bool = True) -> int:
    try:
        return runtime_open_root(path, require_owner=require_owner)
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def _vault_root_identity(path: Path) -> tuple[int, int]:
    descriptor = _open_root(path)
    try:
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def ensure_vault_root(path: Path) -> None:
    try:
        runtime_ensure_vault_root(path)
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def _walk_directory(
    root: Path,
    relative: Path,
    *,
    create: bool,
    final_mode: int = 0o700,
) -> int:
    try:
        return runtime_walk_directory(
            root,
            relative,
            create=create,
            final_mode=final_mode,
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def ensure_relative_directory(root: Path, relative: str, *, mode: int) -> None:
    try:
        runtime_ensure_relative_directory(
            root,
            Path(safe_relative(relative, "managed directory")),
            mode=mode,
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def _sha256_descriptor(descriptor: int) -> str:
    return runtime_sha256_descriptor(descriptor)


def publish_relative(
    staged: Path,
    vault: Path,
    relative: str,
    expected_hash: str,
    *,
    mode: int,
) -> None:
    try:
        runtime_publish_relative(
            staged,
            vault,
            Path(safe_relative(relative, "transaction destination")),
            expected_hash,
            mode=mode,
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


@contextlib.contextmanager
def vault_lock(
    vault: Path,
    *,
    exclusive: bool,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    create: bool,
):
    try:
        with runtime_vault_lock(
            vault,
            exclusive=exclusive,
            timeout=timeout,
            create=create,
        ) as descriptor:
            yield descriptor
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def safe_relative(raw: str, label: str) -> str:
    path = Path(raw)
    unsafe_characters = re.compile(r'[\x00-\x1f\x7f<>:"\\|?*#^%\[\]]')
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in raw.split("/"))
        or unsafe_characters.search(raw)
        or "\\" in raw
    ):
        raise VaultError("INVALID_CONFIG", f"{label} is not a safe relative folder: {raw}", exit_code=2)
    return path.as_posix()


def validate_vault_target(vault: Path) -> Path:
    require_posix()
    expanded = vault.expanduser().absolute()
    if ".." in expanded.parts:
        raise VaultError("UNSAFE_VAULT_TARGET", "Vault target contains parent traversal.", exit_code=2)
    filesystem_root = Path(expanded.anchor)
    home = Path.home().expanduser().absolute()
    if expanded in {filesystem_root, home} or expanded.name == ".obsidian":
        raise VaultError(
            "UNSAFE_VAULT_TARGET",
            f"Refusing a filesystem root, home directory, or .obsidian directory as a vault: {expanded}",
            exit_code=2,
        )
    probe = expanded if expanded.exists() or expanded.is_symlink() else expanded.parent
    try:
        descriptor = _open_root(
            probe,
            require_owner=expanded.exists() or expanded.is_symlink(),
        )
    except VaultError as exc:
        raise VaultError(
            "UNSAFE_VAULT_TARGET",
            "Vault root ownership, type, or permissions are unsafe.",
            exit_code=2,
        ) from exc
    os.close(descriptor)
    for ancestor in expanded.parents:
        if (ancestor / ".obsidian").is_dir():
            raise VaultError(
                "NESTED_VAULT",
                f"Target would be nested inside an existing Obsidian vault: {ancestor}",
                exit_code=2,
            )
    return expanded


def read_config(path: Path) -> dict[str, Any]:
    return validate_config(strict_json_file(path))


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_contract(value, expected="vault-config")
    except ContractError as exc:
        raise VaultError(
            "INVALID_CONFIG",
            "Vault config does not match awesome-capture.vault-config/v1.",
            details=exc.path,
            exit_code=2,
        ) from exc
    unknown = sorted(set(value) - ALLOWED_KEYS)
    if unknown:
        raise VaultError("INVALID_CONFIG", "Unknown config keys.", details=", ".join(unknown), exit_code=2)
    required = ALLOWED_KEYS - {"daily_notes"}
    missing = sorted(required - set(value))
    if missing:
        raise VaultError("INVALID_CONFIG", "Missing config keys.", details=", ".join(missing), exit_code=2)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise VaultError("INVALID_CONFIG", f"schema_version must be {SCHEMA_VERSION}", exit_code=2)
    name = str(value.get("name") or "").strip()
    if not name or len(name) > 100 or any(character in name for character in "/\\\0"):
        raise VaultError("INVALID_CONFIG", "name is empty or unsafe.", exit_code=2)
    profile = str(value.get("profile") or "")
    if profile not in PROFILES:
        raise VaultError("INVALID_CONFIG", f"Unsupported profile: {profile}", exit_code=2)
    language = str(value.get("language") or "").strip()
    if not language or len(language) > 32:
        raise VaultError("INVALID_CONFIG", "language is empty or too long.", exit_code=2)
    raw_folders = value.get("folders")
    if not isinstance(raw_folders, list) or not raw_folders:
        raise VaultError("INVALID_CONFIG", "folders must be a non-empty array.", exit_code=2)
    folders = [safe_relative(str(item), "folder") for item in raw_folders]
    if len(folders) != len(set(folders)):
        raise VaultError("INVALID_CONFIG", "folders contains duplicates.", exit_code=2)
    designated = {}
    for key in ("inbox_folder", "sources_folder", "attachments_folder", "templates_folder"):
        designated[key] = safe_relative(str(value.get(key) or ""), key)
        if designated[key] not in folders:
            raise VaultError("INVALID_CONFIG", f"{key} must appear in folders.", exit_code=2)
    link_style = str(value.get("link_style") or "")
    if link_style not in {"wikilink", "markdown"}:
        raise VaultError("INVALID_CONFIG", "link_style must be wikilink or markdown.", exit_code=2)
    daily = value.get("daily_notes", {"enabled": False, "folder": "Daily", "format": "YYYY-MM-DD"})
    if not isinstance(daily, dict) or set(daily) != {"enabled", "folder", "format"}:
        raise VaultError(
            "INVALID_CONFIG",
            "daily_notes must contain exactly enabled, folder, and format.",
            exit_code=2,
        )
    if not isinstance(daily["enabled"], bool):
        raise VaultError("INVALID_CONFIG", "daily_notes.enabled must be boolean.", exit_code=2)
    daily_folder = safe_relative(str(daily["folder"]), "daily_notes.folder")
    daily_format = str(daily["format"] or "").strip()
    if not daily_format or len(daily_format) > 64:
        raise VaultError("INVALID_CONFIG", "daily_notes.format is empty or too long.", exit_code=2)
    if daily["enabled"] and daily_folder not in folders:
        raise VaultError("INVALID_CONFIG", "Enabled daily_notes.folder must appear in folders.", exit_code=2)
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "profile": profile,
        "language": language,
        "folders": folders,
        **designated,
        "link_style": link_style,
        "daily_notes": {"enabled": daily["enabled"], "folder": daily_folder, "format": daily_format},
    }


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def home_note(config: dict[str, Any]) -> str:
    inbox = config["inbox_folder"]
    sources = config["sources_folder"]
    return "\n".join(
        [
            "---",
            f"title: {json.dumps(config['name'], ensure_ascii=False)}",
            'type: "map"',
            "tags:",
            '  - "map/home"',
            "---",
            "",
            f"# {config['name']}",
            "",
            "## 快速入口",
            "",
            f"- 收件箱路径：`{inbox}/`",
            f"- 原始材料路径：`{sources}/`",
            "",
            "## 当前重点",
            "",
            "- ",
            "",
            "## 每周维护",
            "",
            "1. 清空或归类收件箱。",
            "2. 为有长期价值的内容补充来源和关联。",
            "3. 归档已完成或不再活跃的内容。",
            "",
        ]
    )


def knowledge_template() -> str:
    return """---
created: "{{date}}"
type: "knowledge"
status: "draft"
source:
tags:
  - knowledge
---

# {{title}}

> [!summary]
>

## 核心结论

-

## 论证与证据

-

## 关键概念

-

## 可行动项

-

## 待验证

-

## 关联

- [[]]
"""


def source_template() -> str:
    return """---
created: "{{date}}"
type: "source"
source_url:
author:
tags:
  - source
---

# {{title}}

## 来源信息

-

## 原始内容

"""


def project_template() -> str:
    return """---
created: "{{date}}"
type: "project"
status: "active"
deadline:
tags:
  - project
---

# {{title}}

## 预期结果

-

## 下一步

- [ ]

## 资料与决策

-
"""


def daily_template() -> str:
    return """---
date: "{{date}}"
type: "daily"
tags:
  - daily
---

# {{date}}

## 今日重点

- [ ]

## 捕获

-

## 回顾

-
"""


def desired_files(config: dict[str, Any]) -> dict[Path, str]:
    templates = Path(config["templates_folder"])
    files: dict[Path, str] = {
        Path("Home.md"): home_note(config),
        templates / "Knowledge Note.md": knowledge_template(),
        templates / "Source Note.md": source_template(),
        templates / "Project.md": project_template(),
    }
    if config["daily_notes"]["enabled"]:
        files[templates / "Daily Note.md"] = daily_template()
    return files


def config_digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(config)).hexdigest()


def vault_identity(vault: Path) -> str:
    return hashlib.sha256(
        VAULT_ID_SCHEMA.encode("utf-8") + b"\0" + os.fsencode(str(vault))
    ).hexdigest()


def contract_failure(exc: ContractError, *, receipt: bool = False) -> VaultError:
    if exc.code == "CONTRACT_BUILD_MISMATCH":
        exit_code = 7
    elif receipt:
        exit_code = 4
    else:
        exit_code = 2
    return VaultError(
        exc.code if exc.code == "CONTRACT_BUILD_MISMATCH" else (
            "INVALID_BUILD_RECEIPT" if receipt else exc.code
        ),
        exc.message,
        details=exc.path,
        exit_code=exit_code,
    )


def plan_digest(plan: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in plan.items()
        if key not in {"status", "operation", "plan_sha256", "root_state"}
    }
    return hashlib.sha256(canonical_json(stable)).hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def vault_root_state(vault: Path) -> str:
    """Classify user-visible vault state without counting lock scaffolding.

    A writer must create ``.awesome-capture/vault.lock`` before it can hold the
    vault lock.  Another writer can observe that safe, persistent scaffold in
    the short interval before either process acquires the lock.  Treating it as
    user content makes the outcome depend on process scheduling.  Only an empty
    metadata directory or one containing the exact safe lock file is ignored;
    every other entry still requires explicit existing-vault authorization.
    """

    try:
        metadata = vault.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "existing"
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return "existing"

    root_descriptor = -1
    metadata_descriptor = -1
    lock_descriptor = -1
    try:
        root_descriptor = _open_root(vault)
        root_entries = set(os.listdir(root_descriptor))
        if not root_entries:
            return "empty"
        if root_entries != {".awesome-capture"}:
            return "existing"

        metadata_descriptor = os.open(
            ".awesome-capture",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_descriptor,
        )
        metadata = os.fstat(metadata_descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            return "existing"
        metadata_entries = set(os.listdir(metadata_descriptor))
        if not metadata_entries:
            return "empty"
        if metadata_entries != {"vault.lock"}:
            return "existing"

        lock_descriptor = os.open(
            "vault.lock",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=metadata_descriptor,
        )
        lock_metadata = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or lock_metadata.st_nlink != 1
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            return "existing"
        return "empty"
    except (OSError, VaultError):
        return "existing"
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        if metadata_descriptor >= 0:
            os.close(metadata_descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)


def build_plan(config: dict[str, Any], vault: Path) -> dict[str, Any]:
    vault = validate_vault_target(vault)
    files = desired_files(config)
    create_dirs: list[str] = []
    create_files: list[str] = []
    unchanged_files: list[str] = []
    conflicts: list[str] = []
    for folder in [".obsidian", *config["folders"]]:
        destination = vault / folder
        if not is_within(destination, vault):
            conflicts.append(f"{Path(folder).as_posix()}/")
        elif not destination.exists():
            create_dirs.append(folder)
        else:
            try:
                metadata = destination.lstat()
            except OSError:
                conflicts.append(f"{Path(folder).as_posix()}/")
            else:
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o022
                ):
                    conflicts.append(f"{Path(folder).as_posix()}/")
    for relative, content in files.items():
        destination = vault / relative
        if not is_within(destination, vault):
            conflicts.append(relative.as_posix())
        elif not destination.exists():
            create_files.append(relative.as_posix())
        else:
            try:
                raw = read_regular_file(destination, 4 * 1024 * 1024)
                current = raw.decode("utf-8")
                metadata = destination.lstat()
            except (OSError, UnicodeError, PosixRuntimeError):
                conflicts.append(relative.as_posix())
            else:
                safe_mode = stat.S_IMODE(metadata.st_mode) == 0o644
                (
                    unchanged_files
                    if current == content and safe_mode
                    else conflicts
                ).append(relative.as_posix())
    root_state = vault_root_state(vault)
    plan = {
        "status": "ok",
        "operation": "plan",
        "vault": str(vault),
        "root_state": root_state,
        "config_sha256": config_digest(config),
        "create_directories": create_dirs,
        "create_files": create_files,
        "unchanged_files": unchanged_files,
        "conflicts": conflicts,
    }
    return {**plan, "plan_sha256": plan_digest(plan)}


def validate_build_receipt(
    value: dict[str, Any],
    *,
    vault: Path | None = None,
) -> dict[str, Any]:
    version = value.get("schema_version")
    if version != BUILD_RECEIPT_SCHEMA:
        raise VaultError(
            "UNSUPPORTED_RECEIPT_SCHEMA",
            f"Build receipt must use {BUILD_RECEIPT_SCHEMA}.",
            exit_code=4,
        )
    try:
        validate_contract(value, expected="vault-build-receipt")
    except ContractError as exc:
        raise contract_failure(exc, receipt=True) from exc
    if vault is not None and value["vault_id"] != vault_identity(vault):
        raise VaultError(
            "INVALID_BUILD_RECEIPT",
            "Build receipt belongs to a different vault.",
            exit_code=4,
        )
    return value


def read_build_receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise VaultError(
            "UNSAFE_BUILD_RECEIPT",
            "Build receipt must be a private, owned, single-link regular file.",
            exit_code=4,
        )
    return validate_build_receipt(strict_json_file(path), vault=path.parents[1])


def _open_managed_directory_for_audit(vault: Path, relative: Path) -> int:
    safe = Path(safe_relative(relative.as_posix(), "managed directory"))
    descriptor = _open_root(vault)
    try:
        for component in safe.parts:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError as exc:
                raise VaultError(
                    "MISSING_MANAGED_DIRECTORY",
                    "A managed directory is missing.",
                    exit_code=4,
                ) from exc
            except OSError as exc:
                raise VaultError(
                    "UNSAFE_MANAGED_DIRECTORY",
                    "A managed directory component is unsafe.",
                    exit_code=4,
                ) from exc
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise VaultError(
                    "UNSAFE_MANAGED_DIRECTORY",
                    "Managed directories must be owned by the current user and mode 0700.",
                    exit_code=4,
                )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def verify_managed_content(vault: Path, receipt: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for relative in receipt["managed_directories"]:
        try:
            descriptor = _open_managed_directory_for_audit(
                vault,
                Path(safe_relative(relative, "managed directory")),
            )
        except VaultError as exc:
            findings.append(
                {
                    "severity": "error",
                    "code": (
                        "MISSING_MANAGED_DIRECTORY"
                        if exc.code == "MISSING_MANAGED_DIRECTORY"
                        else "UNSAFE_MANAGED_DIRECTORY"
                    ),
                    "path": relative,
                }
            )
            continue
        os.close(descriptor)
    for item in receipt["managed_files"]:
        safe = Path(safe_relative(item["path"], "managed file"))
        parent_descriptor: int | None = None
        descriptor: int | None = None
        try:
            if safe.parent == Path("."):
                parent_descriptor = _open_root(vault)
            else:
                parent_descriptor = _open_managed_directory_for_audit(
                    vault,
                    safe.parent,
                )
            descriptor = os.open(
                safe.name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            findings.append(
                {"severity": "error", "code": "MISSING_MANAGED_FILE", "path": item["path"]}
            )
            continue
        except VaultError as exc:
            findings.append(
                {
                    "severity": "error",
                    "code": (
                        "MISSING_MANAGED_FILE"
                        if exc.code == "MISSING_MANAGED_DIRECTORY"
                        else "UNSAFE_MANAGED_FILE"
                    ),
                    "path": item["path"],
                }
            )
            continue
        except OSError:
            findings.append(
                {"severity": "error", "code": "UNSAFE_MANAGED_FILE", "path": item["path"]}
            )
            continue
        finally:
            if parent_descriptor is not None:
                os.close(parent_descriptor)
        assert descriptor is not None
        try:
            before = os.fstat(descriptor)
            expected_mode = int(str(item["mode"]), 8)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != expected_mode
            ):
                findings.append(
                    {"severity": "error", "code": "UNSAFE_MANAGED_FILE", "path": item["path"]}
                )
                continue
            digest = _sha256_descriptor(descriptor)
            after = os.fstat(descriptor)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_nlink,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_nlink,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if (
                identity_after != identity_before
                or before.st_size != item["bytes"]
                or digest != item["sha256"]
            ):
                findings.append(
                    {"severity": "error", "code": "MANAGED_FILE_CHANGED", "path": item["path"]}
                )
        finally:
            os.close(descriptor)
    return findings


def receipt_for(
    config: dict[str, Any],
    vault: Path,
    *,
    confirmed_plan_sha256: str,
) -> dict[str, Any]:
    file_entries = []
    for relative, content in sorted(desired_files(config).items(), key=lambda item: item[0].as_posix()):
        encoded = content.encode("utf-8")
        file_entries.append(
            {
                "path": relative.as_posix(),
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "mode": "0644",
            }
        )
    value = {
        "schema_version": BUILD_RECEIPT_SCHEMA,
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "config_schema": SCHEMA_VERSION,
        "config_sha256": config_digest(config),
        "layout_version": LAYOUT_VERSION,
        "link_style": config["link_style"],
        "vault_id": vault_identity(vault),
        "plan_sha256": confirmed_plan_sha256,
        "managed_directories": [".obsidian", *config["folders"]],
        "managed_files": file_entries,
        "producer": {
            "skill": "build-obsidian-vault",
            "contract_digest": contract_digest(),
        },
    }
    try:
        validate_contract(value, expected="vault-build-receipt")
    except ContractError as exc:
        raise contract_failure(exc) from exc
    return value


def transaction_directory(vault: Path, kind: str) -> Path:
    try:
        return runtime_create_transaction_directory(
            vault,
            prefix=f"{kind}-",
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def stage_file(path: Path, data: bytes, *, mode: int) -> None:
    try:
        runtime_write_new_file(path, data, mode=mode)
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def _verify_staged_file(
    transaction: Path,
    name: str,
    *,
    expected_bytes: int,
    expected_hash: str,
    allowed_modes: set[int],
) -> None:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise VaultError("RECOVERY_CONFLICT", "Staged filename is unsafe.", exit_code=4)
    parent_descriptor = _open_root(transaction)
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink not in {1, 2}
            or stat.S_IMODE(before.st_mode) not in allowed_modes
            or before.st_size != expected_bytes
        ):
            raise VaultError("RECOVERY_CONFLICT", "Staged transaction file is unsafe.", exit_code=4)
        digest = _sha256_descriptor(descriptor)
        after = os.fstat(descriptor)
        if (
            (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_nlink,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_nlink,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            or digest != expected_hash
        ):
            raise VaultError("RECOVERY_CONFLICT", "Staged transaction file changed.", exit_code=4)
    except VaultError:
        raise
    except OSError as exc:
        raise VaultError("RECOVERY_CONFLICT", "Staged transaction file is unsafe.", exit_code=4) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def write_transaction_journal(path: Path, value: dict[str, Any]) -> None:
    try:
        validate_contract(value, expected="transaction")
    except ContractError as exc:
        raise contract_failure(exc) from exc
    stage_file(path, stable_json(value).encode("utf-8"), mode=0o600)
    fsync_directory(path.parent)


def completed_transaction_journal(journal: dict[str, Any]) -> dict[str, Any]:
    completed = json.loads(stable_json(journal))
    completed["status"] = "complete"
    for step in completed["steps"]:
        step["status"] = "published"
    return completed


def _strict_json_bytes(data: bytes) -> dict[str, Any]:
    if len(data) > 4 * 1024 * 1024:
        raise VaultError("RECOVERY_CONFLICT", "Completion receipt exceeds its size limit.", exit_code=4)
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_nonfinite,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise VaultError("RECOVERY_CONFLICT", "Completion receipt is not strict JSON.", exit_code=4) from exc
    if not isinstance(value, dict):
        raise VaultError("RECOVERY_CONFLICT", "Completion receipt is not a JSON object.", exit_code=4)
    return value


def _destination_evidence(
    vault: Path,
    relative: str,
    *,
    expected_bytes: int,
    expected_hash: str,
    expected_mode: int,
    staged: Path | None,
) -> bytes:
    safe = Path(safe_relative(relative, "transaction destination"))
    if safe.parent == Path("."):
        parent_descriptor = _open_root(vault)
    else:
        parent_descriptor = _walk_directory(
            vault,
            safe.parent,
            create=False,
            final_mode=0o700,
        )
    try:
        try:
            descriptor = os.open(
                safe.name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise VaultError(
                "RECOVERY_CONFLICT",
                "A completed transaction destination is missing or unsafe.",
                exit_code=4,
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != expected_mode
                or metadata.st_size != expected_bytes
                or _sha256_descriptor(descriptor) != expected_hash
            ):
                raise VaultError(
                    "RECOVERY_CONFLICT",
                    "A completed transaction destination differs.",
                    exit_code=4,
                )
            if metadata.st_nlink != 1:
                raise VaultError(
                    "RECOVERY_CONFLICT",
                    "A completed transaction destination has an unexpected hardlink.",
                    exit_code=4,
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _verify_completed_build(
    vault: Path,
    transaction: Path,
    journal: dict[str, Any],
) -> None:
    if not journal["steps"] or journal["steps"][-1]["operation"] != "publish-receipt":
        raise VaultError("RECOVERY_CONFLICT", "Transaction has no final completion receipt.", exit_code=4)
    by_destination = {str(item["destination"]): item for item in journal["steps"]}
    receipt_step = journal["steps"][-1]
    if receipt_step["destination"] != ".awesome-capture/vault-build.json":
        raise VaultError("RECOVERY_CONFLICT", "Build completion receipt path is invalid.", exit_code=4)
    receipt_raw = _destination_evidence(
        vault,
        str(receipt_step["destination"]),
        expected_bytes=int(receipt_step["bytes"]),
        expected_hash=str(receipt_step["sha256"]),
        expected_mode=0o600,
        staged=transaction / str(receipt_step["source"]),
    )
    receipt = _strict_json_bytes(receipt_raw)
    try:
        validate_contract(receipt, expected="vault-build-receipt")
    except ContractError as exc:
        raise VaultError(
            "RECOVERY_CONFLICT",
            "Build completion receipt is invalid.",
            details=f"{exc.code}:{exc.path}",
            exit_code=4,
        ) from exc
    if receipt["config_sha256"] != journal["job_id"] or receipt["vault_id"] != vault_identity(vault):
        raise VaultError("RECOVERY_CONFLICT", "Build completion identity differs.", exit_code=4)
    receipt_files = {str(item["path"]): item for item in receipt["managed_files"]}
    for relative, item in receipt_files.items():
        step = by_destination.get(relative)
        if step is not None and (
            step["operation"] != "publish-file"
            or step["bytes"] != item["bytes"]
            or step["sha256"] != item["sha256"]
        ):
            raise VaultError("RECOVERY_CONFLICT", "Build receipt file evidence differs.", exit_code=4)
        _destination_evidence(
            vault,
            relative,
            expected_bytes=int(item["bytes"]),
            expected_hash=str(item["sha256"]),
            expected_mode=int(str(item["mode"]), 8),
            staged=transaction / str(step["source"]) if step is not None else None,
        )
    for step in journal["steps"][:-1]:
        if str(step["destination"]) not in receipt_files:
            raise VaultError("RECOVERY_CONFLICT", "Published file is absent from the build receipt.", exit_code=4)
    for relative in receipt["managed_directories"]:
        descriptor = _walk_directory(
            vault,
            Path(safe_relative(relative, "managed directory")),
            create=False,
            final_mode=0o700,
        )
        os.close(descriptor)


def _promote_completed_journal(
    transaction: Path,
    journal: dict[str, Any],
) -> dict[str, Any]:
    completed = completed_transaction_journal(journal)
    completion_path = transaction / ".journal-complete.json"
    if completion_path.exists() or completion_path.is_symlink():
        try:
            existing = None if completion_path.is_symlink() else strict_json_file(completion_path)
        except VaultError as exc:
            raise VaultError("RECOVERY_CONFLICT", "Completion journal is unsafe.", exit_code=4) from exc
        if existing != completed:
            raise VaultError("RECOVERY_CONFLICT", "Completion journal differs.", exit_code=4)
    else:
        write_transaction_journal(completion_path, completed)
    return completed


def _cleanup_completed_transaction(
    root: Path,
    transaction: Path,
    journal: dict[str, Any],
    completed: dict[str, Any],
) -> None:
    actual = {item.name for item in transaction.iterdir()}
    expected_values = (
        {"journal.json": journal}
        if journal["status"] == "complete"
        else {
            "journal.json": journal,
            ".journal-complete.json": completed,
        }
    )
    if actual != set(expected_values):
        raise VaultError(
            "RECOVERY_CONFLICT",
            "Completed transaction contains unexpected residue.",
            exit_code=4,
        )
    expected_files = {
        name: hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()
        for name, value in expected_values.items()
    }
    try:
        runtime_quarantine_completed_transaction(
            root.parents[1],
            transaction,
            expected_files=expected_files,
        )
    except VaultRuntimeError as exc:
        raise _runtime_error(exc) from exc


def recover_transactions_locked(vault: Path, *, kind: str | None = None) -> dict[str, Any]:
    root = vault / ".awesome-capture" / "transactions"
    recovered: list[str] = []
    if not root.exists():
        return {"recovered": recovered}
    if root.is_symlink() or not root.is_dir():
        raise VaultError("RECOVERY_CONFLICT", "Transactions path is unsafe.", exit_code=4)
    for transaction in sorted(root.iterdir()):
        if kind is not None and not transaction.name.startswith(f"{kind}-"):
            continue
        if transaction.is_symlink() or not transaction.is_dir():
            raise VaultError("RECOVERY_CONFLICT", "Transaction entry is unsafe.", exit_code=4)
        transaction_metadata = transaction.lstat()
        if transaction_metadata.st_uid != os.geteuid() or transaction_metadata.st_mode & 0o022:
            raise VaultError("RECOVERY_CONFLICT", "Transaction directory ownership is unsafe.", exit_code=4)
        try:
            initial_entries = {item.name for item in transaction.iterdir()}
        except OSError as exc:
            raise VaultError("RECOVERY_CONFLICT", "Transaction cannot be inspected.", exit_code=4) from exc
        if not initial_entries:
            raise VaultError(
                "RECOVERY_CONFLICT",
                "Empty transaction has no durable ownership marker.",
                exit_code=4,
            )
        journal_path = transaction / "journal.json"
        if not journal_path.is_file() or journal_path.is_symlink():
            raise VaultError("RECOVERY_CONFLICT", "Transaction has no trustworthy journal.", exit_code=4)
        journal = strict_json_file(journal_path)
        try:
            validate_contract(journal, expected="transaction")
        except ContractError as exc:
            raise VaultError(
                "RECOVERY_CONFLICT",
                "Transaction journal does not satisfy transaction/v1.",
                details=f"{exc.code}:{exc.path}",
                exit_code=4,
            ) from exc
        if (
            journal.get("kind") != "vault-build"
            or journal.get("root") != str(vault)
            or journal.get("staging_root") != str(transaction)
            or journal.get("status") not in {"publishing", "recovery_required", "complete"}
            or transaction.name != f"build-{journal.get('transaction_id')}"
        ):
            raise VaultError("RECOVERY_CONFLICT", "Transaction journal is invalid.", exit_code=4)
        completed = completed_transaction_journal(journal)
        if journal["status"] == "complete" and journal != completed:
            raise VaultError("RECOVERY_CONFLICT", "Completed transaction steps are not final.", exit_code=4)
        expected_sources = {str(item["source"]) for item in journal["steps"]}
        try:
            actual_entries = {item.name for item in transaction.iterdir()}
        except OSError as exc:
            raise VaultError(
                "RECOVERY_CONFLICT",
                "Transaction directory cannot be inspected.",
                exit_code=4,
            ) from exc
        allowed_entries = {"journal.json", *expected_sources}
        if journal["status"] != "complete":
            allowed_entries.add(".journal-complete.json")
        if actual_entries - allowed_entries:
            raise VaultError(
                "RECOVERY_CONFLICT", "Transaction contains unknown staged files.",
                exit_code=4,
            )
        if "journal.json" not in actual_entries:
            raise VaultError(
                "RECOVERY_CONFLICT",
                "Transaction is missing its durable journal.",
                exit_code=4,
            )
        if journal["status"] == "complete" and ".journal-complete.json" in actual_entries:
            raise VaultError("RECOVERY_CONFLICT", "Completed transaction has a stale journal.", exit_code=4)
        if ".journal-complete.json" in actual_entries:
            try:
                completion = strict_json_file(transaction / ".journal-complete.json")
            except VaultError as exc:
                raise VaultError("RECOVERY_CONFLICT", "Completion journal is unsafe.", exit_code=4) from exc
            if completion != completed:
                raise VaultError("RECOVERY_CONFLICT", "Completion journal differs.", exit_code=4)
        for expected_index, item in enumerate(journal["steps"]):
            if (
                not isinstance(item, dict)
                or set(item)
                != {
                    "index",
                    "operation",
                    "source",
                    "destination",
                    "bytes",
                    "sha256",
                    "status",
                }
                or item["index"] != expected_index
                or item["status"] not in {"pending", "published"}
            ):
                raise VaultError("RECOVERY_CONFLICT", "Transaction file entry is invalid.", exit_code=4)
            relative = safe_relative(str(item["destination"]), "transaction destination")
            staged = transaction / str(item["source"])
            if journal["status"] != "complete":
                if str(item["source"]) in actual_entries:
                    if (
                        staged.is_symlink()
                        or not staged.is_file()
                        or staged.stat().st_size != item["bytes"]
                        or file_sha256(staged) != item["sha256"]
                    ):
                        raise VaultError(
                            "RECOVERY_CONFLICT",
                            "Staged transaction file changed.",
                            exit_code=4,
                        )
                publish_relative(
                    staged,
                    vault,
                    relative,
                    str(item["sha256"]),
                    mode=0o600 if item["operation"] == "publish-receipt" else 0o644,
                )
                test_failpoint(
                    f"vault-build.after-publish-{expected_index}"
                )
        _verify_completed_build(vault, transaction, completed)
        completion_journal = completed
        if journal["status"] != "complete":
            completion_journal = _promote_completed_journal(transaction, journal)
            test_failpoint("vault-build.after-complete-journal")
        test_failpoint("vault-build.before-cleanup")
        _cleanup_completed_transaction(
            root,
            transaction,
            journal,
            completion_journal,
        )
        recovered.append(transaction.name)
    return {"recovered": recovered}


def recover(vault: Path, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> dict[str, Any]:
    vault = validate_vault_target(vault)
    if not vault.is_dir():
        raise VaultError("VAULT_NOT_FOUND", "Vault does not exist.", exit_code=2)
    with vault_lock(vault, exclusive=True, timeout=lock_timeout, create=True):
        result = recover_transactions_locked(vault)
    return {"status": "ok", "operation": "recover", "vault": str(vault), **result}


def build(
    config: dict[str, Any],
    vault: Path,
    *,
    apply: bool,
    extend_existing: bool,
    expected_plan_sha256: str | None = None,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    vault = validate_vault_target(vault)
    plan = build_plan(config, vault)
    if not apply:
        return {**plan, "result": "dry-run"}
    if (
        expected_plan_sha256 is not None
        and expected_plan_sha256 != plan["plan_sha256"]
        and (
            not vault.exists()
            or not (
                (vault / ".awesome-capture" / "vault.lock").exists()
                or (vault / ".awesome-capture" / "vault-build.json").exists()
            )
        )
    ):
        raise VaultError("STALE_PLAN", "The confirmed build plan is stale.", exit_code=4)
    if not vault.exists():
        ensure_vault_root(vault)
    with vault_lock(vault, exclusive=True, timeout=lock_timeout, create=True):
        recover_transactions_locked(vault, kind="build")
        locked_plan = build_plan(config, vault)
        receipt_path = vault / ".awesome-capture" / "vault-build.json"
        current_receipt = read_build_receipt(receipt_path)
        matching_receipt = (
            current_receipt is not None
            and current_receipt["config_sha256"] == config_digest(config)
            and current_receipt["link_style"] == config["link_style"]
        )
        if current_receipt is not None and not matching_receipt:
            raise VaultError("BUILD_CONFLICT", "Existing build receipt differs.", exit_code=4)
        if matching_receipt:
            if (
                expected_plan_sha256 is not None
                and expected_plan_sha256
                not in {
                    locked_plan["plan_sha256"],
                    current_receipt["plan_sha256"],
                }
            ):
                raise VaultError(
                    "STALE_PLAN",
                    "The confirmed build plan is stale.",
                    exit_code=4,
                )
            content_findings = verify_managed_content(vault, current_receipt)
            if content_findings:
                raise VaultError(
                    "RECOVERY_CONFLICT",
                    "Completed build receipt does not match managed content.",
                    details="\n".join(
                        f"{item['code']}:{item['path']}" for item in content_findings
                    ),
                    exit_code=4,
                )
            return {
                **locked_plan,
                "operation": "build",
                "result": "unchanged",
                "created_files": [],
                "receipt_path": str(receipt_path),
            }
        if expected_plan_sha256 is not None and expected_plan_sha256 != locked_plan["plan_sha256"]:
            raise VaultError("STALE_PLAN", "The vault changed after preview.", exit_code=4)
        if (
            locked_plan["root_state"] == "existing"
            and not extend_existing
            and not matching_receipt
        ):
            raise VaultError(
                "EXISTING_VAULT_REQUIRES_OPT_IN",
                "Target is non-empty; --extend-existing is required.",
                exit_code=4,
            )
        if locked_plan["conflicts"]:
            raise VaultError(
                "BUILD_CONFLICT",
                "Existing files differ from the requested vault.",
                details="\n".join(locked_plan["conflicts"]),
                exit_code=4,
            )
        ensure_relative_directory(vault, ".obsidian", mode=0o700)
        for relative in config["folders"]:
            ensure_relative_directory(vault, relative, mode=0o700)
        ensure_relative_directory(vault, ".awesome-capture", mode=0o700)
        transaction = transaction_directory(vault, "build")
        steps: list[dict[str, Any]] = []
        created: list[str] = []
        for index, (relative, content) in enumerate(
            sorted(desired_files(config).items(), key=lambda item: item[0].as_posix())
        ):
            if (vault / relative).is_file():
                continue
            staged_name = f"file-{index}.md"
            encoded = content.encode("utf-8")
            stage_file(transaction / staged_name, encoded, mode=0o600)
            steps.append(
                {
                    "index": len(steps),
                    "operation": "publish-file",
                    "source": staged_name,
                    "destination": relative.as_posix(),
                    "bytes": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "status": "pending",
                }
            )
            created.append(relative.as_posix())
        receipt_value = receipt_for(
            config,
            vault,
            confirmed_plan_sha256=locked_plan["plan_sha256"],
        )
        receipt_bytes = stable_json(receipt_value).encode("utf-8")
        stage_file(transaction / "receipt.json", receipt_bytes, mode=0o600)
        steps.append(
            {
                "index": len(steps),
                "operation": "publish-receipt",
                "source": "receipt.json",
                "destination": ".awesome-capture/vault-build.json",
                "bytes": len(receipt_bytes),
                "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "status": "pending",
            }
        )
        journal = {
            "schema_version": TRANSACTION_SCHEMA,
            "transaction_id": transaction.name.removeprefix("build-"),
            "kind": "vault-build",
            "status": "publishing",
            "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "job_id": config_digest(config),
            "root": str(vault),
            "staging_root": str(transaction),
            "steps": steps,
        }
        write_transaction_journal(transaction / "journal.json", journal)
        test_failpoint("vault-build.after-journal")
        recover_transactions_locked(vault, kind="build")
        return {
            **locked_plan,
            "operation": "build",
            "result": "created",
            "created_files": created,
            "receipt_path": str(receipt_path),
        }


def audit(
    vault: Path,
    *,
    require_build_receipt: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    requested_vault = vault.expanduser().absolute()
    try:
        vault = validate_vault_target(vault)
    except VaultError as exc:
        if exc.code != "UNSAFE_VAULT_TARGET":
            raise
        return {
            "status": "ok",
            "operation": "audit",
            "vault": str(requested_vault),
            "healthy": False,
            "clean": False,
            "managed_by_builder": False,
            "findings": [
                {
                    "severity": "error",
                    "code": "UNSAFE_VAULT_ROOT",
                    "path": ".",
                }
            ],
        }
    findings: list[dict[str, str]] = []
    managed_by_builder = False
    initial_root_identity: tuple[int, int] | None = None
    if not vault.is_dir():
        findings.append({"severity": "error", "code": "VAULT_NOT_FOUND", "path": str(vault)})
    else:
        initial_root_identity = _vault_root_identity(vault)
        metadata_dir = vault / ".awesome-capture"
        metadata_safe = True
        try:
            metadata = metadata_dir.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            metadata_safe = False
            findings.append(
                {"severity": "error", "code": "UNSAFE_METADATA_DIRECTORY", "path": ".awesome-capture"}
            )
        lock_path = metadata_dir / "vault.lock"
        if metadata_safe and metadata is not None:
            try:
                lock_metadata = lock_path.lstat()
            except FileNotFoundError:
                lock_metadata = None
            if lock_metadata is None:
                metadata_safe = False
                findings.append(
                    {"severity": "error", "code": "MISSING_LOCK", "path": ".awesome-capture/vault.lock"}
                )
            elif (
                stat.S_ISLNK(lock_metadata.st_mode)
                or not stat.S_ISREG(lock_metadata.st_mode)
                or lock_metadata.st_uid != os.geteuid()
                or lock_metadata.st_nlink != 1
                or stat.S_IMODE(lock_metadata.st_mode) != 0o600
            ):
                metadata_safe = False
                findings.append(
                    {"severity": "error", "code": "UNSAFE_LOCK", "path": ".awesome-capture/vault.lock"}
                )
        if metadata_safe:
            with vault_lock(vault, exclusive=False, timeout=lock_timeout, create=False):
                if not (vault / ".obsidian").is_dir() or (vault / ".obsidian").is_symlink():
                    findings.append({"severity": "error", "code": "MISSING_OBSIDIAN_CONFIG", "path": ".obsidian"})
                receipt_path = vault / ".awesome-capture" / "vault-build.json"
                receipt: dict[str, Any] | None = None
                try:
                    receipt = read_build_receipt(receipt_path)
                except VaultError as exc:
                    findings.append({"severity": "error", "code": exc.code, "path": ".awesome-capture/vault-build.json"})
                if receipt is None:
                    findings.append(
                        {
                            "severity": "error" if require_build_receipt else "info",
                            "code": "NO_BUILD_RECEIPT",
                            "path": ".awesome-capture/vault-build.json",
                        }
                    )
                else:
                    managed_by_builder = True
                    findings.extend(verify_managed_content(vault, receipt))
                transactions = vault / ".awesome-capture" / "transactions"
                try:
                    transaction_metadata = transactions.lstat()
                except FileNotFoundError:
                    transaction_metadata = None
                if transaction_metadata is not None:
                    if (
                        stat.S_ISLNK(transaction_metadata.st_mode)
                        or not stat.S_ISDIR(transaction_metadata.st_mode)
                        or transaction_metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(transaction_metadata.st_mode) != 0o700
                    ):
                        findings.append({"severity": "error", "code": "UNSAFE_TRANSACTIONS", "path": ".awesome-capture/transactions"})
                    elif any(transactions.iterdir()):
                        findings.append({"severity": "error", "code": "RECOVERY_REQUIRED", "path": ".awesome-capture/transactions"})
    if initial_root_identity is not None:
        try:
            current_root_identity = _vault_root_identity(vault)
        except VaultError:
            current_root_identity = None
        if current_root_identity != initial_root_identity:
            findings.append(
                {
                    "severity": "error",
                    "code": "UNSAFE_VAULT_ROOT",
                    "path": ".",
                }
            )
    return {
        "status": "ok",
        "operation": "audit",
        "vault": str(vault),
        "healthy": not any(item["severity"] == "error" for item in findings),
        "clean": not any(item["severity"] in {"error", "warning"} for item in findings),
        "managed_by_builder": managed_by_builder,
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-config")
    validate_parser.add_argument("config")
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("config")
    plan_parser.add_argument("--vault", required=True)
    build_parser_ = subparsers.add_parser("build")
    build_parser_.add_argument("config")
    build_parser_.add_argument("--vault", required=True)
    build_parser_.add_argument("--apply", action="store_true")
    build_parser_.add_argument("--extend-existing", action="store_true")
    build_parser_.add_argument("--expected-plan-sha256")
    build_parser_.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--vault", required=True)
    audit_parser.add_argument("--require-build-receipt", action="store_true")
    audit_parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT)
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--vault", required=True)
    recover_parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    import sys

    actual_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if any(item in {"-h", "--help"} for item in actual_argv):
            json_print(
                {
                    "status": "ok",
                    "operation": "help",
                    "commands": ["validate-config", "plan", "build", "audit", "recover"],
                }
            )
            return 0
        args = build_parser().parse_args(actual_argv)
        require_posix()
        try:
            contract_digest()
        except ContractError as exc:
            raise VaultError(
                "CONTRACT_BUILD_MISMATCH",
                "The vendored contract bundle failed its integrity check.",
                exit_code=7,
            ) from exc
        if args.command == "audit":
            result = audit(
                Path(args.vault),
                require_build_receipt=args.require_build_receipt,
                lock_timeout=args.lock_timeout,
            )
        elif args.command == "recover":
            result = recover(Path(args.vault), lock_timeout=args.lock_timeout)
        else:
            config_path = Path(args.config).expanduser().absolute()
            config = read_config(config_path)
            if args.command == "validate-config":
                result = {
                    "status": "ok",
                    "operation": "validate-config",
                    "config": str(config_path),
                    "config_sha256": config_digest(config),
                    "normalized": config,
                }
            elif args.command == "plan":
                result = build_plan(config, Path(args.vault))
            else:
                if args.apply and not args.expected_plan_sha256:
                    raise VaultError(
                        "MISSING_PLAN_CONFIRMATION",
                        "--expected-plan-sha256 is required with --apply.",
                        exit_code=2,
                    )
                result = build(
                    config,
                    Path(args.vault),
                    apply=args.apply,
                    extend_existing=args.extend_existing,
                    expected_plan_sha256=args.expected_plan_sha256,
                    lock_timeout=args.lock_timeout,
                )
        json_print(result)
        return 0
    except VaultError as exc:
        json_print(exc.as_dict(), stream=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        json_print(
            {
                "status": "error",
                "error": {"code": "INTERRUPTED", "message": "Operation interrupted."},
            },
            stream=sys.stderr,
        )
        return 130
    except OSError:
        json_print(
            {
                "status": "error",
                "error": {
                    "code": "FILESYSTEM_FAILED",
                    "message": "A filesystem operation failed; run recover before retrying.",
                },
            },
            stream=sys.stderr,
        )
        return 5
    except ContractError:
        json_print(
            VaultError(
                "CONTRACT_BUILD_MISMATCH",
                "The vendored contract bundle failed its integrity check.",
                exit_code=7,
            ).as_dict(),
            stream=sys.stderr,
        )
        return 7
    except Exception as exc:
        json_print(
            VaultError(
                "RUNTIME_FAILED",
                "The vault operation failed unexpectedly.",
                details=exc.__class__.__name__,
                exit_code=5,
            ).as_dict(),
            stream=sys.stderr,
        )
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
