from __future__ import annotations

import asyncio
import email.message
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "douyin_browser_tests", ROOT / "skills/download-video/scripts/douyin_browser.py"
)
assert SPEC and SPEC.loader
browser = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(browser)

VIDEO_ID = "1234567890123456789"
PAGE = f"https://www.douyin.com/video/{VIDEO_ID}"
MEDIA = "https://v1.douyinvod.com/public-video.mp4?opaque=fixture-value"


def detail_payload():
    def address(height, label):
        return {"width": 576, "height": height, "url_list": [f"https://v1.douyinvod.com/{label}.mp4"]}

    return {
        "status_code": 0,
        "aweme_detail": {
            "aweme_id": VIDEO_ID,
            "desc": "Public example",
            "author": {"nickname": "Example author"},
            "video": {
                "duration": 28633, "width": 576, "height": 1024,
                "play_addr": {"url_list": [MEDIA]},
                "bit_rate": [
                    {"bit_rate": 3000, "play_addr": address(1920, "high")},
                    {"bit_rate": 2000, "play_addr": address(1080, "medium")},
                    {"bit_rate": 1000, "play_addr": address(720, "low")},
                ],
            },
        },
    }


class Response:
    def __init__(self, body=b"complete media", *, status=200, length=None, headers=None, url=MEDIA):
        self.body = io.BytesIO(body)
        self.status = status
        self.url = url
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "video/mp4"
        self.headers["Content-Length"] = str(len(body) if length is None else length)
        for key, value in (headers or {}).items():
            if value is None:
                del self.headers[key]
            else:
                self.headers.replace_header(key, value) if key in self.headers else self.headers.add_header(key, value)

    def read(self, size):
        return self.body.read(size)

    read1 = read

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class BrowserEvidenceTests(unittest.TestCase):
    def test_only_target_detail_endpoint_is_accepted(self):
        good = f"https://www.douyin.com{browser.DETAIL_PATH}?aweme_id={VIDEO_ID}&other=1"
        self.assertTrue(browser._is_detail_url(good, VIDEO_ID))
        for bad in (
            good.replace("www.douyin.com", "www.douyin.com.other.invalid"),
            good.replace("https:", "http:"),
            good + "&aweme_id=999",
            good.replace(VIDEO_ID, "999"),
            good.replace(browser.DETAIL_PATH, "/aweme/v1/web/aweme/feed/"),
            good.replace("www.douyin.com", "user@www.douyin.com"),
        ):
            with self.subTest(url=bad):
                self.assertFalse(browser._is_detail_url(bad, VIDEO_ID))

    def test_payload_identity_is_required_independently_of_request(self):
        for replacement in (None, {}, {"aweme_id": "999"}, {"aweme_id": int(VIDEO_ID)}):
            payload = detail_payload()
            payload["aweme_detail"] = replacement
            with self.subTest(detail=replacement), self.assertRaises(browser.DouyinBrowserError) as error:
                browser._video_details(payload, VIDEO_ID, "best")
            self.assertEqual(error.exception.exit_code, 7)

    def test_quality_uses_height_ceiling_for_portrait_video(self):
        expected = {"best": "high", "1080p": "medium", "720p": "low"}
        for quality, suffix in expected.items():
            details = browser._video_details(detail_payload(), VIDEO_ID, quality)
            self.assertEqual(details["media_url"], f"https://v1.douyinvod.com/{suffix}.mp4")
        payload = detail_payload()
        payload["aweme_detail"]["video"].pop("bit_rate")
        self.assertEqual(browser._video_details(payload, VIDEO_ID, "1080p")["media_url"], MEDIA)
        with self.assertRaises(browser.DouyinBrowserError):
            browser._video_details(payload, VIDEO_ID, "720p")

    def test_public_info_never_copies_urls_or_sensitive_metadata(self):
        payload = detail_payload()
        payload["aweme_detail"]["desc"] = "A https://example.invalid/private?opaque=value token=hidden"
        payload["aweme_detail"]["author"]["nickname"] = "cookie=hidden"
        info = browser._video_details(payload, VIDEO_ID, "best")["info"]
        self.assertEqual(set(info), {"id", "title", "uploader", "webpage_url", "extractor"})
        self.assertNotIn("hidden", str(info))
        self.assertNotIn("opaque", str(info))
        self.assertEqual(info["webpage_url"], PAGE)

    def test_candidate_host_and_scheme_are_exact(self):
        for bad in (
            "https://douyinvod.com.other.invalid/video",
            "https://other.invalid/douyinvod.com/video",
            "http://v1.douyinvod.com/video",
            "https://user:secret@v1.douyinvod.com/video",
            "https://v1.douyinvod.com:444/video",
            "https://v1.douyinvod.com/video#fragment",
            "https://v1.douyinvod.com\\@other.invalid/video",
        ):
            with self.subTest(url=bad):
                self.assertFalse(browser._is_media_url(bad))
        self.assertTrue(browser._is_media_url(MEDIA))

    def test_playback_must_advance_and_match_detail_source_duration_and_page(self):
        details = browser._video_details(detail_payload(), VIDEO_ID, "best")
        first = {"src": MEDIA, "duration": 28.633, "currentTime": 1.0,
                 "paused": False, "readyState": 4, "width": 576, "height": 1024}
        second = {**first, "currentTime": 1.35}
        browser._validate_playback(first, second, details, PAGE, VIDEO_ID)
        for field, value in (
            ("src", "https://v1.douyinvod.com/unrelated.mp4"),
            ("src", "blob:https://www.douyin.com/other"),
            ("duration", 5), ("duration", float("nan")), ("paused", True),
            ("readyState", 1), ("currentTime", 1.0), ("width", 0),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(browser.DouyinBrowserError):
                browser._validate_playback(first, {**second, field: value}, details, PAGE, VIDEO_ID)
        with self.assertRaises(browser.DouyinBrowserError):
            browser._validate_playback(first, second, details, PAGE.replace(VIDEO_ID, "999"), VIDEO_ID)

    def test_player_may_only_add_target_identity_and_cachebuster_query_fields(self):
        permitted = MEDIA + f"&__vid={VIDEO_ID}&temp=0.12345"
        self.assertTrue(browser._matches_playback_source(MEDIA, {MEDIA}, VIDEO_ID))
        self.assertTrue(browser._matches_playback_source(permitted, {MEDIA}, VIDEO_ID))
        for forbidden in (
            permitted.replace(VIDEO_ID, "999"),
            permitted.replace("fixture-value", "different-signature"),
            permitted + "&unknown=1",
            permitted + "&__vid=" + VIDEO_ID,
            permitted + "&temp=2",
            permitted.replace("public-video.mp4", "other-video.mp4"),
            permitted.replace("v1.douyinvod.com", "v2.douyinvod.com"),
            permitted.replace("&temp=0.12345", "&temp="),
        ):
            with self.subTest(url=forbidden):
                self.assertFalse(browser._matches_playback_source(forbidden, {MEDIA}, VIDEO_ID))
        later = MEDIA.replace("fixture-value", "later-request")
        self.assertTrue(browser._matches_playback_source(later + f"&__vid={VIDEO_ID}", {MEDIA, later}, VIDEO_ID))
        self.assertFalse(browser._matches_playback_source(
            MEDIA.replace("fixture-value", "%fe"), {MEDIA.replace("fixture-value", "%ff")}, VIDEO_ID,
        ))


class BrowserTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        self.path.chmod(0o700)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def tearDown(self):
        os.close(self.fd)
        self.temporary.cleanup()

    def transfer(self, response, *, deadline=None, cancelled=None):
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(browser.urllib.request, "build_opener", return_value=opener):
            browser._stream_media(
                MEDIA, self.fd, time.monotonic() + 10 if deadline is None else deadline,
                PAGE, "test-browser", cancelled,
            )
        return opener

    def test_complete_response_is_private_fsynced_and_without_credentials(self):
        with mock.patch.object(browser.os, "fsync", wraps=os.fsync) as sync:
            opener = self.transfer(Response())
        output = self.path / browser.MEDIA_FILENAME
        self.assertEqual(output.read_bytes(), b"complete media")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(output.stat().st_nlink, 1)
        self.assertEqual(len(list(self.path.iterdir())), 1)
        self.assertGreaterEqual(sync.call_count, 2)
        request = opener.open.call_args.args[0]
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("Authorization", request.headers)

    def test_full_range_is_accepted_but_partial_ranges_are_rejected(self):
        self.transfer(Response(b"data", status=206, headers={"Content-Range": "bytes 0-3/4"}))
        (self.path / browser.MEDIA_FILENAME).unlink()
        for value in ("bytes 0-3/100", "bytes 1-4/5", "bytes 0-3/*", None):
            with self.subTest(value=value), self.assertRaises(browser.DouyinBrowserError):
                self.transfer(Response(b"data", status=206, headers={"Content-Range": value}))
            self.assertFalse((self.path / browser.MEDIA_FILENAME).exists())

    def test_declared_length_type_encoding_and_total_bytes_are_checked(self):
        cases = [
            Response(b"short", length=10), Response(b"too long", length=2),
            Response(headers={"Content-Length": None}),
            Response(length=browser.MAX_MEDIA_BYTES + 1),
            Response(headers={"Content-Type": "text/html"}),
            Response(headers={"Transfer-Encoding": "chunked"}),
            Response(headers={"Content-Encoding": "gzip"}),
            Response(status=403),
        ]
        duplicate = Response()
        duplicate.headers.add_header("Content-Length", "14")
        cases.append(duplicate)
        for response in cases:
            with self.subTest(headers=str(response.headers)), self.assertRaises(browser.DouyinBrowserError) as error:
                self.transfer(response)
            self.assertEqual(error.exception.exit_code, 7)
            self.assertNotIn("fixture-value", str(error.exception))
            self.assertFalse((self.path / browser.MEDIA_FILENAME).exists())

    def test_redirects_cannot_send_request_outside_cdn(self):
        handler = browser._MediaRedirectHandler()
        request = urllib.request.Request(MEDIA)
        with self.assertRaises(browser.DouyinBrowserError):
            handler.redirect_request(request, None, 302, "redirect", {}, "https://other.invalid/private")
        permitted = handler.redirect_request(request, None, 302, "redirect", {}, "https://v2.douyinvod.com/public.mp4")
        self.assertEqual(permitted.full_url, "https://v2.douyinvod.com/public.mp4")
        with self.assertRaises(browser.DouyinBrowserError):
            self.transfer(Response(url="https://other.invalid/private"))
        self.assertFalse((self.path / browser.MEDIA_FILENAME).exists())

    def test_existing_files_and_links_are_never_overwritten(self):
        output = self.path / browser.MEDIA_FILENAME
        original = self.path / "existing"
        original.write_bytes(b"keep")
        for kind in ("file", "symlink", "hardlink"):
            if kind == "file":
                output.write_bytes(b"keep")
            elif kind == "symlink":
                output.symlink_to(original)
            else:
                os.link(original, output)
            with self.subTest(kind=kind), mock.patch.object(browser.urllib.request, "build_opener") as network:
                with self.assertRaises(browser.DouyinBrowserError) as error:
                    self.transfer(Response())
                self.assertEqual(error.exception.code, "PATH_COLLISION")
                network.assert_not_called()
                self.assertEqual(output.read_bytes(), b"keep")
            output.unlink()
        self.assertEqual(original.read_bytes(), b"keep")

    def test_transfer_stays_on_opened_directory_after_path_replacement(self):
        original = self.path / "staging"
        original.mkdir(mode=0o700)
        fd = os.open(original, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        moved = self.path / "renamed"
        original.rename(moved)
        original.mkdir(mode=0o700)
        saved_fd, self.fd = self.fd, fd
        try:
            self.transfer(Response())
        finally:
            self.fd = saved_fd
            os.close(fd)
        self.assertEqual((moved / browser.MEDIA_FILENAME).read_bytes(), b"complete media")
        self.assertFalse((original / browser.MEDIA_FILENAME).exists())

    def test_failure_cleanup_does_not_delete_a_replacement_file(self):
        response = Response()

        def replace_then_fail(size):
            output = self.path / browser.MEDIA_FILENAME
            output.unlink()
            output.write_bytes(b"replacement")
            raise OSError("sensitive remote URL")

        response.read1 = replace_then_fail
        with self.assertRaises(browser.DouyinBrowserError) as error:
            self.transfer(response)
        self.assertEqual((self.path / browser.MEDIA_FILENAME).read_bytes(), b"replacement")
        self.assertNotIn("sensitive", str(error.exception))

    def test_deadline_and_cancellation_prevent_publication(self):
        cancelled = threading.Event()
        cancelled.set()
        for deadline, event in ((time.monotonic() - 1, None), (None, cancelled)):
            with self.subTest(cancelled=event is not None), self.assertRaises(browser.DouyinBrowserError):
                self.transfer(Response(), deadline=deadline, cancelled=event)
            self.assertFalse((self.path / browser.MEDIA_FILENAME).exists())


class BrowserOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def fake_playwright(self, *, playback_failure=None):
        response = mock.Mock()
        response.url = f"https://www.douyin.com{browser.DETAIL_PATH}?aweme_id={VIDEO_ID}"
        response.status = 200
        response.all_headers = mock.AsyncMock(return_value={"content-type": "application/json"})
        response.body = mock.AsyncMock(return_value=json.dumps(detail_payload()).encode())
        page = mock.Mock()
        page.url = PAGE

        async def navigate(*args, **kwargs):
            page.on.call_args.args[1](response)
            await asyncio.sleep(0)

        page.goto = mock.AsyncMock(side_effect=navigate)
        first = {"src": MEDIA, "duration": 28.633, "currentTime": 1.0,
                 "paused": False, "readyState": 4, "width": 576, "height": 1024}
        page.evaluate = mock.AsyncMock(side_effect=playback_failure or [first, None, first, {**first, "currentTime": 1.35}, "test-browser"])
        context = mock.Mock(new_page=mock.AsyncMock(return_value=page))
        chromium = mock.Mock(
            new_context=mock.AsyncMock(return_value=context), close=mock.AsyncMock(),
        )
        driver = mock.Mock(chromium=mock.Mock(launch=mock.AsyncMock(return_value=chromium)))
        manager = mock.MagicMock()
        manager.__aenter__ = mock.AsyncMock(return_value=driver)
        manager.__aexit__ = mock.AsyncMock(return_value=False)
        module = types.ModuleType("playwright.async_api")
        module.async_playwright = mock.Mock(return_value=manager)
        return module, driver, chromium

    async def test_public_interface_preserves_identity_and_returns_only_safe_metadata(self):
        module, driver, chromium = self.fake_playwright()
        opener = mock.Mock()
        opener.open.return_value = Response()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            path.chmod(0o700)
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                with (
                    mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.async_api": module}),
                    mock.patch.object(browser.importlib.metadata, "version", return_value="test-version"),
                    mock.patch.object(browser.urllib.request, "build_opener", return_value=opener),
                ):
                    result = await browser.acquire_public_video(PAGE, VIDEO_ID, fd, "best", 10, 1)
                self.assertEqual(result["duration_ms"], 28633)
                self.assertEqual(result["info"]["id"], VIDEO_ID)
                self.assertEqual(result["tool_version"], "test-version")
                self.assertNotIn("fixture-value", json.dumps(result))
                self.assertEqual((path / browser.MEDIA_FILENAME).read_bytes(), b"complete media")
                self.assertEqual(set(driver.chromium.launch.call_args.kwargs), {"headless", "timeout"})
                chromium.new_context.assert_awaited_once_with()
                chromium.close.assert_awaited_once_with()
            finally:
                os.close(fd)

    async def test_browser_failure_reports_layer_without_exception_secrets(self):
        module, _, chromium = self.fake_playwright(playback_failure=TimeoutError(MEDIA))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            path.chmod(0o700)
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                with (
                    mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.async_api": module}),
                    mock.patch.object(browser.importlib.metadata, "version", return_value="test-version"),
                    self.assertRaises(browser.DouyinBrowserError) as error,
                ):
                    await browser.acquire_public_video(PAGE, VIDEO_ID, fd, "best", 10, 1)
                self.assertEqual(error.exception.message, "Anonymous acquisition failed during playback (timeout).")
                self.assertEqual(error.exception.code, "NETWORK_ERROR")
                self.assertNotIn("fixture-value", str(error.exception))
                self.assertFalse((path / browser.MEDIA_FILENAME).exists())
                chromium.close.assert_awaited_once_with()
            finally:
                os.close(fd)

    async def test_pending_browser_javascript_obeys_total_deadline(self):
        async def pending_javascript(*args, **kwargs):
            await asyncio.Event().wait()

        module, _, chromium = self.fake_playwright(playback_failure=pending_javascript)
        with (
            mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.async_api": module}),
            mock.patch.object(browser.importlib.metadata, "version", return_value="test-version"),
            mock.patch.object(browser, "_validate_staging_fd"),
            mock.patch.object(browser, "_stream_media") as transfer,
        ):
            with self.assertRaises(browser.DouyinBrowserError) as error:
                await asyncio.wait_for(browser.acquire_public_video(PAGE, VIDEO_ID, 123, "best", 1, 1), 1.4)
            self.assertEqual(error.exception.message, "Anonymous acquisition failed during playback (timeout).")
            transfer.assert_not_called()
            chromium.close.assert_awaited_once_with()

    async def test_browser_cleanup_is_bounded_and_still_stops_driver(self):
        async def pending_close(*args, **kwargs):
            await asyncio.Event().wait()

        module, _, chromium = self.fake_playwright(playback_failure=TimeoutError(MEDIA))
        chromium.close.side_effect = pending_close
        manager = module.async_playwright.return_value
        with (
            mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.async_api": module}),
            mock.patch.object(browser.importlib.metadata, "version", return_value="test-version"),
            mock.patch.object(browser, "_validate_staging_fd"),
            mock.patch.object(browser, "CLEANUP_TIMEOUT_SECONDS", 0.02),
        ):
            with self.assertRaises(browser.DouyinBrowserError):
                await asyncio.wait_for(browser.acquire_public_video(PAGE, VIDEO_ID, 123, "best", 1, 1), 0.3)
            manager.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
