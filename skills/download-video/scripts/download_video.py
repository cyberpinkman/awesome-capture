#!/usr/bin/env python3
"""Download one supported social video and emit a verified artifact manifest."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _contracts.contract_runtime import (  # noqa: E402
    ContractError,
    contract_digest,
    validate_contract,
    video_probe_evidence_sha256,
)
from _contracts.posix_runtime import (  # noqa: E402
    FileLock,
    PosixRuntimeError,
    atomic_write_noclobber,
    move_verified_noreplace,
    rename_noreplace,
    require_posix,
    test_failpoint,
)
from _contracts.media_runtime import (  # noqa: E402
    SafeRuntimeError,
    quarantine_private_file,
)

SCHEMA_VERSION = "awesome-capture.artifact/v2"
TRANSACTION_SCHEMA_VERSION = "awesome-capture.transaction/v1"
MEDIA_LAYOUT_VERSION = "v2"
MEDIA_ROOT_NAME = ".awesome-capture-media"
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PERSISTED_METADATA_SENSITIVE_PATTERN = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|"
    r"\b(?:api[_-]?key|authorization|bearer|cookie|credential|password|"
    r"private[_-]?header|secret|signature|token)\s*[:=])",
    re.IGNORECASE,
)
PLATFORM_SUFFIXES = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "tiktok": ("tiktok.com",),
    "bilibili": ("bilibili.com", "b23.tv"),
    "youtube": ("youtube.com", "youtu.be"),
    "twitter": ("x.com", "twitter.com"),
}
QUALITY_FORMATS = {
    "best": "bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720p": "bv*[height<=720]+ba/b[height<=720]/b",
}
EXTRACTOR_NAMES = {
    "douyin": "Douyin",
    "tiktok": "TikTok",
    "bilibili": "BiliBili",
    "youtube": "Youtube",
    "twitter": "Twitter",
}
PUBLIC_SOURCE_QUERY_KEYS = {
    "youtube": {"v"},
    "bilibili": {"bvid", "p"},
    "douyin": {"modal_id"},
    "tiktok": set(),
    "twitter": set(),
}
PUBLIC_SOURCE_QUERY_VALUE_PATTERNS = {
    "youtube": {"v": r"[A-Za-z0-9_-]{1,128}"},
    "bilibili": {
        "bvid": r"[A-Za-z0-9_-]{1,128}",
        "p": r"[1-9][0-9]{0,5}",
    },
    "douyin": {"modal_id": r"[0-9]{1,32}"},
    "tiktok": {},
    "twitter": {},
}
TESTED_YTDLP_BASELINE = dt.date(2026, 7, 4)

# This digest is deliberately derived from the owned wire shape, rather than from
# a path in the repository.  A copied skill therefore validates the same contract.
VIDEO_CONTRACT_SHAPE = {
    "schema_version": SCHEMA_VERSION,
    "artifact_type": "video",
    "required": [
        "schema_version",
        "artifact_type",
        "status",
        "created_at",
        "source",
        "media",
        "acquisition",
        "producer",
    ],
    "source": [
        "platform",
        "fingerprint",
        "url",
        "webpage_url",
        "id",
        "title",
        "author",
        "extractor",
    ],
    "media": [
        "path",
        "bytes",
        "sha256",
        "duration_ms",
        "has_video",
        "has_audio",
        "container",
        "video_streams",
        "audio_streams",
        "ffprobe",
    ],
    "acquisition": ["auth_mode", "fallback", "warnings"],
    "producer": ["skill", "tool", "version", "contract_digest"],
}
try:
    CONTRACT_DIGEST = contract_digest()
    CONTRACT_BUNDLE_ERROR: ContractError | None = None
except ContractError as exc:
    CONTRACT_DIGEST = "0" * 64
    CONTRACT_BUNDLE_ERROR = exc


class DownloadError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: str = "", exit_code: int = 5):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "status": "error",
            "error": {"code": self.code, "message": self.message},
        }
        if self.details:
            value["error"]["details"] = redact_sensitive(self.details)
        return value


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DownloadError("INVALID_ARGUMENT", message, exit_code=2)


def json_print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def redact_sensitive(text: str) -> str:
    text = re.sub(r"(?i)(cookie|authorization):\s*[^\r\n]+", r"\1: <redacted>", text)
    text = re.sub(r"(?i)(--cookies(?:-from-browser)?(?:=|\s+))\S+", r"\1<redacted>", text)
    text = re.sub(
        r"(?i)\b(token|signature|secret|password|api[_-]?key)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer <redacted>", text)
    text = re.sub(r"(https?://[^\s?#]+)\?[^\s]+", r"\1?<redacted>", text)
    return text.strip()[-4000:]


def safe_persisted_metadata(value: Any, *, maximum_chars: int) -> str:
    """Keep display metadata only when it cannot carry URLs, paths, or secrets."""

    text = " ".join(str(value or "").split()).strip()
    if (
        any(ord(character) < 0x20 for character in text)
        or "/" in text
        or "\\" in text
        or "@" in text
        or PERSISTED_METADATA_SENSITIVE_PATTERN.search(text)
    ):
        return ""
    return text[:maximum_chars]


def host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def normalize_and_detect(raw_url: str) -> tuple[str, str]:
    value = raw_url.strip()
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise DownloadError("INVALID_URL", f"Invalid URL: {exc}", exit_code=2) from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise DownloadError("INVALID_URL", "Only http and https video URLs are accepted.", exit_code=2)
    if parts.username or parts.password:
        raise DownloadError("INVALID_URL", "Credentials embedded in URLs are not accepted.", exit_code=2)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise DownloadError("INVALID_URL", "The URL has no hostname.", exit_code=2)
    for platform, suffixes in PLATFORM_SUFFIXES.items():
        if any(host_matches(host, suffix) for suffix in suffixes):
            normalized = urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))
            return normalized, platform
    raise DownloadError(
        "UNSUPPORTED_URL",
        "Supported platforms are Douyin, TikTok, Bilibili, YouTube, and X/Twitter.",
        details=f"Unsupported host: {host}",
        exit_code=2,
    )


def sanitize_source_url(raw_url: str, platform_name: str) -> str:
    """Keep only public identity parameters in persisted source URLs."""
    parts = urlsplit(raw_url)
    allowed = PUBLIC_SOURCE_QUERY_KEYS.get(platform_name, set())
    patterns = PUBLIC_SOURCE_QUERY_VALUE_PATTERNS.get(platform_name, {})
    sanitized_pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        pairs = parse_qsl(
            parts.query,
            keep_blank_values=True,
            max_num_fields=16,
        )
    except ValueError:
        pairs = []
    for raw_key, value in pairs:
        key = raw_key.lower()
        if (
            key in allowed
            and key not in seen
            and re.fullmatch(patterns[key], value) is not None
        ):
            sanitized_pairs.append((key, value))
            seen.add(key)
    query = urlencode(sanitized_pairs, doseq=True)
    return urlunsplit(("https", parts.netloc, parts.path or "/", query, ""))


def require_tool(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise DownloadError("DEPENDENCY_MISSING", f"Required executable is missing: {name}", exit_code=3)
    return value


def _run_subprocess_pinned(
    command: list[str],
    *,
    timeout: int,
    pinned_cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a tool with its cwd pinned to an already-open directory inode."""

    directory_fd = -1
    run_options: dict[str, Any] = {}
    if pinned_cwd is not None:
        try:
            directory_fd = _open_directory_chain(pinned_cwd, create=False)
            _check_owned_directory_fd(directory_fd, private=True)
        except (DownloadError, OSError) as exc:
            if directory_fd >= 0:
                os.close(directory_fd)
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "The private staging directory cannot be pinned safely.",
                exit_code=4,
            ) from exc

        def enter_pinned_directory() -> None:
            os.fchdir(directory_fd)

        run_options["preexec_fn"] = enter_pinned_directory
        run_options["pass_fds"] = (directory_fd,)
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            **run_options,
        )
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def run_checked(
    command: list[str],
    *,
    timeout: int,
    pinned_cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        process = _run_subprocess_pinned(
            command,
            timeout=timeout,
            pinned_cwd=pinned_cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(
            "NETWORK_ERROR",
            f"Command timed out after {timeout} seconds.",
            exit_code=5,
        ) from exc
    if process.returncode != 0:
        raise classify_ytdlp_error(process.stderr or process.stdout)
    return process


def classify_ytdlp_error(details: str) -> DownloadError:
    lower = details.lower()
    cases = [
        (("fresh cookies",), "FRESH_COOKIES_REQUIRED", "The platform requires fresh cookies.", 4),
        (
            ("sign in", "login required", "cookies are needed", "authentication required"),
            "SESSION_REQUIRED",
            "The platform requires an authorized session.",
            4,
        ),
        (("ip address is blocked", "ip has been blocked"), "IP_BLOCKED", "The current IP address is blocked.", 5),
        (("not available in your country", "geo-restricted"), "GEO_BLOCKED", "The content is region restricted.", 5),
        (("http error 429", "too many requests"), "RATE_LIMITED", "The platform rate-limited the request.", 5),
        (
            ("http error 412", "precondition failed", "challenge"),
            "SESSION_REQUIRED",
            "The platform rejected the anonymous anti-bot challenge.",
            4,
        ),
        (
            ("video unavailable", "private video", "deleted", "not found", "no longer available"),
            "CONTENT_UNAVAILABLE",
            "The requested video is unavailable or access restricted.",
            5,
        ),
        (
            ("failed to resolve", "temporary failure in name resolution", "timed out", "connection error", "ssl"),
            "NETWORK_ERROR",
            "A DNS, TLS, connection, or timeout error prevented access.",
            5,
        ),
        (("unsupported url",), "UNSUPPORTED_URL", "yt-dlp does not support this URL shape.", 2),
    ]
    for needles, code, message, exit_code in cases:
        if any(needle in lower for needle in needles):
            return DownloadError(code, message, exit_code=exit_code)
    return DownloadError(
        "DOWNLOAD_FAILED",
        "yt-dlp failed to acquire the video.",
        exit_code=5,
    )


def auth_args(args: argparse.Namespace) -> list[str]:
    if getattr(args, "cookies", None):
        cookie_path = Path(args.cookies).expanduser().resolve()
        if not cookie_path.is_file():
            raise DownloadError(
                "INVALID_COOKIE_SOURCE",
                "The explicitly supplied Cookie file does not exist.",
                exit_code=2,
            )
        return ["--cookies", str(cookie_path)]
    if getattr(args, "cookies_from_browser", None):
        return ["--cookies-from-browser", args.cookies_from_browser]
    return []


def base_ytdlp_args(args: argparse.Namespace, platform_name: str) -> list[str]:
    command = [
        require_tool("yt-dlp"),
        "--ignore-config",
        "--no-playlist",
        "--use-extractors",
        EXTRACTOR_NAMES[platform_name],
        "--socket-timeout",
        str(args.socket_timeout),
        "--retries",
        str(args.retries),
        "--fragment-retries",
        str(args.retries),
        "--no-warnings",
    ]
    if platform_name == "twitter":
        command.append("--force-ipv4")
    command.extend(auth_args(args))
    if getattr(args, "impersonate", None):
        command.extend(["--impersonate", args.impersonate])
    if platform_name == "youtube" and shutil.which("deno"):
        command.extend(["--js-runtimes", "deno"])
    return command


def requested_auth_mode(args: argparse.Namespace) -> str:
    if getattr(args, "cookies", None):
        return "user_cookie_file"
    if getattr(args, "cookies_from_browser", None):
        return "user_browser_cookie"
    return "anonymous"


async def collect_douyin_cookies(url: str, wait_seconds: float, timeout_seconds: int) -> list[dict[str, Any]]:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:
        raise DownloadError(
            "BROWSER_FALLBACK_UNAVAILABLE",
            "Playwright is not installed for the isolated Douyin session.",
            exit_code=3,
        ) from exc
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_seconds * 1000,
                )
                await page.wait_for_timeout(max(0, round(wait_seconds * 1000)))
                final_url = page.url
                _, final_platform = normalize_and_detect(final_url)
                if final_platform != "douyin":
                    raise DownloadError(
                        "BROWSER_FALLBACK_FAILED",
                        "The isolated browser left the allowed Douyin hosts.",
                        exit_code=5,
                    )
                cookies = await context.cookies()
            finally:
                await browser.close()
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(
            "BROWSER_FALLBACK_FAILED",
            "The isolated Douyin browser session failed.",
            exit_code=5,
        ) from exc
    filtered = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not host_matches(domain, "douyin.com"):
            continue
        if not name or any(character in f"{name}{value}" for character in "\r\n\t\0"):
            continue
        filtered.append(cookie)
    if not filtered:
        raise DownloadError(
            "BROWSER_FALLBACK_FAILED",
            "The isolated browser produced no usable Douyin cookies.",
            exit_code=5,
        )
    return filtered


def write_netscape_cookies(path: Path, cookies: list[dict[str, Any]]) -> None:
    lines = ["# Netscape HTTP Cookie File", "# Generated from an isolated anonymous context; delete after use."]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        cookie_path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        try:
            expires = max(0, int(float(cookie.get("expires") or 0)))
        except (TypeError, ValueError):
            expires = 0
        if cookie.get("httpOnly"):
            domain = f"#HttpOnly_{domain}"
        lines.append(
            "\t".join(
                [
                    domain,
                    include_subdomains,
                    cookie_path,
                    secure,
                    str(expires),
                    str(cookie.get("name") or ""),
                    str(cookie.get("value") or ""),
                ]
            )
        )
    try:
        atomic_write_noclobber(
            path,
            "\n".join(lines) + "\n",
            mode=PRIVATE_FILE_MODE,
        )
    except PosixRuntimeError as exc:
        raise DownloadError(
            exc.code,
            "The ephemeral Cookie file could not be created safely.",
            exit_code=4 if exc.code in {"PATH_COLLISION", "RECOVERY_CONFLICT"} else 5,
        ) from exc


def run_ytdlp(
    args: argparse.Namespace,
    *,
    url: str,
    platform_name: str,
    tail: list[str],
    timeout: int,
    private_temp_dir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, list[str]]:
    command = base_ytdlp_args(args, platform_name) + tail + [url]
    try:
        return (
            run_checked(command, timeout=timeout, pinned_cwd=private_temp_dir),
            requested_auth_mode(args),
            [],
        )
    except DownloadError as original:
        can_use_ephemeral = (
            platform_name == "douyin"
            and original.code == "FRESH_COOKIES_REQUIRED"
            and requested_auth_mode(args) == "anonymous"
            and getattr(args, "douyin_browser_fallback", "auto") == "auto"
        )
        if not can_use_ephemeral:
            raise
        cookies = asyncio.run(
            collect_douyin_cookies(url, args.browser_wait_seconds, min(args.timeout, 120))
        )
        with tempfile.TemporaryDirectory(
            prefix=".douyin-cookie-",
            dir=private_temp_dir,
        ) as temporary:
            cookie_path = Path(temporary) / "cookies.txt"
            write_netscape_cookies(cookie_path, cookies)
            cookie_argument = (
                os.fspath(cookie_path.relative_to(private_temp_dir))
                if private_temp_dir is not None
                else os.fspath(cookie_path)
            )
            retry = (
                base_ytdlp_args(args, platform_name)
                + ["--cookies", cookie_argument]
                + tail
                + [url]
            )
            process = run_checked(retry, timeout=timeout, pinned_cwd=private_temp_dir)
        return (
            process,
            "ephemeral_browser",
            ["Used an isolated anonymous Chromium context; no user browser profile was read."],
        )


def parse_single_json(output: str) -> dict[str, Any]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise DownloadError(
        "INVALID_TOOL_OUTPUT",
        "yt-dlp did not return a JSON metadata object.",
        exit_code=5,
    )


def safe_metadata(info: dict[str, Any], platform: str, source_url: str) -> dict[str, Any]:
    webpage_url = str(info.get("webpage_url") or source_url)
    return {
        "platform": platform,
        "id": safe_persisted_metadata(info.get("id"), maximum_chars=512),
        "title": safe_persisted_metadata(
            info.get("title"),
            maximum_chars=4096,
        ),
        "author": safe_persisted_metadata(
            info.get("uploader") or info.get("channel"),
            maximum_chars=1024,
        ),
        "duration_seconds": info.get("duration"),
        "webpage_url": sanitize_source_url(webpage_url, platform),
        "extractor": safe_persisted_metadata(
            info.get("extractor_key") or info.get("extractor"),
            maximum_chars=256,
        ),
    }


def probe(args: argparse.Namespace) -> dict[str, Any]:
    url, platform = normalize_and_detect(args.url)
    process, auth_mode, warnings = run_ytdlp(
        args,
        url=url,
        platform_name=platform,
        tail=["--dump-single-json", "--skip-download"],
        timeout=args.timeout,
    )
    info = parse_single_json(process.stdout)
    return {
        "status": "ok",
        "operation": "probe",
        "auth_mode": auth_mode,
        "source": safe_metadata(info, platform, url),
        "warnings": warnings,
    }


def require_posix_capabilities() -> None:
    """Fail closed before a filesystem-mutating operation."""
    if not ((3, 11) <= sys.version_info[:2] <= (3, 14)):
        raise DownloadError(
            "UNSUPPORTED_PLATFORM",
            "Secure media operations require Python 3.11 through 3.14.",
            exit_code=3,
        )
    try:
        require_posix()
    except PosixRuntimeError as exc:
        raise DownloadError(
            "UNSUPPORTED_PLATFORM",
            exc.message,
            exit_code=3,
        ) from exc


def path_within(path: Path, root: Path) -> bool:
    """Lexical containment helper; authorization never relies on resolve()."""
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
        return True
    except ValueError:
        return False


def _absolute_clean_path(raw: str | Path) -> Path:
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise DownloadError(
            "INVALID_OUTPUT_DIRECTORY",
            "The output directory must be an absolute path.",
            exit_code=2,
        )
    if any(part in {".", ".."} for part in expanded.parts):
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            "Dot and parent traversal components are not accepted.",
            exit_code=2,
        )
    absolute = Path(os.path.abspath(expanded))
    # macOS exposes /var and /tmp as fixed system aliases into /private.  Normalize
    # only those two platform-owned aliases; arbitrary symlink components remain
    # forbidden by _open_directory_chain.
    if sys.platform == "darwin" and len(absolute.parts) > 1:
        aliases = {"var": Path("/private/var"), "tmp": Path("/private/tmp")}
        replacement = aliases.get(absolute.parts[1])
        if replacement is not None and Path(os.path.realpath(f"/{absolute.parts[1]}")) == replacement:
            absolute = replacement.joinpath(*absolute.parts[2:])
    return absolute


def _open_directory_chain(path: Path, *, create: bool) -> int:
    """Open an absolute directory without following any component symlink."""
    if not path.is_absolute():
        raise DownloadError("UNSAFE_OUTPUT_DIRECTORY", "Expected an absolute directory.", exit_code=2)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current_fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            parent_stat = os.fstat(current_fd)
            if parent_stat.st_mode & stat.S_IWOTH and not parent_stat.st_mode & stat.S_ISVTX:
                raise DownloadError(
                    "UNSAFE_OUTPUT_DIRECTORY",
                    "A non-sticky parent directory is writable by other users.",
                    exit_code=2,
                )
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, PRIVATE_DIRECTORY_MODE, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise DownloadError(
                    "UNSAFE_OUTPUT_DIRECTORY",
                    "A directory component is a symlink or is not safely accessible.",
                    exit_code=2,
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _check_owned_directory_fd(fd: int, *, private: bool) -> None:
    details = os.fstat(fd)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            "The managed directory must be owned by the current user.",
            exit_code=2,
        )
    if details.st_mode & 0o022:
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            "The managed directory must not be group- or world-writable.",
            exit_code=2,
        )
    if private and stat.S_IMODE(details.st_mode) != PRIVATE_DIRECTORY_MODE:
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            "Managed media directories must have mode 0700.",
            exit_code=2,
        )


def _require_private_managed_directory(
    path: Path,
    *,
    missing_ok: bool = False,
) -> bool:
    """Verify an existing managed directory through a no-follow descriptor."""

    try:
        fd = _open_directory_chain(path, create=False)
    except FileNotFoundError:
        if missing_ok:
            return False
        raise DownloadError(
            "RECOVERY_CONFLICT",
            "A required managed directory is missing.",
            exit_code=4,
        )
    except (DownloadError, OSError) as exc:
        raise DownloadError(
            "RECOVERY_CONFLICT",
            "A managed directory is a symlink or cannot be opened safely.",
            exit_code=4,
        ) from exc
    try:
        details = os.fstat(fd)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != PRIVATE_DIRECTORY_MODE
        ):
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Managed directory ownership or permissions are unsafe.",
                exit_code=4,
            )
    finally:
        os.close(fd)
    return True


def safe_output_root(raw: str) -> Path:
    require_posix_capabilities()
    output_root = _absolute_clean_path(raw)
    root_path = Path(output_root.anchor)
    home_path = _absolute_clean_path(Path.home())
    if output_root in {root_path, home_path}:
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            "Refusing a filesystem root or home directory as the download output.",
            exit_code=2,
        )
    try:
        fd = _open_directory_chain(output_root, create=True)
    except FileNotFoundError as exc:
        raise DownloadError(
            "INVALID_OUTPUT_DIRECTORY",
            "The output directory could not be created.",
            exit_code=2,
        ) from exc
    try:
        _check_owned_directory_fd(fd, private=False)
    finally:
        os.close(fd)
    return output_root


def _ensure_private_child(parent: Path, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise DownloadError("UNSAFE_OUTPUT_DIRECTORY", "Invalid managed directory name.", exit_code=2)
    parent_fd = _open_directory_chain(parent, create=False)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        try:
            os.mkdir(name, PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
        except FileExistsError:
            pass
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            _check_owned_directory_fd(child_fd, private=True)
        finally:
            os.close(child_fd)
    except OSError as exc:
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            "A managed directory is a symlink or unsafe filesystem object.",
            exit_code=2,
        ) from exc
    finally:
        os.close(parent_fd)
    return parent / name


def managed_layout(output_root: Path) -> dict[str, Path]:
    managed = _ensure_private_child(output_root, MEDIA_ROOT_NAME)
    versioned = _ensure_private_child(managed, MEDIA_LAYOUT_VERSION)
    layout = {"root": versioned}
    for name in ("locks", "staging", "downloads", "quarantine"):
        layout[name] = _ensure_private_child(versioned, name)
    return layout


def safe_platform_directory(output_root: Path, platform_name: str) -> Path:
    """Compatibility API returning the v2 managed downloads directory."""
    root = safe_output_root(str(output_root))
    legacy = root / platform_name
    if legacy.is_symlink() or (legacy.exists() and not legacy.is_dir()):
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            "A legacy platform output path is a symlink or unsafe object.",
            exit_code=2,
        )
    platform_dir = _ensure_private_child(managed_layout(root)["downloads"], platform_name)
    return platform_dir


def _regular_file_details(path: Path, *, allow_links: int = 1) -> os.stat_result:
    parent_fd = _open_directory_chain(path.parent, create=False)
    try:
        details = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise DownloadError("INTEGRITY_FAILED", "A required managed file is missing.", exit_code=7) from exc
    finally:
        os.close(parent_fd)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != allow_links
        or stat.S_IMODE(details.st_mode) != PRIVATE_FILE_MODE
    ):
        raise DownloadError(
            "INTEGRITY_FAILED",
            "A managed file is not an owned mode-0600 regular file with the expected link count.",
            exit_code=7,
        )
    return details


def _fsync_directory(path: Path) -> None:
    fd = _open_directory_chain(path, create=False)
    try:
        os.fsync(fd)
    except OSError as exc:
        raise DownloadError(
            "UNSUPPORTED_PLATFORM",
            "Directory fsync is unavailable on this filesystem.",
            exit_code=3,
        ) from exc
    finally:
        os.close(fd)


def _secure_staging_tree(path: Path) -> None:
    try:
        parent_fd = _open_directory_chain(path.parent, create=False)
    except (FileNotFoundError, DownloadError):
        return
    directory_fd = -1
    try:
        try:
            directory_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Staging path became a symlink or unsafe object.",
                exit_code=4,
            ) from exc

        def secure_directory(fd: int) -> None:
            directory_metadata = os.fstat(fd)
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or directory_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(directory_metadata.st_mode)
                != PRIVATE_DIRECTORY_MODE
            ):
                raise DownloadError(
                    "RECOVERY_CONFLICT",
                    "Staging contains a directory with unsafe ownership or mode.",
                    exit_code=4,
                )
            with os.scandir(fd) as entries:
                for entry in entries:
                    details = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(details.st_mode):
                        raise DownloadError(
                            "RECOVERY_CONFLICT",
                            "Staging contains a symbolic link.",
                            exit_code=4,
                        )
                    if stat.S_ISDIR(details.st_mode):
                        child_fd = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd,
                        )
                        try:
                            secure_directory(child_fd)
                        finally:
                            os.close(child_fd)
                    elif stat.S_ISREG(details.st_mode):
                        file_fd = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd,
                        )
                        try:
                            opened = os.fstat(file_fd)
                            if (
                                not stat.S_ISREG(opened.st_mode)
                                or opened.st_uid != os.geteuid()
                                or opened.st_nlink != 1
                                or (opened.st_dev, opened.st_ino)
                                != (details.st_dev, details.st_ino)
                            ):
                                raise DownloadError(
                                    "INTEGRITY_FAILED",
                                    "Downloader output contains an unsafe hard-linked file.",
                                    exit_code=7,
                                )
                            os.fchmod(file_fd, PRIVATE_FILE_MODE)
                        finally:
                            os.close(file_fd)
                    else:
                        raise DownloadError(
                            "RECOVERY_CONFLICT",
                            "Staging contains a special filesystem object.",
                            exit_code=4,
                        )

        secure_directory(directory_fd)
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(parent_fd)


@contextlib.contextmanager
def source_lock(lock_path: Path, timeout: float) -> Iterator[None]:
    if not math.isfinite(timeout) or timeout < 0:
        raise DownloadError(
            "INVALID_ARGUMENT",
            "--lock-timeout must be a finite non-negative number.",
            exit_code=2,
        )
    try:
        with FileLock(
            lock_path,
            exclusive=True,
            timeout=timeout,
            busy_code="RESOURCE_BUSY",
        ):
            yield
    except PosixRuntimeError as exc:
        raise DownloadError(
            exc.code,
            exc.message,
            exit_code=4 if exc.code == "RESOURCE_BUSY" else 2,
        ) from exc


def parse_printed_path(output: str, *, pinned_cwd: Path | None = None) -> Path:
    for line in reversed([item.strip() for item in output.splitlines() if item.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str) and value:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute() and pinned_cwd is None:
                raise DownloadError(
                    "INVALID_TOOL_OUTPUT",
                    "yt-dlp reported a non-absolute media path.",
                    exit_code=7,
                )
            if candidate.is_absolute():
                return Path(os.path.abspath(candidate))
            if (
                len(candidate.parts) != 1
                or candidate.name in {"", ".", ".."}
                or "\0" in candidate.name
            ):
                raise DownloadError(
                    "INVALID_TOOL_OUTPUT",
                    "yt-dlp reported an unsafe relative media path.",
                    exit_code=7,
                )
            return pinned_cwd / candidate.name
    raise DownloadError(
        "INVALID_TOOL_OUTPUT",
        "yt-dlp did not report the final media path.",
        exit_code=5,
    )


def ffprobe(path: Path) -> dict[str, Any]:
    command = [
        require_tool("ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name,size:stream=codec_type",
        "-of",
        "json",
        str(path),
    ]
    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError("INTEGRITY_FAILED", "ffprobe timed out while validating the media.", exit_code=7) from exc
    if process.returncode != 0:
        raise DownloadError(
            "INTEGRITY_FAILED",
            "ffprobe rejected the downloaded media.",
            exit_code=7,
        )
    try:
        data = json.loads(process.stdout)
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DownloadError("INTEGRITY_FAILED", "ffprobe returned invalid media metadata.", exit_code=7) from exc
    streams = data.get("streams") or []
    video_streams = sum(item.get("codec_type") == "video" for item in streams if isinstance(item, dict))
    audio_streams = sum(item.get("codec_type") == "audio" for item in streams if isinstance(item, dict))
    result = {
        "duration_seconds": duration,
        "duration_ms": round(duration * 1000),
        "has_video": video_streams > 0,
        "has_audio": audio_streams > 0,
        "container": str((data.get("format") or {}).get("format_name") or ""),
        "video_streams": video_streams,
        "audio_streams": audio_streams,
    }
    if not result["has_video"] or not math.isfinite(duration) or duration <= 0:
        raise DownloadError(
            "INTEGRITY_FAILED",
            "The output is not a playable video with positive duration.",
            details=json.dumps(result),
            exit_code=7,
        )
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    parent_fd = _open_directory_chain(path.parent, create=False)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    with os.fdopen(descriptor, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json_noclobber(path: Path, value: dict[str, Any]) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    try:
        atomic_write_noclobber(
            path,
            payload,
            PRIVATE_FILE_MODE,
        )
    except PosixRuntimeError as exc:
        raise DownloadError(
            exc.code,
            exc.message,
            exit_code=4 if exc.code in {"PATH_COLLISION", "RECOVERY_CONFLICT"} else 5,
        ) from exc


def version_of(executable: str) -> str:
    process = subprocess.run(
        [executable, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    return (process.stdout or process.stderr).strip().splitlines()[0]


def tool_version_date(version: str) -> dt.date | None:
    match = re.match(r"(\d{4})[.-](\d{1,2})[.-](\d{1,2})", version)
    if not match:
        return None
    try:
        return dt.date(*(int(value) for value in match.groups()))
    except ValueError:
        return None


def version_warnings(version: str) -> list[str]:
    built = tool_version_date(version)
    warnings: list[str] = []
    if built and built < TESTED_YTDLP_BASELINE:
        warnings.append(
            f"yt-dlp {version} is below the tested {TESTED_YTDLP_BASELINE.isoformat()} baseline."
        )
    if built:
        age = (dt.date.today() - built).days
        if age > 90:
            warnings.append(f"yt-dlp is {age} days old; update before diagnosing extractor failures.")
    return warnings


def playwright_chromium_status() -> dict[str, Any]:
    if importlib.util.find_spec("playwright") is None:
        return {"available": False, "reason": "playwright Python package is not installed"}
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path).expanduser().resolve()
    except Exception as exc:
        return {"available": False, "reason": redact_sensitive(str(exc))}
    return {
        "available": executable.is_file() and os.access(executable, os.X_OK),
        "executable_path": str(executable),
        "reason": "" if executable.is_file() else "Playwright Chromium runtime is not installed",
    }


def _require_exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise DownloadError(
            "INVALID_ARTIFACT",
            f"{label} does not match the video artifact v2 contract.",
            exit_code=7,
        )
    return value


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _canonical_validate(value: dict[str, Any], *, expected: str, recovery: bool = False) -> None:
    if CONTRACT_BUNDLE_ERROR is not None:
        raise DownloadError(
            "CONTRACT_BUILD_MISMATCH",
            "The vendored contract bundle failed its integrity check.",
            exit_code=7,
        )
    try:
        validate_contract(value, expected=expected)
    except ContractError as exc:
        code = (
            "UNSUPPORTED_SCHEMA_VERSION"
            if exc.code == "UNSUPPORTED_SCHEMA_VERSION"
            else "RECOVERY_CONFLICT"
            if recovery
            else "INVALID_ARTIFACT"
        )
        raise DownloadError(code, exc.message, details=f"path={exc.path}", exit_code=4 if recovery else 7) from exc


def strict_json_load(
    path: Path,
    *,
    maximum_bytes: int = MAX_ARTIFACT_BYTES,
    allowed_link_counts: tuple[int, ...] = (1,),
) -> dict[str, Any]:
    parent_fd = _open_directory_chain(path.parent, create=False)
    fd = -1
    try:
        try:
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except FileNotFoundError as exc:
            raise DownloadError("INVALID_ARTIFACT", "JSON input is missing.", exit_code=7) from exc
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink not in allowed_link_counts
            or stat.S_IMODE(details.st_mode) != PRIVATE_FILE_MODE
        ):
            raise DownloadError("INVALID_ARTIFACT", "JSON input is an unsafe file.", exit_code=7)
        if details.st_size <= 0 or details.st_size > maximum_bytes:
            raise DownloadError("INVALID_ARTIFACT", "JSON input has an unsafe size.", exit_code=7)

        def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise DownloadError("INVALID_ARTIFACT", "JSON contains a duplicate key.", exit_code=7)
                result[key] = item
            return result

        def invalid_constant(_: str) -> None:
            raise DownloadError("INVALID_ARTIFACT", "JSON contains a non-finite number.", exit_code=7)

        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = json.load(
                handle,
                object_pairs_hook=object_pairs,
                parse_constant=invalid_constant,
            )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DownloadError("INVALID_ARTIFACT", "JSON is malformed.", exit_code=7) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)
    if not isinstance(value, dict):
        raise DownloadError("INVALID_ARTIFACT", "JSON root must be an object.", exit_code=7)
    return value


def validate_video_artifact(
    manifest: dict[str, Any],
    *,
    artifact_path: Path | None = None,
    expected_platform: str | None = None,
    expected_fingerprint: str | None = None,
    revalidate_media: bool = True,
) -> dict[str, Any]:
    _canonical_validate(manifest, expected="video-artifact")
    _require_exact_keys(manifest, set(VIDEO_CONTRACT_SHAPE["required"]), "artifact")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("artifact_type") != "video"
        or manifest.get("status") != "complete"
    ):
        raise DownloadError(
            "UNSUPPORTED_SCHEMA_VERSION",
            "Only complete awesome-capture video artifact v2 manifests are accepted.",
            exit_code=7,
        )
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise DownloadError("INVALID_ARTIFACT", "created_at must be a UTC timestamp.", exit_code=7)
    try:
        parsed_created = dt.datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise DownloadError("INVALID_ARTIFACT", "created_at is not ISO-8601.", exit_code=7) from exc
    if parsed_created.tzinfo is None:
        raise DownloadError("INVALID_ARTIFACT", "created_at must include a timezone.", exit_code=7)

    source = manifest["source"]
    platform_name = source["platform"]
    if platform_name not in PLATFORM_SUFFIXES:
        raise DownloadError("INVALID_ARTIFACT", "source.platform is unsupported.", exit_code=7)
    if not SHA256_RE.fullmatch(source["fingerprint"]):
        raise DownloadError("INVALID_ARTIFACT", "source.fingerprint is invalid.", exit_code=7)
    if expected_platform and platform_name != expected_platform:
        raise DownloadError("INVALID_ARTIFACT", "Artifact platform does not match the request.", exit_code=7)
    if expected_fingerprint and source["fingerprint"] != expected_fingerprint:
        raise DownloadError("INVALID_ARTIFACT", "Artifact source does not match the request.", exit_code=7)
    for key in ("url", "webpage_url"):
        if key not in source:
            continue
        if source[key] != sanitize_source_url(source[key], platform_name):
            raise DownloadError("INVALID_ARTIFACT", "Artifact contains an unsanitized URL.", exit_code=7)
        _, detected_platform = normalize_and_detect(source[key])
        if detected_platform != platform_name:
            raise DownloadError("INVALID_ARTIFACT", "Artifact URL platform is inconsistent.", exit_code=7)
    if (
        "url" in source
        and hashlib.sha256(source["url"].encode("utf-8")).hexdigest()
        != source["fingerprint"]
    ):
        raise DownloadError("INVALID_ARTIFACT", "Artifact source fingerprint is inconsistent.", exit_code=7)

    media = _require_exact_keys(
        manifest.get("media"), set(VIDEO_CONTRACT_SHAPE["media"]), "media"
    )
    if not isinstance(media.get("path"), str) or not Path(media["path"]).is_absolute():
        raise DownloadError("INVALID_ARTIFACT", "media.path must be absolute.", exit_code=7)
    if not _is_plain_int(media.get("bytes")) or media["bytes"] <= 0:
        raise DownloadError("INVALID_ARTIFACT", "media.bytes must be positive.", exit_code=7)
    if not SHA256_RE.fullmatch(str(media.get("sha256") or "")):
        raise DownloadError("INVALID_ARTIFACT", "media.sha256 is invalid.", exit_code=7)
    if not _is_plain_int(media.get("duration_ms")) or media["duration_ms"] <= 0:
        raise DownloadError("INVALID_ARTIFACT", "media.duration_ms must be positive.", exit_code=7)
    if media.get("has_video") is not True or not isinstance(media.get("has_audio"), bool):
        raise DownloadError("INVALID_ARTIFACT", "Video stream flags are invalid.", exit_code=7)
    if not isinstance(media.get("container"), str) or not media["container"]:
        raise DownloadError("INVALID_ARTIFACT", "media.container is required.", exit_code=7)
    for key in ("video_streams", "audio_streams"):
        if not _is_plain_int(media.get(key)) or media[key] < 0:
            raise DownloadError("INVALID_ARTIFACT", f"media.{key} is invalid.", exit_code=7)
    if media["video_streams"] < 1 or (media["audio_streams"] > 0) != media["has_audio"]:
        raise DownloadError("INVALID_ARTIFACT", "Media stream counts are inconsistent.", exit_code=7)
    probe = _require_exact_keys(
        media.get("ffprobe"),
        {"tool", "version", "evidence_sha256"},
        "media.ffprobe",
    )
    if (
        probe.get("tool") != "ffprobe"
        or not isinstance(probe.get("version"), str)
        or not probe["version"]
        or not SHA256_RE.fullmatch(str(probe.get("evidence_sha256") or ""))
        or probe["evidence_sha256"] != video_probe_evidence_sha256(media)
    ):
        raise DownloadError(
            "INVALID_ARTIFACT",
            "ffprobe provenance or evidence digest is invalid.",
            exit_code=7,
        )

    acquisition = _require_exact_keys(
        manifest.get("acquisition"), set(VIDEO_CONTRACT_SHAPE["acquisition"]), "acquisition"
    )
    if not isinstance(acquisition.get("auth_mode"), str) or not isinstance(
        acquisition.get("fallback"), str
    ):
        raise DownloadError("INVALID_ARTIFACT", "Acquisition identity is invalid.", exit_code=7)
    warnings = acquisition.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise DownloadError("INVALID_ARTIFACT", "Acquisition warnings are invalid.", exit_code=7)

    producer = manifest["producer"]
    if (
        producer.get("skill") != "download-video"
        or producer.get("contract_digest") != CONTRACT_DIGEST
    ):
        raise DownloadError(
            "CONTRACT_BUILD_MISMATCH",
            "Artifact producer contract does not match this skill build.",
            exit_code=7,
        )

    if artifact_path is not None:
        _regular_file_details(artifact_path)
        expected_media_path = artifact_path.parent / Path(media["path"]).name
        if Path(media["path"]) != expected_media_path:
            raise DownloadError(
                "INVALID_ARTIFACT",
                "Artifact media path is not adjacent to its commit marker.",
                exit_code=7,
            )
    if revalidate_media:
        media_path = Path(media["path"])
        current = _regular_file_details(media_path)
        if current.st_size != media["bytes"] or sha256_file(media_path) != media["sha256"]:
            raise DownloadError("INTEGRITY_FAILED", "Artifact media bytes have changed.", exit_code=7)
        probed = ffprobe(media_path)
        comparisons = {
            "duration_ms": probed["duration_ms"],
            "has_video": probed["has_video"],
            "has_audio": probed["has_audio"],
            "container": probed["container"],
            "video_streams": probed["video_streams"],
            "audio_streams": probed["audio_streams"],
        }
        if any(media[key] != value for key, value in comparisons.items()):
            raise DownloadError("INTEGRITY_FAILED", "Artifact ffprobe facts have changed.", exit_code=7)
    return manifest


def _source_payload(
    info: dict[str, Any],
    *,
    platform_name: str,
    public_url: str,
    fingerprint: str,
    extractor: str,
) -> dict[str, str]:
    metadata = safe_metadata(info, platform_name, public_url)
    webpage_url = str(metadata.get("webpage_url") or public_url)
    return {
        "platform": platform_name,
        "fingerprint": fingerprint,
        "url": public_url,
        "webpage_url": sanitize_source_url(webpage_url, platform_name),
        "id": safe_persisted_metadata(
            metadata.get("id"),
            maximum_chars=512,
        )
        or fingerprint,
        "title": safe_persisted_metadata(
            metadata.get("title"),
            maximum_chars=4096,
        ),
        "author": safe_persisted_metadata(
            metadata.get("author"),
            maximum_chars=1024,
        ),
        "extractor": safe_persisted_metadata(
            metadata.get("extractor") or extractor,
            maximum_chars=256,
        )
        or "unknown",
    }


def _manifest_payload(
    *,
    media_path: Path,
    platform_name: str,
    source: dict[str, str],
    tool_name: str,
    tool_version: str,
    auth_mode: str,
    fallback: str,
    warnings: list[str],
) -> dict[str, Any]:
    details = ffprobe(media_path)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "video",
        "status": "complete",
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "source": source,
        "media": {
            "path": str(media_path),
            "bytes": _regular_file_details(media_path).st_size,
            "sha256": sha256_file(media_path),
            "duration_ms": details["duration_ms"],
            "has_video": details["has_video"],
            "has_audio": details["has_audio"],
            "container": details["container"],
            "video_streams": details["video_streams"],
            "audio_streams": details["audio_streams"],
            "ffprobe": {
                "tool": "ffprobe",
                "version": version_of(require_tool("ffprobe")),
                "evidence_sha256": video_probe_evidence_sha256(details),
            },
        },
        "acquisition": {
            "auth_mode": auth_mode,
            "fallback": fallback,
            "warnings": [redact_sensitive(item) for item in warnings],
        },
        "producer": {
            "skill": "download-video",
            "tool": tool_name,
            "version": tool_version,
            "contract_digest": CONTRACT_DIGEST,
        },
    }
    validate_video_artifact(manifest, revalidate_media=True)
    return manifest


def manifest_for(
    *,
    media_path: Path,
    platform_name: str,
    source: dict[str, Any],
    tool_name: str,
    tool_version: str,
    auth_mode: str,
    warnings: list[str],
) -> tuple[dict[str, Any], Path]:
    """Compatibility helper; only accepts an already managed final media path."""
    public_url = sanitize_source_url(str(source.get("url") or source.get("webpage_url") or ""), platform_name)
    fingerprint = hashlib.sha256(public_url.encode("utf-8")).hexdigest()
    source_payload = _source_payload(
        source,
        platform_name=platform_name,
        public_url=public_url,
        fingerprint=fingerprint,
        extractor=tool_name,
    )
    manifest = _manifest_payload(
        media_path=media_path,
        platform_name=platform_name,
        source=source_payload,
        tool_name=tool_name,
        tool_version=tool_version,
        auth_mode=auth_mode,
        fallback="none",
        warnings=warnings,
    )
    manifest_path = media_path.parent / "artifact.json"
    atomic_json_noclobber(manifest_path, manifest)
    return manifest, manifest_path


def can_gallery_fallback(args: argparse.Namespace, platform_name: str, error: DownloadError) -> bool:
    return (
        getattr(args, "gallery_fallback", "auto") == "auto"
        and platform_name in {"tiktok", "twitter"}
        and requested_auth_mode(args) == "anonymous"
        and error.code in {"IP_BLOCKED", "DOWNLOAD_FAILED", "NETWORK_ERROR"}
        and bool(shutil.which("gallery-dl"))
    )


def run_process_raw(
    command: list[str],
    *,
    timeout: int,
    pinned_cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return _run_subprocess_pinned(
            command,
            timeout=timeout,
            pinned_cwd=pinned_cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(
            "NETWORK_ERROR",
            f"Fallback command timed out after {timeout} seconds.",
            exit_code=5,
        ) from exc


def _new_staging_directory(layout: dict[str, Path], fingerprint: str) -> Path:
    name = f"{fingerprint}.{uuid.uuid4().hex}"
    parent_fd = _open_directory_chain(layout["staging"], create=False)
    try:
        os.mkdir(name, PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise DownloadError("PATH_COLLISION", "A staging transaction already exists.", exit_code=4) from exc
    finally:
        os.close(parent_fd)
    staging = layout["staging"] / name
    _fsync_directory(layout["staging"])
    return staging


def _quarantine_staging(layout: dict[str, Path], staging: Path) -> Path:
    _secure_staging_tree(staging)
    if staging.parent != layout["staging"]:
        raise DownloadError("RECOVERY_CONFLICT", "Refusing to quarantine an unknown path.", exit_code=4)
    destination = layout["quarantine"] / f"{staging.name}.{uuid.uuid4().hex}"
    source_parent_fd = _open_directory_chain(layout["staging"], create=False)
    destination_parent_fd = _open_directory_chain(layout["quarantine"], create=False)
    source_fd = -1
    destination_fd = -1
    try:
        try:
            source_fd = os.open(
                staging.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_parent_fd,
            )
        except FileNotFoundError:
            return staging
        source_details = os.fstat(source_fd)
        if (
            not stat.S_ISDIR(source_details.st_mode)
            or source_details.st_uid != os.geteuid()
            or stat.S_IMODE(source_details.st_mode) != PRIVATE_DIRECTORY_MODE
        ):
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Refusing to quarantine an unsafe staging object.",
                exit_code=4,
            )
        try:
            os.stat(
                destination.name,
                dir_fd=destination_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise DownloadError("RECOVERY_CONFLICT", "Quarantine destination collided.", exit_code=4)
        try:
            destination_fd = move_verified_noreplace(
                staging.name,
                destination.name,
                source_dir_fd=source_parent_fd,
                destination_dir_fd=destination_parent_fd,
                source_fd=source_fd,
                expected_kind="directory",
                rename_impl=rename_noreplace,
            )
        except FileExistsError as exc:
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Quarantine destination raced with another filesystem object.",
                exit_code=4,
            ) from exc
        except PosixRuntimeError as exc:
            raise DownloadError(
                exc.code,
                exc.message,
                exit_code=4 if exc.code == "RECOVERY_CONFLICT" else 7,
            ) from exc
        os.fsync(destination_fd)
        os.fsync(source_parent_fd)
        os.fsync(destination_parent_fd)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(destination_parent_fd)
        os.close(source_parent_fd)
    return destination


def _quarantine_final_journal(
    layout: dict[str, Path],
    final_dir: Path,
    journal_path: Path,
) -> Path:
    if (
        journal_path != final_dir / ".transaction.json"
        or not path_within(final_dir, layout["downloads"])
    ):
        raise DownloadError(
            "RECOVERY_CONFLICT",
            "Refusing to quarantine an unknown transaction journal.",
            exit_code=4,
        )
    target_name = (
        f"download-{final_dir.parent.name}-{final_dir.name}-"
        f"transaction-{uuid.uuid4().hex}.json"
    )
    try:
        return quarantine_private_file(
            journal_path,
            layout["quarantine"],
            target_name=target_name,
        )
    except SafeRuntimeError as exc:
        raise DownloadError(
            exc.code,
            exc.message,
            exit_code=exc.exit_code,
        ) from exc


def _validated_staging_media(
    staging: Path,
    *,
    printed_path: Path | None,
) -> tuple[Path, dict[str, Any], list[str]]:
    files: list[Path] = []
    for candidate in staging.iterdir():
        details = candidate.lstat()
        if stat.S_ISLNK(details.st_mode) or stat.S_ISDIR(details.st_mode):
            raise DownloadError(
                "INTEGRITY_FAILED",
                "Downloader output contained a link or nested directory.",
                exit_code=7,
            )
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise DownloadError("INTEGRITY_FAILED", "Downloader output is not a safe regular file.", exit_code=7)
        parent_fd = _open_directory_chain(candidate.parent, create=False)
        try:
            descriptor = os.open(
                candidate.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (details.st_dev, details.st_ino)
            ):
                raise DownloadError(
                    "INTEGRITY_FAILED",
                    "Downloader output changed during safety validation.",
                    exit_code=7,
                )
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)
        files.append(candidate)
    info_files = [path for path in files if path.name.endswith(".info.json")]
    incomplete = [path for path in files if path.suffix.lower() in {".part", ".ytdl", ".tmp"}]
    media_files = [path for path in files if path not in info_files and path not in incomplete]
    if incomplete or len(media_files) != 1 or len(info_files) > 1:
        raise DownloadError(
            "INTEGRITY_FAILED",
            "Downloader must produce exactly one complete media file and at most one info file.",
            exit_code=7,
        )
    media_path = media_files[0]
    if printed_path is not None and printed_path != media_path:
        raise DownloadError(
            "INTEGRITY_FAILED",
            "Downloader-reported media path does not match the isolated staging result.",
            exit_code=7,
        )
    ffprobe(media_path)
    info: dict[str, Any] = {}
    warnings: list[str] = []
    if info_files:
        try:
            loaded = strict_json_load(info_files[0])
            info = loaded
        except DownloadError:
            warnings.append("Ignored malformed downloader metadata.")
    return media_path, info, warnings


def _safe_media_suffix(media_path: Path) -> str:
    suffix = media_path.suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".media"


def _descriptor(path: Path) -> dict[str, Any]:
    metadata = _regular_file_details(path)
    return {"bytes": metadata.st_size, "sha256": sha256_file(path)}


def _publish_file_no_clobber(source: Path, destination: Path) -> None:
    source_parent_fd = _open_directory_chain(source.parent, create=False)
    destination_parent_fd = _open_directory_chain(destination.parent, create=False)
    source_fd = -1
    destination_fd = -1
    try:
        try:
            source_fd = os.open(
                source.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_parent_fd,
            )
        except OSError as exc:
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Publish source cannot be opened safely.",
                exit_code=4,
            ) from exc
        source_details = os.fstat(source_fd)
        if (
            not stat.S_ISREG(source_details.st_mode)
            or source_details.st_uid != os.geteuid()
            or source_details.st_nlink != 1
            or stat.S_IMODE(source_details.st_mode) != PRIVATE_FILE_MODE
        ):
            raise DownloadError("RECOVERY_CONFLICT", "Publish source is unsafe.", exit_code=4)
        try:
            destination_fd = move_verified_noreplace(
                source.name,
                destination.name,
                source_dir_fd=source_parent_fd,
                destination_dir_fd=destination_parent_fd,
                source_fd=source_fd,
                expected_kind="file",
                rename_impl=rename_noreplace,
            )
        except FileExistsError as exc:
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Publish destination raced with another writer.",
                exit_code=4,
            ) from exc
        except PosixRuntimeError as exc:
            raise DownloadError(
                exc.code,
                exc.message,
                exit_code=4 if exc.code == "RECOVERY_CONFLICT" else 7,
            ) from exc
        linked_details = os.fstat(destination_fd)
        if (
            not stat.S_ISREG(linked_details.st_mode)
            or linked_details.st_ino != source_details.st_ino
            or linked_details.st_dev != source_details.st_dev
            or linked_details.st_nlink != 1
            or stat.S_IMODE(linked_details.st_mode) != PRIVATE_FILE_MODE
        ):
            raise DownloadError("RECOVERY_CONFLICT", "Published file identity is invalid.", exit_code=4)
        os.fsync(destination_fd)
        os.fsync(destination_parent_fd)
        os.fsync(source_parent_fd)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(destination_parent_fd)
        os.close(source_parent_fd)


def _publish_staging(
    *,
    layout: dict[str, Path],
    staging: Path,
    staged_media: Path,
    platform_name: str,
    source: dict[str, str],
    source_info: dict[str, str],
    tool_name: str,
    tool_version: str,
    auth_mode: str,
    fallback: str,
    warnings: list[str],
) -> dict[str, Any]:
    media_sha256 = sha256_file(staged_media)
    platform_dir = _ensure_private_child(layout["downloads"], platform_name)
    source_dir = _ensure_private_child(platform_dir, source["fingerprint"])
    final_dir = source_dir / media_sha256
    if final_dir.exists() or final_dir.is_symlink():
        _require_private_managed_directory(final_dir)
        artifact_path = final_dir / "artifact.json"
        if artifact_path.is_file() and not artifact_path.is_symlink():
            existing = validate_video_artifact(
                strict_json_load(artifact_path),
                artifact_path=artifact_path,
                expected_platform=platform_name,
                expected_fingerprint=source["fingerprint"],
                revalidate_media=True,
            )
            source_info_path = final_dir / "source.info.json"
            if strict_json_load(source_info_path) != existing["source"]:
                raise DownloadError(
                    "INTEGRITY_FAILED",
                    "Published sanitized source metadata no longer matches the artifact.",
                    exit_code=7,
                )
            _quarantine_staging(layout, staging)
            return _download_result(existing, artifact_path, reused=True)
        raise DownloadError(
            "RECOVERY_CONFLICT",
            "A final media directory exists without a valid commit marker.",
            exit_code=4,
        )

    media_name = f"media{_safe_media_suffix(staged_media)}"
    final_media = final_dir / media_name
    manifest = _manifest_payload(
        media_path=staged_media,
        platform_name=platform_name,
        source=source,
        tool_name=tool_name,
        tool_version=tool_version,
        auth_mode=auth_mode,
        fallback=fallback,
        warnings=warnings,
    )
    manifest["media"]["path"] = str(final_media)
    pending_source_info = staging / "source.info.pending.json"
    pending_artifact = staging / "artifact.pending.json"
    atomic_json_noclobber(pending_source_info, source_info)
    atomic_json_noclobber(pending_artifact, manifest)
    transaction_id = str(uuid.uuid4())
    destinations = {
        "media": final_media.relative_to(layout["root"]).as_posix(),
        "source_info": (final_dir / "source.info.json").relative_to(layout["root"]).as_posix(),
        "artifact": (final_dir / "artifact.json").relative_to(layout["root"]).as_posix(),
    }
    media_descriptor = _descriptor(staged_media)
    source_info_descriptor = _descriptor(pending_source_info)
    artifact_descriptor = _descriptor(pending_artifact)
    journal = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "kind": "download",
        "status": "publishing",
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "job_id": source["fingerprint"],
        "root": str(layout["root"]),
        "staging_root": str(staging),
        "steps": [
            {
                "index": 0,
                "operation": "publish-file",
                "source": staged_media.name,
                "destination": destinations["media"],
                **media_descriptor,
                "status": "pending",
            },
            {
                "index": 1,
                "operation": "publish-file",
                "source": pending_source_info.name,
                "destination": destinations["source_info"],
                **source_info_descriptor,
                "status": "pending",
            },
            {
                "index": 2,
                "operation": "publish-receipt",
                "source": pending_artifact.name,
                "destination": destinations["artifact"],
                **artifact_descriptor,
                "status": "pending",
            },
        ],
    }
    _validate_transaction_contract(journal)
    atomic_json_noclobber(staging / "journal.json", journal)
    test_failpoint("download.after-staging-journal")
    source_fd = _open_directory_chain(source_dir, create=False)
    try:
        os.mkdir(media_sha256, PRIVATE_DIRECTORY_MODE, dir_fd=source_fd)
    except FileExistsError as exc:
        raise DownloadError(
            "RECOVERY_CONFLICT",
            "Final directory raced with another writer.",
            exit_code=4,
        ) from exc
    finally:
        os.close(source_fd)
    _fsync_directory(source_dir)
    _require_private_managed_directory(final_dir)
    atomic_json_noclobber(final_dir / ".transaction.json", journal)
    test_failpoint("download.after-final-journal")
    source_paths = [staged_media, pending_source_info, pending_artifact]
    destination_paths = [
        final_media,
        final_dir / "source.info.json",
        final_dir / "artifact.json",
    ]
    for index, (source_path, destination_path) in enumerate(zip(source_paths, destination_paths)):
        if index == 2:
            validate_video_artifact(manifest, artifact_path=None, revalidate_media=True)
        _publish_file_no_clobber(source_path, destination_path)
        test_failpoint(f"download.after-publish-{index}")
    artifact_path = final_dir / "artifact.json"
    validate_video_artifact(
        manifest,
        artifact_path=artifact_path,
        expected_platform=platform_name,
        expected_fingerprint=source["fingerprint"],
        revalidate_media=True,
    )
    test_failpoint("download.before-cleanup")
    _quarantine_final_journal(
        layout,
        final_dir,
        final_dir / ".transaction.json",
    )
    _quarantine_staging(layout, staging)
    return _download_result(manifest, artifact_path, reused=False)


def _download_result(
    manifest: dict[str, Any], artifact_path: Path, *, reused: bool
) -> dict[str, Any]:
    return {
        "status": "ok",
        "operation": "download",
        "result": "reused" if reused else "created",
        "media_path": manifest["media"]["path"],
        "manifest_path": str(artifact_path),
        "artifact_path": str(artifact_path),
        "manifest": manifest,
    }


def _find_reusable(
    layout: dict[str, Path],
    *,
    platform_name: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    platform_dir = layout["downloads"] / platform_name
    source_dir = layout["downloads"] / platform_name / fingerprint
    if not source_dir.exists():
        return None
    _require_private_managed_directory(platform_dir)
    _require_private_managed_directory(source_dir)
    artifacts: list[tuple[dict[str, Any], Path]] = []
    for final_dir in source_dir.iterdir():
        _require_private_managed_directory(final_dir)
        artifact_path = final_dir / "artifact.json"
        if not artifact_path.exists():
            if (final_dir / ".transaction.json").exists():
                continue
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "An uncommitted media directory requires explicit recovery.",
                exit_code=4,
            )
        artifact = validate_video_artifact(
            strict_json_load(artifact_path),
            artifact_path=artifact_path,
            expected_platform=platform_name,
            expected_fingerprint=fingerprint,
            revalidate_media=True,
        )
        if final_dir.name != artifact["media"]["sha256"]:
            raise DownloadError("INVALID_ARTIFACT", "Final directory hash is inconsistent.", exit_code=7)
        source_info = strict_json_load(final_dir / "source.info.json")
        if source_info != artifact["source"]:
            raise DownloadError(
                "INTEGRITY_FAILED",
                "Published sanitized source metadata no longer matches the artifact.",
                exit_code=7,
            )
        artifacts.append((artifact, artifact_path))
    if len(artifacts) > 1:
        raise DownloadError(
            "RECOVERY_CONFLICT",
            "Multiple committed artifacts exist for the same source.",
            exit_code=4,
        )
    if not artifacts:
        return None
    artifact, artifact_path = artifacts[0]
    return _download_result(artifact, artifact_path, reused=True)


def _validate_transaction_contract(journal: dict[str, Any]) -> None:
    _canonical_validate(journal, expected="transaction", recovery=True)
    required = {
        "schema_version",
        "transaction_id",
        "kind",
        "status",
        "created_at",
        "job_id",
        "root",
        "staging_root",
        "steps",
    }
    if set(journal) != required or journal.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise DownloadError("RECOVERY_CONFLICT", "Transaction journal is invalid.", exit_code=4)
    if journal.get("kind") != "download" or journal.get("status") != "publishing":
        raise DownloadError("RECOVERY_CONFLICT", "Transaction state is invalid.", exit_code=4)
    try:
        transaction_id = uuid.UUID(str(journal.get("transaction_id")))
    except ValueError as exc:
        raise DownloadError("RECOVERY_CONFLICT", "Transaction id is invalid.", exit_code=4) from exc
    if str(transaction_id) != journal["transaction_id"]:
        raise DownloadError("RECOVERY_CONFLICT", "Transaction id is not canonical.", exit_code=4)
    if not SHA256_RE.fullmatch(str(journal.get("job_id") or "")):
        raise DownloadError("RECOVERY_CONFLICT", "Transaction job identity is invalid.", exit_code=4)
    if not isinstance(journal.get("root"), str) or not Path(journal["root"]).is_absolute():
        raise DownloadError("RECOVERY_CONFLICT", "Transaction root is invalid.", exit_code=4)
    if not isinstance(journal.get("staging_root"), str) or not Path(
        journal["staging_root"]
    ).is_absolute():
        raise DownloadError("RECOVERY_CONFLICT", "Transaction staging root is invalid.", exit_code=4)
    steps = journal.get("steps")
    if not isinstance(steps, list) or len(steps) != 3:
        raise DownloadError("RECOVERY_CONFLICT", "Transaction steps are invalid.", exit_code=4)
    expected_operations = ("publish-file", "publish-file", "publish-receipt")
    for index, step in enumerate(steps):
        if (
            not isinstance(step, dict)
            or set(step)
            != {
                "index",
                "operation",
                "source",
                "destination",
                "bytes",
                "sha256",
                "status",
            }
            or step.get("index") != index
            or step.get("operation") != expected_operations[index]
            or step.get("status") not in {"pending", "published"}
            or not isinstance(step.get("source"), str)
            or not isinstance(step.get("destination"), str)
            or Path(step["source"]).is_absolute()
            or Path(step["destination"]).is_absolute()
            or ".." in Path(step["source"]).parts
            or ".." in Path(step["destination"]).parts
            or not _is_plain_int(step.get("bytes"))
            or step["bytes"] < 0
            or not SHA256_RE.fullmatch(str(step.get("sha256") or ""))
        ):
            raise DownloadError("RECOVERY_CONFLICT", "Transaction step is invalid.", exit_code=4)


def _load_transaction(path: Path) -> dict[str, Any]:
    journal = strict_json_load(path)
    _validate_transaction_contract(journal)
    return journal


def _recover_final_transaction(
    layout: dict[str, Path], final_dir: Path, journal_path: Path
) -> dict[str, Any]:
    _require_private_managed_directory(final_dir)
    journal = _load_transaction(journal_path)
    if Path(journal["root"]) != layout["root"] or not path_within(final_dir, layout["downloads"]):
        raise DownloadError("RECOVERY_CONFLICT", "Transaction containment is invalid.", exit_code=4)
    staging = Path(journal["staging_root"])
    if (
        not path_within(staging, layout["staging"])
        or staging.parent != layout["staging"]
        or journal["job_id"] != staging.name.split(".", 1)[0]
    ):
        raise DownloadError("RECOVERY_CONFLICT", "Transaction staging path is invalid.", exit_code=4)
    _require_private_managed_directory(staging)
    steps = journal["steps"]
    source_paths = [staging / step["source"] for step in steps]
    destination_paths = [layout["root"] / step["destination"] for step in steps]
    if any(
        not path_within(destination, layout["downloads"])
        or destination.parent != final_dir
        for destination in destination_paths
    ):
        raise DownloadError("RECOVERY_CONFLICT", "Transaction destination is invalid.", exit_code=4)
    if destination_paths[-1] != final_dir / "artifact.json":
        raise DownloadError("RECOVERY_CONFLICT", "Transaction commit marker is invalid.", exit_code=4)
    artifact_source = source_paths[-1]
    artifact_path = destination_paths[-1]
    if artifact_source.exists():
        manifest = strict_json_load(artifact_source)
    elif artifact_path.exists():
        manifest = strict_json_load(artifact_path)
    else:
        raise DownloadError("RECOVERY_CONFLICT", "Pending artifact payload is missing.", exit_code=4)
    media_path = Path(manifest.get("media", {}).get("path", ""))
    if (
        media_path != destination_paths[0]
        or final_dir.name != manifest.get("media", {}).get("sha256")
    ):
        raise DownloadError("RECOVERY_CONFLICT", "Transaction media path is invalid.", exit_code=4)
    allowed = {journal_path.name, *(path.name for path in destination_paths)}
    if any(entry.name not in allowed for entry in final_dir.iterdir()):
        raise DownloadError("RECOVERY_CONFLICT", "Unknown files exist in a pending transaction.", exit_code=4)

    allowed_staging = {"journal.json", *(path.name for path in source_paths)}
    if not staging.exists() or staging.is_symlink() or any(
        entry.name not in allowed_staging and not entry.name.endswith(".info.json")
        for entry in staging.iterdir()
    ):
        raise DownloadError("RECOVERY_CONFLICT", "Unknown staging state exists.", exit_code=4)

    for index, (source_path, destination_path, step) in enumerate(
        zip(source_paths, destination_paths, steps)
    ):
        source_exists = source_path.exists() and not source_path.is_symlink()
        destination_exists = destination_path.exists() and not destination_path.is_symlink()
        if source_exists:
            source_details = source_path.lstat()
            if not stat.S_ISREG(source_details.st_mode) or source_details.st_uid != os.geteuid():
                raise DownloadError("RECOVERY_CONFLICT", "Transaction source is unsafe.", exit_code=4)
            if (
                source_details.st_nlink != 1
                or stat.S_IMODE(source_details.st_mode) != PRIVATE_FILE_MODE
            ):
                raise DownloadError("RECOVERY_CONFLICT", "Transaction source mode changed.", exit_code=4)
            if source_details.st_size != step["bytes"] or sha256_file(source_path) != step["sha256"]:
                raise DownloadError("RECOVERY_CONFLICT", "Transaction source hash changed.", exit_code=4)
        if destination_exists:
            destination_details = destination_path.lstat()
            if (
                not stat.S_ISREG(destination_details.st_mode)
                or destination_details.st_uid != os.geteuid()
                or stat.S_IMODE(destination_details.st_mode) != PRIVATE_FILE_MODE
                or destination_details.st_size != step["bytes"]
                or sha256_file(destination_path) != step["sha256"]
            ):
                raise DownloadError("RECOVERY_CONFLICT", "Transaction destination hash changed.", exit_code=4)
        if source_exists and destination_exists:
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Transaction source and destination both exist.",
                exit_code=4,
            )
        elif source_exists:
            if index == 2:
                validate_video_artifact(manifest, revalidate_media=True)
            _publish_file_no_clobber(source_path, destination_path)
        elif destination_exists:
            _regular_file_details(destination_path)
        else:
            raise DownloadError("RECOVERY_CONFLICT", "Transaction step has no source or destination.", exit_code=4)
    artifact = validate_video_artifact(
        manifest, artifact_path=artifact_path, revalidate_media=True
    )
    _quarantine_final_journal(layout, final_dir, journal_path)
    _quarantine_staging(layout, staging)
    return _download_result(artifact, artifact_path, reused=False)


def _validate_completed_download_for_recovery(
    *,
    platform_dir: Path,
    source_dir: Path,
    final_dir: Path,
) -> dict[str, Any]:
    """Strictly and read-only revalidate an unjournaled commit directory."""

    try:
        platform_name = platform_dir.name
        fingerprint = source_dir.name
        if platform_name not in PLATFORM_SUFFIXES:
            raise DownloadError(
                "INVALID_ARTIFACT",
                "Completed download uses an unknown platform directory.",
                exit_code=7,
            )
        if not SHA256_RE.fullmatch(fingerprint):
            raise DownloadError(
                "INVALID_ARTIFACT",
                "Completed download uses an invalid source directory.",
                exit_code=7,
            )
        if not SHA256_RE.fullmatch(final_dir.name):
            raise DownloadError(
                "INVALID_ARTIFACT",
                "Completed download uses an invalid media directory.",
                exit_code=7,
            )

        artifact_path = final_dir / "artifact.json"
        artifact = validate_video_artifact(
            strict_json_load(artifact_path),
            artifact_path=artifact_path,
            expected_platform=platform_name,
            expected_fingerprint=fingerprint,
            revalidate_media=True,
        )
        media_path = Path(artifact["media"]["path"])
        if final_dir.name != artifact["media"]["sha256"]:
            raise DownloadError(
                "INVALID_ARTIFACT",
                "Completed media directory does not match the media hash.",
                exit_code=7,
            )
        if media_path.name in {"artifact.json", "source.info.json", ".transaction.json"}:
            raise DownloadError(
                "INVALID_ARTIFACT",
                "Completed media filename collides with managed metadata.",
                exit_code=7,
            )

        source_info_path = final_dir / "source.info.json"
        source_info = strict_json_load(source_info_path)
        if source_info != artifact["source"]:
            raise DownloadError(
                "INTEGRITY_FAILED",
                "Published source metadata no longer matches the artifact.",
                exit_code=7,
            )

        directory_fd = _open_directory_chain(final_dir, create=False)
        try:
            directory_metadata = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or directory_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(directory_metadata.st_mode)
                != PRIVATE_DIRECTORY_MODE
            ):
                raise DownloadError(
                    "INTEGRITY_FAILED",
                    "Completed media directory permissions are unsafe.",
                    exit_code=7,
                )
            with os.scandir(directory_fd) as entries:
                actual_names = {entry.name for entry in entries}
        finally:
            os.close(directory_fd)
        expected_names = {
            "artifact.json",
            "source.info.json",
            media_path.name,
        }
        if actual_names != expected_names:
            raise DownloadError(
                "INTEGRITY_FAILED",
                "Completed media directory contains missing or unknown entries.",
                exit_code=7,
            )
        return artifact
    except DownloadError as exc:
        raise DownloadError(
            "RECOVERY_CONFLICT",
            "Completed download evidence failed strict recovery validation.",
            exit_code=4,
        ) from exc
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        raise DownloadError(
            "RECOVERY_CONFLICT",
            "Completed download evidence could not be safely revalidated.",
            exit_code=4,
        ) from exc


def recover_layout(
    layout: dict[str, Path], *, only_fingerprint: str | None = None
) -> dict[str, Any]:
    recovered: list[str] = []
    # The staging journal is fsynced before the final journal.  Recreate a
    # missing final copy after a crash in that narrow window.
    for staging in list(layout["staging"].iterdir()):
        _require_private_managed_directory(staging)
        fingerprint = staging.name.split(".", 1)[0]
        if only_fingerprint and fingerprint != only_fingerprint:
            continue
        staging_journal_path = staging / "journal.json"
        if not staging_journal_path.exists():
            continue
        staging_journal = _load_transaction(staging_journal_path)
        if (
            staging_journal["job_id"] != fingerprint
            or Path(staging_journal["root"]) != layout["root"]
            or Path(staging_journal["staging_root"]) != staging
        ):
            raise DownloadError("RECOVERY_CONFLICT", "Staging journal identity is invalid.", exit_code=4)
        destinations = [
            layout["root"] / step["destination"] for step in staging_journal["steps"]
        ]
        final_dir = destinations[-1].parent
        if (
            not path_within(final_dir, layout["downloads"])
            or any(destination.parent != final_dir for destination in destinations)
        ):
            raise DownloadError("RECOVERY_CONFLICT", "Staging journal destination is invalid.", exit_code=4)
        final_journal_path = final_dir / ".transaction.json"
        if not final_dir.exists():
            parent_fd = _open_directory_chain(final_dir.parent, create=False)
            try:
                os.mkdir(final_dir.name, PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
            except FileExistsError:
                pass
            finally:
                os.close(parent_fd)
            _fsync_directory(final_dir.parent)
        _require_private_managed_directory(final_dir)
        if final_journal_path.exists():
            final_journal = _load_transaction(final_journal_path)
            if final_journal != staging_journal:
                raise DownloadError("RECOVERY_CONFLICT", "Transaction journal copies disagree.", exit_code=4)
        else:
            atomic_json_noclobber(final_journal_path, staging_journal)

    for platform_dir in layout["downloads"].iterdir():
        _require_private_managed_directory(platform_dir)
        for source_dir in platform_dir.iterdir():
            _require_private_managed_directory(source_dir)
            if only_fingerprint and source_dir.name != only_fingerprint:
                continue
            for final_dir in source_dir.iterdir():
                _require_private_managed_directory(final_dir)
                journal_path = final_dir / ".transaction.json"
                if journal_path.exists():
                    result = _recover_final_transaction(layout, final_dir, journal_path)
                    recovered.append(result["manifest_path"])
                else:
                    _validate_completed_download_for_recovery(
                        platform_dir=platform_dir,
                        source_dir=source_dir,
                        final_dir=final_dir,
                    )
    quarantined: list[str] = []
    for staging in list(layout["staging"].iterdir()):
        _require_private_managed_directory(staging)
        fingerprint = staging.name.split(".", 1)[0]
        if only_fingerprint and fingerprint != only_fingerprint:
            continue
        # A matching final journal consumes its staging directory above.  Anything
        # left has no recoverable commit intent and is isolated, never reused.
        destination = _quarantine_staging(layout, staging)
        quarantined.append(str(destination))
    return {
        "status": "ok",
        "operation": "recover",
        "recovered": recovered,
        "quarantined": quarantined,
        "conflicts": [],
    }


def _gallery_download_locked(
    args: argparse.Namespace,
    *,
    url: str,
    platform_name: str,
    layout: dict[str, Path],
    fingerprint: str,
    original_error: DownloadError,
) -> dict[str, Any]:
    gallery = require_tool("gallery-dl")
    staging = _new_staging_directory(layout, fingerprint)
    try:
        command = [
            gallery,
            "--config-ignore",
            "--no-input",
        ]
        if platform_name == "twitter":
            command.append("--force-ipv4")
        command.extend(
            [
                "--range",
                "1",
                "-D",
                ".",
                "-f",
                "download.{extension}",
                url,
            ]
        )
        process = run_process_raw(command, timeout=args.timeout, pinned_cwd=staging)
        _secure_staging_tree(staging)
        if process.returncode != 0:
            raise DownloadError(
                original_error.code,
                f"{original_error.message} gallery-dl fallback also failed.",
                exit_code=original_error.exit_code,
            )
        media_path, _, metadata_warnings = _validated_staging_media(staging, printed_path=None)
        public_url = sanitize_source_url(url, platform_name)
        source = _source_payload(
            {},
            platform_name=platform_name,
            public_url=public_url,
            fingerprint=fingerprint,
            extractor="gallery-dl",
        )
        warnings = [
            f"yt-dlp failed with {original_error.code}; used the bounded gallery-dl fallback.",
            *metadata_warnings,
        ]
        return _publish_staging(
            layout=layout,
            staging=staging,
            staged_media=media_path,
            platform_name=platform_name,
            source=source,
            source_info=source,
            tool_name="gallery-dl",
            tool_version=version_of(gallery),
            auth_mode="anonymous",
            fallback="gallery-dl",
            warnings=warnings,
        )
    except Exception:
        if staging.exists():
            _quarantine_staging(layout, staging)
        raise


def gallery_download(
    args: argparse.Namespace,
    *,
    url: str,
    platform_name: str,
    output_root: Path,
    original_error: DownloadError,
) -> dict[str, Any]:
    root = safe_output_root(str(output_root))
    layout = managed_layout(root)
    public_url = sanitize_source_url(url, platform_name)
    fingerprint = hashlib.sha256(public_url.encode("utf-8")).hexdigest()
    with source_lock(layout["locks"] / f"{fingerprint}.lock", getattr(args, "lock_timeout", 30.0)):
        recover_layout(layout, only_fingerprint=fingerprint)
        reusable = _find_reusable(
            layout, platform_name=platform_name, fingerprint=fingerprint
        )
        if reusable:
            return reusable
        return _gallery_download_locked(
            args,
            url=url,
            platform_name=platform_name,
            layout=layout,
            fingerprint=fingerprint,
            original_error=original_error,
        )


def download(args: argparse.Namespace) -> dict[str, Any]:
    url, platform = normalize_and_detect(args.url)
    public_url = sanitize_source_url(url, platform)
    fingerprint = hashlib.sha256(public_url.encode("utf-8")).hexdigest()
    require_tool("ffmpeg")
    output_root = safe_output_root(args.output_dir)
    layout = managed_layout(output_root)
    with source_lock(layout["locks"] / f"{fingerprint}.lock", args.lock_timeout):
        recover_layout(layout, only_fingerprint=fingerprint)
        reusable = _find_reusable(layout, platform_name=platform, fingerprint=fingerprint)
        if reusable:
            return reusable
        staging = _new_staging_directory(layout, fingerprint)
        template = "%(id)s--%(title).120B.%(ext)s"
        tail = [
            "--part",
            "--no-overwrites",
            "--write-info-json",
            "--format",
            QUALITY_FORMATS[args.quality],
            "--paths",
            "temp:.",
            "--output",
            template,
            "--print",
            "after_move:%(filepath)j",
        ]
        try:
            try:
                process, auth_mode, fallback_warnings = run_ytdlp(
                    args,
                    url=url,
                    platform_name=platform,
                    tail=tail,
                    timeout=args.timeout,
                    private_temp_dir=staging,
                )
            except DownloadError as error:
                _secure_staging_tree(staging)
                _quarantine_staging(layout, staging)
                if can_gallery_fallback(args, platform, error):
                    return _gallery_download_locked(
                        args,
                        url=url,
                        platform_name=platform,
                        layout=layout,
                        fingerprint=fingerprint,
                        original_error=error,
                    )
                raise
            _secure_staging_tree(staging)
            printed_path = parse_printed_path(process.stdout, pinned_cwd=staging)
            media_path, info, metadata_warnings = _validated_staging_media(
                staging, printed_path=printed_path
            )
            source = _source_payload(
                info,
                platform_name=platform,
                public_url=public_url,
                fingerprint=fingerprint,
                extractor="yt-dlp",
            )
            yt_version = version_of(require_tool("yt-dlp"))
            warnings = [
                *version_warnings(yt_version),
                *fallback_warnings,
                *metadata_warnings,
            ]
            return _publish_staging(
                layout=layout,
                staging=staging,
                staged_media=media_path,
                platform_name=platform,
                source=source,
                source_info=source,
                tool_name="yt-dlp",
                tool_version=yt_version,
                auth_mode=auth_mode,
                fallback="ephemeral_browser" if auth_mode == "ephemeral_browser" else "none",
                warnings=warnings,
            )
        except Exception:
            if staging.exists():
                _quarantine_staging(layout, staging)
            raise


def recover(args: argparse.Namespace) -> dict[str, Any]:
    output_root = safe_output_root(args.output_dir)
    layout = managed_layout(output_root)
    # Recovery takes each persistent source lock independently.  Unjournaled
    # staging is quarantined under its own source lock.
    fingerprints: set[str] = set()
    for staging in layout["staging"].iterdir():
        fingerprints.add(staging.name.split(".", 1)[0])
    for lock in layout["locks"].iterdir():
        try:
            metadata = lock.lstat()
        except OSError as exc:
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Managed lock directory changed during recovery.",
                exit_code=4,
            ) from exc
        if (
            re.fullmatch(r"[0-9a-f]{64}\.lock", lock.name) is None
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE
        ):
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Managed lock directory contains an unknown or unsafe object.",
                exit_code=4,
            )
    for platform_dir in layout["downloads"].iterdir():
        if platform_dir.name not in PLATFORM_SUFFIXES:
            raise DownloadError(
                "RECOVERY_CONFLICT",
                "Downloads root contains an unknown platform directory.",
                exit_code=4,
            )
        _require_private_managed_directory(platform_dir)
        for source_dir in platform_dir.iterdir():
            _require_private_managed_directory(source_dir)
            if not SHA256_RE.fullmatch(source_dir.name):
                raise DownloadError(
                    "RECOVERY_CONFLICT",
                    "Downloads root contains an unknown source identity.",
                    exit_code=4,
                )
            fingerprints.add(source_dir.name)
    aggregate = {"status": "ok", "operation": "recover", "recovered": [], "quarantined": [], "conflicts": []}
    for fingerprint in sorted(fingerprints):
        if not SHA256_RE.fullmatch(fingerprint):
            raise DownloadError("RECOVERY_CONFLICT", "Unknown managed job identity.", exit_code=4)
        with source_lock(layout["locks"] / f"{fingerprint}.lock", args.lock_timeout):
            result = recover_layout(layout, only_fingerprint=fingerprint)
            aggregate["recovered"].extend(result["recovered"])
            aggregate["quarantined"].extend(result["quarantined"])
    return aggregate


def doctor(_: argparse.Namespace) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for name in ("yt-dlp", "ffmpeg", "ffprobe", "deno", "gallery-dl"):
        executable = shutil.which(name)
        tools[name] = {"available": bool(executable), "path": executable or ""}
        if executable:
            try:
                tools[name]["version"] = version_of(executable)
            except (OSError, subprocess.SubprocessError):
                tools[name]["version"] = "unknown"
    warnings: list[str] = []
    version = str(tools.get("yt-dlp", {}).get("version") or "")
    built = tool_version_date(version)
    if built:
        age = (dt.date.today() - built).days
        tools["yt-dlp"]["age_days"] = age
        tools["yt-dlp"]["stale"] = age > 90
        tools["yt-dlp"]["below_tested_baseline"] = built < TESTED_YTDLP_BASELINE
        warnings.extend(version_warnings(version))
    security_error = ""
    try:
        require_posix_capabilities()
    except DownloadError as exc:
        security_error = exc.message
    ready = (
        all(tools[name]["available"] for name in ("yt-dlp", "ffmpeg", "ffprobe"))
        and not security_error
    )
    return {
        "status": "ok" if ready else "error",
        "operation": "doctor",
        "ready": ready,
        "tools": tools,
        "warnings": warnings,
        "optional": {
            "douyin_ephemeral_browser": playwright_chromium_status(),
            "tiktok_twitter_gallery_fallback": bool(shutil.which("gallery-dl")),
            "youtube_javascript_runtime": bool(shutil.which("deno")),
        },
        "supported_platforms": sorted(PLATFORM_SUFFIXES),
        "security_runtime": {
            "ready": not security_error,
            "python": ".".join(str(item) for item in sys.version_info[:3]),
            "platform": sys.platform,
            "reason": security_error,
        },
    }


def add_network_options(parser: argparse.ArgumentParser) -> None:
    cookies = parser.add_mutually_exclusive_group()
    cookies.add_argument("--cookies", help="Authorized Netscape-format Cookie file.")
    cookies.add_argument(
        "--cookies-from-browser", help="Authorized yt-dlp browser[:profile] Cookie source."
    )
    parser.add_argument("--impersonate", help="Explicit yt-dlp impersonation target.")
    parser.add_argument(
        "--douyin-browser-fallback",
        choices=("auto", "off"),
        default="auto",
        help="Use an isolated anonymous Chromium context for fresh Douyin cookies.",
    )
    parser.add_argument(
        "--gallery-fallback",
        choices=("auto", "off"),
        default="auto",
        help="Use gallery-dl once for recoverable TikTok/X failures.",
    )
    parser.add_argument(
        "--browser-wait-seconds",
        type=lambda value: bounded_float_argument(
            value,
            name="--browser-wait-seconds",
            minimum=0.0,
            maximum=120.0,
        ),
        default=15.0,
    )
    parser.add_argument(
        "--socket-timeout",
        type=lambda value: bounded_integer_argument(
            value,
            name="--socket-timeout",
            minimum=1,
            maximum=300,
        ),
        default=20,
    )
    parser.add_argument(
        "--retries",
        type=lambda value: bounded_integer_argument(
            value,
            name="--retries",
            minimum=0,
            maximum=10,
        ),
        default=3,
    )
    parser.add_argument(
        "--timeout",
        type=lambda value: bounded_integer_argument(
            value,
            name="--timeout",
            minimum=1,
            maximum=86400,
        ),
        default=900,
    )


def bounded_integer_argument(
    value: str,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer.") from exc
    if parsed < minimum or parsed > maximum:
        raise argparse.ArgumentTypeError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return parsed


def bounded_float_argument(
    value: str,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a number.") from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise argparse.ArgumentTypeError(
            f"{name} must be finite and between {minimum:g} and {maximum:g}."
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    detect_parser = subparsers.add_parser("detect")
    detect_parser.add_argument("url")
    subparsers.add_parser("doctor")
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("url")
    add_network_options(probe_parser)
    download_parser = subparsers.add_parser("download")
    download_parser.add_argument("url")
    download_parser.add_argument("--output-dir", required=True)
    download_parser.add_argument("--quality", choices=sorted(QUALITY_FORMATS), default="best")
    download_parser.add_argument(
        "--lock-timeout",
        type=lambda value: bounded_float_argument(
            value,
            name="--lock-timeout",
            minimum=0.0,
            maximum=86400.0,
        ),
        default=30.0,
    )
    add_network_options(download_parser)
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--output-dir", required=True)
    recover_parser.add_argument(
        "--lock-timeout",
        type=lambda value: bounded_float_argument(
            value,
            name="--lock-timeout",
            minimum=0.0,
            maximum=86400.0,
        ),
        default=30.0,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if any(item in {"-h", "--help"} for item in actual_argv):
            json_print(
                {
                    "status": "ok",
                    "operation": "help",
                    "commands": ["detect", "doctor", "probe", "download", "recover"],
                }
            )
            return 0
        args = build_parser().parse_args(actual_argv)
        try:
            current_contract_digest = contract_digest()
        except ContractError as exc:
            raise DownloadError(
                "CONTRACT_BUILD_MISMATCH",
                "The vendored contract bundle failed its integrity check.",
                exit_code=7,
            ) from exc
        if CONTRACT_BUNDLE_ERROR is not None or current_contract_digest != CONTRACT_DIGEST:
            raise DownloadError(
                "CONTRACT_BUILD_MISMATCH",
                "The vendored contract bundle failed its integrity check.",
                exit_code=7,
            )
        if args.command == "detect":
            url, platform = normalize_and_detect(args.url)
            sanitized = sanitize_source_url(url, platform)
            result = {
                "status": "ok",
                "operation": "detect",
                "platform": platform,
                "sanitized_url": sanitized,
                "source_fingerprint": hashlib.sha256(sanitized.encode("utf-8")).hexdigest(),
            }
        elif args.command == "doctor":
            result = doctor(args)
            if result.get("status") != "ok":
                missing = sorted(
                    name
                    for name in ("yt-dlp", "ffmpeg", "ffprobe")
                    if not result["tools"][name]["available"]
                )
                security = result.get("security_runtime") or {}
                if missing:
                    raise DownloadError(
                        "DEPENDENCY_MISSING",
                        "The download environment is missing required executables.",
                        details=json.dumps({"missing": missing}, sort_keys=True),
                        exit_code=3,
                    )
                raise DownloadError(
                    "UNSUPPORTED_PLATFORM",
                    "The secure POSIX download runtime is unavailable.",
                    details=str(security.get("reason") or ""),
                    exit_code=3,
                )
        elif args.command == "probe":
            result = probe(args)
        elif args.command == "recover":
            result = recover(args)
        else:
            result = download(args)
        json_print(result)
        return 0
    except DownloadError as exc:
        json_print(exc.as_dict(), stream=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        json_print(
            DownloadError("INTERRUPTED", "Operation interrupted.", exit_code=130).as_dict(),
            stream=sys.stderr,
        )
        return 130
    except Exception as exc:
        error = DownloadError(
            "RUNTIME_FAILED",
            "The download operation failed unexpectedly.",
            details=exc.__class__.__name__,
            exit_code=5,
        )
        json_print(error.as_dict(), stream=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
