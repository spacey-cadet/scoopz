from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


class CaptionJobState(str, Enum):
    queued = "queued"
    processing = "processing"
    complete = "complete"
    failed = "failed"


class CaptionJobCreate(BaseModel):
    video_ids: list[str] = Field(..., min_length=1, max_length=7)
    prompt: str | None = None


class CaptionResult(BaseModel):
    video_id: str
    caption: str | None = None
    hashtags: str | None = None
    error: str | None = None


class CaptionJob(BaseModel):
    job_id: str = Field(default_factory=lambda: uuid4().hex)
    status: CaptionJobState = CaptionJobState.queued
    video_ids: list[str]
    prompt: str | None = None
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    results: list[CaptionResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CaptionJobStatus(BaseModel):
    job_id: str
    status: CaptionJobState
    progress: float = Field(ge=0.0, le=1.0)
