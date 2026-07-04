"""Data models for recordings and transcript segments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class RecordingStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class SegmentLanguage(str, Enum):
    AR = "ar"
    EN = "en"
    MIXED = "mixed"


@dataclass
class Recording:
    id: str
    source_path: str
    filename: str
    duration_ms: Optional[int]
    status: RecordingStatus
    error_message: Optional[str]
    summary: Optional[str]
    created_at: datetime


@dataclass
class RecordingListItem:
    id: str
    filename: str
    status: RecordingStatus
    segment_count: int
    created_at: datetime


@dataclass
class Segment:
    recording_id: str
    start_ms: int
    end_ms: int
    text: str
    language: SegmentLanguage
    segment_index: int
    speaker: Optional[str] = None
    id: Optional[int] = None
