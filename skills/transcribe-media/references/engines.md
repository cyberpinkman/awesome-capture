# Engine selection and identity

All ASR execution is explicit and local. The script never downloads a model,
accepts a Hub/repository identifier, or silently changes engines.

## Selection policy

| Engine | Required local inputs | Selection |
|---|---|---|
| sidecar subtitle | Exact-basename `.srt` or `.vtt` | Preferred automatically unless `--ignore-sidecar` |
| whisper.cpp | Model file; version-probed `whisper-cli` | Explicit or the only `auto` choice |
| faster-whisper | Fully materialized model directory | Explicit only |
| MLX Whisper | Fully materialized model directory and compatible MLX runtime | Explicit only |
| external adapter | Executable adapter, model file/directory, trust flag | Explicit only |

`auto` succeeds only when `--model` is an existing non-empty local file,
`--whisper-cpp-bin` explicitly names the local executable, and that executable's
`--version` probe succeeds. It does not inspect `PATH` or fall
back to faster-whisper, MLX, or external.

Every non-sidecar engine requires `--model`. Arbitrary symlink components,
symlinked model entries, hard-linked model-tree files, non-regular files, and
empty model content are rejected.

## Content-level identity

The engine content identity is part of both the job ID and resumable settings:

- A file records absolute path, bytes, and SHA-256.
- A model directory records its deterministic tree SHA-256, total bytes, and
  file count. The digest covers sorted relative paths, sizes, and each file's
  content hash; mtimes are not identities.
- A local executable/adapter records its own file content identity.
- Python engines record exact installed package versions.
- whisper.cpp additionally records the probed binary version.

Absolute paths remain recorded evidence for reopening and revalidation, but
they are deliberately excluded from `identity_sha256` and the stable job
digest. Byte-identical source/model/adapter content copied to another safe
local path therefore keeps the same content-level identity; any byte, size,
tree, package, or probed-version change produces a different identity.

The source, snapshot, model, executable/adapter, explicit upstream artifact,
and identity digest are checked again before `transcript.json` is published.
Changing any content during a run produces `IDENTITY_CHANGED`; partial output
is not promoted to a complete artifact.

## whisper.cpp

Example:

```bash
python3 scripts/transcribe_media.py transcribe \
  "/absolute/path/media.mp4" \
  --output-dir "/absolute/path/output" \
  --engine whisper-cpp \
  --model "/absolute/path/ggml-small.bin" \
  --whisper-cpp-bin "/absolute/path/whisper-cli"
```

Omit `--whisper-cpp-bin` only with explicit `--engine whisper-cpp` when a
suitable local `whisper-cli` is on `PATH`; `--engine auto` always requires the
binary option.

For each signal-bearing chunk, the adapter requests full JSON. It first starts
the normal GPU-capable command. Timeout, signal, nonzero exit, absent JSON, or
invalid JSON causes one CPU retry with `-ng`. After the first GPU failure, the
same job sends remaining chunks directly to CPU. Both failures abort the job.
`--whisper-cpp-cpu-only` skips the GPU attempt.

GPU/CPU device, attempted fallback, diagnostic summary, and raw JSON hash are
bound into chunk state. Import or build availability alone is not evidence
that the GPU path works.

## faster-whisper

Use an explicit local CTranslate2 model directory:

```bash
python3 scripts/transcribe_media.py transcribe \
  "/absolute/path/media.mp4" \
  --output-dir "/absolute/path/output" \
  --engine faster-whisper \
  --model "/absolute/path/ctranslate2-model-directory"
```

The runner enables `local_files_only=True` and sets the Hugging Face and
Transformers offline environment while loading the model. Its identity
includes the model-tree digest plus installed `faster-whisper` and
`ctranslate2` versions. A model nickname such as `small` or a repository ID is
not accepted.

## MLX Whisper

MLX is explicit-only and normally relevant on Apple Silicon:

```bash
python3 scripts/transcribe_media.py transcribe \
  "/absolute/path/media.mp4" \
  --output-dir "/absolute/path/output" \
  --engine mlx-whisper \
  --model "/absolute/path/mlx-model-directory"
```

The local directory is passed directly to `path_or_hf_repo` while offline
environment flags are active. Its identity includes the model-tree digest and
installed `mlx-whisper` and `mlx` versions. `auto` never selects MLX.

## External adapter protocol

External adapters are trusted local executable code, not remote ASR as a
product feature. They require all of:

```bash
python3 scripts/transcribe_media.py transcribe \
  "/absolute/path/media.mp4" \
  --output-dir "/absolute/path/output" \
  --engine external \
  --model "/absolute/path/local-model-or-directory" \
  --adapter "/absolute/path/executable-adapter" \
  --trust-external-adapter
```

For each chunk the script invokes, without a shell:

```text
<adapter>
  --protocol awesome-capture.external-asr/v1
  --model <absolute-local-model-path>
  --input <absolute-private-chunk-path>
  [--language <requested-language>]
```

The adapter must emit exactly one strict JSON object to stdout:

```json
{
  "protocol": "awesome-capture.external-asr/v1",
  "language": "zh",
  "segments": [
    {"start": 0.0, "end": 2.4, "text": "示例"}
  ]
}
```

`language` may be null. Segment times are finite seconds relative to the
chunk. Unknown output fields, duplicate JSON keys, NaN/Infinity, non-string
text, non-monotonic or out-of-bounds timestamps, nonzero exit, or extra stdout
make the chunk fail.

The child receives a minimized, offline-hinted environment. The trust flag
acknowledges that an executable can still perform arbitrary actions available
to the current user; this is not an OS-level network or process sandbox.

## Chunking and quality

Without a sidecar, ffmpeg normalizes the private source snapshot into
single-channel 16 kHz PCM16 WAV chunks. The script derives each offset from
actual WAV sample counts and publishes the whole directory only after a
complete `awesome-capture.chunk-set/v1` manifest has been validated.

Chunking bounds memory and enables resume, but a word cut exactly at a boundary
can still affect recognition. Choose `--chunk-seconds` for the engine and
media, then keep it stable for resumes. Changing the value creates a different
job rather than mixing results.

Use small models for smoke tests, not as universal quality defaults. Chinese,
mixed-language speech, names, accents, and noisy recordings require
representative local evaluation.

## Primary references

- whisper.cpp repository: <https://github.com/ggml-org/whisper.cpp>
- whisper.cpp CLI: <https://github.com/ggml-org/whisper.cpp/blob/master/examples/cli/README.md>
- whisper.cpp models: <https://github.com/ggml-org/whisper.cpp/blob/master/models/README.md>
- faster-whisper: <https://github.com/SYSTRAN/faster-whisper>
- MLX Whisper: <https://github.com/ml-explore/mlx-examples/tree/main/whisper>
- FFmpeg: <https://ffmpeg.org/ffmpeg.html>
