from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/download-video/scripts/download_video.py"


def load_download_module():
    spec = importlib.util.spec_from_file_location("download_video_routes", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


download_video = load_download_module()


class DownloadRouteTests(unittest.TestCase):
    SHORT_URL = "https://v.douyin.com/fixture/"
    VIDEO_ID = "7123456789012345678"
    CANONICAL_URL = f"https://www.douyin.com/video/{VIDEO_ID}"

    def test_douyin_share_path_normalizes_to_the_owned_extractor_shape(self):
        result = download_video.resolve_download_url(
            "https://www.iesdouyin.com/share/video/7123456789012345678/?region=CN",
            "douyin",
            10,
        )
        self.assertEqual(result, "https://www.douyin.com/video/7123456789012345678")

    def test_already_canonical_and_other_platform_routes_need_no_resolution(self):
        cases = (
            ("https://www.douyin.com/video/7123456789012345678", "douyin"),
            ("https://www.youtube.com/watch?v=fixture", "youtube"),
            ("https://x.com/fixture/status/123", "twitter"),
        )
        with mock.patch.object(download_video, "_douyin_redirect_location") as request:
            for url, platform in cases:
                with self.subTest(platform=platform):
                    self.assertEqual(download_video.resolve_download_url(url, platform, 10), url)
        request.assert_not_called()

    def test_short_links_resolve_each_allowed_hop_before_extraction(self):
        with mock.patch.object(
            download_video,
            "_douyin_redirect_location",
            side_effect=["/second/", f"https://www.iesdouyin.com/share/video/{self.VIDEO_ID}/"],
        ) as request:
            result = download_video.resolve_download_url(self.SHORT_URL, "douyin", 10)
        self.assertEqual(result, self.CANONICAL_URL)
        self.assertEqual(
            [call.args[0] for call in request.call_args_list],
            [self.SHORT_URL, "https://v.douyin.com/second/"],
        )

    def test_each_redirect_is_validated_before_another_network_request(self):
        unsafe_targets = (
            "http://www.douyin.com/video/123",
            "https://www.douyin.com:8443/video/123",
            "https://user:password@www.douyin.com/video/123",
            "https://www.douyin.com.evil.example/video/123",
            "https://unrecognized.douyin.com/video/123",
            "https://127.0.0.1/video/123",
            "file:///tmp/video",
        )
        for target in unsafe_targets:
            with self.subTest(target=target), mock.patch.object(
                download_video, "_douyin_redirect_location", return_value=target
            ) as request:
                with self.assertRaises(download_video.DownloadError):
                    download_video.resolve_download_url(self.SHORT_URL, "douyin", 10)
                request.assert_called_once()

    def test_unresolved_loop_and_excessive_redirects_are_bounded(self):
        redirects = (
            [None],
            [self.SHORT_URL],
            [f"https://v.douyin.com/hop{index}/" for index in range(8)],
        )
        for locations in redirects:
            with self.subTest(locations=locations), mock.patch.object(
                download_video, "_douyin_redirect_location", side_effect=locations
            ) as request:
                with self.assertRaises(download_video.DownloadError):
                    download_video.resolve_download_url(self.SHORT_URL, "douyin", 10)
                self.assertLessEqual(request.call_count, 5)

    def test_detect_keeps_original_public_identity_and_remains_offline(self):
        raw_url = self.SHORT_URL + "?signature=unpublishable"
        with (
            mock.patch.object(download_video, "resolve_download_url") as resolve,
            mock.patch.object(download_video, "run_ytdlp") as acquire,
            mock.patch.object(download_video, "json_print") as output,
        ):
            result = download_video.main(["detect", raw_url])
        self.assertEqual(result, 0)
        resolve.assert_not_called()
        acquire.assert_not_called()
        value = output.call_args.args[0]
        self.assertEqual(value["sanitized_url"], self.SHORT_URL)
        self.assertEqual(
            value["source_fingerprint"], hashlib.sha256(self.SHORT_URL.encode()).hexdigest()
        )

    def test_probe_uses_the_same_resolved_route_as_media_acquisition(self):
        args = download_video.build_parser().parse_args(["probe", self.SHORT_URL])
        process = subprocess.CompletedProcess([], 0, json.dumps({"id": self.VIDEO_ID}), "")
        with (
            mock.patch.object(
                download_video, "resolve_download_url", return_value=self.CANONICAL_URL
            ) as resolve,
            mock.patch.object(
                download_video, "run_ytdlp", return_value=(process, "anonymous", [])
            ) as acquire,
        ):
            result = download_video.probe(args)
        resolve.assert_called_once()
        self.assertEqual(acquire.call_args.kwargs["url"], self.CANONICAL_URL)
        self.assertEqual(result["source"]["webpage_url"], self.CANONICAL_URL)

    def test_download_resolves_transport_but_keeps_original_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = download_video.build_parser().parse_args([
                "download", self.SHORT_URL, "--output-dir", str(Path(temporary) / "output")
            ])
            process = subprocess.CompletedProcess([], 0, '"fixture.mp4"\n', "")
            with (
                mock.patch.object(download_video, "require_tool", return_value="/fixture/yt-dlp"),
                mock.patch.object(download_video, "version_of", return_value="2026.07.04"),
                mock.patch.object(
                    download_video, "resolve_download_url", return_value=self.CANONICAL_URL
                ) as resolve,
                mock.patch.object(
                    download_video, "run_ytdlp", return_value=(process, "anonymous", [])
                ) as acquire,
                mock.patch.object(
                    download_video, "_validated_staging_media",
                    return_value=(Path(temporary) / "fixture.mp4", {"id": self.VIDEO_ID}, []),
                ),
                mock.patch.object(download_video, "_publish_staging", return_value={}) as publish,
            ):
                download_video.download(args)
        resolve.assert_called_once()
        self.assertEqual(acquire.call_args.kwargs["url"], self.CANONICAL_URL)
        source = publish.call_args.kwargs["source"]
        self.assertEqual(source["url"], self.SHORT_URL)
        self.assertEqual(source["webpage_url"], self.CANONICAL_URL)
        self.assertEqual(source["fingerprint"], hashlib.sha256(self.SHORT_URL.encode()).hexdigest())

    def test_missing_extractor_is_a_url_shape_failure(self):
        error = download_video.classify_ytdlp_error(
            "ERROR: No suitable extractor found for URL https://v.douyin.com/fixture/"
        )
        self.assertEqual(error.code, "UNSUPPORTED_URL")
        self.assertEqual(error.exit_code, 2)

    def test_cookie_hint_does_not_assert_that_a_user_session_is_necessary(self):
        error = download_video.classify_ytdlp_error(
            "Fresh cookies (not necessarily logged in) are needed"
        )
        self.assertEqual(error.code, "FRESH_COOKIES_REQUIRED")
        self.assertNotIn("requires fresh cookies", error.message.lower())
        self.assertNotIn("requires an authorized session", error.message.lower())
        self.assertTrue(error.details)

    def test_diagnostics_keep_safe_facts_without_raw_tool_output(self):
        raw = (
            "ERROR: HTTP Error 429: Too Many Requests (caused by HTTPError) "
            "https://cdn.example.invalid/media.mp4?signature=unpublishable-value\n"
            "Cookie: session=unpublishable-cookie\n"
            "unexpected extractor debug marker: unpublishable-marker"
        )
        error = download_video.classify_ytdlp_error(raw)
        self.assertEqual(error.code, "RATE_LIMITED")
        self.assertIn("429", str(error.details))
        rendered = json.dumps(error.as_dict(), sort_keys=True)
        for forbidden in ("unpublishable", "cdn.example.invalid", "media.mp4"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_explicit_access_limits_take_precedence_over_an_extractor_cookie_hint(self):
        cases = (
            ("Login required", "SESSION_REQUIRED"),
            ("HTTP Error 412: Precondition Failed", "SESSION_REQUIRED"),
            ("Private video", "CONTENT_UNAVAILABLE"),
            ("Video unavailable", "CONTENT_UNAVAILABLE"),
            ("geo-restricted", "GEO_BLOCKED"),
            ("HTTP Error 429: Too Many Requests", "RATE_LIMITED"),
        )
        for rejection, expected in cases:
            with self.subTest(rejection=rejection):
                error = download_video.classify_ytdlp_error(
                    "Fresh cookies (not necessarily logged in) are needed\n" + rejection
                )
                self.assertEqual(error.code, expected)

    def test_browser_media_fallback_requires_anonymous_recoverable_douyin_failure(self):
        args = argparse.Namespace(
            cookies=None, cookies_from_browser=None, douyin_browser_fallback="auto"
        )
        recoverable = {"FRESH_COOKIES_REQUIRED", "DOWNLOAD_FAILED"}
        codes = recoverable | {
            "SESSION_REQUIRED", "CONTENT_UNAVAILABLE", "GEO_BLOCKED", "RATE_LIMITED",
            "NETWORK_ERROR", "IP_BLOCKED", "UNSUPPORTED_URL", "INTEGRITY_FAILED",
        }
        for platform in ("douyin", "tiktok", "youtube"):
            for code in sorted(codes):
                with self.subTest(platform=platform, code=code):
                    error = download_video.DownloadError(code, "fixture")
                    allowed = download_video.can_douyin_browser_fallback(args, platform, error)
                    self.assertEqual(allowed, platform == "douyin" and code in recoverable)
        for changed in (
            {"cookies": "/explicit/cookies.txt"},
            {"cookies_from_browser": "chrome"},
            {"douyin_browser_fallback": "off"},
        ):
            with self.subTest(changed=changed):
                restricted = argparse.Namespace(**(vars(args) | changed))
                self.assertFalse(download_video.can_douyin_browser_fallback(
                    restricted, "douyin", download_video.DownloadError("DOWNLOAD_FAILED", "fixture")
                ))

    def test_browser_route_only_publishes_media_with_matching_playback_evidence(self):
        for duration_matches in (True, False):
            with self.subTest(duration_matches=duration_matches), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                fixture = root / "fixture.mp4"
                subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "color=c=black:s=64x64:d=0.25", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(fixture),
                    ],
                    check=True,
                )
                fixture.chmod(0o600)
                duration_ms = download_video.ffprobe(fixture)["duration_ms"]
                output = root / "output"
                args = download_video.build_parser().parse_args([
                    "download", self.SHORT_URL, "--output-dir", str(output)
                ])

                async def acquire_public_video(**kwargs):
                    self.assertEqual(kwargs["url"], self.CANONICAL_URL)
                    self.assertEqual(kwargs["video_id"], self.VIDEO_ID)
                    descriptor = os.open(
                        "browser.mp4", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600, dir_fd=kwargs["staging_fd"],
                    )
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(fixture.read_bytes())
                    return {
                        "info": {"id": self.VIDEO_ID, "title": "Public fixture"},
                        "duration_ms": duration_ms if duration_matches else duration_ms + 5000,
                        "tool_version": "1.60.0",
                    }

                with (
                    mock.patch.object(
                        download_video, "_douyin_redirect_location",
                        return_value=self.CANONICAL_URL,
                    ),
                    mock.patch.object(
                        download_video, "run_ytdlp",
                        side_effect=download_video.DownloadError("DOWNLOAD_FAILED", "fixture failure"),
                    ) as ytdlp,
                    mock.patch("douyin_browser.acquire_public_video", side_effect=acquire_public_video) as browser,
                ):
                    if duration_matches:
                        result = download_video.download(args)
                    else:
                        with self.assertRaises(download_video.DownloadError) as raised:
                            download_video.download(args)
                        self.assertEqual(raised.exception.code, "INTEGRITY_FAILED")
                        self.assertEqual(raised.exception.exit_code, 7)
                ytdlp.assert_called_once()
                browser.assert_called_once()
                managed = output / ".awesome-capture-media" / "v2"
                if not duration_matches:
                    self.assertEqual(list(managed.glob("downloads/**/artifact.json")), [])
                    self.assertEqual(list((managed / "staging").iterdir()), [])
                    self.assertTrue(list((managed / "quarantine").glob("*/browser.mp4")))
                    continue
                artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
                download_video.validate_video_artifact(artifact, revalidate_media=True)
                self.assertEqual(artifact["source"]["url"], self.SHORT_URL)
                self.assertEqual(artifact["source"]["webpage_url"], self.CANONICAL_URL)
                self.assertEqual(
                    artifact["source"]["fingerprint"], hashlib.sha256(self.SHORT_URL.encode()).hexdigest()
                )
                self.assertEqual(artifact["producer"]["tool"], "playwright")
                self.assertEqual(artifact["producer"]["version"], "1.60.0")
                self.assertEqual(artifact["acquisition"]["auth_mode"], "ephemeral_browser")
                self.assertEqual(artifact["acquisition"]["fallback"], "ephemeral_browser")
                self.assertEqual(artifact["media"]["duration_ms"], duration_ms)


if __name__ == "__main__":
    unittest.main()
