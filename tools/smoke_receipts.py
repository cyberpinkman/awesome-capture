#!/usr/bin/env python3
"""Validate sanitized smoke receipts and compute implementation digests."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contracts.contract_runtime import (  # noqa: E402
    ContractError,
    read_json_strict,
    validate_contract,
)

CASES_PATH = ROOT / "smoke" / "cases.json"
RELEASE_SCOPE_PATH = ROOT / "smoke" / "release-scope.json"
FUTURE_SKEW = dt.timedelta(minutes=5)
CASES_SCHEMA = "awesome-capture.smoke-cases/v3"
RELEASE_SCOPE_SCHEMA = "awesome-capture.smoke-release-scope/v1"
CONTROLLED_FAULT_BINDINGS = {
    "twitter-gallery-fallback": (
        "twitter",
        "x-first-ytdlp-network-error-v1",
    ),
    "tiktok-gallery-fallback": (
        "tiktok",
        "tiktok-first-ytdlp-network-error-v1",
    ),
}
CONTROLLED_FAULT_TOOL = "awesome-capture-smoke-fault"
CONTROLLED_FAULT_WARNING = "controlled-ytdlp-network-error-injection"
CONTROLLED_FAULT_ASSERTIONS = {
    "controlled-fault-profile-applied",
    "controlled-ytdlp-command-verified",
    "controlled-ytdlp-network-error-observed",
    "controlled-ytdlp-fault-triggered-exactly-once",
    "controlled-production-fallback-gate-accepted",
    "controlled-gallery-dl-command-verified",
    "controlled-gallery-dl-executed-exactly-once",
    "controlled-downloader-entrypoints-stable",
    "controlled-fallback-fresh-output-observed",
}
CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ENV_NAME_PATTERN = re.compile(r"^AWESOME_CAPTURE_SMOKE_[A-Z0-9_]+$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
BINARY_CASES = {
    "whisper-cpp-local",
    "whisper-cpp-cpu",
    "whisper-cpp-gpu-fallback",
    "external-local",
    "external-long-resume",
}
# The 0.1.0 version boundary predates the immutable-tag release workflow and
# never had a tag or GitHub Release. Keep this one historical boundary pinned by
# its full commit identity; every later baseline must resolve through its
# published lightweight tag.
BOOTSTRAP_RELEASE_COMMITS = {
    "0.1.0": "f0f4c46f07aa1b508f7dac5e1586b25fbb879009",
}


def tracked_files() -> list[Path]:
    """Return the implementation surface, including not-yet-tracked source files."""

    result: set[Path] = set()
    roots = (
        ROOT / "contracts",
        ROOT / "skills",
        ROOT / "tools",
        ROOT / "tests",
        ROOT / ".github" / "workflows",
    )
    for directory in roots:
        if not directory.is_dir():
            continue
        for target in directory.rglob("*"):
            if (
                target.is_file()
                and "__pycache__" not in target.parts
                and target.suffix not in {".pyc", ".pyo"}
            ):
                result.add(target.relative_to(ROOT))
    for relative in (
        Path("AGENTS.md"),
        Path("README.md"),
        Path("SECURITY.md"),
        Path("requirements-ci.lock"),
        Path("smoke/cases.json"),
        Path("smoke/release-scope.json"),
    ):
        if (ROOT / relative).is_file():
            result.add(relative)
    return sorted(result, key=lambda item: item.as_posix())


def implementation_digest(paths: list[Path] | None = None) -> str:
    digest = hashlib.sha256()
    for relative in paths or tracked_files():
        target = ROOT / relative
        if not target.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(target.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def parse_timestamp(raw: str) -> dt.datetime:
    value = raw.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def load_case_registry() -> dict[str, dict[str, Any]]:
    try:
        value = read_json_strict(
            CASES_PATH,
            validate=False,
            maximum_bytes=1024 * 1024,
        )
    except (ContractError, OSError) as exc:
        raise ContractError(
            "SMOKE_CASES_INVALID",
            "Preregistered smoke cases are unavailable.",
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "cases"}
        or value["schema_version"] != CASES_SCHEMA
        or not isinstance(value["cases"], list)
    ):
        raise ContractError(
            "SMOKE_CASES_INVALID",
            "Preregistered smoke cases are malformed.",
        )
    registry: dict[str, dict[str, Any]] = {}
    common = {"case_id", "suite", "platform", "source_env", "required_tools"}
    for raw in value["cases"]:
        if not isinstance(raw, dict):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke case identity is malformed.",
            )
        suite = raw.get("suite")
        case_id = raw.get("case_id")
        if suite == "download":
            required = common | {"source_fingerprint"}
            allowed = required | {"expectation"}
            if (
                isinstance(case_id, str)
                and case_id in CONTROLLED_FAULT_BINDINGS
            ):
                required |= {"fault_profile"}
                allowed |= {"fault_profile"}
        elif suite == "transcription":
            required = common | {"engine", "model_env"}
            if raw.get("case_id") in BINARY_CASES:
                required |= {"binary_env"}
            allowed = required | {"expectation"}
        else:
            required = set()
            allowed = set()
        if (
            not required
            or not required.issubset(raw)
            or not set(raw).issubset(allowed)
            or not isinstance(case_id, str)
            or CASE_ID_PATTERN.fullmatch(case_id) is None
            or case_id in registry
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke case identity is malformed.",
            )
        expected_platforms = (
            {"douyin", "tiktok", "bilibili", "youtube", "twitter"}
            if suite == "download"
            else {"local"}
        )
        if (
            not isinstance(raw.get("platform"), str)
            or raw["platform"] not in expected_platforms
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke case platform is malformed.",
            )
        if suite == "transcription" and (
            not isinstance(raw.get("engine"), str)
            or raw["engine"]
            not in {
                "whisper-cpp",
                "faster-whisper",
                "mlx-whisper",
                "external",
            }
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke engine is malformed.",
            )
        for key, item in raw.items():
            if key.endswith("_env") and (
                not isinstance(item, str)
                or ENV_NAME_PATTERN.fullmatch(item) is None
            ):
                raise ContractError(
                    "SMOKE_CASES_INVALID",
                    "Preregistered smoke environment reference is malformed.",
                )
        if suite == "download" and (
            not isinstance(raw.get("source_fingerprint"), str)
            or SHA_PATTERN.fullmatch(raw["source_fingerprint"]) is None
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered download source identity is malformed.",
            )
        expectation = raw.get("expectation")
        valid_expectations = (
            {"ephemeral_browser", "gallery-dl"}
            if suite == "download"
            else {"cpu_only", "gpu_fallback", "sigkill_resume"}
        )
        if expectation is not None and (
            not isinstance(expectation, str)
            or expectation not in valid_expectations
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke expectation is malformed.",
            )
        fault_profile = raw.get("fault_profile")
        controlled_binding = CONTROLLED_FAULT_BINDINGS.get(case_id)
        if fault_profile is not None and (
            controlled_binding is None
            or suite != "download"
            or raw.get("platform") != controlled_binding[0]
            or fault_profile != controlled_binding[1]
            or expectation != "gallery-dl"
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered controlled fault binding is malformed.",
            )
        if (
            controlled_binding is not None
            and fault_profile != controlled_binding[1]
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Registered gallery fallback lacks its controlled fault profile.",
            )
        required_tools = raw.get("required_tools")
        if (
            not isinstance(required_tools, list)
            or not required_tools
            or any(
                not isinstance(item, str)
                or TOOL_NAME_PATTERN.fullmatch(item) is None
                for item in required_tools
            )
            or len(required_tools) != len(set(required_tools))
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered smoke tool evidence is malformed.",
            )
        if (CONTROLLED_FAULT_TOOL in required_tools) != (
            fault_profile is not None
        ):
            raise ContractError(
                "SMOKE_CASES_INVALID",
                "Preregistered controlled tool evidence binding is malformed.",
            )
        registry[case_id] = raw
    download_fingerprints = [
        case["source_fingerprint"]
        for case in registry.values()
        if case["suite"] == "download"
    ]
    if len(download_fingerprints) != len(set(download_fingerprints)):
        raise ContractError(
            "SMOKE_CASES_INVALID",
            "Preregistered download source identities are duplicated.",
        )
    registered_controlled_cases = {
        case_id
        for case_id, case in registry.items()
        if case.get("fault_profile") is not None
    }
    if (
        registered_controlled_cases != set(CONTROLLED_FAULT_BINDINGS)
        or len({binding[1] for binding in CONTROLLED_FAULT_BINDINGS.values()})
        != len(CONTROLLED_FAULT_BINDINGS)
    ):
        raise ContractError(
            "SMOKE_CASES_INVALID",
            "Preregistered controlled faults are incomplete or duplicated.",
        )
    return registry


def smoke_component_cases(
    registry: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    cases = registry if registry is not None else load_case_registry()
    components: dict[str, set[str]] = {}
    for case_id, case in cases.items():
        suite = case["suite"]
        if suite == "download":
            leaf = f"download:{case['platform']}"
        else:
            leaf = f"transcription:{case['engine']}"
        components.setdefault(suite, set()).add(case_id)
        components.setdefault(leaf, set()).add(case_id)
    return {
        component: sorted(case_ids)
        for component, case_ids in sorted(components.items())
    }


def load_release_scope(
    path: Path = RELEASE_SCOPE_PATH,
) -> dict[str, Any]:
    try:
        value = read_json_strict(
            path,
            validate=False,
            maximum_bytes=64 * 1024,
        )
    except (ContractError, OSError) as exc:
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Release smoke scope is unavailable.",
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "base_commit",
            "base_version",
            "external_impact",
            "required_components",
        }
        or value["schema_version"] != RELEASE_SCOPE_SCHEMA
        or not isinstance(value["base_commit"], str)
        or COMMIT_PATTERN.fullmatch(value["base_commit"]) is None
        or not isinstance(value["base_version"], str)
        or SEMVER_PATTERN.fullmatch(value["base_version"]) is None
        or not isinstance(value["external_impact"], str)
        or value["external_impact"] not in {"none", "selected"}
        or not isinstance(value["required_components"], list)
        or any(
            not isinstance(component, str)
            for component in value["required_components"]
        )
    ):
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Release smoke scope is malformed.",
        )
    components = value["required_components"]
    if (value["external_impact"] == "none") != (components == []):
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Release smoke scope impact and components are inconsistent.",
        )
    if components != sorted(components) or len(components) != len(set(components)):
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Release smoke components must be sorted and unique.",
        )
    available = smoke_component_cases()
    unknown = [component for component in components if component not in available]
    if unknown:
        raise ContractError(
            "SMOKE_COMPONENT_UNKNOWN",
            "Release smoke scope names an unknown component.",
        )
    selected = set(components)
    if any(
        ":" in component and component.split(":", 1)[0] in selected
        for component in components
    ):
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Release smoke scope contains redundant components.",
        )
    return {
        "base_commit": value["base_commit"],
        "base_version": value["base_version"],
        "external_impact": value["external_impact"],
        "required_components": components,
    }


def infer_components_for_paths(paths: list[str]) -> list[str]:
    inferred: set[str] = set()
    for raw in paths:
        if (
            not isinstance(raw, str)
            or not raw
            or raw.startswith("/")
            or "\\" in raw
            or any(component in {"", ".", ".."} for component in raw.split("/"))
        ):
            raise ContractError(
                "SMOKE_IMPACT_UNMAPPED",
                "Release change path cannot be classified safely.",
            )
        parts = raw.split("/")
        if raw == "smoke/cases.json":
            inferred.update({"download", "transcription"})
            continue
        if parts[0] == "contracts":
            inferred.update({"download", "transcription"})
            continue
        if (
            parts[:2] == ["skills", "download-video"]
            and parts[2:] != ["VERSION"]
        ):
            inferred.add("download")
            continue
        if (
            parts[:2] == ["skills", "transcribe-media"]
            and parts[2:] != ["VERSION"]
        ):
            inferred.add("transcription")
            continue
        if (
            parts[0] == "skills"
            and len(parts) >= 3
            and parts[2] == "scripts"
            and parts[1]
            not in {
                "build-obsidian-vault",
                "download-video",
                "ingest-knowledge",
                "transcribe-media",
            }
        ):
            raise ContractError(
                "SMOKE_IMPACT_UNMAPPED",
                "An unknown skill execution surface changed.",
            )
    return sorted(inferred)


def _strict_git_json(raw: bytes) -> Any:
    if len(raw) > 4 * 1024 * 1024:
        raise ContractError(
            "SMOKE_IMPACT_UNMAPPED",
            "Historical smoke registry is too large.",
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def reject_constant(_: str) -> None:
        raise ValueError("non-finite number")

    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError(
            "SMOKE_IMPACT_UNMAPPED",
            "Historical smoke registry cannot be classified safely.",
        ) from exc


def changed_case_registry_components(
    before: Any,
    after: Any,
) -> list[str]:
    def project(value: Any) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "cases"}
            or not isinstance(value["schema_version"], str)
            or not isinstance(value["cases"], list)
        ):
            raise ContractError(
                "SMOKE_IMPACT_UNMAPPED",
                "Smoke registry shape cannot be classified safely.",
            )
        cases: dict[str, dict[str, Any]] = {}
        for case in value["cases"]:
            if (
                not isinstance(case, dict)
                or not isinstance(case.get("case_id"), str)
                or case["case_id"] in cases
                or case.get("suite") not in {"download", "transcription"}
            ):
                raise ContractError(
                    "SMOKE_IMPACT_UNMAPPED",
                    "Smoke registry case cannot be classified safely.",
                )
            cases[case["case_id"]] = case
        return cases, {
            key: item
            for key, item in value.items()
            if key not in {"schema_version", "cases"}
        }

    before_cases, before_global = project(before)
    after_cases, after_global = project(after)
    if before_global != after_global:
        return ["download", "transcription"]
    inferred: set[str] = set()
    for case_id in set(before_cases) | set(after_cases):
        old = before_cases.get(case_id)
        new = after_cases.get(case_id)
        if old == new:
            continue
        for case in (old, new):
            if case is not None:
                inferred.add(case["suite"])
    return sorted(inferred)


def changed_case_components_since(base_commit: str) -> list[str]:
    try:
        before_raw = subprocess.run(
            ["git", "show", f"{base_commit}:smoke/cases.json"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
        after_raw = subprocess.run(
            ["git", "show", "HEAD:smoke/cases.json"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(
            "SMOKE_IMPACT_UNMAPPED",
            "Smoke registry history cannot be verified.",
        ) from exc
    return changed_case_registry_components(
        _strict_git_json(before_raw),
        _strict_git_json(after_raw),
    )


def changed_paths_since(base_commit: str, base_version: str) -> list[str]:
    if COMMIT_PATTERN.fullmatch(base_commit) is None:
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Release smoke baseline is malformed.",
        )
    try:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{base_commit}^{{commit}}"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_commit, "HEAD"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if exists.returncode != 0 or ancestor.returncode != 0:
            raise ContractError(
                "SMOKE_RELEASE_SCOPE_INVALID",
                "Release smoke baseline is unavailable or not an ancestor.",
            )
        tag_ref = f"refs/tags/v{base_version}"
        tag = subprocess.run(
            ["git", "show-ref", "--verify", "--hash", tag_ref],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if tag.returncode == 0:
            tag_type = subprocess.run(
                ["git", "cat-file", "-t", tag_ref],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
            ).stdout
            tag_commit = subprocess.run(
                ["git", "rev-parse", f"{tag_ref}^{{commit}}"],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
            ).stdout
            if (
                tag_type != b"commit\n"
                or tag_commit != f"{base_commit}\n".encode("ascii")
            ):
                raise ContractError(
                    "SMOKE_RELEASE_SCOPE_INVALID",
                    "Release smoke baseline tag does not identify its commit.",
                )
        elif BOOTSTRAP_RELEASE_COMMITS.get(base_version) != base_commit:
            raise ContractError(
                "SMOKE_RELEASE_SCOPE_INVALID",
                "Release smoke baseline has no immutable release identity.",
            )
        version = subprocess.run(
            ["git", "show", f"{base_commit}:VERSION"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
        changelog = subprocess.run(
            ["git", "show", f"{base_commit}:CHANGELOG.md"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout.decode("utf-8", errors="strict")
        if version != f"{base_version}\n".encode("ascii"):
            raise ContractError(
                "SMOKE_RELEASE_SCOPE_INVALID",
                "Release smoke baseline version does not match its commit.",
            )
        unreleased = re.search(
            r"(?ms)^## \[Unreleased\]\s*\n(.*?)(?=^## \[|\Z)",
            changelog,
        )
        released_heading = re.search(
            rf"(?m)^## \[{re.escape(base_version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
            changelog,
        )
        if (
            unreleased is None
            or unreleased.group(1).strip()
            or released_heading is None
        ):
            raise ContractError(
                "SMOKE_RELEASE_SCOPE_INVALID",
                "Release smoke baseline is not a completed release boundary.",
            )
        changed = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "--diff-filter=ACDMRTUXB",
                base_commit,
                "HEAD",
                "--",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        decoded = changed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(
            "SMOKE_IMPACT_UNMAPPED",
            "Release change paths are not valid UTF-8.",
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Release smoke baseline cannot be verified.",
        ) from exc
    paths = decoded.split("\0")
    if paths and paths[-1] == "":
        paths.pop()
    return paths


def version_at_head() -> str:
    try:
        raw = subprocess.run(
            ["git", "show", "HEAD:VERSION"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
        value = raw.decode("ascii", errors="strict")
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Candidate release version cannot be verified.",
        ) from exc
    if not value.endswith("\n") or SEMVER_PATTERN.fullmatch(value[:-1]) is None:
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Candidate release version is malformed.",
        )
    return value[:-1]


def release_metadata_at_head() -> tuple[list[str], bool]:
    try:
        raw = subprocess.run(
            ["git", "show", "HEAD:CHANGELOG.md"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
        changelog = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INVALID",
            "Candidate release changelog cannot be verified.",
        ) from exc
    versions = re.findall(
        r"(?m)^## \[((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
        r"(?:0|[1-9][0-9]*))\] - \d{4}-\d{2}-\d{2}$",
        changelog,
    )
    unreleased = re.search(
        r"(?ms)^## \[Unreleased\]\s*\n(.*?)(?=^## \[|\Z)",
        changelog,
    )
    return versions, unreleased is not None and not unreleased.group(1).strip()


def _semver_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def validate_release_scope(
    path: Path = RELEASE_SCOPE_PATH,
    *,
    require_prior_version: bool = False,
) -> dict[str, Any]:
    scope = load_release_scope(path)
    candidate_version = version_at_head()
    base_version = _semver_tuple(scope["base_version"])
    current_version = _semver_tuple(candidate_version)
    if base_version > current_version or (
        require_prior_version and base_version >= current_version
    ):
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_VERSION_MISMATCH",
            "Release smoke baseline version is not prior to the candidate.",
        )
    if require_prior_version:
        release_versions, unreleased_is_empty = release_metadata_at_head()
        if (
            not unreleased_is_empty
            or len(release_versions) < 2
            or release_versions[0] != candidate_version
            or release_versions[1] != scope["base_version"]
        ):
            raise ContractError(
                "SMOKE_RELEASE_SCOPE_VERSION_MISMATCH",
                "Release smoke baseline is not the prior changelog release.",
            )
    changed_paths = changed_paths_since(
        scope["base_commit"],
        scope["base_version"],
    )
    inferred = set(
        infer_components_for_paths(
            [path for path in changed_paths if path != "smoke/cases.json"]
        )
    )
    if "smoke/cases.json" in changed_paths:
        inferred.update(changed_case_components_since(scope["base_commit"]))
    inferred_components = sorted(inferred)
    component_cases = smoke_component_cases()
    inferred_cases = {
        case_id
        for component in inferred_components
        for case_id in component_cases[component]
    }
    declared_cases = {
        case_id
        for component in scope["required_components"]
        for case_id in component_cases[component]
    }
    if not inferred_cases.issubset(declared_cases):
        raise ContractError(
            "SMOKE_RELEASE_SCOPE_INCOMPLETE",
            "Release smoke scope omits a changed execution surface.",
        )
    return {
        **scope,
        "candidate_version": candidate_version,
        "inferred_components": inferred_components,
    }


def _required_assertions(case: dict[str, Any]) -> set[str]:
    if case["suite"] == "download":
        required = {
            "registered-source-detected",
            "registered-platform-matches",
            "registered-source-fingerprint-matches",
            "registered-source-is-canonical",
            "required-tools-observed",
            "download-command-succeeded",
            "video-artifact-v2-valid",
            "video-media-reverified",
        }
        if case.get("expectation"):
            required.add(f"required-{case['expectation']}-observed")
        else:
            required.add("anonymous-route-observed")
        controlled_binding = CONTROLLED_FAULT_BINDINGS.get(case["case_id"])
        if (
            controlled_binding is not None
            and case.get("platform") == controlled_binding[0]
            and case.get("fault_profile") == controlled_binding[1]
        ):
            required.update(CONTROLLED_FAULT_ASSERTIONS)
        return required
    required = {
        "registered-local-media-exists",
        "explicit-local-model-exists",
        "required-tools-observed",
        "transcription-command-succeeded",
        "transcript-artifact-v2-valid",
        "transcript-evidence-reverified",
    }
    if "binary_env" in case:
        required.add("explicit-local-binary-exists")
    expectation_assertions = {
        "cpu_only": {"whisper-cpp-cpu-only-observed"},
        "gpu_fallback": {"whisper-cpp-gpu-fallback-observed"},
        "sigkill_resume": {
            "partial-chunk-state-observed",
            "transcription-process-sigkilled",
            "long-transcription-resumed-after-sigkill",
            "partial-chunk-results-reused",
        },
    }
    required.update(expectation_assertions.get(case.get("expectation"), set()))
    return required


def validate_case_evidence(value: dict[str, Any]) -> None:
    registry = load_case_registry()
    case = registry.get(value["case_id"])
    if case is None:
        raise ContractError(
            "UNKNOWN_SMOKE_CASE",
            "Smoke receipt case_id is not preregistered.",
        )
    passing = value["outcome"] == "pass"
    assertion_names = [item["name"] for item in value["assertions"]]
    if len(assertion_names) != len(set(assertion_names)):
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Smoke assertion names must be unique.",
        )
    tool_names = [item["name"] for item in value["tools"]]
    if len(tool_names) != len(set(tool_names)):
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Smoke tool evidence names must be unique.",
        )
    tools = {item["name"]: item["version"] for item in value["tools"]}
    unknown_controlled_assertions = {
        name
        for name in assertion_names
        if name.startswith("controlled-")
        and name not in CONTROLLED_FAULT_ASSERTIONS
    }
    unknown_controlled_warnings = {
        warning
        for warning in value["warnings"]
        if warning.startswith("controlled-")
        and warning != CONTROLLED_FAULT_WARNING
    }
    unknown_controlled_tools = {
        name
        for name in tool_names
        if name.startswith(CONTROLLED_FAULT_TOOL)
        and name != CONTROLLED_FAULT_TOOL
    }
    if (
        unknown_controlled_assertions
        or unknown_controlled_warnings
        or unknown_controlled_tools
    ):
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Smoke receipt contains unknown controlled fault evidence.",
        )
    controlled_assertions_present = (
        CONTROLLED_FAULT_ASSERTIONS & set(assertion_names)
    )
    controlled_claimed = bool(
        controlled_assertions_present
        or CONTROLLED_FAULT_WARNING in value["warnings"]
        or CONTROLLED_FAULT_TOOL in tools
    )
    controlled_binding = CONTROLLED_FAULT_BINDINGS.get(case["case_id"])
    registered_fault = (
        controlled_binding is not None
        and case.get("platform") == controlled_binding[0]
        and case.get("fault_profile") == controlled_binding[1]
    )
    if controlled_claimed and not registered_fault:
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Unregistered smoke case claims a controlled fault.",
        )
    if registered_fault and controlled_claimed and (
        tools.get(CONTROLLED_FAULT_TOOL) != case["fault_profile"]
        or CONTROLLED_FAULT_WARNING not in value["warnings"]
        or controlled_assertions_present != CONTROLLED_FAULT_ASSERTIONS
    ):
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Controlled fault evidence is incomplete or inconsistent.",
        )
    if passing and registered_fault and not controlled_claimed:
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Passing controlled fallback receipt lacks its declared fault evidence.",
        )
    if case["suite"] == "download":
        if value["source"]["platform"] != case["platform"]:
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "Download smoke receipt does not match its registered platform.",
            )
        if passing and (
            value["engine"] is not None
            or value["source"]["fingerprint"] != case["source_fingerprint"]
        ):
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "Download smoke receipt does not match its registered source.",
            )
        if passing:
            expectation = case.get("expectation")
            if expectation is None:
                if (
                    value["source"]["auth_mode"] != "anonymous"
                    or value["source"]["fallback"] not in {None, "none"}
                ):
                    raise ContractError(
                        "SMOKE_CASE_MISMATCH",
                        "Anonymous smoke unexpectedly used authentication or fallback.",
                    )
            elif expectation == "ephemeral_browser":
                if (
                    value["source"]["auth_mode"] != "ephemeral_browser"
                    or value["source"]["fallback"] != expectation
                ):
                    raise ContractError(
                        "SMOKE_CASE_MISMATCH",
                        "Ephemeral browser evidence was not observed.",
                    )
            elif (
                value["source"]["fallback"] != expectation
                or (
                    expectation == "gallery-dl"
                    and value["source"]["auth_mode"] != "anonymous"
                )
            ):
                raise ContractError(
                    "SMOKE_CASE_MISMATCH",
                    "Registered download fallback evidence was not observed.",
                )
    else:
        engine = value["engine"]
        if value["source"]["platform"] != "local":
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "ASR smoke receipt does not match its registered engine.",
            )
        if not passing:
            if isinstance(engine, dict) and engine["name"] != case.get("engine"):
                raise ContractError(
                    "SMOKE_CASE_MISMATCH",
                    "Failed ASR smoke names another registered engine.",
                )
            return
        if (
            value["source"]["auth_mode"] != "not-applicable"
            or value["source"]["fallback"] is not None
            or not isinstance(engine, dict)
            or engine["name"] != case.get("engine")
        ):
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "ASR smoke receipt does not match its registered engine.",
            )
        if case.get("engine") == "mlx-whisper" and value["environment"]["os"] != "macos":
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "MLX smoke evidence must come from macOS.",
            )
    required_assertions = _required_assertions(case)
    if value["outcome"] == "pass" and set(assertion_names) != required_assertions:
        raise ContractError(
            "SMOKE_CASE_MISMATCH",
            "Passing receipt assertions do not exactly match the registered case.",
        )
    if value["outcome"] == "pass":
        if any(
            tools.get(name) in {None, "unavailable"}
            for name in case["required_tools"]
        ):
            raise ContractError(
                "SMOKE_CASE_MISMATCH",
                "Passing receipt lacks required tool version evidence.",
            )


def validate_receipt(
    path: Path,
    *,
    require_pass: bool,
    require_current_digest: bool,
) -> dict[str, Any]:
    value = read_json_strict(path, expected="smoke-receipt")
    validate_contract(value, expected="smoke-receipt")
    validate_case_evidence(value)
    if require_pass and value["outcome"] != "pass":
        raise ContractError("SMOKE_FAILED", "Smoke receipt outcome is not pass.")
    if value["outcome"] == "pass" and not all(
        assertion["passed"] for assertion in value["assertions"]
    ):
        raise ContractError(
            "INCONSISTENT_SMOKE_RECEIPT",
            "Passing receipt contains a failed assertion.",
        )
    now = dt.datetime.now(dt.timezone.utc)
    created_at = parse_timestamp(value["created_at"])
    if created_at > now + FUTURE_SKEW:
        raise ContractError(
            "FUTURE_SMOKE_RECEIPT",
            "Smoke receipt timestamp is in the future.",
        )
    if require_current_digest and value["implementation_digest"] != implementation_digest():
        raise ContractError(
            "STALE_SMOKE_RECEIPT",
            "Smoke receipt implementation digest does not match the current tree.",
        )
    return {
        "path": str(path),
        "case_id": value["case_id"],
        "outcome": value["outcome"],
        "implementation_digest": value["implementation_digest"],
    }


def validate_required_component_coverage(
    receipts: list[dict[str, str]],
    required_components: list[str],
) -> dict[str, Any]:
    components = smoke_component_cases()
    if any(component not in components for component in required_components):
        raise ContractError(
            "SMOKE_COMPONENT_UNKNOWN",
            "Required smoke coverage names an unknown component.",
        )
    required = {
        case_id
        for component in required_components
        for case_id in components[component]
    }
    receipts_by_case: dict[str, dict[str, str]] = {}
    for receipt in receipts:
        case_id = receipt["case_id"]
        if case_id in receipts_by_case:
            raise ContractError(
                "SMOKE_RECEIPT_SET_INVALID",
                "Smoke receipt case identities must be unique.",
            )
        receipts_by_case[case_id] = receipt
    missing = sorted(required - set(receipts_by_case))
    if missing:
        raise ContractError(
            "SMOKE_EVIDENCE_MISSING",
            "Required component smoke evidence is missing.",
        )
    current_digest = implementation_digest()
    for case_id in sorted(required):
        receipt = receipts_by_case[case_id]
        if receipt["outcome"] != "pass":
            raise ContractError(
                "SMOKE_FAILED",
                "Required component smoke receipt outcome is not pass.",
            )
        if receipt["implementation_digest"] != current_digest:
            raise ContractError(
                "STALE_SMOKE_RECEIPT",
                "Required component smoke receipt does not match the current tree.",
            )
    return {
        "required_components": required_components,
        "required_case_count": len(required),
        "covered_case_count": len(required & set(receipts_by_case)),
        "implementation_digest": current_digest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    digest_parser = subparsers.add_parser("digest")
    digest_parser.add_argument("paths", nargs="*")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("receipts", nargs="+")
    validate_parser.add_argument("--require-pass", action="store_true")
    validate_parser.add_argument("--require-current-digest", action="store_true")
    validate_parser.add_argument("--require-single", action="store_true")
    validate_parser.add_argument("--require-case")
    existing_parser = subparsers.add_parser(
        "validate-existing",
        help="Validate tracked receipts when present; an empty directory is valid for PR CI.",
    )
    existing_parser.add_argument(
        "--directory",
        default=str(ROOT / "smoke" / "receipts"),
    )
    existing_parser.add_argument("--require-pass", action="store_true")
    existing_parser.add_argument("--require-current-digest", action="store_true")
    existing_parser.add_argument(
        "--require-all-cases",
        action="store_true",
        help=(
            "Compatibility diagnostic that requires current passing evidence "
            "for both aggregate smoke suites; formal releases use validate-release."
        ),
    )
    components_parser = subparsers.add_parser(
        "components",
        help="List valid release smoke components and their registered cases.",
    )
    components_parser.set_defaults(command="components")
    scope_parser = subparsers.add_parser(
        "validate-scope",
        help="Validate the reviewed release smoke scope without requiring receipts.",
    )
    scope_parser.add_argument(
        "--scope",
        default=str(RELEASE_SCOPE_PATH),
    )
    release_parser = subparsers.add_parser(
        "validate-release",
        help=(
            "Validate every tracked receipt and require current passing evidence "
            "for the reviewed release scope."
        ),
    )
    release_parser.add_argument(
        "--directory",
        default=str(ROOT / "smoke" / "receipts"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "digest":
            paths = [Path(item) for item in args.paths] if args.paths else None
            result: dict[str, Any] = {
                "status": "ok",
                "implementation_digest": implementation_digest(paths),
            }
        elif args.command == "components":
            result = {
                "status": "ok",
                "components": smoke_component_cases(),
            }
        elif args.command == "validate-scope":
            scope = validate_release_scope(Path(args.scope))
            result = {
                "status": "ok",
                **scope,
            }
        elif args.command == "validate":
            if args.require_single and len(args.receipts) != 1:
                raise ContractError(
                    "SMOKE_RECEIPT_SET_INVALID",
                    "Exactly one smoke receipt is required.",
                )
            receipts = [
                validate_receipt(
                    Path(item),
                    require_pass=args.require_pass,
                    require_current_digest=args.require_current_digest,
                )
                for item in args.receipts
            ]
            if args.require_case is not None and any(
                receipt["case_id"] != args.require_case
                for receipt in receipts
            ):
                raise ContractError(
                    "SMOKE_CASE_MISMATCH",
                    "Smoke receipt does not match the requested case.",
                )
            result = {"status": "ok", "validated": receipts}
        elif args.command == "validate-existing":
            directory = Path(args.directory)
            paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
            receipts = [
                validate_receipt(
                    path,
                    require_pass=args.require_pass,
                    require_current_digest=args.require_current_digest,
                )
                for path in paths
            ]
            result = {
                "status": "ok",
                "validated": receipts,
                "receipt_count": len(receipts),
                **(
                    validate_required_component_coverage(
                        receipts,
                        ["download", "transcription"],
                    )
                    if args.require_all_cases
                    else {}
                ),
            }
        else:
            directory = Path(args.directory)
            paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
            receipts = [
                validate_receipt(
                    path,
                    require_pass=True,
                    require_current_digest=False,
                )
                for path in paths
            ]
            if any(
                path.name != f"{receipt['case_id']}.json"
                for path, receipt in zip(paths, receipts, strict=True)
            ):
                raise ContractError(
                    "SMOKE_RECEIPT_SET_INVALID",
                    "Release smoke receipt filenames must match their case identities.",
                )
            scope = validate_release_scope(require_prior_version=True)
            coverage = validate_required_component_coverage(
                receipts,
                scope["required_components"],
            )
            result = {
                "status": "ok",
                "base_commit": scope["base_commit"],
                "base_version": scope["base_version"],
                "candidate_version": scope["candidate_version"],
                "inferred_components": scope["inferred_components"],
                "validated_case_ids": sorted(
                    receipt["case_id"] for receipt in receipts
                ),
                "receipt_count": len(receipts),
                **coverage,
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ContractError, OSError, ValueError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "SMOKE_RECEIPT_INVALID"
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {"code": code, "message": "Smoke receipt validation failed."},
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
