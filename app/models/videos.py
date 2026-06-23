from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class VideoSummary(BaseModel):
    video_id: str
    name: str
    path: Path
    size_bytes: int
    created_at: datetime | None = None
    thumbnail_path: Path | None = None


class VideoDetail(VideoSummary):
    modified_at: datetime | None = None


class VideoListResponse(BaseModel):
    count: int
    videos: list[VideoSummary]

