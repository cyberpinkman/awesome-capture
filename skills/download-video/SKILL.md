---
name: download-video
description: Identify and download individual video URLs from Douyin, TikTok, Bilibili, YouTube, and X/Twitter into verified local media artifacts. Use when a user asks to save a supported social video, when another skill needs a local video from a URL, or when diagnosing whether one of those links is downloadable. Require explicit authorization before reading browser cookies; never treat metadata, a thumbnail, or a partial file as a successful video download.
---

# Download video

Produce one local, playable video and an adjacent artifact manifest. Keep acquisition separate from transcription and analysis.

## Workflow

1. Extract exactly one `http` or `https` video URL from the request. Ask which URL only when multiple candidates remain.
2. Run the deterministic detector:

   ```bash
   python3 scripts/download_video.py detect "<url>"
   ```

3. Run `doctor` once per environment. If `yt-dlp` is stale or `ffmpeg`/`ffprobe` is missing, report the exact remediation before downloading.
4. Try anonymous download first. The script restricts yt-dlp to the detected platform extractor and disables the generic extractor:

   ```bash
   python3 scripts/download_video.py download "<url>" --output-dir "<absolute-dir>"
   ```

5. Apply only the bounded fallback for that platform:
   - Douyin `FRESH_COOKIES_REQUIRED`: the default `auto` mode may open an isolated headless Chromium context, collect short-lived anonymous `.douyin.com` cookies, retry once, then delete the temporary Cookie file. It never reads the user's browser profile. Report `auth_mode: ephemeral_browser`.
   - TikTok/X recoverable extractor or IP rejection: use `gallery-dl` once when installed. Do not use it for login, private, deleted, geo-restricted, or rate-limited content.
   - Any platform requiring the user's session: use `--cookies <netscape-file>` or `--cookies-from-browser <browser[:profile]>` only after the user explicitly authorizes that named source. Never enumerate browser profiles automatically.
6. Treat success as valid only when the script exits zero and returns a manifest whose `status` is `complete`, `media.has_video` is true, and `media.path` exists.
7. Return the absolute media path and manifest path. Do not transcribe unless the user also requested it.

## Reliability rules

- Keep `--ignore-config` enabled through the bundled script so machine-global yt-dlp settings cannot change behavior.
- Default to one video. This first version intentionally rejects playlist expansion; add a separate batch adapter later.
- Never bypass DRM, paywalls, private access, or platform authorization.
- Never log, copy, or persist Cookie values in the manifest.
- Reject filesystem roots, the user's home directory, and symlinked platform output directories.
- Do not promise that anonymous downloading works on every network. Anti-bot sites require a current authorized session or a different network.
- Keep the Douyin browser fallback isolated and anonymous. If it would require login, stop and obtain explicit user direction.
- Prefer `yt-dlp 2026.07.04` or newer; that release repaired observed Bilibili extraction and includes security fixes absent from the installed 2026.03.17 build.
- Read [platforms.md](references/platforms.md) when handling authentication, anti-bot errors, format selection, or platform-specific limitations.

## Output contract

The script prints JSON to stdout. A successful download writes:

- the downloaded media;
- yt-dlp's source `.info.json`;
- `<media-file>.artifact.json`, conforming to `awesome-capture.artifact/v1`.

Pass the manifest, not an inferred filename, to downstream skills. The manifest records `auth_mode`, engine/version, and fallback warnings without Cookie values.
