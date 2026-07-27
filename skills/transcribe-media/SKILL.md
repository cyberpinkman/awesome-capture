---
name: transcribe-media
description: Transcribe local audio or video files into timestamped JSON and readable Markdown with resumable processing and source provenance. Use when a user asks to transcribe, extract spoken content, create subtitles or text from local media, or passes a supported video URL for transcription. For URLs, invoke $download-video first and consume its verified artifact manifest. After a non-empty transcript is complete, ask whether the user wants to invoke $ingest-knowledge; never write to a knowledge base without confirmation.
---

# Transcribe media

Turn one verified local media input into a traceable transcript artifact. Do not combine transcription with summarization or knowledge-base writes.

## Workflow

1. Determine whether the input is a URL or a local path.
   - For a URL, invoke `$download-video`, require its completed manifest, then use `manifest.media.path`.
   - For a local path, inspect it directly. Do not send local media to a remote service unless the user explicitly chooses a remote adapter.
2. Check the environment:

   ```bash
   python3 scripts/transcribe_media.py doctor \
     --model "<absolute-local-ggml-model-path>" \
     --whisper-cpp-bin "<absolute-whisper-cli-path>"
   ```

   Omit `--whisper-cpp-bin` only when `whisper-cli` is already on `PATH`.
   For speech recognition without a sidecar subtitle, require
   `ready_for_asr: true`; `status: ok` only means the diagnostic command itself
   completed.

3. Inspect the media and confirm it has an audio stream:

   ```bash
   python3 scripts/transcribe_media.py inspect "<absolute-media-path>"
   ```

4. Prefer an exact-basename `.srt` or `.vtt` sidecar when present. Otherwise select an engine:
   - `auto`: a version-probed `whisper-cli` only when `--model` names an existing local model file; otherwise an installed faster-whisper.
   - `whisper-cpp`: first-class local engine. It requires an explicit local `--model` file and optionally `--whisper-cpp-bin`.
   - `faster-whisper`: portable local default.
   - `mlx-whisper`: explicit Apple Silicon option; `auto` does not select it until its pinned configuration has passed local verification.
   - `external`: an explicitly supplied executable that accepts a chunk path and returns the documented JSON.
5. Run:

   ```bash
   python3 scripts/transcribe_media.py transcribe "<absolute-media-path>" \
     --output-dir "<absolute-dir>" \
     --engine auto \
     --model "<absolute-local-ggml-model-path>"
   ```

   To bypass the initial whisper.cpp GPU subprocess, add `--whisper-cpp-cpu-only`. Normally leave it off: the first GPU failure is recorded, retried with `-ng`, and cached so the remaining chunks in that task go directly to CPU.

6. Require zero exit status and a completed `transcript.json`. Report the engine, detected language, duration, segment count, transcript Markdown path, and any empty/silent result.
7. If the transcript contains speech, ask exactly one follow-up: “是否需要把这份内容写入本地 Obsidian 知识库？” If yes, invoke `$ingest-knowledge` with `transcript.json`. If no, stop.

## Reliability rules

- Preserve source timestamps; never invent missing speech.
- Keep chunks and `state.json` so an interrupted run can resume. Reject state when the media hash, engine binary hash, model hash, chunk hash, or transcription settings differ.
- Offset each chunk by the cumulative duration measured from the normalized WAV sample counts. Never multiply the nominal `--chunk-seconds` value by the chunk index.
- Do not download models. The caller must provide and trust a local model file; its absolute path, byte size, and SHA-256 are recorded.
- Treat a platform title, description, chapter summary, or LLM-generated reconstruction as metadata, not transcription.
- Do not summarize before the transcript artifact is complete.
- Do not silently switch from local to remote transcription.
- Read [engines.md](references/engines.md) before choosing models, handling long files, or adding an engine.
- Read [artifact-contract.md](references/artifact-contract.md) when composing with another skill.
