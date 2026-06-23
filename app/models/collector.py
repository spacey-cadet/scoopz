from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl


class LinkIngestRequest(BaseModel):
    urls: list[HttpUrl] = Field(..., min_length=1)


class LinkIngestResponse(BaseModel):
    accepted: int
    inbox_path: Path


class CollectorState(BaseModel):
    inbox_path: Path
    history_file: Path
    pending_links: int
    downloaded_ids: int

