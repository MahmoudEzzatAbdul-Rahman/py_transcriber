"""ffmpeg/ffprobe helpers for extracting and normalizing call recording audio."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".m4a", ".mp4", ".webm", ".mov", ".mkv", ".ogg", ".flac"}
)

VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mov", ".mkv"})

TARGET_SAMPLE_RATE = 16_000
TARGET_CHANNELS = 1


class AudioError(Exception):
    """Base error for audio validation and extraction."""


class FFmpegNotFoundError(AudioError):
    """ffmpeg or ffprobe is not available on PATH."""


class UnsupportedFormatError(AudioError):
    """File extension is not in the supported set."""


class SourceNotFoundError(AudioError):
    """Source file does not exist or is not a regular file."""


class NoAudioStreamError(AudioError):
    """Media file has no audio track."""


class ProbeError(AudioError):
    """ffprobe failed to read media metadata."""


class ExtractError(AudioError):
    """ffmpeg failed to extract or convert audio."""


@dataclass(frozen=True)
class MediaProbe:
    """Metadata gathered from ffprobe."""

    duration_ms: Optional[int]
    has_audio: bool
    is_video: bool


@dataclass(frozen=True)
class PreparedAudio:
    """Normalized audio file ready for transcription upload."""

    path: Path
    duration_ms: Optional[int]
    source_path: Path
    is_temporary: bool


def require_ffmpeg() -> None:
    """Raise FFmpegNotFoundError if ffmpeg or ffprobe is missing from PATH."""
    missing = [
        tool
        for tool in ("ffmpeg", "ffprobe")
        if shutil.which(tool) is None
    ]
    if missing:
        joined = " and ".join(missing)
        raise FFmpegNotFoundError(
            f"{joined} not found on PATH. Install ffmpeg and ensure it is available."
        )


def validate_source_path(source: Path | str) -> Path:
    """Resolve and validate an input recording path."""
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise SourceNotFoundError(f"File not found: {path}")
    if not path.is_file():
        raise SourceNotFoundError(f"Not a regular file: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise UnsupportedFormatError(
            f"Unsupported file type '{suffix or '(none)'}' for {path.name}. "
            f"Supported: {supported}"
        )
    return path


def _run_command(args: list[str], *, error_cls: type[AudioError], message: str) -> str:
    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise FFmpegNotFoundError(
            "ffmpeg or ffprobe not found on PATH. Install ffmpeg and ensure it is available."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if detail:
            raise error_cls(f"{message}: {detail}") from exc
        raise error_cls(message) from exc
    return result.stdout.strip()


def probe_media(path: Path) -> MediaProbe:
    """Inspect a media file for duration and audio/video streams."""
    require_ffmpeg()

    payload = _run_command(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        error_cls=ProbeError,
        message=f"ffprobe failed for {path.name}",
    )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path.name}") from exc

    streams = data.get("streams") or []
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    is_video = any(stream.get("codec_type") == "video" for stream in streams)

    duration_ms: Optional[int] = None
    duration_raw = (data.get("format") or {}).get("duration")
    if duration_raw is not None:
        try:
            duration_ms = max(0, int(float(duration_raw) * 1000))
        except (TypeError, ValueError):
            duration_ms = None

    return MediaProbe(
        duration_ms=duration_ms,
        has_audio=has_audio,
        is_video=is_video,
    )


def get_duration_ms(path: Path) -> Optional[int]:
    """Return media duration in milliseconds, or None if unavailable."""
    return probe_media(path).duration_ms


def has_audio_stream(path: Path) -> bool:
    """Return True when the file contains at least one audio stream."""
    return probe_media(path).has_audio


def is_video_file(path: Path) -> bool:
    """Return True when the file contains a video stream."""
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return True
    return probe_media(path).is_video


def extract_audio(
    source: Path,
    *,
    output_path: Optional[Path] = None,
) -> PreparedAudio:
    """
    Extract or convert audio to mono 16 kHz WAV.

    Video inputs have their audio track extracted; audio-only inputs are
    re-encoded to the same normalized format for consistent downstream handling.
    """
    require_ffmpeg()
    source = validate_source_path(source)

    probe = probe_media(source)
    if not probe.has_audio:
        raise NoAudioStreamError(f"No audio track found in {source.name}")

    if output_path is None:
        handle = tempfile.NamedTemporaryFile(
            suffix=".wav",
            prefix="transcriber-",
            delete=False,
        )
        handle.close()
        output = Path(handle.name)
        is_temporary = True
    else:
        output = output_path.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        is_temporary = False

    logger.info("Extracting audio from %s -> %s", source.name, output.name)
    _run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            str(TARGET_CHANNELS),
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-f",
            "wav",
            str(output),
        ],
        error_cls=ExtractError,
        message=f"ffmpeg failed to extract audio from {source.name}",
    )

    if not output.is_file() or output.stat().st_size == 0:
        if is_temporary:
            output.unlink(missing_ok=True)
        raise ExtractError(f"ffmpeg produced an empty audio file for {source.name}")

    duration_ms = get_duration_ms(output) or probe.duration_ms
    return PreparedAudio(
        path=output,
        duration_ms=duration_ms,
        source_path=source,
        is_temporary=is_temporary,
    )


@contextmanager
def prepared_audio(source: Path | str) -> Iterator[PreparedAudio]:
    """
    Yield normalized audio for transcription and delete temp output on exit.

    Use this in the transcribe pipeline so extracted WAV files are cleaned up
    after upload/transcription completes.
    """
    prepared = extract_audio(validate_source_path(source))
    try:
        yield prepared
    finally:
        if prepared.is_temporary:
            prepared.path.unlink(missing_ok=True)
            logger.debug("Deleted temporary audio file %s", prepared.path.name)
