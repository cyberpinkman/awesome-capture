# Engine selection

## Local engines

| Engine | Best fit | Main constraint |
|---|---|---|
| whisper.cpp | Auditable local inference, including Apple Silicon | Requires a local GGML model; GPU support must be probed, not assumed |
| faster-whisper | macOS/Linux/Windows | Python package plus CTranslate2 model |
| MLX Whisper | Explicitly configured Apple Silicon use | macOS/Apple Silicon only; not selected by `auto` until a pinned setup is locally verified |
| external adapter | Existing local/remote service | User must explicitly trust and configure the executable |

`auto` has a deliberately narrow policy:

1. Choose whisper.cpp only when `whisper-cli --version` succeeds and `--model` is an existing, non-empty local file.
2. Otherwise choose faster-whisper only when its Python module is installed.
3. Never auto-select MLX or an external adapter.

For whisper.cpp, the script records the resolved binary path, binary version, binary SHA-256, model path, model byte count, and model SHA-256. It never downloads a model. Obtain models separately, pin their upstream revision and expected checksum, and pass the verified file explicitly:

```bash
python3 scripts/transcribe_media.py transcribe media.mp4 \
  --output-dir output \
  --engine whisper-cpp \
  --model "/absolute/path/ggml-small.bin" \
  --whisper-cpp-bin "/absolute/path/whisper-cli"
```

The whisper.cpp adapter invokes `whisper-cli` in a child process and requests full JSON. It first tries the normal GPU-enabled command. A timeout, signal, non-zero exit, missing JSON, or invalid JSON causes one CPU retry with `-ng`. After the first GPU failure, that runner caches GPU as unavailable and sends all remaining chunks in the same task directly to CPU, avoiding repeated crashes. If CPU also fails, the task fails with diagnostics from both attempts. `--whisper-cpp-cpu-only` skips the GPU attempt.

For explicit faster-whisper runs, the identity includes the installed `faster-whisper` and `ctranslate2` package versions. For explicit MLX runs, it includes `mlx-whisper` and `mlx` versions. These identities are part of resumable settings, so an engine upgrade invalidates old chunk state instead of silently mixing results from different runtimes.

Use a small model for smoke tests, not as a universal quality default. In one local M1 test, multilingual `tiny` and `base` each made one error on a short Mandarin sample, while `small` matched the reference. This is evidence for a model ladder, not a general accuracy guarantee. Chinese, mixed-language speech, names, and noisy recordings need representative evaluation.

The bundled script splits normalized mono 16 kHz PCM into chunks. Chunking supports resumability and bounds memory, but a word cut exactly at a chunk boundary may be imperfect. FFmpeg segment durations are not guaranteed to equal the requested duration. The script reads each WAV sample count, builds cumulative offsets from actual duration, hashes every chunk, and stores the resulting timeline in `state.json` and `transcript.json`.

Do not treat import availability as device availability. A local Apple Silicon smoke test built whisper.cpp successfully but its Metal child process failed while the CPU retry succeeded. Capability checks must therefore be subprocess-based and failure-isolated.

## Primary references

- whisper.cpp repository: <https://github.com/ggml-org/whisper.cpp>
- whisper.cpp CLI options and JSON output: <https://github.com/ggml-org/whisper.cpp/blob/master/examples/cli/README.md>
- whisper.cpp model documentation: <https://github.com/ggml-org/whisper.cpp/blob/master/models/README.md>
- faster-whisper repository and examples: <https://github.com/SYSTRAN/faster-whisper>
- faster-whisper transcription API: <https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py>
- Apple MLX Whisper example: <https://github.com/ml-explore/mlx-examples/tree/main/whisper>
- FFmpeg documentation: <https://ffmpeg.org/ffmpeg.html>
