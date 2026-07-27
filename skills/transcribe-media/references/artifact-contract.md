# Artifact contract

`transcript.json` uses `awesome-capture.artifact/v1` and contains:

- `artifact_type: transcript`
- `status: complete`
- `source.path`, `source.sha256`, `source.bytes`, and media duration
- `transcription.engine`, model, requested/detected language, and nominal chunk size
- `transcription.engine_identity`; for whisper.cpp this contains resolved binary/model paths, binary version, byte count, and SHA-256 identities; faster-whisper and MLX identities contain their relevant installed package versions
- optional `transcription.chunk_timeline[]` with each normalized chunk SHA-256, sample count/rate, actual cumulative `offset_ms`, and actual `duration_ms`; current ASR producers emit it, while early compatible v1 artifacts may omit it
- `transcription.devices_used` and `gpu_fallback_count`
- `segments[]` with integer `start_ms`, `end_ms`, text, and `chunk_index`
- concatenated `text`
- absolute `markdown_path`, `text_path`, `srt_path`, `vtt_path`, and `state_path`

A downstream skill must reject:

- non-complete status;
- absent source hash;
- absent whisper.cpp binary/model hashes when `transcription.engine` is `whisper-cpp`;
- a present chunk timeline whose offsets are negative, overlapping, or inconsistent with cumulative durations;
- negative or decreasing timestamps;
- segments outside the media duration, allowing a small decoder tolerance;
- a claimed non-empty transcript with no non-whitespace segment text.

An external engine executable receives the chunk path as its last argument and must write one JSON object to stdout:

```json
{
  "language": "zh",
  "segments": [
    {"start": 0.0, "end": 2.4, "text": "示例"}
  ]
}
```

Times are relative to that chunk in seconds. Diagnostics belong on stderr.

`state.json` binds resumability to the source hash, engine identity, model identity, settings, and normalized chunk hashes. A completed chunk records its actual offset/duration, raw engine-output hash when available, runtime device, and whether GPU failed before CPU succeeded. A caller must not copy completed chunk state into a job with different identities.
