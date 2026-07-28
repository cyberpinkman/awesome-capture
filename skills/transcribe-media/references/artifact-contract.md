# Artifact and resumability contracts

The public JSON Schemas are vendored under `scripts/_contracts/schemas/`.
Runtime validation uses the adjacent dependency-free contract runtime. The
producer validates before publication; every consumer must independently
validate again.

## Explicit video handoff

For URL input, `$download-video` produces a complete
`awesome-capture.artifact/v2` video artifact. Invoke transcription with both
paths:

```bash
python3 scripts/transcribe_media.py transcribe \
  "<video-artifact.media.path>" \
  --source-artifact "<absolute-video-artifact-v2-path>" \
  --output-dir "<absolute-output-dir>" \
  --engine auto \
  --model "<absolute-local-model-file>"
```

The script never probes `<media>.artifact.json` or scans a directory for an
artifact. Omitting `--source-artifact` means “standalone local media” and
records `source.upstream: null`.

When supplied, the video artifact must have exactly these top-level objects:

- `schema_version: awesome-capture.artifact/v2`
- `artifact_type: video`
- `status: complete`
- `source`, `media`, `acquisition`, and `producer`

The consumer checks the canonical contract digest and revalidates current
media path, bytes, SHA-256, integer duration, audio/video flags, container,
stream counts, and ffprobe evidence. Artifact/v1, unknown properties, changed
media, a mismatched path, or a different contract build is a hard failure.
Only the upstream artifact path/hash plus sanitized platform/fingerprint are
carried into the transcript; URLs and extractor metadata are not copied.

## Transcript artifact v2

`transcript.json` is a complete `awesome-capture.artifact/v2` transcript and
the final job commit marker. Its exact top-level fields are:

- `schema_version`, `artifact_type`, `status`, and `created_at`
- `source`
- `transcription`
- `segments`, `text`, and `no_speech_detected`
- `outputs`
- `warnings`
- `producer`

`source` contains:

- the original absolute `path` and private managed `snapshot_path`;
- `sha256`, `bytes`, integer `duration_ms`, `has_audio`, and `has_video`;
- `upstream`, either null or the explicit video artifact path/hash and
  sanitized platform/fingerprint;
- `sidecar`, either null or the exact-basename subtitle path, bytes, and hash.

The private snapshot is the transcription evidence. Downstream ingest does not
need to reopen the original media, subtitle, or derivative files to validate
the transcript's internal evidence.

`transcription` contains:

- the full 64-hex `job_id`;
- `settings_sha256`, recomputed from the embedded producer/source,
  transcription algorithm implementation, content-level
  engine/model/adapter identity, language, chunk, CPU-only, sidecar, and
  upstream identity fields. Source and component absolute paths are excluded
  from this stable projection; `job_id` is the domain-separated digest of
  this value;
- a versioned `algorithm` identity containing the content digest of the
  standalone transcription script and safety runtime;
- `execution_guard_sha256`, kept outside the stable job identity, binding
  inode/ctime/mtime evidence for the private source/sidecar snapshots and the
  concrete model, binary, and adapter used by this run so an in-place change
  followed by byte restoration is rejected;
- the selected `engine`;
- `engine_identity`;
- requested and detected language;
- nominal `chunk_seconds`;
- `chunk_set`, either null for a sidecar or a manifest path/hash/count plus an
  inline contiguous bytes/hash/timeline copy that consumers can validate
  without reopening the chunk files;
- observed devices and GPU fallback count.

`engine_identity` has the same shape for every engine:

```json
{
  "identity_sha256": "<sha256>",
  "model": null,
  "executable": null,
  "adapter": null,
  "packages": []
}
```

Each non-null content identity records `kind` (`file` or `directory`),
absolute path, byte count, and SHA-256. Directory identities also record
`file_count`; executable identities may record a probed version. Package
entries contain exact installed name/version pairs. `identity_sha256` is the
canonical JSON digest of their content fields and packages, excluding the
absolute `path` metadata. Consumers still use the recorded path to re-open and
hash the concrete local object when that filesystem context is required.

Each segment has integer `start_ms`, `end_ms`, non-empty `text`, and
`chunk_index`, with optional finite `avg_logprob`. Segments must be
timestamp-monotonic, strictly positive in duration, inside the media and chunk
bounds, and refer to an existing chunk index. There is no decoder tolerance or
silent timestamp clamping. `text` is exactly the segment texts joined by
newline, and `no_speech_detected` is exactly `not segments`.

`outputs` contains descriptors for Markdown, plain text, SRT, VTT, final
state, and optionally the chunk manifest. Every descriptor has an absolute
path, bytes, and SHA-256. Consumers authorized to read derivatives must verify
all three rather than trust the path.

`producer.skill` is `transcribe-media`; `producer.contract_digest` identifies
the complete vendored contract bundle. A different digest is
`CONTRACT_BUILD_MISMATCH`.

## Chunk-set v1

ASR jobs publish `awesome-capture.chunk-set/v1` at
`chunks/chunks.manifest.json` only after all normalized chunks exist and have
been fsynced. It binds:

- `job_id`, source SHA-256, and nominal chunk size;
- mono, 16 kHz, 16-bit PCM format;
- exact chunk count and cumulative duration;
- every contiguous `chunk-NNNNN.wav` index/name/path;
- bytes, SHA-256, sample frames/rate, actual offset, and actual duration.

The directory must contain exactly the manifest and its declared chunks.
Missing, extra, reordered, hard-linked, symlinked, malformed, or hash-changed
files are `CHUNK_SET_CONFLICT`. Offsets come from cumulative sample counts, not
the nominal chunk size.

## Transcription state v1

`state.json` uses `awesome-capture.transcription-state/v1`. It contains:

- `status: running|ready_to_publish|complete`, `job_id`, and canonical
  settings digest;
- the full source, upstream, engine/model/adapter, language, chunking, and
  contract settings;
- a null or exact chunk-set reference;
- one complete result object per processed chunk.

Each completed result binds the chunk hash/offset/duration, language, silence
status, optional raw engine-output hash, normalized segments, and a strict
runtime record. Resume is allowed only when the state settings and chunk-set
identities exactly match the current job. A missing result remains resumable;
it is never interpreted as silent speech. Final publication requires the
state's result keys to equal the manifest chunk keys.

Legacy artifact/v1, unversioned state, duplicate JSON keys, NaN/Infinity,
unknown fields, and unknown versions are rejected without migration.

## Durable publication and recovery

Chunk results and derivative outputs are first written while state remains
`running`. A fully validated `transcript.pending.json` is then stored as the
durable publication intent. Only after that intent exists may state advance to
`ready_to_publish`. Promotion revalidates source/model/adapter/chunks, advances
state to `complete`, refreshes the pending artifact's state descriptor, and
publishes `transcript.json` last with no clobber. Thus no crash can leave a
publishable state without its reconstruction intent.

`recover --output-dir ...` acquires each persistent job lock and:

- returns complete when a strict final artifact and all declared files match;
- publishes a strict pending artifact when the final marker is absent;
- reports a running workspace as pending;
- refuses unknown workspace entries, unsafe file types/links, conflicting
  identities, malformed state, or changed hashes.

Re-running `transcribe` performs the same recovery for its job before doing new
work. Recovery never overwrites a final artifact or guesses how to repair
foreign content.
