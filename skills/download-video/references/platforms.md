# Platform and dependency notes

## Decision table

| Platform | Accepted hosts | Anonymous first | Authorized fallback |
|---|---|---:|---|
| YouTube | `youtube.com`, `youtu.be` | Yes | Explicit cookie file/browser session |
| Bilibili | `bilibili.com`, `b23.tv` | Yes | Explicit fresh browser session when HTTP 412 occurs |
| Douyin | `douyin.com`, `iesdouyin.com` | Yes | Isolated anonymous cookies, then verified public webpage media; user session only with explicit authorization |
| TikTok | `tiktok.com` | Yes | `gallery-dl` once for recoverable failures; otherwise session/network action |
| X/Twitter | `x.com`, `twitter.com` | Yes | Explicit session for restricted posts |

The bundled X/Twitter yt-dlp and gallery-dl commands use their official
`--force-ipv4` option. On the tested macOS/Python 3.14 network, the default
address-family route repeatedly ended X media downloads with a TLS
`UNEXPECTED_EOF_WHILE_READING`, while the same public sources and certificate
validation completed over IPv4. The option is scoped to X/Twitter; it neither
disables TLS verification nor changes TikTok or other platforms.

## Douyin links and anonymous fallback

`detect` stays offline and fingerprints the sanitized input. `probe` and
`download` resolve `v.douyin.com` short links and `iesdouyin.com/share/video/`
links to `https://www.douyin.com/video/<id>` before invoking the restricted
Douyin extractor. Redirects are bounded and validated; unrelated destinations,
missing video IDs, and unsafe addresses are rejected. Artifacts retain the input
as `source.url` and record the resolved page separately in `source.webpage_url`.

Douyin's upstream `FRESH_COOKIES_REQUIRED` can mean the detail extractor returned
no usable video data; it is not proof that this public video requires a login.
In default `auto` mode, this error triggers anonymous cookies from isolated
Chromium and one yt-dlp retry. Eligible extraction failures, including an initial
`DOWNLOAD_FAILED`, may use one isolated anonymous browser session to read details
matching the requested video ID and obtain its public media. A generic download
failure does not require a Cookie retry first.

The webpage-media route accepts only complete MP4 media from HTTPS
`douyinvod.com` hosts listed in the target video's details, up to 1 GiB per file.
`--quality 1080p` and `720p` select by video height, as in the yt-dlp route;
`best` has no height ceiling. Before publication, the script requires complete
HTTP media bytes, consistent video identity and duration, and the usual local
hash/ffprobe checks. A player source that cannot be bound to the target video or
an incomplete byte range fails closed. It never substitutes a recommended video,
thumbnail, or unverified file.

Both browser routes use `auth_mode` and `fallback` of `ephemeral_browser`. The
webpage-media route additionally records `producer.tool: playwright` and an
explanatory warning. Signed CDN URLs and raw page responses are not published.
The browser has a fresh context, never reads a personal profile, and removes
temporary cookies. The initial cookie fallback waits 15 seconds by default;
the wait remains configurable because page initialization varies by network.
Explicit login, private, unavailable, geo-restricted, and rate-limited responses
stop the flow; browser playback is not an authorization bypass.

## Required tools

- Python 3.11 through 3.14 on macOS or Linux. Secure writes require POSIX
  `fcntl`, `dir_fd`, no-follow directory opens, persistent advisory locks, and
  directory `fsync`; there is no reduced-security fallback.
- `yt-dlp[default,curl-cffi,deno]`: use 2026.07.04 or newer because extractors and security fixes change with the platforms.
- `ffmpeg` and `ffprobe`: merge formats and verify that the result contains a playable video stream.
- `deno`: use as yt-dlp's JavaScript runtime for YouTube challenges.
- `playwright` plus its Chromium browser: optional, isolated Douyin anonymous
  cookie and public webpage-media fallbacks.
- `gallery-dl` 1.32.8 or newer: optional TikTok/X fallback only.
- `curl_cffi`: use only where the extractor recommends impersonation. Do not force impersonation globally because yt-dlp warns that doing so can reduce stability.

The exact Python package set exercised on 2026-07-27 was:

```text
yt-dlp                 2026.7.4
yt-dlp-ejs             0.8.0
curl-cffi              0.15.0
gallery-dl             1.32.8
playwright             1.60.0
```

Keep this as the reproducible baseline rather than an indefinite upper pin. On
upgrade, rerun one public smoke URL per platform before promoting a new version.
`ffmpeg`/`ffprobe` 8.1 and Deno 2.9.4 were used in the same test. Playwright is
optional; installing its Python package is insufficient until its Chromium
runtime is installed.

Run:

```bash
python3 scripts/download_video.py doctor
python3 scripts/download_video.py detect "<url>"
python3 scripts/download_video.py probe "<url>"
```

The doctor marks builds older than 2026.07.04 as below the tested baseline and
date-based builds older than 90 days as stale. It also reports
`security_runtime.ready`; a false value blocks `download` and `recover`.
A warning is not proof that every extractor is broken, but production
diagnosis must first use a current, smoke-tested version.

`detect` persists or prints only public query keys. `probe` resolves supported
Douyin links and performs network metadata extraction without downloading media;
it is diagnostic evidence, not a successful download artifact.

## Secure download and recovery

Use an absolute output directory:

```bash
python3 scripts/download_video.py download "<url>" \
  --output-dir "<absolute-dir>" \
  --quality best \
  --lock-timeout 30
```

The downloader creates
`<output>/.awesome-capture-media/v2/{locks,staging,downloads,quarantine}`.
All external media output is confined to a new `0700` staging directory.
Before publication, the script requires exactly one safe media file and
rechecks it with ffprobe, byte length, and SHA-256. Final media and JSON files
are `0600`. A persistent source lock serializes the same URL; lock files remain
after release.

Publication follows the canonical `awesome-capture.transaction/v1` journal.
Media and sanitized source metadata are no-clobber published and fsynced before
`artifact.json`; that video artifact v2 is the final commit marker. An
interrupted source transaction is recovered automatically before another
download, or explicitly with:

```bash
python3 scripts/download_video.py recover \
  --output-dir "<absolute-dir>" \
  --lock-timeout 30
```

Recovery only completes steps whose source, destination, bytes, SHA-256,
hardlink provenance, root, and journal agree. Unjournaled staging is moved to
private quarantine. Unknown files, path changes, or mismatched journal copies
produce `RECOVERY_CONFLICT` and are left intact for inspection.

Reuse follows the same rule: only a complete
`awesome-capture.artifact/v2` with the matching source fingerprint, local
contract digest, regular single-link media, current hash, and current ffprobe
facts is reusable. Legacy artifact v1, an unversioned manifest, and a bare
preseeded gallery-dl file are rejected rather than adopted.

## Error policy

- `FRESH_COOKIES_REQUIRED`: for Douyin, allow the bounded isolated anonymous
  routes first. Explain the extractor failure using redacted diagnostics;
  do not equate missing detail data with a confirmed login requirement. A
  personal Cookie source still requires separate, explicit authorization.
- `SESSION_REQUIRED`: the platform rejected anonymous extraction; do not loop blindly.
- `IP_BLOCKED` or `GEO_BLOCKED`: retries with the same session and address are unlikely to help.
- `RATE_LIMITED`: wait or reduce request volume.
- `CONTENT_UNAVAILABLE`: verify the URL in a normal browser; do not substitute another post.
- `NETWORK_ERROR`: distinguish DNS/TLS/timeout from platform rejection.
- `RESOURCE_BUSY`: another process holds the persistent source lock; wait or
  choose a longer `--lock-timeout`, rather than deleting the lock.
- `RECOVERY_CONFLICT`: managed state no longer matches its transaction; do not
  overwrite or manually merge it without inspecting the reported output root.
- `INTEGRITY_FAILED`: the isolated output failed type, stream, size, hash, or
  path validation. The script retains it only inside private staging/quarantine;
  never pass it downstream.
- `UNSUPPORTED_SCHEMA_VERSION`: artifact v1, unversioned, or unknown contracts
  are intentionally unsupported after the v2 breaking change.
- `UNSUPPORTED_PLATFORM`: install/use Python 3.11–3.14 on a supported POSIX
  runtime; do not bypass the capability check.

Successful commands return one JSON object on stdout; command exceptions return
one redacted JSON error on stderr with stdout empty. `doctor` follows the same
contract: unavailable dependencies or platform capabilities produce a
redacted error on stderr and exit 3 rather than a success-shaped status object
on stdout. Signed query strings, Cookie values, authorization headers, tokens,
signatures, passwords, and API keys must not appear in persisted metadata or
reported error details.

## Primary references

- yt-dlp README and options: <https://github.com/yt-dlp/yt-dlp/blob/master/README.md>
- yt-dlp FAQ, including Cookie handling: <https://github.com/yt-dlp/yt-dlp/wiki/FAQ>
- yt-dlp releases: <https://github.com/yt-dlp/yt-dlp/releases>
- yt-dlp 2026.07.04 security/stable release: <https://github.com/yt-dlp/yt-dlp/releases/tag/2026.07.04>
- yt-dlp EJS runtime guide: <https://github.com/yt-dlp/yt-dlp/wiki/EJS>
- yt-dlp YouTube PO Token guide: <https://github.com/yt-dlp/yt-dlp/wiki/Po-Token-Guide>
- gallery-dl supported sites: <https://github.com/mikf/gallery-dl/blob/master/docs/supportedsites.md>
- FFprobe documentation: <https://ffmpeg.org/ffprobe.html>
