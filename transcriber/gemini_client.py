"""Gemini Files API upload and structured call transcription."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional, TypeVar

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from transcriber.models import Segment, SegmentLanguage

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
FILE_POLL_INTERVAL_SEC = 2.0
FILE_POLL_TIMEOUT_SEC = 300.0

# Mock mode for testing without API key
_MOCK_MODE = os.getenv("GEMINI_MOCK_MODE", "").lower() in ("1", "true", "yes")

_AUDIO_MIME_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}

TRANSCRIPTION_PROMPT = """\
Transcribe this call recording verbatim.
 
Requirements:
- Preserve Arabic script exactly; do not translate Arabic to English.
- Preserve English text exactly in Latin script (a-z); do NOT transliterate English words into Arabic script.
- When speakers code-switch within a sentence, keep the mix in one segment with each word in its original script.
- Modern Standard Arabic and regional dialects are both acceptable; transcribe what you hear.
- Provide segment-level start_ms and end_ms timestamps in milliseconds from the start of the audio.
- Identify distinct speakers as "Speaker 1", "Speaker 2", etc. when discernible; otherwise omit speaker.
- Tag each segment's dominant language as "ar", "en", or "mixed".
- Do not invent words, names, or content that are not clearly spoken.
- Include a brief one-line summary of the call in English in the summary field.
 
CRITICAL: English words must remain in Latin script (e.g., "hello", "computer"), never in Arabic script (e.g., not "هلو" or "كمبيوتر"). Arabic words must remain in Arabic script.
"""

T = TypeVar("T")


class GeminiError(Exception):
    """Base error for Gemini client operations."""


class GeminiConfigError(GeminiError):
    """Missing or invalid Gemini configuration."""


class GeminiUploadError(GeminiError):
    """Audio upload or file processing failed."""


class GeminiTranscriptionError(GeminiError):
    """Structured transcription request or response parsing failed."""


class TranscriptSegmentSchema(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(min_length=1)
    language: Literal["ar", "en", "mixed"]
    speaker: Optional[str] = None


class TranscriptionResponseSchema(BaseModel):
    summary: str = Field(min_length=1)
    segments: list[TranscriptSegmentSchema] = Field(min_length=1)


@dataclass(frozen=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    language: SegmentLanguage
    segment_index: int
    speaker: Optional[str] = None


@dataclass(frozen=True)
class TranscriptionResult:
    summary: str
    segments: list[TranscriptSegment]

    def to_db_segments(self, recording_id: str) -> list[Segment]:
        """Convert parsed segments into DB-ready Segment rows."""
        return [
            Segment(
                recording_id=recording_id,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                language=segment.language,
                speaker=segment.speaker,
                segment_index=segment.segment_index,
            )
            for segment in self.segments
        ]


def _ensure_env_loaded() -> None:
    load_dotenv()


def get_api_key() -> str:
    """Return GEMINI_API_KEY from the environment."""
    if _MOCK_MODE:
        return "mock-key"
    _ensure_env_loaded()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise GeminiConfigError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your API key, or set GEMINI_MOCK_MODE=1 to use mock mode."
        )
    return api_key


def get_model_name() -> str:
    """Return the configured Gemini model name."""
    _ensure_env_loaded()
    model = os.getenv("GEMINI_MODEL", "").strip()
    return model or DEFAULT_MODEL


def create_client(*, api_key: Optional[str] = None) -> genai.Client:
    """Create a Gemini developer client."""
    return genai.Client(api_key=api_key or get_api_key())


def guess_audio_mime_type(path: Path) -> str:
    """Infer MIME type for an audio file from its extension."""
    mime_type = _AUDIO_MIME_TYPES.get(path.suffix.lower())
    if mime_type is None:
        raise GeminiUploadError(
            f"Unsupported audio type '{path.suffix}' for upload. "
            f"Supported: {', '.join(sorted(_AUDIO_MIME_TYPES))}"
        )
    return mime_type


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", 0) == 429:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return False


def _with_retry(operation: Callable[[], T], *, action: str) -> T:
    """Run an API operation once, retrying once on transient failures."""
    last_error: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt == 1 and _is_transient_error(exc):
                logger.warning(
                    "Transient Gemini error during %s (attempt %d): %s",
                    action,
                    attempt,
                    exc,
                )
                time.sleep(1.0)
                continue
            break
    assert last_error is not None
    raise last_error


def wait_for_file_active(
    client: genai.Client,
    uploaded_file: types.File,
    *,
    poll_interval_sec: float = FILE_POLL_INTERVAL_SEC,
    timeout_sec: float = FILE_POLL_TIMEOUT_SEC,
) -> types.File:
    """Poll the Files API until the uploaded file is ready."""
    if not uploaded_file.name:
        raise GeminiUploadError("Uploaded file is missing a resource name.")

    deadline = time.monotonic() + timeout_sec
    current = uploaded_file

    while True:
        state = current.state
        if state in (None, types.FileState.ACTIVE):
            return current
        if state == types.FileState.FAILED:
            raise GeminiUploadError(
                f"Gemini failed to process uploaded file {uploaded_file.name}."
            )
        if time.monotonic() >= deadline:
            raise GeminiUploadError(
                f"Timed out waiting for uploaded file {uploaded_file.name} to become ACTIVE."
            )

        time.sleep(poll_interval_sec)
        current = _with_retry(
            lambda: client.files.get(name=uploaded_file.name),
            action="file status poll",
        )


def upload_audio(
    client: genai.Client,
    audio_path: Path | str,
    *,
    display_name: Optional[str] = None,
) -> types.File:
    """Upload normalized audio via the Gemini Files API and wait until ACTIVE."""
    path = Path(audio_path).expanduser().resolve()
    if not path.is_file():
        raise GeminiUploadError(f"Audio file not found: {path}")

    mime_type = guess_audio_mime_type(path)
    logger.info("Uploading audio to Gemini Files API: %s", path.name)

    uploaded = _with_retry(
        lambda: client.files.upload(
            file=path,
            config=types.UploadFileConfig(
                display_name=display_name or path.name,
                mime_type=mime_type,
            ),
        ),
        action="audio upload",
    )
    return wait_for_file_active(client, uploaded)


def parse_transcription_response(
    payload: TranscriptionResponseSchema | dict | str,
) -> TranscriptionResult:
    """Parse Gemini structured transcription JSON into domain objects."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise GeminiTranscriptionError(
                "Gemini returned invalid JSON for transcription."
            ) from exc

    if isinstance(payload, dict):
        try:
            payload = TranscriptionResponseSchema.model_validate(payload)
        except ValidationError as exc:
            raise GeminiTranscriptionError(
                "Gemini transcription JSON did not match the expected schema."
            ) from exc

    segments: list[TranscriptSegment] = []
    for index, segment in enumerate(payload.segments):
        if segment.end_ms < segment.start_ms:
            raise GeminiTranscriptionError(
                f"Segment {index} has end_ms before start_ms."
            )
        try:
            language = SegmentLanguage(segment.language)
        except ValueError as exc:
            raise GeminiTranscriptionError(
                f"Segment {index} has invalid language '{segment.language}'."
            ) from exc

        segments.append(
            TranscriptSegment(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text.strip(),
                language=language,
                speaker=segment.speaker.strip() if segment.speaker else None,
                segment_index=index,
            )
        )

    return TranscriptionResult(
        summary=payload.summary.strip(),
        segments=segments,
    )


def _generate_transcription(
    client: genai.Client,
    uploaded_file: types.File,
    *,
    model: str,
) -> TranscriptionResult:
    if not uploaded_file.uri or not uploaded_file.mime_type:
        raise GeminiTranscriptionError("Uploaded file is missing URI or MIME type.")

    audio_part = types.Part.from_uri(
        file_uri=uploaded_file.uri,
        mime_type=uploaded_file.mime_type,
    )

    response = _with_retry(
        lambda: client.models.generate_content(
            model=model,
            contents=[TRANSCRIPTION_PROMPT, audio_part],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TranscriptionResponseSchema,
            ),
        ),
        action="transcription",
    )

    if response.parsed is not None:
        if not isinstance(response.parsed, TranscriptionResponseSchema):
            raise GeminiTranscriptionError(
                "Gemini returned an unexpected parsed transcription type."
            )
        return parse_transcription_response(response.parsed)

    if response.text:
        return parse_transcription_response(response.text)

    raise GeminiTranscriptionError("Gemini returned an empty transcription response.")


def _mock_transcription(audio_path: Path | str) -> TranscriptionResult:
    """Return mock transcription data for testing without API key."""
    logger.info("Using mock transcription mode (no API call)")
    return TranscriptionResult(
        summary="Mock call summary: A brief conversation about project requirements and timeline.",
        segments=[
            TranscriptSegment(
                start_ms=0,
                end_ms=4200,
                text="Hello, how are you today?",
                language=SegmentLanguage.EN,
                segment_index=0,
                speaker="Speaker 1",
            ),
            TranscriptSegment(
                start_ms=4200,
                end_ms=8500,
                text="أنا بخير، شكراً لك. كيف يمكنني مساعدتك؟",
                language=SegmentLanguage.AR,
                segment_index=1,
                speaker="Speaker 2",
            ),
            TranscriptSegment(
                start_ms=8500,
                end_ms=12000,
                text="We need to discuss the project timeline and deliverables.",
                language=SegmentLanguage.EN,
                segment_index=2,
                speaker="Speaker 1",
            ),
            TranscriptSegment(
                start_ms=12000,
                end_ms=16000,
                text="بالتأكيد، لدي بعض الأسئلة حول المواصفات الفنية",
                language=SegmentLanguage.MIXED,
                segment_index=3,
                speaker="Speaker 2",
            ),
            TranscriptSegment(
                start_ms=16000,
                end_ms=20000,
                text="Sure, let's go through them one by one.",
                language=SegmentLanguage.EN,
                segment_index=4,
                speaker="Speaker 1",
            ),
        ],
    )


def transcribe_audio(
    audio_path: Path | str,
    *,
    client: Optional[genai.Client] = None,
    model: Optional[str] = None,
) -> TranscriptionResult:
    """
    Upload audio to Gemini and return structured transcript segments.

    Uploaded files are deleted from Gemini storage after transcription completes.
    In mock mode (GEMINI_MOCK_MODE=1), returns fake data without API calls.
    """
    if _MOCK_MODE:
        return _mock_transcription(audio_path)

    owns_client = client is None
    gemini_client = client or create_client()
    model_name = model or get_model_name()
    uploaded_file: Optional[types.File] = None

    try:
        uploaded_file = upload_audio(gemini_client, audio_path)
        result = _generate_transcription(
            gemini_client,
            uploaded_file,
            model=model_name,
        )
        logger.info(
            "Transcription complete: %d segments, summary length %d",
            len(result.segments),
            len(result.summary),
        )
        return result
    except genai_errors.APIError as exc:
        raise GeminiTranscriptionError(f"Gemini API error: {exc}") from exc
    finally:
        if uploaded_file and uploaded_file.name:
            try:
                gemini_client.files.delete(name=uploaded_file.name)
                logger.debug("Deleted uploaded Gemini file %s", uploaded_file.name)
            except Exception as exc:
                logger.warning(
                    "Failed to delete uploaded Gemini file %s: %s",
                    uploaded_file.name,
                    exc,
                )
        if owns_client:
            close = getattr(gemini_client, "close", None)
            if callable(close):
                close()
