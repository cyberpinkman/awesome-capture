"""Strict, dependency-free runtime for Awesome Capture JSON contracts.

The JSON Schema documents are the public wire specification.  This module
implements the deliberately small Draft 2020-12 keyword subset used by those
schemas, plus cross-field and optional filesystem checks that JSON Schema
cannot express.  It is copied verbatim into every standalone skill.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit


SMALL_DOCUMENT_LIMIT = 4 * 1024 * 1024
LARGE_DOCUMENT_LIMIT = 64 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|password|"
    r"private[_-]?header|secret|signature|token)",
    re.IGNORECASE,
)
SENSITIVE_METADATA_PATTERN = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|"
    r"\b(?:api[_-]?key|authorization|bearer|cookie|credential|password|"
    r"private[_-]?header|secret|signature|token)\s*[:=])",
    re.IGNORECASE,
)

CONTRACT_NAMES: tuple[str, ...] = (
    "video-artifact",
    "transcript-artifact",
    "transcription-state",
    "chunk-set",
    "transaction",
    "vault-config",
    "vault-build-receipt",
    "ingest-receipt",
    "smoke-receipt",
)

_SCHEMA_FILES = {
    "video-artifact": "artifact-video-v2.schema.json",
    "transcript-artifact": "artifact-transcript-v2.schema.json",
    "transcription-state": "transcription-state-v1.schema.json",
    "chunk-set": "chunk-set-v1.schema.json",
    "transaction": "transaction-v1.schema.json",
    "vault-config": "vault-config-v1.schema.json",
    "vault-build-receipt": "vault-build-receipt-v1.schema.json",
    "ingest-receipt": "ingest-receipt-v1.schema.json",
    "smoke-receipt": "smoke-receipt-v1.schema.json",
}

_VERSION_TO_NAME = {
    "awesome-capture.transcription-state/v1": "transcription-state",
    "awesome-capture.chunk-set/v1": "chunk-set",
    "awesome-capture.transaction/v1": "transaction",
    "awesome-capture.vault-config/v1": "vault-config",
    "awesome-capture.vault-build-receipt/v1": "vault-build-receipt",
    "awesome-capture.ingest-receipt/v1": "ingest-receipt",
    "awesome-capture.smoke-receipt/v1": "smoke-receipt",
}

_EXPECTED_VERSIONS = {
    "video-artifact": "awesome-capture.artifact/v2",
    "transcript-artifact": "awesome-capture.artifact/v2",
    **{name: version for version, name in _VERSION_TO_NAME.items()},
}

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


class ContractError(ValueError):
    """A stable validation failure suitable for translation to CLI errors."""

    def __init__(self, code: str, message: str, *, path: str = "$"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


def _reject_constant(value: str) -> None:
    raise ContractError("INVALID_JSON", f"JSON numeric constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("DUPLICATE_JSON_KEY", f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _bounded_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > 4300:
        raise ContractError(
            "INVALID_JSON",
            "JSON integer exceeds the explicit 4300-digit limit.",
        )
    return int(value)


def loads_strict(
    data: str | bytes,
    *,
    max_bytes: int = SMALL_DOCUMENT_LIMIT,
) -> Any:
    """Parse strict UTF-8 JSON, rejecting duplicates and non-finite numbers."""

    if isinstance(data, bytes):
        raw = data
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError("INVALID_JSON", "JSON must be valid UTF-8.") from exc
    elif isinstance(data, str):
        text = data
        try:
            raw = data.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ContractError(
                "INVALID_JSON",
                "JSON contains an invalid Unicode scalar value.",
            ) from exc
    else:
        raise ContractError("INVALID_JSON", "JSON input must be str or bytes.")
    if len(raw) > max_bytes:
        raise ContractError(
            "JSON_TOO_LARGE",
            f"JSON exceeds the {max_bytes}-byte limit.",
        )
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_int=_bounded_json_integer,
        )
        _reject_nonfinite(value)
        return value
    except ContractError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ContractError("INVALID_JSON", "Malformed or unsupported JSON input.") from exc


def _document_limit(value: Any) -> int:
    if not isinstance(value, dict):
        return SMALL_DOCUMENT_LIMIT
    if value.get("schema_version") == "awesome-capture.transcription-state/v1":
        return LARGE_DOCUMENT_LIMIT
    if (
        value.get("schema_version") == "awesome-capture.artifact/v2"
        and value.get("artifact_type") == "transcript"
    ):
        return LARGE_DOCUMENT_LIMIT
    return SMALL_DOCUMENT_LIMIT


def _absolute_without_symlink_resolution(path: str | os.PathLike[str]) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = Path(os.path.abspath(target))
    # macOS exposes /tmp and /var as fixed system aliases.  Normalize only
    # those two aliases; arbitrary symlinks remain forbidden.
    if sys.platform == "darwin" and len(target.parts) > 1:
        aliases = {"tmp": Path("/private/tmp"), "var": Path("/private/var")}
        replacement = aliases.get(target.parts[1])
        if (
            replacement is not None
            and Path(os.path.realpath(f"/{target.parts[1]}")) == replacement
        ):
            target = replacement.joinpath(*target.parts[2:])
    return target


def _open_parent_directory_no_follow(target: Path, *, error_code: str) -> tuple[int, str]:
    """Open ``target``'s parent one component at a time without following links."""

    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC")):
        raise ContractError(
            "UNSUPPORTED_PLATFORM",
            "Secure contract file access requires POSIX no-follow directory operations.",
        )
    absolute = _absolute_without_symlink_resolution(target)
    parts = absolute.parts
    if len(parts) < 2 or absolute.name in {"", ".", ".."}:
        raise ContractError(error_code, "Contract path must name a file.")
    try:
        directory_fd = os.open(
            parts[0],
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        for component in parts[1:-1]:
            if component in {"", ".", ".."}:
                raise ContractError(error_code, "Unsafe contract path component.")
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            metadata = os.fstat(next_fd)
            if metadata.st_mode & stat.S_IWOTH and not metadata.st_mode & stat.S_ISVTX:
                os.close(next_fd)
                raise ContractError(
                    error_code,
                    "Contract path traverses a non-sticky world-writable directory.",
                )
            os.close(directory_fd)
            directory_fd = next_fd
    except ContractError:
        try:
            os.close(directory_fd)
        except (OSError, UnboundLocalError):
            pass
        raise
    except FileNotFoundError as exc:
        try:
            os.close(directory_fd)
        except (OSError, UnboundLocalError):
            pass
        code = "JSON_NOT_READABLE" if error_code == "UNSAFE_JSON_FILE" else error_code
        raise ContractError(code, "Contract path does not exist.") from exc
    except OSError as exc:
        try:
            os.close(directory_fd)
        except (OSError, UnboundLocalError):
            pass
        raise ContractError(error_code, "Contract path cannot be opened safely.") from exc
    return directory_fd, absolute.name


def _open_regular_file_no_follow(
    path: str | os.PathLike[str],
    *,
    error_code: str,
) -> tuple[int, os.stat_result]:
    parent_fd, name = _open_parent_directory_no_follow(Path(path), error_code=error_code)
    try:
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except FileNotFoundError as exc:
            code = "JSON_NOT_READABLE" if error_code == "UNSAFE_JSON_FILE" else error_code
            raise ContractError(code, "Contract file does not exist.") from exc
        except OSError as exc:
            raise ContractError(error_code, "Contract file cannot be opened safely.") from exc
    finally:
        os.close(parent_fd)
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
    ):
        os.close(fd)
        raise ContractError(
            error_code,
            "Contract file must be a current-user-owned, non-hard-linked regular file.",
        )
    return fd, metadata


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_json_document(
    path: str | os.PathLike[str],
    *,
    expected: str | None = None,
    validate: bool = True,
    maximum_bytes: int | None = None,
) -> tuple[Any, bytes]:
    """Read stable JSON bytes from one held descriptor and parse them strictly."""

    if maximum_bytes is not None and (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes <= 0
        or maximum_bytes > LARGE_DOCUMENT_LIMIT
    ):
        raise ContractError(
            "INVALID_JSON_LIMIT",
            f"JSON limit must be between 1 and {LARGE_DOCUMENT_LIMIT} bytes.",
        )
    hard_limit = maximum_bytes or LARGE_DOCUMENT_LIMIT
    try:
        fd, metadata = _open_regular_file_no_follow(
            path,
            error_code="UNSAFE_JSON_FILE",
        )
    except ContractError:
        raise
    if metadata.st_size > hard_limit:
        os.close(fd)
        raise ContractError(
            "JSON_TOO_LARGE",
            f"JSON exceeds the {hard_limit}-byte hard limit.",
        )
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, min(1024 * 1024, hard_limit + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > hard_limit:
                raise ContractError(
                    "JSON_TOO_LARGE",
                    f"JSON exceeds the {hard_limit}-byte hard limit.",
                )
        final_metadata = os.fstat(fd)
        if _metadata_identity(final_metadata) != _metadata_identity(metadata):
            raise ContractError(
                "UNSAFE_JSON_FILE",
                "JSON input changed while it was being read.",
            )
        raw = b"".join(chunks)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError("JSON_NOT_READABLE", "Cannot read JSON file safely.") from exc
    finally:
        os.close(fd)
    value = loads_strict(raw, max_bytes=hard_limit)
    limit = _document_limit(value)
    if len(raw) > limit:
        raise ContractError("JSON_TOO_LARGE", f"JSON exceeds the {limit}-byte contract limit.")
    if validate:
        validate_contract(value, expected=expected)
    return value, raw


def read_json_strict(
    path: str | os.PathLike[str],
    *,
    expected: str | None = None,
    validate: bool = True,
    maximum_bytes: int | None = None,
) -> Any:
    """Read a regular non-symlink JSON file and optionally validate its contract."""

    value, unused_raw = _read_json_document(
        path,
        expected=expected,
        validate=validate,
        maximum_bytes=maximum_bytes,
    )
    del unused_raw
    return value


def read_json_strict_with_sha256(
    path: str | os.PathLike[str],
    *,
    expected: str | None = None,
    validate: bool = True,
    maximum_bytes: int | None = None,
) -> tuple[Any, str]:
    """Return strict JSON and the SHA-256 of the exact bytes parsed."""

    value, raw = _read_json_document(
        path,
        expected=expected,
        validate=validate,
        maximum_bytes=maximum_bytes,
    )
    return value, hashlib.sha256(raw).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical UTF-8 representation used for contract identities."""

    _reject_nonfinite(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_nonfinite(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("NON_FINITE_NUMBER", "Numbers must be finite.", path=path)
    if isinstance(value, str) and any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise ContractError(
            "INVALID_UNICODE",
            "Strings must contain only valid Unicode scalar values.",
            path=path,
        )
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractError(
                    "INVALID_JSON",
                    "JSON object keys must be strings.",
                    path=path,
                )
            if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                raise ContractError(
                    "INVALID_UNICODE",
                    "JSON object keys must contain valid Unicode scalar values.",
                    path=path,
                )
            _reject_nonfinite(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonfinite(child, f"{path}[{index}]")


def _schema_root() -> Path:
    return Path(__file__).resolve().parent / "schemas"


def _manifest_path() -> Path:
    return Path(__file__).resolve().parent / "manifest.json"


def _load_schema(name: str) -> dict[str, Any]:
    if name not in _SCHEMA_FILES:
        raise ContractError("UNKNOWN_CONTRACT", f"Unknown contract name: {name}")
    if name not in _SCHEMA_CACHE:
        path = _schema_root() / _SCHEMA_FILES[name]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ContractError("CONTRACT_BUILD_MISMATCH", f"Missing schema: {path.name}") from exc
        value = loads_strict(raw, max_bytes=SMALL_DOCUMENT_LIMIT)
        if not isinstance(value, dict):
            raise ContractError("CONTRACT_BUILD_MISMATCH", f"Schema is not an object: {path.name}")
        _SCHEMA_CACHE[name] = value
    return _SCHEMA_CACHE[name]


def detect_contract(value: Any) -> str:
    """Return the contract name, rejecting legacy, absent, and unknown versions."""

    if not isinstance(value, dict):
        raise ContractError("INVALID_CONTRACT", "Contract document must be a JSON object.")
    version = value.get("schema_version")
    if not isinstance(version, str):
        raise ContractError(
            "UNSUPPORTED_SCHEMA_VERSION",
            "schema_version is required and must be a string.",
            path="$.schema_version",
        )
    if version == "awesome-capture.artifact/v2":
        artifact_type = value.get("artifact_type")
        if artifact_type == "video":
            return "video-artifact"
        if artifact_type == "transcript":
            return "transcript-artifact"
        raise ContractError(
            "UNKNOWN_CONTRACT",
            "artifact_type must be video or transcript.",
            path="$.artifact_type",
        )
    name = _VERSION_TO_NAME.get(version)
    if name:
        return name
    raise ContractError(
        "UNSUPPORTED_SCHEMA_VERSION",
        f"Unsupported schema_version: {version}",
        path="$.schema_version",
    )


def validate_contract(value: Any, *, expected: str | None = None) -> str:
    """Validate structure and semantic invariants, returning the contract name."""

    _reject_nonfinite(value)
    name = detect_contract(value)
    if expected is not None:
        if expected not in CONTRACT_NAMES:
            raise ContractError("UNKNOWN_CONTRACT", f"Unknown expected contract: {expected}")
        if name != expected:
            raise ContractError(
                "WRONG_CONTRACT_TYPE",
                f"Expected {expected}, received {name}.",
            )
    schema = _load_schema(name)
    _validate_schema_node(value, schema, schema, "$")
    _SEMANTIC_VALIDATORS[name](value)
    return name


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ContractError("CONTRACT_BUILD_MISMATCH", f"Unsupported schema type: {expected}")


def _json_equal(left: Any, right: Any) -> bool:
    """Apply JSON Schema equality without Python's bool/int equivalence."""

    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if (
        isinstance(left, (int, float))
        and isinstance(right, (int, float))
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    ):
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_json_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and set(left) == set(right)
            and all(_json_equal(left[key], right[key]) for key in left)
        )
    return type(left) is type(right) and left == right


def _resolve_ref(root: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise ContractError("CONTRACT_BUILD_MISMATCH", f"Only local $ref is supported: {reference}")
    current: Any = root
    for raw in reference[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise ContractError("CONTRACT_BUILD_MISMATCH", f"Unresolvable $ref: {reference}")
        current = current[part]
    if not isinstance(current, dict):
        raise ContractError("CONTRACT_BUILD_MISMATCH", f"$ref is not a schema: {reference}")
    return current


def _validate_schema_node(
    value: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
) -> None:
    if "$ref" in schema:
        _validate_schema_node(value, _resolve_ref(root, str(schema["$ref"])), root, path)
        return
    if "allOf" in schema:
        for item in schema["allOf"]:
            _validate_schema_node(value, item, root, path)
    if "anyOf" in schema:
        successes = 0
        for item in schema["anyOf"]:
            try:
                _validate_schema_node(value, item, root, path)
                successes += 1
            except ContractError:
                pass
        if successes == 0:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "Value matches no anyOf branch.", path=path)
    if "oneOf" in schema:
        successes = 0
        for item in schema["oneOf"]:
            try:
                _validate_schema_node(value, item, root, path)
                successes += 1
            except ContractError:
                pass
        if successes != 1:
            raise ContractError(
                "SCHEMA_VALIDATION_FAILED",
                f"Value must match exactly one oneOf branch; matched {successes}.",
                path=path,
            )
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise ContractError(
            "SCHEMA_VALIDATION_FAILED",
            f"Value must equal {schema['const']!r}.",
            path=path,
        )
    if "enum" in schema and not any(
        _json_equal(value, candidate) for candidate in schema["enum"]
    ):
        raise ContractError("SCHEMA_VALIDATION_FAILED", "Value is not in the allowed enum.", path=path)
    type_rule = schema.get("type")
    if type_rule is not None:
        allowed = [type_rule] if isinstance(type_rule, str) else list(type_rule)
        if not any(_json_type_matches(value, item) for item in allowed):
            raise ContractError(
                "SCHEMA_VALIDATION_FAILED",
                f"Expected JSON type {' or '.join(allowed)}.",
                path=path,
            )
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ContractError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"Missing required property: {key}",
                    path=path,
                )
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ContractError("CONTRACT_BUILD_MISMATCH", "properties must be an object.")
        patterns = schema.get("patternProperties", {})
        if not isinstance(patterns, dict):
            raise ContractError("CONTRACT_BUILD_MISMATCH", "patternProperties must be an object.")
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in properties:
                _validate_schema_node(child, properties[key], root, child_path)
                continue
            matched_patterns = [
                child_schema
                for pattern, child_schema in patterns.items()
                if re.search(pattern, key) is not None
            ]
            if matched_patterns:
                for child_schema in matched_patterns:
                    _validate_schema_node(child, child_schema, root, child_path)
            elif additional is False:
                raise ContractError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"Unknown property: {key}",
                    path=child_path,
                )
            elif isinstance(additional, dict):
                _validate_schema_node(child, additional, root, child_path)
        minimum = schema.get("minProperties")
        maximum = schema.get("maxProperties")
        if minimum is not None and len(value) < minimum:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "Too few object properties.", path=path)
        if maximum is not None and len(value) > maximum:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "Too many object properties.", path=path)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "Array has too few items.", path=path)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "Array has too many items.", path=path)
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for index, child in enumerate(value):
                if any(_json_equal(child, prior) for prior in seen):
                    raise ContractError(
                        "SCHEMA_VALIDATION_FAILED",
                        "Array items must be unique.",
                        path=f"{path}[{index}]",
                    )
                seen.append(child)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                _validate_schema_node(child, item_schema, root, f"{path}[{index}]")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "String is too short.", path=path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "String is too long.", path=path)
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "String does not match pattern.", path=path)
        if schema.get("format") == "date-time":
            valid_datetime = RFC3339_PATTERN.fullmatch(value) is not None
            if valid_datetime:
                try:
                    parsed_datetime = dt.datetime.fromisoformat(
                        value[:-1] + "+00:00" if value.endswith("Z") else value
                    )
                    valid_datetime = (
                        parsed_datetime.tzinfo is not None
                        and parsed_datetime.utcoffset() is not None
                    )
                except ValueError:
                    valid_datetime = False
            if not valid_datetime:
                raise ContractError(
                    "SCHEMA_VALIDATION_FAILED",
                    "Invalid RFC 3339 date-time.",
                    path=path,
                )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "Number is below minimum.", path=path)
        if "maximum" in schema and value > schema["maximum"]:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "Number is above maximum.", path=path)
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "Number is below exclusive minimum.", path=path)
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ContractError("SCHEMA_VALIDATION_FAILED", "Number is above exclusive maximum.", path=path)


def _require_sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Expected lowercase SHA-256.", path=path)
    return value


def _require_local_contract_digest(value: Any, path: str) -> None:
    digest = _require_sha256(value, path)
    if digest != contract_digest():
        raise ContractError(
            "CONTRACT_BUILD_MISMATCH",
            "Producer and consumer use different contract bundles.",
            path=path,
        )


def _require_absolute(value: Any, path: str) -> PurePosixPath:
    if not isinstance(value, str) or "\0" in value:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Expected an absolute POSIX path.", path=path)
    candidate = PurePosixPath(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Expected an absolute POSIX path.", path=path)
    return candidate


def _require_relative(value: Any, path: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Expected a safe relative POSIX path.", path=path)
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Expected a safe relative POSIX path.", path=path)
    return candidate


def _walk(value: Any, visit: Callable[[str, Any], None], path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            visit(key, child)
            _walk(child, visit, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, visit, f"{path}[{index}]")


def _reject_sensitive_keys(value: Any) -> None:
    def visit(key: str, child: Any) -> None:
        del child
        if SENSITIVE_KEY_PATTERN.search(key):
            raise ContractError(
                "SENSITIVE_DATA_FORBIDDEN",
                f"Sensitive field names are forbidden: {key}",
            )

    _walk(value, visit)


def _reject_sensitive_metadata(value: Any, path: str) -> None:
    """Reject secrets, raw URLs and private path-like text in controlled metadata."""

    if value is None:
        return
    if not isinstance(value, str):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Controlled metadata must be a string.",
            path=path,
        )
    if (
        any(ord(character) < 0x20 for character in value)
        or "/" in value
        or "\\" in value
        or "@" in value
        or SENSITIVE_METADATA_PATTERN.search(value)
    ):
        raise ContractError(
            "SENSITIVE_DATA_FORBIDDEN",
            "Controlled metadata contains a URL, private path, or sensitive value.",
            path=path,
        )


def _validate_public_url(value: str, platform: str, path: str) -> None:
    try:
        parsed = urlsplit(value)
        hostname_value = parsed.hostname
        port = parsed.port
        query_pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            max_num_fields=16,
        )
    except ValueError as exc:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Source URL is malformed.",
            path=path,
        ) from exc
    if (
        parsed.scheme != "https"
        or not hostname_value
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Source URL must be public HTTPS without userinfo or fragment.",
            path=path,
        )
    suffixes = {
        "douyin": ("douyin.com", "iesdouyin.com"),
        "tiktok": ("tiktok.com",),
        "bilibili": ("bilibili.com", "b23.tv"),
        "youtube": ("youtube.com", "youtu.be"),
        "twitter": ("x.com", "twitter.com"),
    }
    hostname = hostname_value.lower().rstrip(".")
    if not any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes[platform]):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Source URL host does not match platform.",
            path=path,
        )
    allowed = {
        "youtube": {"v": r"[A-Za-z0-9_-]{1,128}"},
        "bilibili": {
            "bvid": r"[A-Za-z0-9_-]{1,128}",
            "p": r"[1-9][0-9]{0,5}",
        },
        "douyin": {"modal_id": r"[0-9]{1,32}"},
        "tiktok": {},
        "twitter": {},
    }[platform]
    seen: set[str] = set()
    for key, query_value in query_pairs:
        if (
            key in seen
            or key not in allowed
            or re.fullmatch(allowed[key], query_value) is None
        ):
            raise ContractError(
                "SENSITIVE_DATA_FORBIDDEN",
                "Source URL contains a non-canonical public query parameter.",
                path=path,
            )
        seen.add(key)


def video_probe_evidence_sha256(media: Mapping[str, Any]) -> str:
    """Digest the normalized ffprobe facts carried by video artifact v2."""

    return canonical_json_sha256(
        {
            "schema_version": "awesome-capture.ffprobe-evidence/v1",
            "duration_ms": media["duration_ms"],
            "has_video": media["has_video"],
            "has_audio": media["has_audio"],
            "container": media["container"],
            "video_streams": media["video_streams"],
            "audio_streams": media["audio_streams"],
        }
    )


def _validate_video(value: dict[str, Any]) -> None:
    _reject_sensitive_keys(value)
    _require_sha256(value["source"]["fingerprint"], "$.source.fingerprint")
    platform = value["source"]["platform"]
    for key in ("id", "title", "author", "extractor"):
        if key in value["source"]:
            _reject_sensitive_metadata(
                value["source"][key],
                f"$.source.{key}",
            )
    for key in ("url", "webpage_url"):
        if key in value["source"]:
            _validate_public_url(value["source"][key], platform, f"$.source.{key}")
    if "url" in value["source"]:
        expected_fingerprint = hashlib.sha256(value["source"]["url"].encode("utf-8")).hexdigest()
        if value["source"]["fingerprint"] != expected_fingerprint:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Source fingerprint does not match source.url.",
                path="$.source.fingerprint",
            )
    media = value["media"]
    _require_absolute(media["path"], "$.media.path")
    _require_sha256(media["sha256"], "$.media.sha256")
    if not media["has_video"]:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Video artifact must contain video.", path="$.media.has_video")
    if media["video_streams"] < 1:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "video_streams must be positive.", path="$.media.video_streams")
    if media["has_audio"] != (media["audio_streams"] > 0):
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "has_audio must match audio_streams.", path="$.media.has_audio")
    probe = media["ffprobe"]
    _reject_sensitive_metadata(probe["version"], "$.media.ffprobe.version")
    _require_sha256(
        probe["evidence_sha256"],
        "$.media.ffprobe.evidence_sha256",
    )
    if probe["evidence_sha256"] != video_probe_evidence_sha256(media):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "ffprobe evidence digest does not match normalized media facts.",
            path="$.media.ffprobe.evidence_sha256",
        )
    for index, warning in enumerate(value["acquisition"]["warnings"]):
        _reject_sensitive_metadata(
            warning,
            f"$.acquisition.warnings[{index}]",
        )
    _reject_sensitive_metadata(
        value["acquisition"]["fallback"],
        "$.acquisition.fallback",
    )
    for key in ("tool", "version"):
        if key in value["producer"]:
            _reject_sensitive_metadata(
                value["producer"][key],
                f"$.producer.{key}",
            )
    _require_local_contract_digest(value["producer"]["contract_digest"], "$.producer.contract_digest")


def _content_identity_projection(
    component: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if component is None:
        return None
    return {
        key: component[key]
        for key in ("kind", "sha256", "bytes", "file_count", "version")
        if key in component
    }


def _engine_identity_projection(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "model": _content_identity_projection(identity["model"]),
        "executable": _content_identity_projection(identity["executable"]),
        "adapter": _content_identity_projection(identity["adapter"]),
        "packages": identity["packages"],
    }


def _transcription_settings_identity(
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "contract_digest": settings["contract_digest"],
        "algorithm": settings["algorithm"],
        "source_sha256": settings["source_sha256"],
        "source_bytes": settings["source_bytes"],
        "upstream_artifact_sha256": settings["upstream_artifact_sha256"],
        "engine": settings["engine"],
        "engine_identity_sha256": settings["engine_identity"]["identity_sha256"],
        "requested_language": settings["requested_language"],
        "chunk_seconds": settings["chunk_seconds"],
        "whisper_cpp_cpu_only": settings["whisper_cpp_cpu_only"],
        "sidecar_sha256": settings["sidecar_sha256"],
    }


def _validate_engine_identity(
    identity: dict[str, Any],
    engine: str,
    *,
    path: str,
    sidecar_present: bool,
) -> None:
    _require_sha256(identity["identity_sha256"], f"{path}.identity_sha256")
    for name in ("model", "executable", "adapter"):
        component = identity[name]
        if component is not None:
            _require_absolute(component["path"], f"{path}.{name}.path")
            _require_sha256(component["sha256"], f"{path}.{name}.sha256")
            if component["kind"] == "directory" and "file_count" not in component:
                raise ContractError(
                    "SEMANTIC_VALIDATION_FAILED",
                    "Directory content identity requires file_count.",
                    path=f"{path}.{name}.file_count",
                )
            if component["kind"] == "file" and "file_count" in component:
                raise ContractError(
                    "SEMANTIC_VALIDATION_FAILED",
                    "File content identity cannot contain file_count.",
                    path=f"{path}.{name}.file_count",
                )
            if component.get("version") is not None:
                _reject_sensitive_metadata(
                    component.get("version"),
                    f"{path}.{name}.version",
                )
    for index, package in enumerate(identity["packages"]):
        _reject_sensitive_metadata(
            package["name"],
            f"{path}.packages[{index}].name",
        )
        _reject_sensitive_metadata(
            package["version"],
            f"{path}.packages[{index}].version",
        )
    expected_packages = {
        "faster-whisper": ["faster-whisper", "ctranslate2"],
        "mlx-whisper": ["mlx-whisper", "mlx"],
    }.get(engine, [])
    if [package["name"] for package in identity["packages"]] != expected_packages:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Engine package identities must use the exact ordered package set.",
            path=f"{path}.packages",
        )
    expected_identity = canonical_json_sha256(
        _engine_identity_projection(identity)
    )
    if identity["identity_sha256"] != expected_identity:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "engine identity digest does not match its content identities.",
            path=f"{path}.identity_sha256",
        )
    if engine == "whisper-cpp" and (
        identity["model"] is None
        or identity["model"]["kind"] != "file"
        or identity["executable"] is None
        or identity["executable"]["kind"] != "file"
        or identity["adapter"] is not None
        or identity["packages"]
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "whisper-cpp requires only file model and executable identities.",
            path=path,
        )
    if engine in {"faster-whisper", "mlx-whisper"} and (
        identity["model"] is None
        or identity["model"]["kind"] != "directory"
        or identity["executable"] is not None
        or identity["adapter"] is not None
        or not identity["packages"]
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Python engines require a local model directory and package identities.",
            path=path,
        )
    if engine == "external" and (
        identity["model"] is None
        or identity["executable"] is not None
        or identity["adapter"] is None
        or identity["adapter"]["kind"] != "file"
        or identity["packages"]
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "External engine requires only a content-identified model and file adapter.",
            path=path,
        )
    if engine == "sidecar-subtitle" and (
        not sidecar_present
        or identity["model"] is not None
        or identity["executable"] is not None
        or identity["adapter"] is None
        or identity["adapter"]["kind"] != "file"
        or identity["packages"]
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Sidecar engine requires only one file sidecar content identity.",
            path=path,
        )


def _job_id_for_settings(settings_sha256: str) -> str:
    return hashlib.sha256(
        b"awesome-capture.transcription-job/v2\0"
        + settings_sha256.encode("ascii")
    ).hexdigest()


def _validate_chunk_reference(
    reference: dict[str, Any],
    *,
    path: str,
) -> list[dict[str, Any]]:
    _require_absolute(reference["manifest_path"], f"{path}.manifest_path")
    _require_sha256(reference["manifest_sha256"], f"{path}.manifest_sha256")
    timeline = reference["timeline"]
    if reference["count"] != len(timeline):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Chunk reference count must match its inline timeline.",
            path=f"{path}.count",
        )
    expected_offset = 0
    for index, chunk in enumerate(timeline):
        _require_sha256(chunk["sha256"], f"{path}.timeline[{index}].sha256")
        if chunk["index"] != index or chunk["offset_ms"] != expected_offset:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Inline chunk timeline must have contiguous indexes and offsets.",
                path=f"{path}.timeline[{index}]",
            )
        expected_offset += chunk["duration_ms"]
    return timeline


def _validate_transcript(value: dict[str, Any]) -> None:
    _reject_sensitive_keys(value)
    source = value["source"]
    _require_absolute(source["path"], "$.source.path")
    _require_absolute(source["snapshot_path"], "$.source.snapshot_path")
    _require_sha256(source["sha256"], "$.source.sha256")
    upstream = source["upstream"]
    if upstream is not None:
        _require_absolute(upstream["artifact_path"], "$.source.upstream.artifact_path")
        _require_sha256(upstream["artifact_sha256"], "$.source.upstream.artifact_sha256")
        _require_sha256(upstream["fingerprint"], "$.source.upstream.fingerprint")
    sidecar = source["sidecar"]
    if sidecar is not None:
        _require_absolute(sidecar["path"], "$.source.sidecar.path")
        _require_sha256(sidecar["sha256"], "$.source.sidecar.sha256")
    if not source["has_audio"]:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Transcript source must contain audio.", path="$.source.has_audio")
    transcription = value["transcription"]
    _require_sha256(transcription["job_id"], "$.transcription.job_id")
    _require_sha256(
        transcription["execution_guard_sha256"],
        "$.transcription.execution_guard_sha256",
    )
    _validate_engine_identity(
        transcription["engine_identity"],
        transcription["engine"],
        path="$.transcription.engine_identity",
        sidecar_present=source["sidecar"] is not None,
    )
    if (
        transcription["engine"] != "whisper-cpp"
        and transcription["whisper_cpp_cpu_only"]
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "whisper_cpp_cpu_only must be false for other engines.",
            path="$.transcription.whisper_cpp_cpu_only",
        )
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
    expected_settings_sha256 = canonical_json_sha256(
        _transcription_settings_identity(settings)
    )
    if transcription["settings_sha256"] != expected_settings_sha256:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Transcript settings digest does not match its self-contained identity fields.",
            path="$.transcription.settings_sha256",
        )
    if transcription["job_id"] != _job_id_for_settings(expected_settings_sha256):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Transcript job_id does not match its settings digest.",
            path="$.transcription.job_id",
        )
    if source["sidecar"] is not None:
        adapter = transcription["engine_identity"]["adapter"]
        if adapter is None or {
            key: adapter[key] for key in ("path", "sha256", "bytes")
        } != {
            key: source["sidecar"][key] for key in ("path", "sha256", "bytes")
        }:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Sidecar source and engine content identities differ.",
                path="$.transcription.engine_identity.adapter",
            )
    chunk_set = transcription["chunk_set"]
    if transcription["engine"] == "sidecar-subtitle":
        if source["sidecar"] is None or chunk_set is not None:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Sidecar transcripts require sidecar evidence and must not declare a chunk set.",
                path="$.transcription.chunk_set",
            )
    elif source["sidecar"] is not None or chunk_set is None:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Local ASR transcripts require a chunk set and must not declare sidecar evidence.",
            path="$.transcription.chunk_set",
        )
    chunk_timeline: list[dict[str, Any]] = []
    if chunk_set is not None:
        chunk_timeline = _validate_chunk_reference(
            chunk_set,
            path="$.transcription.chunk_set",
        )
        if (
            chunk_timeline[-1]["offset_ms"]
            + chunk_timeline[-1]["duration_ms"]
            != source["duration_ms"]
        ):
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Inline chunks must cover the source duration exactly.",
                path="$.transcription.chunk_set.timeline",
            )
    segments = value["segments"]
    prior_end = 0
    texts: list[str] = []
    for index, segment in enumerate(segments):
        if segment["start_ms"] < prior_end:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Transcript segments overlap or are not monotonic.",
                path=f"$.segments[{index}].start_ms",
            )
        if segment["end_ms"] <= segment["start_ms"]:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Segment end_ms must be greater than start_ms.",
                path=f"$.segments[{index}].end_ms",
            )
        if segment["end_ms"] > source["duration_ms"]:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Segment exceeds source duration.",
                path=f"$.segments[{index}].end_ms",
            )
        if not segment["text"].strip():
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Segment text must not be blank.",
                path=f"$.segments[{index}].text",
            )
        if chunk_set is not None and segment["chunk_index"] >= chunk_set["count"]:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Segment chunk_index is outside the chunk set.",
                path=f"$.segments[{index}].chunk_index",
            )
        if chunk_set is not None:
            chunk = chunk_timeline[segment["chunk_index"]]
            if (
                segment["start_ms"] < chunk["offset_ms"]
                or segment["end_ms"]
                > chunk["offset_ms"] + chunk["duration_ms"]
            ):
                raise ContractError(
                    "SEMANTIC_VALIDATION_FAILED",
                    "Segment timestamp lies outside its declared chunk.",
                    path=f"$.segments[{index}]",
                )
        if chunk_set is None and segment["chunk_index"] != 0:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Sidecar transcript segments must use chunk_index 0.",
                path=f"$.segments[{index}].chunk_index",
            )
        prior_end = segment["end_ms"]
        texts.append(segment["text"])
    expected_text = "\n".join(texts)
    if value["text"] != expected_text:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "text must be the newline join of segment text.",
            path="$.text",
        )
    if value["no_speech_detected"] != (len(segments) == 0):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "no_speech_detected must exactly reflect an empty segment list.",
            path="$.no_speech_detected",
        )
    for index, warning in enumerate(value["warnings"]):
        _reject_sensitive_metadata(warning, f"$.warnings[{index}]")
    for key, descriptor in value["outputs"].items():
        if descriptor is not None:
            _require_absolute(descriptor["path"], f"$.outputs.{key}.path")
            _require_sha256(descriptor["sha256"], f"$.outputs.{key}.sha256")
    _require_local_contract_digest(value["producer"]["contract_digest"], "$.producer.contract_digest")


def _validate_chunk_set(value: dict[str, Any]) -> None:
    _require_sha256(value["job_id"], "$.job_id")
    _require_sha256(value["source_sha256"], "$.source_sha256")
    chunks = value["chunks"]
    if value["count"] != len(chunks):
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "count must match chunks.", path="$.count")
    expected_offset = 0
    names: set[str] = set()
    for index, chunk in enumerate(chunks):
        expected_name = f"chunk-{index:05d}.wav"
        if chunk["index"] != index:
            raise ContractError("SEMANTIC_VALIDATION_FAILED", "Chunk indexes must be contiguous.", path=f"$.chunks[{index}].index")
        if chunk["name"] != expected_name:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Chunk name must match its contiguous index.",
                path=f"$.chunks[{index}].name",
            )
        if chunk["offset_ms"] != expected_offset:
            raise ContractError("SEMANTIC_VALIDATION_FAILED", "Chunk timeline must be contiguous.", path=f"$.chunks[{index}].offset_ms")
        if chunk["name"] in names:
            raise ContractError("SEMANTIC_VALIDATION_FAILED", "Chunk names must be unique.", path=f"$.chunks[{index}].name")
        names.add(chunk["name"])
        chunk_path = _require_absolute(chunk["path"], f"$.chunks[{index}].path")
        if chunk_path.name != expected_name:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Chunk path basename must match its manifest name.",
                path=f"$.chunks[{index}].path",
            )
        _require_sha256(chunk["sha256"], f"$.chunks[{index}].sha256")
        expected_offset += chunk["duration_ms"]
        expected_duration = round(chunk["sample_frames"] * 1000 / chunk["sample_rate"])
        if chunk["duration_ms"] != expected_duration:
            raise ContractError("SEMANTIC_VALIDATION_FAILED", "Chunk duration does not match sample frames.", path=f"$.chunks[{index}].duration_ms")
    if value["total_duration_ms"] != expected_offset:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Chunk durations must cover total_duration_ms exactly.", path="$.total_duration_ms")


def _validate_state(value: dict[str, Any]) -> None:
    _require_sha256(value["job_id"], "$.job_id")
    _require_sha256(value["settings_sha256"], "$.settings_sha256")
    _require_sha256(
        value["execution_guard_sha256"],
        "$.execution_guard_sha256",
    )
    settings = value["settings"]
    _require_local_contract_digest(settings["contract_digest"], "$.settings.contract_digest")
    _require_sha256(
        settings["algorithm"]["sha256"],
        "$.settings.algorithm.sha256",
    )
    _require_absolute(settings["source_path"], "$.settings.source_path")
    _require_sha256(settings["source_sha256"], "$.settings.source_sha256")
    for key in ("upstream_artifact_sha256", "sidecar_sha256"):
        if settings[key] is not None:
            _require_sha256(settings[key], f"$.settings.{key}")
    if (settings["engine"] == "sidecar-subtitle") != (
        settings["sidecar_sha256"] is not None
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Only sidecar state may bind sidecar_sha256.",
            path="$.settings.sidecar_sha256",
        )
    _validate_engine_identity(
        settings["engine_identity"],
        settings["engine"],
        path="$.settings.engine_identity",
        sidecar_present=settings["sidecar_sha256"] is not None,
    )
    if (
        settings["engine"] != "whisper-cpp"
        and settings["whisper_cpp_cpu_only"]
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "whisper_cpp_cpu_only must be false for other engines.",
            path="$.settings.whisper_cpp_cpu_only",
        )
    if value["settings_sha256"] != canonical_json_sha256(
        _transcription_settings_identity(settings)
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "settings_sha256 does not match canonical settings.",
            path="$.settings_sha256",
        )
    if value["job_id"] != _job_id_for_settings(value["settings_sha256"]):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "State job_id does not match settings_sha256.",
            path="$.job_id",
        )
    chunk_set = value["chunk_set"]
    if settings["engine"] == "sidecar-subtitle":
        if chunk_set is not None:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Sidecar state must not declare a chunk set.",
                path="$.chunk_set",
            )
    elif chunk_set is None and value["status"] != "running":
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Publishable local ASR state requires a chunk set.",
            path="$.chunk_set",
        )
    if chunk_set is not None:
        chunk_timeline = _validate_chunk_reference(
            chunk_set,
            path="$.chunk_set",
        )
    else:
        chunk_timeline = []
    chunks = value["chunks"]
    if settings["engine"] == "sidecar-subtitle":
        allowed_names = {"sidecar"}
    elif chunk_set is not None:
        allowed_names = {f"chunk-{index:05d}.wav" for index in range(chunk_set["count"])}
    else:
        allowed_names = set()
    if not set(chunks).issubset(allowed_names):
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "State has unknown chunk entries.", path="$.chunks")
    if value["status"] in {"ready_to_publish", "complete"} and set(chunks) != allowed_names:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Publishable state requires every expected chunk.",
            path="$.chunks",
        )
    for name, chunk in chunks.items():
        chunk_path = f"$.chunks.{name}"
        _require_sha256(chunk["chunk_sha256"], f"{chunk_path}.chunk_sha256")
        if name == "sidecar" and chunk["chunk_sha256"] != settings["sidecar_sha256"]:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Sidecar state hash differs from settings.",
                path=f"{chunk_path}.chunk_sha256",
            )
        if chunk["raw_output_sha256"] is not None:
            _require_sha256(chunk["raw_output_sha256"], f"{chunk_path}.raw_output_sha256")
        expected_index = 0 if name == "sidecar" else int(name[6:11])
        if name != "sidecar":
            evidence = chunk_timeline[expected_index]
            if (
                chunk["chunk_sha256"] != evidence["sha256"]
                or chunk["offset_ms"] != evidence["offset_ms"]
                or chunk["duration_ms"] != evidence["duration_ms"]
            ):
                raise ContractError(
                    "SEMANTIC_VALIDATION_FAILED",
                    "State chunk result differs from inline chunk evidence.",
                    path=chunk_path,
                )
        prior_end = chunk["offset_ms"]
        chunk_end = chunk["offset_ms"] + chunk["duration_ms"]
        for index, segment in enumerate(chunk["segments"]):
            if segment["chunk_index"] != expected_index:
                raise ContractError("SEMANTIC_VALIDATION_FAILED", "Segment chunk_index does not match state key.", path=f"{chunk_path}.segments[{index}].chunk_index")
            if (
                segment["start_ms"] < prior_end
                or segment["end_ms"] <= segment["start_ms"]
                or segment["end_ms"] > chunk_end
                or not segment["text"].strip()
            ):
                raise ContractError("SEMANTIC_VALIDATION_FAILED", "State segment timeline is invalid.", path=f"{chunk_path}.segments[{index}]")
            prior_end = segment["end_ms"]
        if chunk["silent"] != (len(chunk["segments"]) == 0):
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Chunk silent must exactly reflect an empty segment list.",
                path=f"{chunk_path}.silent",
            )
        runtime = chunk["runtime"]
        if runtime is not None and runtime["gpu_fallback"] and (
            not runtime["gpu_attempted"] or runtime["device"] != "cpu"
        ):
            raise ContractError("SEMANTIC_VALIDATION_FAILED", "GPU fallback runtime is inconsistent.", path=f"{chunk_path}.runtime")


def _validate_transaction(value: dict[str, Any]) -> None:
    root = _require_absolute(value["root"], "$.root")
    staging = _require_absolute(value["staging_root"], "$.staging_root")
    try:
        relative_staging = staging.relative_to(root)
    except ValueError as exc:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "staging_root must be contained by root.",
            path="$.staging_root",
        ) from exc
    if not relative_staging.parts:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "staging_root must be a strict descendant of root.",
            path="$.staging_root",
        )
    indexes: list[int] = []
    destinations: set[str] = set()
    pending_seen = False
    receipt_indexes: list[int] = []
    for index, step in enumerate(value["steps"]):
        indexes.append(step["index"])
        _require_relative(step["source"], f"$.steps[{index}].source")
        destination = _require_relative(
            step["destination"], f"$.steps[{index}].destination"
        ).as_posix()
        if destination in destinations:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Transaction destinations must be unique.",
                path=f"$.steps[{index}].destination",
            )
        destinations.add(destination)
        _require_sha256(step["sha256"], f"$.steps[{index}].sha256")
        if step["status"] == "pending":
            pending_seen = True
        elif pending_seen:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Published transaction steps must form a contiguous prefix.",
                path=f"$.steps[{index}].status",
            )
        if step["operation"] == "publish-receipt":
            receipt_indexes.append(index)
    if indexes != list(range(len(indexes))):
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Transaction step indexes must be contiguous.", path="$.steps")
    if (
        len(receipt_indexes) != 1
        or receipt_indexes[0] != len(value["steps"]) - 1
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "A transaction must have exactly one final receipt publish step.",
            path="$.steps",
        )
    if value["status"] == "complete" and any(step["status"] != "published" for step in value["steps"]):
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Complete transaction requires published steps.", path="$.steps")


def _vault_folder(value: str, path: str) -> str:
    if (
        any(part in {"", ".", ".."} for part in value.split("/"))
        or re.search(r'[\x00-\x1f\x7f<>:"\\|?*#^%\[\]]', value)
    ):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Vault folder must be a safe relative POSIX path.",
            path=path,
        )
    return _require_relative(value, path).as_posix()


def _validate_vault_config(value: dict[str, Any]) -> None:
    name = value["name"].strip()
    if not name or any(character in value["name"] for character in "/\\\0"):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Vault name is empty or unsafe.",
            path="$.name",
        )
    if not value["language"].strip():
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Vault language is empty.",
            path="$.language",
        )
    folders = [
        _vault_folder(folder, f"$.folders[{index}]")
        for index, folder in enumerate(value["folders"])
    ]
    if len(folders) != len(set(folders)):
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Vault folders must be unique.",
            path="$.folders",
        )
    for field in (
        "inbox_folder",
        "sources_folder",
        "attachments_folder",
        "templates_folder",
    ):
        designated = _vault_folder(value[field], f"$.{field}")
        if designated not in folders:
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                f"{field} must appear in folders.",
                path=f"$.{field}",
            )
    daily = value.get(
        "daily_notes",
        {"enabled": False, "folder": "Daily", "format": "YYYY-MM-DD"},
    )
    daily_folder = _vault_folder(daily["folder"], "$.daily_notes.folder")
    if not daily["format"].strip():
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "daily_notes.format is empty.",
            path="$.daily_notes.format",
        )
    if daily["enabled"] and daily_folder not in folders:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Enabled daily_notes.folder must appear in folders.",
            path="$.daily_notes.folder",
        )


def _validate_build_receipt(value: dict[str, Any]) -> None:
    for key in ("config_sha256", "vault_id", "plan_sha256"):
        _require_sha256(value[key], f"$.{key}")
    seen: set[str] = set()
    for index, relative in enumerate(value["managed_directories"]):
        normalized = _require_relative(relative, f"$.managed_directories[{index}]").as_posix()
        if normalized in seen:
            raise ContractError("SEMANTIC_VALIDATION_FAILED", "Managed paths must be unique.", path=f"$.managed_directories[{index}]")
        seen.add(normalized)
    for index, item in enumerate(value["managed_files"]):
        normalized = _require_relative(item["path"], f"$.managed_files[{index}].path").as_posix()
        if normalized in seen:
            raise ContractError("SEMANTIC_VALIDATION_FAILED", "Managed paths must be unique.", path=f"$.managed_files[{index}].path")
        seen.add(normalized)
        _require_sha256(item["sha256"], f"$.managed_files[{index}].sha256")
    _require_local_contract_digest(value["producer"]["contract_digest"], "$.producer.contract_digest")


def _validate_ingest_receipt(value: dict[str, Any]) -> None:
    for key in (
        "ingest_id",
        "transcript_artifact_sha256",
        "transcript_semantic_sha256",
        "source_sha256",
        "draft_sha256",
        "request_sha256",
        "plan_sha256",
    ):
        _require_sha256(value[key], f"$.{key}")
    expected_id = hashlib.sha256(
        b"awesome-capture.ingest-id/v1\0"
        + value["transcript_artifact_sha256"].encode("ascii")
    ).hexdigest()
    if value["ingest_id"] != expected_id:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "ingest_id does not match transcript artifact identity.", path="$.ingest_id")
    knowledge_note = _require_relative(value["knowledge_note"], "$.knowledge_note")
    source_note = _require_relative(value["source_note"], "$.source_note")
    if knowledge_note == source_note:
        raise ContractError(
            "SEMANTIC_VALIDATION_FAILED",
            "Knowledge and source notes must use distinct paths.",
            path="$.source_note",
        )
    paths: set[str] = set()
    for index, item in enumerate(value["initial_files"]):
        relative = _require_relative(item["path"], f"$.initial_files[{index}].path").as_posix()
        if relative in paths:
            raise ContractError("SEMANTIC_VALIDATION_FAILED", "initial_files paths must be unique.", path=f"$.initial_files[{index}].path")
        paths.add(relative)
        _require_sha256(item["sha256"], f"$.initial_files[{index}].sha256")
        if item["identity_marker"] != f"awesome_capture_id: {value['ingest_id']}":
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "identity_marker must exactly bind the full ingest_id.",
                path=f"$.initial_files[{index}].identity_marker",
            )
    if {value["knowledge_note"], value["source_note"]} - paths:
        raise ContractError("SEMANTIC_VALIDATION_FAILED", "Both note paths must appear in initial_files.", path="$.initial_files")
    _require_local_contract_digest(value["producer"]["contract_digest"], "$.producer.contract_digest")


def _validate_smoke_receipt(value: dict[str, Any]) -> None:
    _reject_sensitive_keys(value)
    _require_sha256(value["implementation_digest"], "$.implementation_digest")
    _require_sha256(value["source"]["fingerprint"], "$.source.fingerprint")
    for index, item in enumerate(value["artifacts"]):
        _require_sha256(item["sha256"], f"$.artifacts[{index}].sha256")
    if value["outcome"] == "pass":
        if any(not assertion["passed"] for assertion in value["assertions"]):
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Passing smoke receipt cannot contain a failed assertion.",
                path="$.assertions",
            )
        artifact_types = [item["type"] for item in value["artifacts"]]
        if value["source"]["platform"] == "local":
            if (
                value["engine"] is None
                or artifact_types != ["transcript-artifact"]
            ):
                raise ContractError(
                    "SEMANTIC_VALIDATION_FAILED",
                    "Passing local smoke requires one engine and one transcript artifact.",
                    path="$.artifacts",
                )
            engine = value["engine"]
            if engine["model_sha256"] is None:
                raise ContractError(
                    "SEMANTIC_VALIDATION_FAILED",
                    "Passing ASR smoke requires a model content hash.",
                    path="$.engine.model_sha256",
                )
            if (engine["name"] == "external") != (
                engine["adapter_sha256"] is not None
            ):
                raise ContractError(
                    "SEMANTIC_VALIDATION_FAILED",
                    "Only external ASR smoke requires an adapter content hash.",
                    path="$.engine.adapter_sha256",
                )
        elif (
            value["engine"] is not None
            or artifact_types != ["video-artifact"]
        ):
            raise ContractError(
                "SEMANTIC_VALIDATION_FAILED",
                "Passing download smoke requires one video artifact and no ASR engine.",
                path="$.artifacts",
            )

    forbidden_assignment = re.compile(
        r"(?i)\b(?:host(?:name)?|user(?:name)?|login|cookie|authorization|"
        r"bearer|token|secret|password|api[_-]?key|header)\s*[:=]"
    )
    forbidden_hostname = re.compile(
        r"(?i)\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
        r"(?:local|lan|internal)\b"
    )
    forbidden_media_name = re.compile(
        r"(?i)(?:^|[\s=:])[^ \t\r\n/\\]+"
        r"\.(?:mp4|mov|mkv|webm|avi|wav|mp3|m4a|flac|aac|srt|vtt|"
        r"log|txt|md|json|bin|gguf|model)(?:$|[\s,;])"
    )

    def reject_private_strings(child: Any) -> None:
        if isinstance(child, str):
            if child == "awesome-capture.smoke-receipt/v1":
                return
            if (
                "/" in child
                or "\\" in child
                or "@" in child
                or any(ord(character) < 0x20 for character in child)
                or forbidden_assignment.search(child)
                or forbidden_hostname.search(child)
                or forbidden_media_name.search(child)
                or re.search(r"(?i)\bbuilt\s+on\b", child)
            ):
                raise ContractError(
                    "SENSITIVE_DATA_FORBIDDEN",
                    "Smoke receipt contains non-canonical or private text.",
                )
        elif isinstance(child, dict):
            for nested in child.values():
                reject_private_strings(nested)
        elif isinstance(child, list):
            for nested in child:
                reject_private_strings(nested)

    reject_private_strings(value)


_SEMANTIC_VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {
    "video-artifact": _validate_video,
    "transcript-artifact": _validate_transcript,
    "transcription-state": _validate_state,
    "chunk-set": _validate_chunk_set,
    "transaction": _validate_transaction,
    "vault-config": _validate_vault_config,
    "vault-build-receipt": _validate_build_receipt,
    "ingest-receipt": _validate_ingest_receipt,
    "smoke-receipt": _validate_smoke_receipt,
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_open_regular_file(
    fd: int,
    metadata: os.stat_result,
    descriptor: Mapping[str, Any],
    field_path: str,
) -> None:
    try:
        if (
            stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != descriptor["bytes"]
        ):
            raise ContractError(
                "FILE_CONTEXT_MISMATCH",
                "Declared managed file mode or size does not match.",
                path=field_path,
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
        final_metadata = os.fstat(fd)
        if (
            _metadata_identity(final_metadata) != _metadata_identity(metadata)
            or total != metadata.st_size
        ):
            raise ContractError(
                "FILE_CONTEXT_MISMATCH",
                "Declared file changed while it was being verified.",
                path=field_path,
            )
        if digest.hexdigest() != descriptor["sha256"]:
            raise ContractError(
                "FILE_CONTEXT_MISMATCH",
                "Declared file hash does not match.",
                path=field_path,
            )
    except OSError as exc:
        raise ContractError(
            "FILE_CONTEXT_MISMATCH",
            "Declared file cannot be read safely.",
            path=field_path,
        ) from exc
    finally:
        os.close(fd)


def _verify_regular_file(path: Path, descriptor: Mapping[str, Any], field_path: str) -> None:
    try:
        fd, metadata = _open_regular_file_no_follow(
            path,
            error_code="FILE_CONTEXT_MISMATCH",
        )
    except ContractError as exc:
        if exc.path == "$":
            exc.path = field_path
        raise
    _verify_open_regular_file(fd, metadata, descriptor, field_path)


def _verify_complete_chunk_directory(value: Mapping[str, Any]) -> None:
    chunks = value["chunks"]
    first_path = Path(chunks[0]["path"])
    directory_fd, unused_name = _open_parent_directory_no_follow(
        first_path,
        error_code="FILE_CONTEXT_MISMATCH",
    )
    del unused_name
    try:
        metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ContractError(
                "FILE_CONTEXT_MISMATCH",
                "Chunk directory must be a private current-user-owned directory.",
                path="$.chunks",
            )
        expected_names = {
            "chunks.manifest.json",
            *(descriptor["name"] for descriptor in chunks),
        }
        if set(os.listdir(directory_fd)) != expected_names:
            raise ContractError(
                "FILE_CONTEXT_MISMATCH",
                "Chunk directory contains missing or extra files.",
                path="$.chunks",
            )
        expected_parent = _absolute_without_symlink_resolution(first_path).parent
        for index, descriptor in enumerate(chunks):
            candidate = _absolute_without_symlink_resolution(descriptor["path"])
            if candidate.parent != expected_parent:
                raise ContractError(
                    "FILE_CONTEXT_MISMATCH",
                    "All chunks must share the manifest directory.",
                    path=f"$.chunks[{index}].path",
                )
            try:
                fd = os.open(
                    descriptor["name"],
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise ContractError(
                    "FILE_CONTEXT_MISMATCH",
                    "Chunk cannot be opened safely.",
                    path=f"$.chunks[{index}].path",
                ) from exc
            file_metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(file_metadata.st_mode)
                or file_metadata.st_uid != os.geteuid()
                or file_metadata.st_nlink != 1
                or stat.S_IMODE(file_metadata.st_mode) != 0o600
            ):
                os.close(fd)
                raise ContractError(
                    "FILE_CONTEXT_MISMATCH",
                    "Chunk must be a private owned single-link regular file.",
                    path=f"$.chunks[{index}].path",
                )
            _verify_open_regular_file(
                fd,
                file_metadata,
                descriptor,
                f"$.chunks[{index}].path",
            )
    finally:
        os.close(directory_fd)


def validate_file_context(
    value: Mapping[str, Any],
    *,
    verify_source: bool = False,
    verify_outputs: bool = False,
    verify_chunks: bool = False,
) -> None:
    """Verify files named by an already structurally valid contract.

    Consumers opt in to only the filesystem evidence they are authorized to
    read.  In particular, ingest validates transcript semantics without setting
    ``verify_source`` or ``verify_outputs``.
    """

    name = validate_contract(dict(value))
    if name == "video-artifact":
        _verify_regular_file(Path(value["media"]["path"]), value["media"], "$.media.path")
        return
    if name == "transcript-artifact":
        if verify_source:
            _verify_regular_file(
                Path(value["source"]["snapshot_path"]),
                value["source"],
                "$.source.snapshot_path",
            )
        if verify_outputs:
            for output_name, descriptor in value["outputs"].items():
                if descriptor is not None:
                    _verify_regular_file(Path(descriptor["path"]), descriptor, f"$.outputs.{output_name}.path")
        return
    if name == "chunk-set" and verify_chunks:
        _verify_complete_chunk_directory(value)


def _contract_manifest_entries() -> list[tuple[str, str]]:
    entries = [
        ("contract_runtime.py", _sha256_file(Path(__file__).resolve())),
    ]
    for filename in sorted(_SCHEMA_FILES.values()):
        entries.append((f"schemas/{filename}", _sha256_file(_schema_root() / filename)))
    return entries


def _aggregate_digest(entries: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for relative, sha256 in entries:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def verify_contract_bundle() -> str:
    """Verify the complete standalone bundle and return the wire digest."""

    from .bundle_runtime import verify_contract_bundle as verify_bundle

    return verify_bundle()


def contract_digest(*, verify: bool = True) -> str:
    if verify:
        return verify_contract_bundle()
    try:
        manifest = loads_strict(_manifest_path().read_bytes())
    except OSError as exc:
        raise ContractError("CONTRACT_BUILD_MISMATCH", "Contract manifest is missing.") from exc
    digest = manifest.get("contract_digest") if isinstance(manifest, dict) else None
    return _require_sha256(digest, "$.contract_digest")
