"""Typer CLI for call recording transcription."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from transcriber.audio import (
    FFmpegNotFoundError,
    NoAudioStreamError,
    PreparedAudio,
    prepared_audio,
    require_ffmpeg,
    validate_source_path,
)
from transcriber.db import (
    create_recording,
    get_recording,
    get_segments,
    get_segment_count,
    insert_segments,
    list_recordings,
    update_recording,
)
from transcriber.gemini_client import (
    GeminiConfigError,
    GeminiError,
    GeminiTranscriptionError,
    transcribe_audio,
)
from transcriber.models import RecordingStatus

app = typer.Typer(help="Transcribe call recordings with timestamped Arabic/English segments.")
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _format_timestamp(ms: int) -> str:
    """Format milliseconds as MM:SS.mm."""
    total_seconds = ms / 1000
    minutes = int(total_seconds // 60)
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:05.2f}"


def _print_segment_preview(segments: list, max_segments: int = 3) -> None:
    """Print a preview of the first few segments."""
    for segment in segments[:max_segments]:
        speaker = f" {segment.speaker}" if segment.speaker else ""
        lang = f" ({segment.language})" if segment.language else ""
        start = _format_timestamp(segment.start_ms)
        end = _format_timestamp(segment.end_ms)
        console.print(
            f"[dim][{start} - {end}]{speaker}{lang}[/dim] {segment.text}"
        )
    if len(segments) > max_segments:
        console.print(f"[dim]... and {len(segments) - max_segments} more segments[/dim]")


@app.command()
def transcribe(
    file_path: str = typer.Argument(..., help="Path to audio or video file to transcribe."),
) -> None:
    """
    Transcribe a call recording and save to the database.

    Extracts audio from video files if needed, uploads to Gemini for transcription,
    and persists timestamped segments to SQLite.
    """
    try:
        require_ffmpeg()
    except FFmpegNotFoundError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    source = validate_source_path(file_path)

    console.print(f"[dim]Creating recording entry for {source.name}...[/dim]")
    recording = create_recording(
        source_path=str(source),
        filename=source.name,
        status=RecordingStatus.PENDING,
    )

    try:
        console.print(f"[dim]Recording ID: {recording.id}[/dim]")
        update_recording(recording.id, status=RecordingStatus.PROCESSING)

        with console.status("[bold green]Extracting audio..."):
            with prepared_audio(source) as prepared:
                console.print(f"[dim]Audio extracted: {prepared.path.name}[/dim]")

                with console.status("[bold green]Transcribing with Gemini..."):
                    result = transcribe_audio(prepared.path)

        console.print(f"[dim]Transcription complete: {len(result.segments)} segments[/dim]")

        db_segments = result.to_db_segments(recording.id)
        insert_segments(recording.id, db_segments)

        update_recording(
            recording.id,
            status=RecordingStatus.DONE,
            summary=result.summary,
            duration_ms=prepared.duration_ms,
        )

        console.print(f"[bold green]✓ Recording {recording.id} saved ({len(db_segments)} segments)[/bold green]")
        console.print(f"[dim]Summary: {result.summary}[/dim]")
        console.print("[dim]Preview:[/dim]")
        _print_segment_preview(db_segments)

    except GeminiConfigError as exc:
        console.print(f"[red]Configuration error: {exc}[/red]")
        update_recording(recording.id, status=RecordingStatus.FAILED, error_message=str(exc))
        raise typer.Exit(code=1)
    except GeminiError as exc:
        console.print(f"[red]Transcription failed: {exc}[/red]")
        update_recording(recording.id, status=RecordingStatus.FAILED, error_message=str(exc))
        raise typer.Exit(code=1)
    except Exception as exc:
        console.print(f"[red]Unexpected error: {exc}[/red]")
        update_recording(recording.id, status=RecordingStatus.FAILED, error_message=str(exc))
        raise typer.Exit(code=1)


@app.command()
def list() -> None:
    """List all recordings in the database."""
    recordings = list_recordings()

    if not recordings:
        console.print("[dim]No recordings found.[/dim]")
        return

    table = Table(title="Recordings")
    table.add_column("ID", style="cyan", width=12)
    table.add_column("Filename", style="white", width=30)
    table.add_column("Status", style="magenta", width=10)
    table.add_column("Segments", style="green", width=8)
    table.add_column("Created", style="dim", width=20)

    for rec in recordings:
        status_color = {
            RecordingStatus.DONE: "green",
            RecordingStatus.PROCESSING: "yellow",
            RecordingStatus.FAILED: "red",
            RecordingStatus.PENDING: "dim",
        }.get(rec.status, "white")

        created_str = rec.created_at.strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(
            rec.id,
            rec.filename[:30] + "..." if len(rec.filename) > 30 else rec.filename,
            f"[{status_color}]{rec.status}[/{status_color}]",
            str(rec.segment_count),
            created_str,
        )

    console.print(table)


@app.command()
def show(
    recording_id: str = typer.Argument(..., help="Recording ID to display."),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON instead of formatted text."),
    from_seconds: Optional[int] = typer.Option(None, "--from", help="Start time in seconds."),
    to_seconds: Optional[int] = typer.Option(None, "--to", help="End time in seconds."),
) -> None:
    """
    Display a full transcript with timestamps.

    Use --json for raw export, or --from/--to to filter by time range in seconds.
    """
    recording = get_recording(recording_id)
    if recording is None:
        console.print(f"[red]Recording not found: {recording_id}[/red]")
        raise typer.Exit(code=1)

    from_ms = from_seconds * 1000 if from_seconds is not None else None
    to_ms = to_seconds * 1000 if to_seconds is not None else None

    segments = get_segments(recording_id, from_ms=from_ms, to_ms=to_ms)

    if not segments:
        console.print("[dim]No segments found for this recording.[/dim]")
        return

    if json_output:
        output = {
            "recording_id": recording.id,
            "filename": recording.filename,
            "status": recording.status.value,
            "summary": recording.summary,
            "segments": [
                {
                    "id": seg.id,
                    "start_ms": seg.start_ms,
                    "end_ms": seg.end_ms,
                    "text": seg.text,
                    "language": seg.language.value,
                    "speaker": seg.speaker,
                    "segment_index": seg.segment_index,
                }
                for seg in segments
            ],
        }
        console.print_json(json.dumps(output))
    else:
        console.print(f"[bold]Recording: {recording.filename}[/bold]")
        console.print(f"[dim]ID: {recording.id} | Status: {recording.status.value}[/dim]")
        if recording.summary:
            console.print(f"[dim]Summary: {recording.summary}[/dim]")
        console.print()

        for segment in segments:
            speaker = f" {segment.speaker}" if segment.speaker else ""
            lang = f" ({segment.language})" if segment.language else ""
            start = _format_timestamp(segment.start_ms)
            end = _format_timestamp(segment.end_ms)
            console.print(
                f"[cyan][{start} - {end}]{speaker}{lang}[/cyan] {segment.text}"
            )


if __name__ == "__main__":
    app()
