#!/usr/bin/env python3
"""Plan, build, and audit a conservative Obsidian vault."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "awesome-capture.vault-config/v1"
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


def json_print(value: Any, *, stream: Any = None) -> None:
    import sys

    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        file=stream or sys.stdout,
    )


def safe_relative(raw: str, label: str) -> str:
    path = Path(raw)
    unsafe_characters = re.compile(r'[\x00-\x1f\x7f<>:"\\|?*#^%\[\]]')
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or unsafe_characters.search(raw)
        or "\\" in raw
    ):
        raise VaultError("INVALID_CONFIG", f"{label} is not a safe relative folder: {raw}", exit_code=2)
    return path.as_posix()


def validate_vault_target(vault: Path) -> Path:
    target = vault.expanduser().resolve()
    filesystem_root = Path(target.anchor).resolve()
    if target in {filesystem_root, Path.home().resolve()} or target.name == ".obsidian":
        raise VaultError(
            "UNSAFE_VAULT_TARGET",
            f"Refusing a filesystem root, home directory, or .obsidian directory as a vault: {target}",
            exit_code=2,
        )
    for ancestor in target.parents:
        if (ancestor / ".obsidian").is_dir():
            raise VaultError(
                "NESTED_VAULT",
                f"Target would be nested inside an existing Obsidian vault: {ancestor}",
                exit_code=2,
            )
    return target


def read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise VaultError("CONFIG_NOT_FOUND", f"Config does not exist: {path}", exit_code=2)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VaultError("INVALID_CONFIG", f"Cannot read config: {exc}", exit_code=2) from exc
    if not isinstance(value, dict):
        raise VaultError("INVALID_CONFIG", "Config must be one JSON object.", exit_code=2)
    return validate_config(value)


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
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


def home_note(config: dict[str, Any]) -> str:
    inbox = config["inbox_folder"]
    sources = config["sources_folder"]
    return "\n".join(
        [
            "---",
            f"title: {json.dumps(config['name'], ensure_ascii=False)}",
            f"created: {dt.date.today().isoformat()}",
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
    compact = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(compact.encode()).hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def build_plan(config: dict[str, Any], vault: Path) -> dict[str, Any]:
    vault = validate_vault_target(vault)
    files = desired_files(config)
    create_dirs: list[str] = []
    create_files: list[str] = []
    unchanged_files: list[str] = []
    conflicts: list[str] = []
    for folder in [".obsidian", ".awesome-capture", *config["folders"]]:
        destination = vault / folder
        if not is_within(destination, vault):
            conflicts.append(f"{Path(folder).as_posix()}/")
        elif not destination.exists():
            create_dirs.append(folder)
        elif not destination.is_dir() or destination.is_symlink():
            conflicts.append(f"{Path(folder).as_posix()}/")
    for relative, content in files.items():
        destination = vault / relative
        if not is_within(destination, vault):
            conflicts.append(relative.as_posix())
        elif not destination.exists():
            create_files.append(relative.as_posix())
        elif destination.is_file() and not destination.is_symlink():
            try:
                current = destination.read_text(encoding="utf-8")
            except OSError:
                conflicts.append(relative.as_posix())
            else:
                (unchanged_files if current == content else conflicts).append(relative.as_posix())
        else:
            conflicts.append(relative.as_posix())
    root_state = "missing" if not vault.exists() else "empty" if vault.is_dir() and not any(vault.iterdir()) else "existing"
    return {
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


def atomic_create(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise VaultError("PATH_COLLISION", f"Destination appeared during build: {path}", exit_code=4)
        os.link(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def build(config: dict[str, Any], vault: Path, *, apply: bool, extend_existing: bool) -> dict[str, Any]:
    vault = validate_vault_target(vault)
    plan = build_plan(config, vault)
    if not apply:
        return {**plan, "result": "dry-run"}
    receipt_path = vault / ".awesome-capture" / "vault-build.json"
    matching_receipt = False
    if receipt_path.exists() and (receipt_path.is_symlink() or not is_within(receipt_path, vault)):
        raise VaultError(
            "BUILD_CONFLICT",
            f"Existing build receipt is a symbolic link or escapes the vault: {receipt_path}",
            exit_code=4,
        )
    if receipt_path.is_file():
        try:
            current_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultError(
                "BUILD_CONFLICT",
                f"Existing build receipt is unreadable: {receipt_path}",
                details=str(exc),
                exit_code=4,
            ) from exc
        matching_receipt = current_receipt.get("config_sha256") == config_digest(config)
        if not matching_receipt:
            raise VaultError(
                "BUILD_CONFLICT",
                f"Existing build receipt differs: {receipt_path}",
                exit_code=4,
            )
    if plan["root_state"] == "existing" and not extend_existing and not matching_receipt:
        raise VaultError(
            "EXISTING_VAULT_REQUIRES_OPT_IN",
            "Target directory is non-empty. Preview it, then pass --extend-existing explicitly.",
            exit_code=4,
        )
    if plan["conflicts"]:
        raise VaultError(
            "BUILD_CONFLICT",
            "Existing files differ from the requested vault.",
            details="\n".join(plan["conflicts"]),
            exit_code=4,
        )
    vault.mkdir(parents=True, exist_ok=True)
    for relative in [".obsidian", ".awesome-capture", *config["folders"]]:
        (vault / relative).mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for relative, content in desired_files(config).items():
        destination = vault / relative
        if destination.is_file():
            continue
        atomic_create(destination, content)
        created.append(relative.as_posix())
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "built_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "config_sha256": config_digest(config),
        "config": config,
        "created_files": created,
    }
    if not receipt_path.exists():
        atomic_create(receipt_path, stable_json(receipt))
    return {
        **plan,
        "operation": "build",
        "result": "created" if created else "unchanged",
        "created_files": created,
        "receipt_path": str(receipt_path),
    }


def audit(vault: Path) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    if not vault.is_dir():
        findings.append({"severity": "error", "code": "VAULT_NOT_FOUND", "path": str(vault)})
    else:
        if not (vault / ".obsidian").is_dir():
            findings.append({"severity": "error", "code": "MISSING_OBSIDIAN_CONFIG", "path": ".obsidian"})
        if not (vault / "Home.md").is_file():
            findings.append({"severity": "warning", "code": "MISSING_HOME_NOTE", "path": "Home.md"})
        receipt_path = vault / ".awesome-capture" / "vault-build.json"
        if receipt_path.is_symlink() or not is_within(receipt_path, vault):
            findings.append({"severity": "error", "code": "UNSAFE_BUILD_RECEIPT", "path": str(receipt_path)})
        elif receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                validate_config(receipt["config"])
            except (OSError, json.JSONDecodeError, KeyError, VaultError) as exc:
                findings.append({"severity": "error", "code": "INVALID_BUILD_RECEIPT", "path": str(receipt_path), "details": str(exc)})
        else:
            findings.append({"severity": "info", "code": "NO_BUILD_RECEIPT", "path": str(receipt_path)})
    return {
        "status": "ok",
        "operation": "audit",
        "vault": str(vault),
        "healthy": not any(item["severity"] == "error" for item in findings),
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--vault", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    import sys

    args = build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            result = audit(Path(args.vault).expanduser().resolve())
        else:
            config_path = Path(args.config).expanduser().resolve()
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
                result = build_plan(config, Path(args.vault).expanduser().resolve())
            else:
                result = build(
                    config,
                    Path(args.vault).expanduser().resolve(),
                    apply=args.apply,
                    extend_existing=args.extend_existing,
                )
        json_print(result)
        return 0
    except VaultError as exc:
        json_print(exc.as_dict(), stream=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
