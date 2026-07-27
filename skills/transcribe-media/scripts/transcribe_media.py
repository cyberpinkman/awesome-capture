#!/usr/bin/env python3
"""Create a resumable, timestamped transcript artifact from local media."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


SCHEMA_VERSION = "awesome-capture.artifact/v1"


class TranscriptionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: str = "", exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details.strip()[-4000:]
        return {"status": "error", "error": error}


def json_print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def require_tool(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise TranscriptionError("DEPENDENCY_MISSING", f"Required executable is missing: {name}", exit_code=3)
    return value


def reject_url(value: str) -> None:
    parts = urlsplit(value)
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        raise TranscriptionError(
            "USE_DOWNLOAD_VIDEO",
            "URL input must be downloaded with $download-video before transcription.",
            exit_code=2,
        )


def media_path(raw: str) -> Path:
    reject_url(raw)
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise TranscriptionError("INPUT_NOT_FOUND", f"Media file does not exist: {path}", exit_code=2)
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_process(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscriptionError(
            "PROCESS_TIMEOUT",
            f"Process timed out after {timeout} seconds.",
            details=str(exc),
            exit_code=5,
        ) from exc
    except OSError as exc:
        raise TranscriptionError(
            "PROCESS_START_FAILED",
            f"Could not start executable: {command[0]}",
            details=str(exc),
            exit_code=5,
        ) from exc


def inspect_media(path: Path) -> dict[str, Any]:
    process = run_process(
        [
            require_tool("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name,size:stream=index,codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ],
        timeout=30,
    )
    if process.returncode != 0:
        raise TranscriptionError(
            "INVALID_MEDIA",
            "ffprobe could not read the media file.",
            details=process.stderr,
            exit_code=2,
        )
    try:
        data = json.loads(process.stdout)
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise TranscriptionError("INVALID_MEDIA", "ffprobe returned invalid metadata.", exit_code=2) from exc
    streams = data.get("streams") or []
    if duration <= 0:
        raise TranscriptionError(
            "INVALID_MEDIA",
            "Media duration must be positive.",
            exit_code=2,
        )
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "duration_seconds": duration,
        "container": str((data.get("format") or {}).get("format_name") or ""),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        "has_video": any(stream.get("codec_type") == "video" for stream in streams),
        "streams": streams,
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def exact_sidecar(path: Path) -> Path | None:
    for suffix in (".srt", ".vtt"):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def parse_clock(value: str) -> int:
    clean = value.strip().replace(",", ".")
    parts = clean.split(":")
    if len(parts) == 2:
        hours = 0
        minutes = int(parts[0])
        seconds = float(parts[1])
    elif len(parts) == 3:
        hours, minutes = int(parts[0]), int(parts[1])
        seconds = float(parts[2])
    else:
        raise ValueError(value)
    return round((hours * 3600 + minutes * 60 + seconds) * 1000)


def strip_subtitle_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\{\\[^}]+\}", "", value)
    return " ".join(value.split()).strip()


def parse_sidecar(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    segments: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if "-->" not in line:
            index += 1
            continue
        start_raw, end_raw = [item.strip().split(" ", 1)[0] for item in line.split("-->", 1)]
        text_lines: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index].strip())
            index += 1
        try:
            start_ms, end_ms = parse_clock(start_raw), parse_clock(end_raw)
        except ValueError:
            continue
        text = strip_subtitle_markup(" ".join(text_lines))
        if text and end_ms >= start_ms:
            segments.append(
                {"start_ms": start_ms, "end_ms": end_ms, "text": text, "chunk_index": 0}
            )
    return deduplicate_segments(segments)


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def executable_path(explicit: str | None, default_name: str) -> Path | None:
    if explicit:
        expanded = Path(explicit).expanduser()
        if expanded.parent != Path(".") or expanded.is_absolute():
            candidate = expanded.resolve()
            return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
        discovered = shutil.which(explicit)
    else:
        discovered = shutil.which(default_name)
    if not discovered:
        return None
    candidate = Path(discovered).expanduser().resolve()
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def local_model_path(value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value).expanduser().resolve()
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        return None
    return candidate


def probe_whisper_cpp(binary: str | None, *, timeout: int = 10) -> dict[str, Any]:
    executable = executable_path(binary, "whisper-cli")
    if executable is None:
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "whisper.cpp executable was not found. Supply --whisper-cpp-bin or install whisper-cli.",
            exit_code=3,
        )
    process = run_process([str(executable), "--version"], timeout=min(timeout, 10))
    if process.returncode != 0:
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "whisper.cpp executable failed its version probe.",
            details=process.stderr or process.stdout,
            exit_code=3,
        )
    version_output = "\n".join(
        line.strip()
        for line in f"{process.stdout}\n{process.stderr}".splitlines()
        if line.strip()
    )
    if not version_output:
        raise TranscriptionError(
            "ENGINE_UNAVAILABLE",
            "whisper.cpp executable returned no version information.",
            exit_code=3,
        )
    return {
        "binary_path": str(executable),
        "binary_version": version_output[:512],
    }


def whisper_cpp_identity(
    model_name: str | None, binary: str | None, *, timeout: int
) -> dict[str, Any]:
    model = local_model_path(model_name)
    if model is None:
        raise TranscriptionError(
            "MODEL_UNAVAILABLE",
            "whisper.cpp requires --model to be an existing, non-empty local model file.",
            exit_code=3,
        )
    identity = probe_whisper_cpp(binary, timeout=timeout)
    executable = Path(identity["binary_path"])
    return {
        **identity,
        "binary_sha256": file_sha256(executable),
        "model_path": str(model),
        "model_bytes": model.stat().st_size,
        "model_sha256": file_sha256(model),
    }


def select_engine(
    requested: str,
    model_name: str | None = None,
    whisper_cpp_bin: str | None = None,
    *,
    timeout: int = 10,
) -> str:
    if requested != "auto":
        return requested
    if local_model_path(model_name) is not None:
        try:
            probe_whisper_cpp(whisper_cpp_bin, timeout=timeout)
            return "whisper-cpp"
        except TranscriptionError:
            pass
    if module_available("faster_whisper"):
        return "faster-whisper"
    raise TranscriptionError(
        "ENGINE_UNAVAILABLE",
        "No supported auto engine is ready. Supply a local whisper.cpp model with "
        "--model and an available whisper-cli, or explicitly configure faster-whisper/MLX.",
        exit_code=3,
    )


def normalize_chunks(path: Path, chunks_dir: Path, chunk_seconds: int, timeout: int) -> list[Path]:
    chunks_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(chunks_dir.glob("chunk-*.wav"))
    if existing:
        return existing
    temporary_dir = chunks_dir.parent / f".{chunks_dir.name}.building"
    if temporary_dir.exists():
        raise TranscriptionError(
            "STATE_CONFLICT",
            f"An incomplete chunk build exists: {temporary_dir}. Inspect it before retrying.",
            exit_code=4,
        )
    temporary_dir.mkdir(parents=True)
    pattern = temporary_dir / "chunk-%05d.wav"
    process = run_process(
        [
            require_tool("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            str(pattern),
        ],
        timeout=timeout,
    )
    if process.returncode != 0:
        raise TranscriptionError(
            "AUDIO_EXTRACTION_FAILED",
            "ffmpeg could not normalize the audio stream.",
            details=process.stderr,
            exit_code=4,
        )
    built = sorted(temporary_dir.glob("chunk-*.wav"))
    if not built:
        raise TranscriptionError("AUDIO_EXTRACTION_FAILED", "ffmpeg produced no audio chunks.", exit_code=4)
    for item in built:
        os.replace(item, chunks_dir / item.name)
    temporary_dir.rmdir()
    return sorted(chunks_dir.glob("chunk-*.wav"))


def wav_sample_duration(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            sample_rate = handle.getframerate()
    except (OSError, EOFError, wave.Error) as exc:
        raise TranscriptionError(
            "INVALID_CHUNK",
            f"Normalized audio chunk is not a readable WAV file: {path}",
            details=str(exc),
            exit_code=4,
        ) from exc
    if frames < 0 or sample_rate <= 0:
        raise TranscriptionError(
            "INVALID_CHUNK",
            f"Normalized audio chunk has invalid sample timing: {path}",
            exit_code=4,
        )
    return frames, sample_rate


def chunk_timeline(chunks: list[Path]) -> list[dict[str, Any]]:
    cumulative = Fraction(0, 1)
    timeline: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        frames, sample_rate = wav_sample_duration(chunk)
        start = cumulative
        cumulative += Fraction(frames, sample_rate)
        start_ms = round(start * 1000)
        end_ms = round(cumulative * 1000)
        timeline.append(
            {
                "index": index,
                "name": chunk.name,
                "path": str(chunk),
                "sha256": file_sha256(chunk),
                "sample_frames": frames,
                "sample_rate": sample_rate,
                "offset_ms": start_ms,
                "duration_ms": end_ms - start_ms,
            }
        )
    return timeline


def chunk_has_signal(path: Path) -> bool:
    process = run_process(
        [
            require_tool("ffmpeg"),
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        timeout=120,
    )
    if process.returncode != 0:
        raise TranscriptionError(
            "AUDIO_INSPECTION_FAILED",
            f"ffmpeg could not inspect normalized audio: {path}",
            details=process.stderr,
            exit_code=4,
        )
    output = f"{process.stdout}\n{process.stderr}".lower()
    return "max_volume: -inf db" not in output


def normalize_engine_segments(
    raw_segments: list[dict[str, Any]], *, chunk_index: int, offset_ms: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in raw_segments:
        text = " ".join(str(item.get("text") or "").split()).strip()
        if not text:
            continue
        try:
            start_ms = offset_ms + round(float(item.get("start") or 0) * 1000)
            end_ms = offset_ms + round(float(item.get("end") or 0) * 1000)
        except (TypeError, ValueError):
            continue
        if end_ms < start_ms:
            continue
        segment: dict[str, Any] = {
            "start_ms": max(0, start_ms),
            "end_ms": max(0, end_ms),
            "text": text,
            "chunk_index": chunk_index,
        }
        if item.get("avg_logprob") is not None:
            segment["avg_logprob"] = item["avg_logprob"]
        result.append(segment)
    return result


def whisper_cpp_milliseconds(item: dict[str, Any], edge: str) -> int:
    offsets = item.get("offsets")
    if isinstance(offsets, dict) and offsets.get(edge) is not None:
        try:
            return int(offsets[edge])
        except (TypeError, ValueError):
            pass
    timestamps = item.get("timestamps")
    if isinstance(timestamps, dict) and timestamps.get(edge) is not None:
        try:
            return parse_clock(str(timestamps[edge]))
        except ValueError:
            pass
    raise ValueError(f"missing {edge} timestamp")


def parse_whisper_cpp_json(raw: bytes, requested_language: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranscriptionError(
            "INVALID_ENGINE_OUTPUT",
            "whisper.cpp returned invalid JSON.",
            details=str(exc),
            exit_code=5,
        ) from exc
    transcription = value.get("transcription") if isinstance(value, dict) else None
    if not isinstance(transcription, list):
        raise TranscriptionError(
            "INVALID_ENGINE_OUTPUT",
            "whisper.cpp JSON has no transcription array.",
            exit_code=5,
        )
    segments: list[dict[str, Any]] = []
    for item in transcription:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split()).strip()
        if not text:
            continue
        try:
            start_ms = whisper_cpp_milliseconds(item, "from")
            end_ms = whisper_cpp_milliseconds(item, "to")
        except ValueError:
            continue
        if start_ms < 0 or end_ms < start_ms:
            continue
        segment: dict[str, Any] = {
            "start": start_ms / 1000,
            "end": end_ms / 1000,
            "text": text,
        }
        segments.append(segment)
    result = value.get("result") if isinstance(value, dict) else None
    params = value.get("params") if isinstance(value, dict) else None
    language = result.get("language") if isinstance(result, dict) else None
    if not language and isinstance(params, dict):
        candidate = params.get("language")
        language = None if candidate == "auto" else candidate
    return {
        "segments": segments,
        "language": language or requested_language,
        "raw_output_sha256": hashlib.sha256(raw).hexdigest(),
    }


def whisper_cpp_runner(
    identity: dict[str, Any],
    language: str | None,
    timeout: int,
    *,
    cpu_only: bool,
    gpu_previously_failed: bool,
) -> Callable[[Path], dict[str, Any]]:
    executable = str(identity["binary_path"])
    model = str(identity["model_path"])
    gpu_available = not cpu_only and not gpu_previously_failed
    gpu_disabled_by_failure = gpu_previously_failed

    def run_attempt(path: Path, output_prefix: Path, *, use_gpu: bool) -> tuple[dict[str, Any] | None, str]:
        command = [
            executable,
            "-m",
            model,
            "-f",
            str(path),
            "-l",
            language or "auto",
            "-ojf",
            "-of",
            str(output_prefix),
            "-np",
        ]
        if not use_gpu:
            command.append("-ng")
        try:
            process = run_process(command, timeout=timeout)
        except TranscriptionError as exc:
            return None, f"{exc.code}: {exc.message} {exc.details}".strip()
        if process.returncode != 0:
            diagnostic = (process.stderr or process.stdout or "no diagnostic output").strip()
            return None, f"exit {process.returncode}: {diagnostic[-4000:]}"
        output_path = Path(f"{output_prefix}.json")
        if not output_path.is_file():
            return None, f"exit 0 but JSON output is missing: {output_path}"
        try:
            parsed = parse_whisper_cpp_json(output_path.read_bytes(), language)
        except (OSError, TranscriptionError) as exc:
            if isinstance(exc, TranscriptionError):
                return None, f"{exc.code}: {exc.message} {exc.details}".strip()
            return None, f"could not read JSON output: {exc}"
        return parsed, ""

    def run(path: Path) -> dict[str, Any]:
        nonlocal gpu_available, gpu_disabled_by_failure
        with tempfile.TemporaryDirectory(prefix="transcribe-whisper-cpp-") as temporary:
            temporary_path = Path(temporary)
            gpu_failure = ""
            gpu_attempted = gpu_available
            if gpu_attempted:
                result, gpu_failure = run_attempt(
                    path, temporary_path / "gpu-result", use_gpu=True
                )
                if result is not None:
                    result["runtime"] = {
                        "device": "gpu",
                        "gpu_attempted": True,
                        "gpu_fallback": False,
                    }
                    return result
                gpu_available = False
                gpu_disabled_by_failure = True
            result, cpu_failure = run_attempt(
                path, temporary_path / "cpu-result", use_gpu=False
            )
            if result is None:
                details = f"CPU attempt failed: {cpu_failure}"
                if gpu_failure:
                    details = f"GPU attempt failed: {gpu_failure}\n{details}"
                raise TranscriptionError(
                    "TRANSCRIPTION_FAILED",
                    "whisper.cpp failed to transcribe the audio chunk.",
                    details=details,
                    exit_code=5,
                )
            result["runtime"] = {
                "device": "cpu",
                "gpu_attempted": gpu_attempted,
                "gpu_fallback": gpu_attempted and bool(gpu_failure),
                "gpu_failure": gpu_failure or None,
                "gpu_disabled_after_failure": gpu_disabled_by_failure,
            }
            return result

    return run


def faster_whisper_runner(model_name: str | None, language: str | None) -> Callable[[Path], dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:
        raise TranscriptionError("ENGINE_UNAVAILABLE", f"faster-whisper import failed: {exc}", exit_code=3) from exc
    selected_model = model_name or "small"
    try:
        model = WhisperModel(selected_model, device="cpu", compute_type="int8")
    except Exception as exc:
        raise TranscriptionError("MODEL_UNAVAILABLE", f"Could not load faster-whisper model {selected_model}: {exc}", exit_code=3) from exc

    def run(path: Path) -> dict[str, Any]:
        try:
            generated, info = model.transcribe(
                str(path),
                language=language,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            segments = [
                {
                    "start": float(segment.start),
                    "end": float(segment.end),
                    "text": str(segment.text),
                    "avg_logprob": getattr(segment, "avg_logprob", None),
                }
                for segment in generated
            ]
            return {"segments": segments, "language": getattr(info, "language", language)}
        except Exception as exc:
            raise TranscriptionError("TRANSCRIPTION_FAILED", f"faster-whisper failed: {exc}", exit_code=5) from exc

    return run


def mlx_whisper_runner(model_name: str | None, language: str | None) -> Callable[[Path], dict[str, Any]]:
    try:
        import mlx_whisper  # type: ignore
    except Exception as exc:
        raise TranscriptionError("ENGINE_UNAVAILABLE", f"mlx-whisper import failed: {exc}", exit_code=3) from exc

    def run(path: Path) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if model_name:
            kwargs["path_or_hf_repo"] = model_name
        if language:
            kwargs["language"] = language
        try:
            value = mlx_whisper.transcribe(str(path), **kwargs)
        except Exception as exc:
            raise TranscriptionError("TRANSCRIPTION_FAILED", f"mlx-whisper failed: {exc}", exit_code=5) from exc
        return {
            "segments": value.get("segments") or [],
            "language": value.get("language") or language,
        }

    return run


def external_runner(adapter: str | None, language: str | None, timeout: int) -> Callable[[Path], dict[str, Any]]:
    if not adapter:
        raise TranscriptionError("INVALID_ADAPTER", "--adapter is required for the external engine.", exit_code=2)
    executable = Path(adapter).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise TranscriptionError("INVALID_ADAPTER", f"Adapter is not executable: {executable}", exit_code=2)

    def run(path: Path) -> dict[str, Any]:
        process = run_process([str(executable), str(path)], timeout=timeout)
        if process.returncode != 0:
            raise TranscriptionError(
                "TRANSCRIPTION_FAILED",
                "External adapter failed.",
                details=process.stderr,
                exit_code=5,
            )
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise TranscriptionError("INVALID_ADAPTER_OUTPUT", "External adapter returned invalid JSON.", exit_code=5) from exc
        if not isinstance(value, dict) or not isinstance(value.get("segments"), list):
            raise TranscriptionError("INVALID_ADAPTER_OUTPUT", "External adapter output has no segments array.", exit_code=5)
        return {"segments": value["segments"], "language": value.get("language") or language}

    return run


def engine_identity_for(
    engine: str,
    model_name: str | None,
    adapter: str | None,
    whisper_cpp_bin: str | None,
    *,
    timeout: int,
) -> dict[str, Any]:
    if engine == "whisper-cpp":
        return whisper_cpp_identity(model_name, whisper_cpp_bin, timeout=timeout)
    if engine == "external":
        if not adapter:
            return {"adapter_path": None}
        executable = Path(adapter).expanduser().resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise TranscriptionError(
                "INVALID_ADAPTER",
                f"Adapter is not executable: {executable}",
                exit_code=2,
            )
        return {
            "adapter_path": str(executable),
            "adapter_sha256": file_sha256(executable),
        }
    if engine == "faster-whisper":
        return {
            "model": model_name or "small",
            "package_versions": {
                "faster-whisper": package_version("faster-whisper"),
                "ctranslate2": package_version("ctranslate2"),
            },
        }
    if engine == "mlx-whisper":
        return {
            "model": model_name or "default",
            "package_versions": {
                "mlx-whisper": package_version("mlx-whisper"),
                "mlx": package_version("mlx"),
            },
        }
    return {"model": model_name or "default"}


def runner_for(
    engine: str,
    model_name: str | None,
    language: str | None,
    adapter: str | None,
    timeout: int,
    *,
    engine_identity: dict[str, Any],
    whisper_cpp_cpu_only: bool,
    whisper_cpp_gpu_previously_failed: bool,
) -> Callable[[Path], dict[str, Any]]:
    if engine == "whisper-cpp":
        return whisper_cpp_runner(
            engine_identity,
            language,
            timeout,
            cpu_only=whisper_cpp_cpu_only,
            gpu_previously_failed=whisper_cpp_gpu_previously_failed,
        )
    if engine == "faster-whisper":
        return faster_whisper_runner(model_name, language)
    if engine == "mlx-whisper":
        return mlx_whisper_runner(model_name, language)
    return external_runner(adapter, language, timeout)


def deduplicate_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(segments, key=lambda item: (item["start_ms"], item["end_ms"], item["text"]))
    result: list[dict[str, Any]] = []
    for segment in ordered:
        if result:
            previous = result[-1]
            same_text = segment["text"].casefold() == previous["text"].casefold()
            near = abs(segment["start_ms"] - previous["start_ms"]) <= 1500
            if same_text and near:
                if segment["end_ms"] > previous["end_ms"]:
                    previous["end_ms"] = segment["end_ms"]
                continue
        result.append(segment)
    return result


def format_time(milliseconds: int) -> str:
    seconds, ms = divmod(max(0, milliseconds), 1000)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}.{ms:03d}"


def transcript_markdown(artifact: dict[str, Any]) -> str:
    source = artifact["source"]
    transcription = artifact["transcription"]
    lines = [
        "# Transcript",
        "",
        f"- Source: `{source['path']}`",
        f"- Source SHA-256: `{source['sha256']}`",
        f"- Engine: `{transcription['engine']}`",
        f"- Model: `{transcription['model']}`",
        f"- Language: `{transcription.get('detected_language') or transcription.get('requested_language') or 'auto'}`",
        "",
        "## Timestamped text",
        "",
    ]
    if not artifact["segments"]:
        lines.append("_No speech was detected._")
    else:
        for segment in artifact["segments"]:
            lines.append(
                f"[{format_time(segment['start_ms'])} --> {format_time(segment['end_ms'])}] "
                f"{segment['text']}"
            )
    lines.append("")
    return "\n".join(lines)


def transcript_text(segments: list[dict[str, Any]]) -> str:
    return "\n".join(segment["text"] for segment in segments) + ("\n" if segments else "")


def srt_clock(milliseconds: int) -> str:
    return format_time(milliseconds).replace(".", ",")


def transcript_srt(segments: list[dict[str, Any]]) -> str:
    blocks = [
        f"{index}\n{srt_clock(segment['start_ms'])} --> {srt_clock(segment['end_ms'])}\n{segment['text']}"
        for index, segment in enumerate(segments, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def transcript_vtt(segments: list[dict[str, Any]]) -> str:
    blocks = [
        f"{format_time(segment['start_ms'])} --> {format_time(segment['end_ms'])}\n{segment['text']}"
        for segment in segments
    ]
    body = "\n\n".join(blocks)
    return f"WEBVTT\n\n{body}{chr(10) if body else ''}"


def upstream_source(path: Path, source_hash: str) -> tuple[dict[str, Any] | None, list[str]]:
    manifest_path = Path(f"{path}.artifact.json")
    if not manifest_path.is_file():
        return None, []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, [f"Ignored unreadable upstream manifest: {manifest_path}"]
    manifest_hash = str((manifest.get("media") or {}).get("sha256") or "")
    manifest_path_value = str((manifest.get("media") or {}).get("path") or "")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("artifact_type") != "video"
        or manifest.get("status") != "complete"
        or manifest_hash != source_hash
        or Path(manifest_path_value).expanduser().resolve() != path
    ):
        return None, [f"Ignored mismatched upstream manifest: {manifest_path}"]
    source = manifest.get("source")
    if not isinstance(source, dict):
        return None, [f"Ignored upstream manifest with no source object: {manifest_path}"]
    return {**source, "manifest_path": str(manifest_path)}, []


def transcribe(args: argparse.Namespace) -> dict[str, Any]:
    path = media_path(args.media)
    media = inspect_media(path)
    if not media["has_audio"]:
        raise TranscriptionError("NO_AUDIO_STREAM", "The media has no audio stream.", exit_code=2)
    source_hash = file_sha256(path)
    workspace = Path(args.output_dir).expanduser().resolve() / f"{path.stem}-{source_hash[:12]}"
    workspace.mkdir(parents=True, exist_ok=True)
    state_path = workspace / "state.json"
    transcript_path = workspace / "transcript.json"
    markdown_path = workspace / "transcript.md"
    text_path = workspace / "transcript.txt"
    srt_path = workspace / "transcript.srt"
    vtt_path = workspace / "transcript.vtt"
    upstream, warnings = upstream_source(path, source_hash)
    sidecar = None if args.ignore_sidecar else exact_sidecar(path)
    whisper_cpp_bin = getattr(args, "whisper_cpp_bin", None)
    whisper_cpp_cpu_only = bool(getattr(args, "whisper_cpp_cpu_only", False))
    engine = (
        "sidecar-subtitle"
        if sidecar
        else select_engine(
            args.engine,
            args.model,
            whisper_cpp_bin,
            timeout=min(args.timeout, 10),
        )
    )
    if sidecar:
        model_name = "sidecar"
        engine_identity = {
            "sidecar_path": str(sidecar),
            "sidecar_sha256": file_sha256(sidecar),
        }
    else:
        model_name = args.model or ("small" if engine == "faster-whisper" else "default")
        engine_identity = engine_identity_for(
            engine,
            args.model,
            args.adapter,
            whisper_cpp_bin,
            timeout=args.timeout,
        )
        if engine == "whisper-cpp":
            model_name = str(engine_identity["model_path"])
    settings = {
        "source_sha256": source_hash,
        "engine": engine,
        "model": model_name,
        "engine_identity": engine_identity,
        "requested_language": args.language,
        "chunk_seconds": args.chunk_seconds,
        "adapter": str(Path(args.adapter).expanduser().resolve()) if args.adapter else None,
        "whisper_cpp_cpu_only": whisper_cpp_cpu_only if engine == "whisper-cpp" else None,
    }
    state: dict[str, Any] = {"settings": settings, "chunks": {}}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TranscriptionError("STATE_CONFLICT", f"State file is unreadable: {state_path}", exit_code=4) from exc
        if state.get("settings") != settings:
            raise TranscriptionError(
                "STATE_CONFLICT",
                "Existing state uses different media or transcription settings. Use a different output directory.",
                details=json.dumps({"existing": state.get("settings"), "requested": settings}, ensure_ascii=False),
                exit_code=4,
            )
    detected_languages: list[str] = []
    if sidecar:
        segments = parse_sidecar(sidecar)
        state["chunks"] = {"sidecar": {"status": "complete", "segments": segments}}
        atomic_json(state_path, state)
    else:
        chunks = normalize_chunks(path, workspace / "chunks", args.chunk_seconds, args.timeout)
        timeline = chunk_timeline(chunks)
        persisted_timeline = state.get("chunk_timeline")
        if persisted_timeline is not None and persisted_timeline != timeline:
            raise TranscriptionError(
                "STATE_CONFLICT",
                "Normalized chunk files no longer match the recorded chunk timeline.",
                details=json.dumps(
                    {"existing": persisted_timeline, "current": timeline},
                    ensure_ascii=False,
                ),
                exit_code=4,
            )
        state["chunk_timeline"] = timeline
        atomic_json(state_path, state)
        whisper_cpp_gpu_previously_failed = engine == "whisper-cpp" and any(
            isinstance(chunk_state, dict)
            and isinstance(chunk_state.get("runtime"), dict)
            and (
                chunk_state["runtime"].get("gpu_fallback")
                or chunk_state["runtime"].get("gpu_disabled_after_failure")
            )
            for chunk_state in state["chunks"].values()
        )
        run_engine = runner_for(
            engine,
            args.model,
            args.language,
            args.adapter,
            args.timeout,
            engine_identity=engine_identity,
            whisper_cpp_cpu_only=whisper_cpp_cpu_only,
            whisper_cpp_gpu_previously_failed=whisper_cpp_gpu_previously_failed,
        )
        for chunk_info in timeline:
            chunk_index = int(chunk_info["index"])
            chunk = Path(chunk_info["path"])
            key = str(chunk_info["name"])
            existing = state["chunks"].get(key)
            if isinstance(existing, dict) and existing.get("status") == "complete":
                recorded = {
                    field: existing.get(field)
                    for field in ("chunk_sha256", "offset_ms", "duration_ms")
                }
                expected = {
                    "chunk_sha256": chunk_info["sha256"],
                    "offset_ms": chunk_info["offset_ms"],
                    "duration_ms": chunk_info["duration_ms"],
                }
                if recorded != expected:
                    raise TranscriptionError(
                        "STATE_CONFLICT",
                        f"Completed chunk state no longer matches {key}.",
                        details=json.dumps(
                            {"existing": recorded, "current": expected},
                            ensure_ascii=False,
                        ),
                        exit_code=4,
                    )
                if existing.get("language"):
                    detected_languages.append(str(existing["language"]))
                continue
            if not chunk_has_signal(chunk):
                result = {"segments": [], "language": args.language, "silent": True}
            else:
                result = run_engine(chunk)
            normalized = normalize_engine_segments(
                result.get("segments") or [],
                chunk_index=chunk_index,
                offset_ms=int(chunk_info["offset_ms"]),
            )
            state["chunks"][key] = {
                "status": "complete",
                "language": result.get("language"),
                "silent": bool(result.get("silent")),
                "chunk_sha256": chunk_info["sha256"],
                "offset_ms": chunk_info["offset_ms"],
                "duration_ms": chunk_info["duration_ms"],
                "raw_output_sha256": result.get("raw_output_sha256"),
                "runtime": result.get("runtime"),
                "segments": normalized,
            }
            if result.get("language"):
                detected_languages.append(str(result["language"]))
            atomic_json(state_path, state)
        segments = [
            segment
            for chunk_info in timeline
            for segment in (
                state["chunks"].get(str(chunk_info["name"]), {}).get("segments") or []
            )
        ]
    segments = deduplicate_segments(segments)
    maximum_end_ms = round(float(media["duration_seconds"]) * 1000) + 2000
    if any(segment["end_ms"] > maximum_end_ms for segment in segments):
        raise TranscriptionError(
            "INVALID_ENGINE_OUTPUT",
            "A transcript segment exceeds the media duration by more than 2 seconds.",
            exit_code=5,
        )
    runtimes = [
        chunk.get("runtime")
        for chunk in state["chunks"].values()
        if isinstance(chunk, dict) and isinstance(chunk.get("runtime"), dict)
    ]
    devices_used = sorted(
        {
            str(runtime["device"])
            for runtime in runtimes
            if isinstance(runtime, dict) and runtime.get("device")
        }
    )
    gpu_fallback_count = sum(
        1
        for runtime in runtimes
        if isinstance(runtime, dict) and runtime.get("gpu_fallback")
    )
    if gpu_fallback_count:
        warnings.append(
            f"whisper.cpp GPU failed for {gpu_fallback_count} chunk(s); CPU retry succeeded."
        )
    detected_language = max(set(detected_languages), key=detected_languages.count) if detected_languages else args.language
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "transcript",
        "status": "complete",
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "source": {
            "path": str(path),
            "sha256": source_hash,
            "bytes": media["bytes"],
            "duration_seconds": media["duration_seconds"],
            "has_audio": media["has_audio"],
            "has_video": media["has_video"],
            "sidecar_path": str(sidecar) if sidecar else None,
            "upstream": upstream,
        },
        "transcription": {
            "engine": engine,
            "model": model_name,
            "engine_identity": engine_identity,
            "requested_language": args.language,
            "detected_language": detected_language,
            "chunk_seconds": args.chunk_seconds,
            "chunk_timeline": state.get("chunk_timeline") or [],
            "devices_used": devices_used,
            "gpu_fallback_count": gpu_fallback_count,
        },
        "segments": segments,
        "text": "\n".join(segment["text"] for segment in segments),
        "no_speech_detected": not segments,
        "markdown_path": str(markdown_path),
        "text_path": str(text_path),
        "srt_path": str(srt_path),
        "vtt_path": str(vtt_path),
        "state_path": str(state_path),
        "warnings": warnings,
        "producer": {"skill": "transcribe-media"},
    }
    atomic_text(markdown_path, transcript_markdown(artifact))
    atomic_text(text_path, transcript_text(segments))
    atomic_text(srt_path, transcript_srt(segments))
    atomic_text(vtt_path, transcript_vtt(segments))
    atomic_json(transcript_path, artifact)
    return {
        "status": "ok",
        "operation": "transcribe",
        "transcript_path": str(transcript_path),
        "markdown_path": str(markdown_path),
        "text_path": str(text_path),
        "srt_path": str(srt_path),
        "vtt_path": str(vtt_path),
        "state_path": str(state_path),
        "segment_count": len(segments),
        "no_speech_detected": not segments,
        "engine": engine,
        "detected_language": detected_language,
        "warnings": warnings,
    }


def doctor(args: argparse.Namespace) -> dict[str, Any]:
    tools = {name: {"available": bool(shutil.which(name)), "path": shutil.which(name) or ""} for name in ("ffmpeg", "ffprobe")}
    whisper_cpp_details: dict[str, Any]
    try:
        whisper_cpp_details = {
            "available": True,
            **probe_whisper_cpp(getattr(args, "whisper_cpp_bin", None)),
        }
    except TranscriptionError as exc:
        whisper_cpp_details = {
            "available": False,
            "error": exc.as_dict()["error"],
        }
    model = local_model_path(getattr(args, "model", None))
    whisper_cpp_details["model_ready"] = model is not None
    whisper_cpp_details["model_path"] = str(model) if model else None
    engines = {
        "whisper-cpp": bool(whisper_cpp_details["available"]),
        "faster-whisper": module_available("faster_whisper"),
        "mlx-whisper": module_available("mlx_whisper"),
        "external": True,
    }
    compatible_mlx = platform.system() == "Darwin" and platform.machine() == "arm64"
    auto_engine = None
    try:
        auto_engine = select_engine(
            "auto",
            getattr(args, "model", None),
            getattr(args, "whisper_cpp_bin", None),
        )
    except TranscriptionError:
        pass
    ready_for_inspection = all(item["available"] for item in tools.values())
    return {
        "status": "ok" if ready_for_inspection else "error",
        "operation": "doctor",
        "ready_for_inspection": ready_for_inspection,
        "ready_for_asr": ready_for_inspection and auto_engine is not None,
        "tools": tools,
        "engines": engines,
        "whisper_cpp": whisper_cpp_details,
        "platform": {"system": platform.system(), "machine": platform.machine(), "mlx_compatible": compatible_mlx},
        "auto_engine": auto_engine,
        "auto_policy": "verified whisper.cpp with explicit local model, then faster-whisper; MLX is explicit-only",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--model")
    doctor_parser.add_argument("--whisper-cpp-bin")
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("media")
    transcribe_parser = subparsers.add_parser("transcribe")
    transcribe_parser.add_argument("media")
    transcribe_parser.add_argument("--output-dir", required=True)
    transcribe_parser.add_argument(
        "--engine",
        choices=("auto", "whisper-cpp", "faster-whisper", "mlx-whisper", "external"),
        default="auto",
    )
    transcribe_parser.add_argument("--model")
    transcribe_parser.add_argument("--language")
    transcribe_parser.add_argument("--adapter")
    transcribe_parser.add_argument("--whisper-cpp-bin")
    transcribe_parser.add_argument("--whisper-cpp-cpu-only", action="store_true")
    transcribe_parser.add_argument("--chunk-seconds", type=int, default=600)
    transcribe_parser.add_argument("--timeout", type=int, default=3600)
    transcribe_parser.add_argument("--ignore-sidecar", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor(args)
        elif args.command == "inspect":
            path = media_path(args.media)
            result = {"status": "ok", "operation": "inspect", "media": inspect_media(path)}
        else:
            if args.chunk_seconds < 30:
                raise TranscriptionError("INVALID_ARGUMENT", "--chunk-seconds must be at least 30.", exit_code=2)
            result = transcribe(args)
        json_print(result)
        return 0
    except TranscriptionError as exc:
        json_print(exc.as_dict(), stream=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
