#!/usr/bin/env python3
"""Download one supported social video and emit a verified artifact manifest."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = "awesome-capture.artifact/v1"
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
    "bilibili": {"p"},
    "douyin": {"modal_id"},
    "tiktok": set(),
    "twitter": set(),
}
TESTED_YTDLP_BASELINE = dt.date(2026, 7, 4)


class DownloadError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: str = "", exit_code: int = 1):
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


def json_print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def redact_sensitive(text: str) -> str:
    text = re.sub(r"(?i)(cookie|authorization):\s*[^\r\n]+", r"\1: <redacted>", text)
    text = re.sub(r"(?i)(--cookies(?:-from-browser)?(?:=|\s+))\S+", r"\1<redacted>", text)
    text = re.sub(r"(https?://[^\s?#]+)\?[^\s]+", r"\1?<redacted>", text)
    return text.strip()[-4000:]


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
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() in allowed
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", query, ""))


def require_tool(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise DownloadError("DEPENDENCY_MISSING", f"Required executable is missing: {name}", exit_code=3)
    return value


def run_checked(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(
            "NETWORK_ERROR",
            f"Command timed out after {timeout} seconds.",
            details=str(exc),
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
        (("not available in your country", "geo-restricted"), "GEO_BLOCKED", "The content is region restricted.", 6),
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
            6,
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
            return DownloadError(code, message, details=details, exit_code=exit_code)
    return DownloadError("DOWNLOAD_FAILED", "yt-dlp failed to acquire the video.", details=details)


def auth_args(args: argparse.Namespace) -> list[str]:
    if getattr(args, "cookies", None):
        cookie_path = Path(args.cookies).expanduser().resolve()
        if not cookie_path.is_file():
            raise DownloadError("INVALID_COOKIE_SOURCE", f"Cookie file does not exist: {cookie_path}", exit_code=2)
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
            details=str(exc),
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
                        details=final_url,
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
            details=str(exc),
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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def run_ytdlp(
    args: argparse.Namespace,
    *,
    url: str,
    platform_name: str,
    tail: list[str],
    timeout: int,
) -> tuple[subprocess.CompletedProcess[str], str, list[str]]:
    command = base_ytdlp_args(args, platform_name) + tail + [url]
    try:
        return run_checked(command, timeout=timeout), requested_auth_mode(args), []
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
        with tempfile.TemporaryDirectory(prefix="awesome-capture-douyin-") as temporary:
            cookie_path = Path(temporary) / "cookies.txt"
            write_netscape_cookies(cookie_path, cookies)
            retry = base_ytdlp_args(args, platform_name) + ["--cookies", str(cookie_path)] + tail + [url]
            process = run_checked(retry, timeout=timeout)
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
    raise DownloadError("INVALID_TOOL_OUTPUT", "yt-dlp did not return a JSON metadata object.")


def safe_metadata(info: dict[str, Any], platform: str, source_url: str) -> dict[str, Any]:
    webpage_url = str(info.get("webpage_url") or source_url)
    return {
        "platform": platform,
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or ""),
        "author": str(info.get("uploader") or info.get("channel") or ""),
        "duration_seconds": info.get("duration"),
        "webpage_url": sanitize_source_url(webpage_url, platform),
        "extractor": str(info.get("extractor_key") or info.get("extractor") or ""),
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


def path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_output_root(raw: str) -> Path:
    output_root = Path(raw).expanduser().resolve()
    if output_root in {Path(output_root.anchor).resolve(), Path.home().resolve()}:
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            f"Refusing a filesystem root or home directory as the download output: {output_root}",
            exit_code=2,
        )
    output_root.mkdir(parents=True, exist_ok=True)
    if not output_root.is_dir():
        raise DownloadError(
            "INVALID_OUTPUT_DIRECTORY",
            f"Download output is not a directory: {output_root}",
            exit_code=2,
        )
    return output_root


def safe_platform_directory(output_root: Path, platform_name: str) -> Path:
    platform_dir = output_root / platform_name
    if platform_dir.exists() and (not platform_dir.is_dir() or platform_dir.is_symlink()):
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            f"Platform output is not a real directory: {platform_dir}",
            exit_code=2,
        )
    platform_dir.mkdir(parents=True, exist_ok=True)
    if not path_within(platform_dir, output_root):
        raise DownloadError(
            "UNSAFE_OUTPUT_DIRECTORY",
            f"Platform output escapes the requested directory: {platform_dir}",
            exit_code=2,
        )
    return platform_dir


def parse_printed_path(output: str) -> Path:
    for line in reversed([item.strip() for item in output.splitlines() if item.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str) and value:
            return Path(value).expanduser().resolve()
    raise DownloadError("INVALID_TOOL_OUTPUT", "yt-dlp did not report the final media path.")


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
        raise DownloadError("INTEGRITY_FAILED", "ffprobe rejected the downloaded media.", details=process.stderr, exit_code=7)
    try:
        data = json.loads(process.stdout)
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DownloadError("INTEGRITY_FAILED", "ffprobe returned invalid media metadata.", exit_code=7) from exc
    streams = data.get("streams") or []
    result = {
        "duration_seconds": duration,
        "has_video": any(item.get("codec_type") == "video" for item in streams),
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
        "container": str((data.get("format") or {}).get("format_name") or ""),
    }
    if not result["has_video"] or duration <= 0:
        raise DownloadError(
            "INTEGRITY_FAILED",
            "The output is not a playable video with positive duration.",
            details=json.dumps(result),
            exit_code=7,
        )
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


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
    details = ffprobe(media_path)
    safe_source = dict(source)
    for key in ("url", "webpage_url"):
        value = safe_source.get(key)
        if isinstance(value, str) and value:
            safe_source[key] = sanitize_source_url(value, platform_name)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "video",
        "status": "complete",
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "source": {**safe_source, "platform": platform_name},
        "media": {
            "path": str(media_path),
            "bytes": media_path.stat().st_size,
            "sha256": sha256_file(media_path),
            **details,
        },
        "acquisition": {"auth_mode": auth_mode, "warnings": warnings},
        "producer": {
            "skill": "download-video",
            "tool": tool_name,
            "version": tool_version,
        },
    }
    manifest_path = Path(f"{media_path}.artifact.json")
    atomic_json(manifest_path, manifest)
    return manifest, manifest_path


def can_gallery_fallback(args: argparse.Namespace, platform_name: str, error: DownloadError) -> bool:
    return (
        getattr(args, "gallery_fallback", "auto") == "auto"
        and platform_name in {"tiktok", "twitter"}
        and requested_auth_mode(args) == "anonymous"
        and error.code in {"IP_BLOCKED", "DOWNLOAD_FAILED", "NETWORK_ERROR"}
        and bool(shutil.which("gallery-dl"))
    )


def gallery_download(
    args: argparse.Namespace,
    *,
    url: str,
    platform_name: str,
    output_root: Path,
    original_error: DownloadError,
) -> dict[str, Any]:
    gallery = require_tool("gallery-dl")
    platform_dir = safe_platform_directory(output_root, platform_name)
    public_url = sanitize_source_url(url, platform_name)
    source_id = hashlib.sha256(public_url.encode()).hexdigest()[:16]
    existing = [
        path
        for path in platform_dir.glob(f"{source_id}--gallery-dl.*")
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".artifact.json")
    ]
    if len(existing) > 1:
        raise DownloadError(
            "PATH_COLLISION",
            f"Multiple fallback assets already exist for source {source_id}.",
            details="\n".join(str(path) for path in existing),
            exit_code=7,
        )
    if existing:
        destination = existing[0]
        ffprobe(destination)
        gallery_version = version_of(gallery)
        warnings = [
            f"yt-dlp failed with {original_error.code}; reused the verified gallery-dl fallback asset."
        ]
        manifest, manifest_path = manifest_for(
            media_path=destination,
            platform_name=platform_name,
            source={
                "url": public_url,
                "webpage_url": public_url,
                "id": source_id,
                "title": "",
                "author": "",
                "duration_seconds": None,
                "extractor": "gallery-dl",
            },
            tool_name="gallery-dl",
            tool_version=gallery_version,
            auth_mode="anonymous",
            warnings=warnings,
        )
        return {
            "status": "ok",
            "operation": "download",
            "media_path": str(destination),
            "manifest_path": str(manifest_path),
            "manifest": manifest,
        }
    with tempfile.TemporaryDirectory(prefix=".gallery-", dir=platform_dir) as temporary:
        staging = Path(temporary)
        command = [
            gallery,
            "--config-ignore",
            "--no-input",
            "--range",
            "1",
            "-D",
            str(staging),
            "-f",
            f"{source_id}.{{extension}}",
            url,
        ]
        process = run_process_raw(command, timeout=args.timeout)
        if process.returncode != 0:
            raise DownloadError(
                original_error.code,
                f"{original_error.message} gallery-dl fallback also failed.",
                details=f"yt-dlp: {original_error.details}\ngallery-dl: {process.stderr or process.stdout}",
                exit_code=original_error.exit_code,
            )
        candidates = [
            path
            for path in staging.rglob("*")
            if path.is_file() and path.suffix.lower() not in {".json", ".part", ".txt"}
        ]
        selected: Path | None = None
        for candidate in candidates:
            try:
                ffprobe(candidate)
            except DownloadError:
                continue
            selected = candidate
            break
        if selected is None:
            raise DownloadError(
                "INTEGRITY_FAILED",
                "gallery-dl did not produce a playable video.",
                details=process.stderr or process.stdout,
                exit_code=7,
            )
        destination = platform_dir / f"{source_id}--gallery-dl{selected.suffix.lower()}"
        if destination.exists():
            if destination.is_symlink() or sha256_file(destination) != sha256_file(selected):
                raise DownloadError(
                    "PATH_COLLISION",
                    f"Fallback destination already exists with different content: {destination}",
                    exit_code=7,
                )
        else:
            os.replace(selected, destination)
    gallery_version = version_of(gallery)
    warnings = [
        f"yt-dlp failed with {original_error.code}; used the bounded gallery-dl fallback."
    ]
    manifest, manifest_path = manifest_for(
        media_path=destination,
        platform_name=platform_name,
        source={
            "url": public_url,
            "webpage_url": public_url,
            "id": source_id,
            "title": "",
            "author": "",
            "duration_seconds": None,
            "extractor": "gallery-dl",
        },
        tool_name="gallery-dl",
        tool_version=gallery_version,
        auth_mode="anonymous",
        warnings=warnings,
    )
    return {
        "status": "ok",
        "operation": "download",
        "media_path": str(destination),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
    }


def run_process_raw(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(
            "NETWORK_ERROR",
            f"Fallback command timed out after {timeout} seconds.",
            details=str(exc),
            exit_code=5,
        ) from exc


def download(args: argparse.Namespace) -> dict[str, Any]:
    url, platform = normalize_and_detect(args.url)
    require_tool("ffmpeg")
    output_root = safe_output_root(args.output_dir)
    platform_dir = safe_platform_directory(output_root, platform)
    template = str(platform_dir / "%(id)s--%(title).120B.%(ext)s")
    tail = [
        "--continue",
        "--part",
        "--no-overwrites",
        "--write-info-json",
        "--format",
        QUALITY_FORMATS[args.quality],
        "--output",
        template,
        "--print",
        "after_move:%(filepath)j",
    ]
    try:
        process, auth_mode, fallback_warnings = run_ytdlp(
            args,
            url=url,
            platform_name=platform,
            tail=tail,
            timeout=args.timeout,
        )
    except DownloadError as error:
        if can_gallery_fallback(args, platform, error):
            return gallery_download(
                args,
                url=url,
                platform_name=platform,
                output_root=output_root,
                original_error=error,
            )
        raise
    media_path = parse_printed_path(process.stdout)
    if not path_within(media_path, output_root):
        raise DownloadError("INTEGRITY_FAILED", "yt-dlp reported a path outside the requested output directory.", exit_code=7)
    if not media_path.is_file() or media_path.is_symlink():
        raise DownloadError("INTEGRITY_FAILED", "The reported media file is missing or is a symbolic link.", exit_code=7)
    ffprobe(media_path)
    info_path = media_path.with_suffix(f".info.json")
    info: dict[str, Any] = {}
    metadata_warnings: list[str] = []
    if info_path.is_symlink():
        metadata_warnings.append(f"Ignored symbolic-link metadata file: {info_path}")
    elif info_path.is_file():
        info_path.chmod(0o600)
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
    source = safe_metadata(info, platform, url)
    source["url"] = sanitize_source_url(url, platform)
    yt_version = version_of(require_tool("yt-dlp"))
    warnings = [*version_warnings(yt_version), *fallback_warnings, *metadata_warnings]
    manifest, manifest_path = manifest_for(
        media_path=media_path,
        platform_name=platform,
        source=source,
        tool_name="yt-dlp",
        tool_version=yt_version,
        auth_mode=auth_mode,
        warnings=warnings,
    )
    return {
        "status": "ok",
        "operation": "download",
        "media_path": str(media_path),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
    }


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
    ready = all(tools[name]["available"] for name in ("yt-dlp", "ffmpeg", "ffprobe"))
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
    parser.add_argument("--browser-wait-seconds", type=float, default=15.0)
    parser.add_argument("--socket-timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
    add_network_options(download_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "detect":
            url, platform = normalize_and_detect(args.url)
            result = {"status": "ok", "operation": "detect", "platform": platform, "url": url}
        elif args.command == "doctor":
            result = doctor(args)
        elif args.command == "probe":
            result = probe(args)
        else:
            result = download(args)
        json_print(result)
        return 0 if result.get("status") == "ok" else 3
    except DownloadError as exc:
        json_print(exc.as_dict(), stream=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
