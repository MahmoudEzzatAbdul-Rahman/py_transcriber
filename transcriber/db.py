"""SQLite persistence for recordings and transcript segments."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from transcriber.models import (
    Recording,
    RecordingListItem,
    RecordingStatus,
    Segment,
    SegmentLanguage,
)

DEFAULT_DATABASE_PATH = Path("./data/transcriber.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    duration_ms INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    error_message TEXT,
    summary TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    language TEXT NOT NULL CHECK (language IN ('ar', 'en', 'mixed')),
    speaker TEXT,
    segment_index INTEGER NOT NULL,
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_segments_recording_id ON segments(recording_id);
CREATE INDEX IF NOT EXISTS idx_segments_recording_start ON segments(recording_id, start_ms);
"""


def get_database_path() -> Path:
    """Resolve database path from DATABASE_PATH env or default."""
    raw = os.getenv("DATABASE_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_DATABASE_PATH


def init_db(db_path: Optional[Path] = None) -> Path:
    """Create tables and indexes if they do not exist. Returns the DB path used."""
    path = db_path or get_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(_SCHEMA_SQL)
    return path


@contextmanager
def get_connection(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection with schema initialized."""
    path = init_db(db_path)
    with _connect(path) as conn:
        yield conn


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _recording_from_row(row: sqlite3.Row) -> Recording:
    return Recording(
        id=row["id"],
        source_path=row["source_path"],
        filename=row["filename"],
        duration_ms=row["duration_ms"],
        status=RecordingStatus(row["status"]),
        error_message=row["error_message"],
        summary=row["summary"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _segment_from_row(row: sqlite3.Row) -> Segment:
    return Segment(
        id=row["id"],
        recording_id=row["recording_id"],
        start_ms=row["start_ms"],
        end_ms=row["end_ms"],
        text=row["text"],
        language=SegmentLanguage(row["language"]),
        speaker=row["speaker"],
        segment_index=row["segment_index"],
    )


def create_recording(
    source_path: str,
    filename: str,
    *,
    duration_ms: Optional[int] = None,
    status: RecordingStatus = RecordingStatus.PENDING,
    db_path: Optional[Path] = None,
) -> Recording:
    """Insert a new recording and return it."""
    recording_id = uuid.uuid4().hex[:12]
    created_at = _utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO recordings (
                id, source_path, filename, duration_ms, status,
                error_message, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                recording_id,
                source_path,
                filename,
                duration_ms,
                status.value,
                created_at.isoformat(),
            ),
        )
    return Recording(
        id=recording_id,
        source_path=source_path,
        filename=filename,
        duration_ms=duration_ms,
        status=status,
        error_message=None,
        summary=None,
        created_at=created_at,
    )


def update_recording(
    recording_id: str,
    *,
    status: Optional[RecordingStatus] = None,
    error_message: Optional[str] = None,
    summary: Optional[str] = None,
    duration_ms: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Optional[Recording]:
    """Update mutable recording fields. Returns the updated row or None if missing."""
    updates: list[str] = []
    params: list[object] = []

    if status is not None:
        updates.append("status = ?")
        params.append(status.value)
    if error_message is not None:
        updates.append("error_message = ?")
        params.append(error_message)
    if summary is not None:
        updates.append("summary = ?")
        params.append(summary)
    if duration_ms is not None:
        updates.append("duration_ms = ?")
        params.append(duration_ms)

    if not updates:
        return get_recording(recording_id, db_path=db_path)

    params.append(recording_id)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE recordings SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        if cursor.rowcount == 0:
            return None
    return get_recording(recording_id, db_path=db_path)


def get_recording(
    recording_id: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[Recording]:
    """Fetch a single recording by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM recordings WHERE id = ?",
            (recording_id,),
        ).fetchone()
    if row is None:
        return None
    return _recording_from_row(row)


def list_recordings(*, db_path: Optional[Path] = None) -> list[RecordingListItem]:
    """List all recordings with segment counts, newest first."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                r.id,
                r.filename,
                r.status,
                r.created_at,
                COUNT(s.id) AS segment_count
            FROM recordings r
            LEFT JOIN segments s ON s.recording_id = r.id
            GROUP BY r.id
            ORDER BY r.created_at DESC
            """
        ).fetchall()
    return [
        RecordingListItem(
            id=row["id"],
            filename=row["filename"],
            status=RecordingStatus(row["status"]),
            segment_count=row["segment_count"],
            created_at=_parse_datetime(row["created_at"]),
        )
        for row in rows
    ]


def insert_segments(
    recording_id: str,
    segments: list[Segment],
    *,
    db_path: Optional[Path] = None,
) -> list[Segment]:
    """Bulk-insert segments for a recording. Returns segments with assigned IDs."""
    if not segments:
        return []

    with get_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM recordings WHERE id = ?",
            (recording_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"Recording not found: {recording_id}")

        inserted: list[Segment] = []
        for segment in segments:
            cursor = conn.execute(
                """
                INSERT INTO segments (
                    recording_id, start_ms, end_ms, text, language,
                    speaker, segment_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    segment.start_ms,
                    segment.end_ms,
                    segment.text,
                    segment.language.value,
                    segment.speaker,
                    segment.segment_index,
                ),
            )
            inserted.append(
                Segment(
                    id=cursor.lastrowid,
                    recording_id=recording_id,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=segment.text,
                    language=segment.language,
                    speaker=segment.speaker,
                    segment_index=segment.segment_index,
                )
            )
    return inserted


def get_segments(
    recording_id: str,
    *,
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> list[Segment]:
    """Fetch segments for a recording, optionally filtered to a time range."""
    query = """
        SELECT * FROM segments
        WHERE recording_id = ?
    """
    params: list[object] = [recording_id]

    if from_ms is not None:
        query += " AND end_ms > ?"
        params.append(from_ms)
    if to_ms is not None:
        query += " AND start_ms < ?"
        params.append(to_ms)

    query += " ORDER BY segment_index ASC"

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_segment_from_row(row) for row in rows]


def get_segment_count(
    recording_id: str,
    *,
    db_path: Optional[Path] = None,
) -> int:
    """Return the number of segments stored for a recording."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM segments WHERE recording_id = ?",
            (recording_id,),
        ).fetchone()
    return int(row["count"])
