"""Bounded acquisition of a public Douyin video in an anonymous browser.

The caller owns the source lock, staging directory, ffprobe verification and
transactional publication. Signed playback addresses exist only in memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import importlib.metadata
import json
import math
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit


MAX_MEDIA_BYTES = 1024 * 1024 * 1024
MAX_DETAIL_BYTES = 4 * 1024 * 1024
MAX_DETAIL_RESPONSES = 16
CLEANUP_TIMEOUT_SECONDS = 2.0
MEDIA_FILENAME = "browser.mp4"
DETAIL_PATH = "/aweme/v1/web/aweme/detail/"


class DouyinBrowserError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 5):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _failure(message: str, *, integrity: bool = False) -> DouyinBrowserError:
    return DouyinBrowserError(
        "INTEGRITY_FAILED" if integrity else "BROWSER_FALLBACK_FAILED",
        message,
        7 if integrity else 5,
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DouyinBrowserError("NETWORK_ERROR", "The anonymous acquisition timed out.")
    return remaining


def _https_parts(url: str):
    if not isinstance(url, str) or len(url) > 16384 or re.search(r"[\x00-\x20\x7f\\]", url):
        return None
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or not parts.hostname
        ):
            return None
        return parts
    except ValueError:
        return None


def _is_target_page(url: str, video_id: str) -> bool:
    parts = _https_parts(url)
    return bool(
        parts
        and parts.hostname == "www.douyin.com"
        and parts.path in (f"/video/{video_id}", f"/video/{video_id}/")
    )


def _is_detail_url(url: str, video_id: str) -> bool:
    parts = _https_parts(url)
    return bool(
        parts
        and parts.hostname == "www.douyin.com"
        and parts.path == DETAIL_PATH
        and parse_qs(parts.query).get("aweme_id") == [video_id]
    )


def _is_media_url(url: str) -> bool:
    parts = _https_parts(url)
    return bool(
        parts
        and (parts.hostname == "douyinvod.com" or parts.hostname.endswith(".douyinvod.com"))
        and not parts.fragment
    )


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _public_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    text = re.sub(r"(?i)[a-z][a-z0-9+.-]*://\S+", "<redacted-url>", text)
    text = re.sub(
        r"(?i)\b(?:api[_-]?key|authorization|bearer|cookie|credential|password|"
        r"private[_-]?header|secret|signature|token)\s*[:=]\s*\S+",
        "<redacted>",
        text,
    )
    return text[:limit]


def _video_details(payload: Any, video_id: str, quality: str) -> dict[str, Any]:
    if quality not in ("best", "1080p", "720p"):
        raise DouyinBrowserError("INVALID_ARGUMENT", "Unsupported video quality.", 2)
    detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict) or detail.get("aweme_id") != video_id:
        raise _failure("The anonymous response does not identify the requested video.", integrity=True)
    if payload.get("status_code", 0) != 0:
        raise _failure("The anonymous detail response did not succeed.")
    video = detail.get("video")
    if not isinstance(video, dict):
        raise _failure("The public page did not expose video playback data.")
    duration = _positive_int(video.get("duration"))
    if duration is None or duration > 24 * 60 * 60 * 1000:
        raise _failure("The public video duration is invalid.", integrity=True)
    # Only known playback-address fields are candidates; never search arbitrary
    # JSON recursively for addresses or accept an unrelated feed item.
    candidates: list[dict[str, Any]] = []

    def add(address: Any, bitrate: Any = 0) -> None:
        if not isinstance(address, dict):
            return
        height = _positive_int(address.get("height")) or _positive_int(video.get("height"))
        width = _positive_int(address.get("width")) or _positive_int(video.get("width"))
        if height is None or width is None:
            return
        urls = address.get("url_list")
        if not isinstance(urls, list):
            return
        for candidate in urls[:16]:
            if _is_media_url(candidate):
                candidates.append({
                    "url": candidate, "height": height, "width": width,
                    "bitrate": _positive_int(bitrate) or 0,
                })

    for key in ("play_addr", "play_addr_h264", "play_addr_bytevc1"):
        add(video.get(key))
    bitrates = video.get("bit_rate", [])
    if isinstance(bitrates, list):
        for item in bitrates[:64]:
            if isinstance(item, dict) and not item.get("is_bytevc2"):
                add(item.get("play_addr"), item.get("bit_rate"))
    all_urls = {item["url"] for item in candidates}
    ceiling = {"best": math.inf, "1080p": 1080, "720p": 720}[quality]
    eligible = [item for item in candidates if item["height"] <= ceiling]
    if not eligible:
        raise _failure("The public page has no permitted playback source at the requested quality.")
    chosen = max(eligible, key=lambda item: (item["height"], item["width"], item["bitrate"]))
    author = detail.get("author")
    return {
        "media_url": chosen["url"],
        "playback_urls": all_urls,
        "duration_ms": duration,
        "info": {
            "id": video_id,
            "title": _public_text(detail.get("desc"), 4096),
            "uploader": _public_text(author.get("nickname"), 1024) if isinstance(author, dict) else "",
            "webpage_url": f"https://www.douyin.com/video/{video_id}",
            "extractor": "DouyinBrowser",
        },
    }


def _matches_playback_source(source: Any, candidates: set[str], video_id: str) -> bool:
    """Allow only the two player-added fields observed on the public page.

    All original query pairs, including signatures and request-tracking values,
    must still match one complete detail response. Never strip arbitrary query
    strings or combine fields from separate responses.
    """
    if not _is_media_url(source):
        return False
    actual = urlsplit(source)
    try:
        actual_pairs = parse_qsl(actual.query, keep_blank_values=True, errors="strict")
    except (ValueError, UnicodeError):
        return False
    actual_keys = {key for key, _ in actual_pairs}
    for candidate in candidates:
        expected = urlsplit(candidate)
        if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path):
            continue
        try:
            expected_pairs = parse_qsl(expected.query, keep_blank_values=True, errors="strict")
        except (ValueError, UnicodeError):
            continue
        expected_keys = {key for key, _ in expected_pairs}
        added_keys = actual_keys - expected_keys
        if not added_keys <= {"__vid", "temp"}:
            continue
        extras = {key: [value for name, value in actual_pairs if name == key] for key in added_keys}
        if "__vid" in extras and extras["__vid"] != [video_id]:
            continue
        if "temp" in extras and (len(extras["temp"]) != 1 or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", extras["temp"][0])):
            continue
        original_pairs = [(key, value) for key, value in actual_pairs if key not in added_keys]
        if sorted(original_pairs) == sorted(expected_pairs):
            return True
    return False


def _validate_playback(first: Any, second: Any, details: dict[str, Any], page_url: str, video_id: str) -> None:
    if not _is_target_page(page_url, video_id):
        raise _failure("The anonymous page left the requested video.", integrity=True)
    for state in (first, second):
        if not isinstance(state, dict) or not _matches_playback_source(state.get("src"), details["playback_urls"], video_id):
            raise _failure("Playback could not be bound to the requested video.", integrity=True)
        duration = state.get("duration")
        current_time = state.get("currentTime")
        if (
            not isinstance(duration, (float, int)) or isinstance(duration, bool)
            or not math.isfinite(duration) or duration <= 0
            or abs(duration * 1000 - details["duration_ms"]) > 1000
            or not isinstance(current_time, (float, int)) or isinstance(current_time, bool)
            or not math.isfinite(current_time) or current_time < 0
            or state.get("paused") is not False
            or (_positive_int(state.get("readyState")) or 0) < 2
            or not _positive_int(state.get("width")) or not _positive_int(state.get("height"))
        ):
            raise _failure("The anonymous page did not demonstrate complete playable video metadata.", integrity=True)
    if first["src"] != second["src"] or second["currentTime"] <= first["currentTime"]:
        raise _failure("The requested video did not advance during anonymous playback.")


class _MediaRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_media_url(newurl):
            raise _failure("The media response redirected outside the permitted CDN.", integrity=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _response_length(response: Any) -> int:
    values = response.headers.get_all("Content-Length", [])
    if len(values) != 1 or not re.fullmatch(r"[0-9]{1,12}", values[0]):
        raise _failure("The media response has no unambiguous declared length.", integrity=True)
    length = int(values[0])
    if length <= 0 or length > MAX_MEDIA_BYTES:
        raise _failure("The media response exceeds the supported size bounds.", integrity=True)
    if response.headers.get("Transfer-Encoding") or response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise _failure("The media response uses an unsupported transfer encoding.", integrity=True)
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("video/mp4", "video/quicktime", "application/octet-stream"):
        raise _failure("The media response is not a supported video content type.", integrity=True)
    ranges = response.headers.get_all("Content-Range", [])
    if response.status == 206:
        match = re.fullmatch(r"bytes 0-([0-9]+)/([0-9]+)", ranges[0]) if len(ranges) == 1 else None
        if not match or int(match[1]) + 1 != length or int(match[2]) != length:
            raise _failure("The server returned only part of the video.", integrity=True)
    elif response.status != 200 or ranges:
        raise _failure("The media response did not contain a complete video.", integrity=True)
    return length


def _validate_staging_fd(staging_fd: int) -> None:
    try:
        metadata = os.fstat(staging_fd)
    except (OSError, TypeError, ValueError):
        raise DouyinBrowserError("UNSAFE_OUTPUT_DIRECTORY", "The staging descriptor is invalid.", 2) from None
    if (
        not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DouyinBrowserError("UNSAFE_OUTPUT_DIRECTORY", "The staging directory must be private and owned.", 2)


def _stream_media(
    media_url: str, staging_fd: int, deadline: float, referer: str,
    user_agent: str, cancelled: threading.Event | None = None,
) -> None:
    """Write one declared, complete response through the caller's pinned FD."""
    if not _is_media_url(media_url):
        raise _failure("The playback address is outside the permitted CDN.", integrity=True)
    _validate_staging_fd(staging_fd)
    file_fd = -1
    created_identity = None
    success = False
    try:
        try:
            file_fd = os.open(
                MEDIA_FILENAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=staging_fd,
            )
        except FileExistsError:
            raise DouyinBrowserError("PATH_COLLISION", "The browser media destination already exists.", 4) from None
        metadata = os.fstat(file_fd)
        created_identity = (metadata.st_dev, metadata.st_ino)
        os.fchmod(file_fd, 0o600)
        request = urllib.request.Request(media_url, headers={
            "Referer": referer, "User-Agent": user_agent, "Accept-Encoding": "identity",
        })
        opener = urllib.request.build_opener(_MediaRedirectHandler())
        with opener.open(request, timeout=min(5.0, _remaining(deadline))) as response:
            if not _is_media_url(response.geturl()):
                raise _failure("The media response left the permitted CDN.", integrity=True)
            expected = _response_length(response)
            received = 0
            read = getattr(response, "read1", response.read)
            while True:
                _remaining(deadline)
                if cancelled is not None and cancelled.is_set():
                    raise DouyinBrowserError("INTERRUPTED", "The anonymous acquisition was interrupted.", 130)
                block = read(min(64 * 1024, expected - received + 1))
                if not block:
                    break
                received += len(block)
                if received > expected:
                    raise _failure("The media response exceeds its declared length.", integrity=True)
                view = memoryview(block)
                while view:
                    written = os.write(file_fd, view)
                    if written <= 0:
                        raise OSError("write failed")
                    view = view[written:]
            if received != expected:
                raise _failure("The media response ended before its declared length.", integrity=True)
        os.fsync(file_fd)
        os.fsync(staging_fd)
        success = True
    except DouyinBrowserError:
        raise
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        raise DouyinBrowserError("NETWORK_ERROR", "The public video transfer failed.") from None
    finally:
        if not success and created_identity is not None:
            with contextlib.suppress(OSError):
                current = os.stat(MEDIA_FILENAME, dir_fd=staging_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == created_identity:
                    os.unlink(MEDIA_FILENAME, dir_fd=staging_fd)
                    os.fsync(staging_fd)
        if file_fd >= 0:
            os.close(file_fd)


_FIND_VIDEO = """
  const bases = urls.map(u => { const p = new URL(u); return p.origin + p.pathname; });
  const video = [...document.querySelectorAll('video')].find(v => {
    try { const p = new URL(v.currentSrc); return bases.includes(p.origin + p.pathname); }
    catch { return false; }
  });
"""
_PLAYBACK_STATE = "urls => {" + _FIND_VIDEO + """
  return video ? {src: video.currentSrc, duration: video.duration,
    currentTime: video.currentTime, paused: video.paused, readyState: video.readyState,
    width: video.videoWidth, height: video.videoHeight} : null;
}"""


@contextlib.asynccontextmanager
async def _anonymous_browser(async_playwright, deadline: float):
    """Bound every browser await, including context creation and JS promises."""
    manager = async_playwright()
    instance = None
    try:
        async with asyncio.timeout(_remaining(deadline)):
            playwright = await manager.__aenter__()
            instance = await playwright.chromium.launch(
                headless=True, timeout=min(60000, _remaining(deadline) * 1000),
            )
            yield instance
    finally:
        # Cleanup gets a separate small allowance after the operation deadline.
        # Always stop the driver even if browser.close fails or times out.
        if instance is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(instance.close(), CLEANUP_TIMEOUT_SECONDS)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(manager.__aexit__(None, None, None), CLEANUP_TIMEOUT_SECONDS)


async def acquire_public_video(
    url: str, video_id: str, staging_fd: int, quality: str, timeout: int, wait_seconds: float,
) -> dict[str, Any]:
    """Acquire exactly one publicly playable target; never read a user profile."""
    if (
        not isinstance(video_id, str) or not re.fullmatch(r"[0-9]{1,32}", video_id)
        or url != f"https://www.douyin.com/video/{video_id}"
        or quality not in ("best", "1080p", "720p")
        or not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 86400
        or not isinstance(wait_seconds, (float, int)) or isinstance(wait_seconds, bool)
        or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 120
    ):
        raise DouyinBrowserError("INVALID_ARGUMENT", "Invalid anonymous video acquisition settings.", 2)
    _validate_staging_fd(staging_fd)
    try:
        from playwright.async_api import async_playwright
        tool_version = importlib.metadata.version("playwright")
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise DouyinBrowserError("BROWSER_FALLBACK_UNAVAILABLE", "Playwright is unavailable for anonymous acquisition.", 3) from None
    deadline = time.monotonic() + timeout
    details: dict[str, Any] | None = None
    response_error: DouyinBrowserError | None = None
    detail_ready = asyncio.Event()
    tasks: set[asyncio.Task] = set()
    matched_responses = 0
    stage = "launch"

    async def consume_response(response) -> None:
        nonlocal details, response_error
        try:
            if response.status != 200:
                raise _failure("The public detail response did not succeed.")
            headers = await response.all_headers()
            length = headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > MAX_DETAIL_BYTES):
                raise _failure("The public detail response exceeds the supported size.", integrity=True)
            body = await response.body()
            if len(body) > MAX_DETAIL_BYTES:
                raise _failure("The public detail response exceeds the supported size.", integrity=True)
            observed = _video_details(json.loads(body), video_id, quality)
            if details is None:
                details = observed
            else:
                if abs(details["duration_ms"] - observed["duration_ms"]) > 1000:
                    raise _failure("The public detail responses disagree on video duration.", integrity=True)
                details["playback_urls"].update(observed["playback_urls"])
        except DouyinBrowserError as exc:
            response_error = exc
        except Exception:
            response_error = _failure("The public detail response could not be verified.")
        finally:
            detail_ready.set()

    def received_response(response) -> None:
        nonlocal matched_responses
        if matched_responses >= MAX_DETAIL_RESPONSES or not _is_detail_url(response.url, video_id):
            return
        matched_responses += 1
        task = asyncio.create_task(consume_response(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        async with _anonymous_browser(async_playwright, deadline) as browser:
            try:
                context = await browser.new_context()
                page = await context.new_page()
                page.on("response", received_response)
                stage = "navigation"
                await page.goto(url, wait_until="domcontentloaded", timeout=min(60000, _remaining(deadline) * 1000))
                stage = "detail"
                await asyncio.wait_for(detail_ready.wait(), timeout=min(max(1.0, wait_seconds), _remaining(deadline)))
                if response_error is not None:
                    raise response_error
                if details is None:
                    raise _failure("The public page exposed no matching video detail.")
                stage = "playback"
                playback_deadline = min(deadline, time.monotonic() + 15)
                while True:
                    candidates = sorted(details["playback_urls"])
                    state = await page.evaluate(_PLAYBACK_STATE, candidates)
                    if (
                        isinstance(state, dict)
                        and _matches_playback_source(state.get("src"), details["playback_urls"], video_id)
                        and (_positive_int(state.get("readyState")) or 0) >= 2
                    ):
                        break
                    if response_error is not None:
                        raise response_error
                    if time.monotonic() >= playback_deadline:
                        raise TimeoutError()
                    await asyncio.sleep(min(0.1, _remaining(playback_deadline)))
                await page.evaluate(
                    "async urls => {" + _FIND_VIDEO
                    + "if (video) { video.muted = true; await video.play(); }}", candidates,
                )
                first = await page.evaluate(_PLAYBACK_STATE, candidates)
                await asyncio.sleep(min(0.35, _remaining(deadline)))
                second = await page.evaluate(_PLAYBACK_STATE, candidates)
                if response_error is not None:
                    raise response_error
                _validate_playback(first, second, details, page.url, video_id)
                user_agent = await page.evaluate("navigator.userAgent")
                if not isinstance(user_agent, str) or len(user_agent) > 1024 or re.search(r"[\r\n\x00]", user_agent):
                    raise _failure("The anonymous browser identity is invalid.", integrity=True)
            finally:
                pending = tuple(tasks)
                for task in pending:
                    task.cancel()
                if pending:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True), CLEANUP_TIMEOUT_SECONDS,
                        )
        # Keep the writer alive until a cancellation has actually stopped it;
        # otherwise a background thread could race caller recovery/quarantine.
        stage = "transfer"
        cancelled = threading.Event()
        transfer = asyncio.create_task(asyncio.to_thread(
            _stream_media, details["media_url"], staging_fd, deadline, url, user_agent, cancelled,
        ))
        try:
            await asyncio.shield(transfer)
        except asyncio.CancelledError:
            cancelled.set()
            with contextlib.suppress(Exception):
                await asyncio.shield(transfer)
            raise
        return {"info": details["info"], "duration_ms": details["duration_ms"], "tool_version": tool_version}
    except DouyinBrowserError:
        raise
    except Exception as exc:
        # Do not persist the third-party exception text: it may embed signed
        # playback URLs, request headers or page content. Fixed categories keep
        # the failed layer diagnosable without disclosing those values.
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or type(exc).__name__ == "TimeoutError":
            category = "timeout"
        elif isinstance(exc, (json.JSONDecodeError, UnicodeError)):
            category = "parse"
        elif type(exc).__module__.startswith("playwright."):
            category = "browser"
        else:
            category = "runtime"
        message = f"Anonymous acquisition failed during {stage} ({category})."
        raise DouyinBrowserError("NETWORK_ERROR" if category == "timeout" else "BROWSER_FALLBACK_FAILED", message) from None
