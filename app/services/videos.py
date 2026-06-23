from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.models.videos import VideoDetail, VideoSummary


class VideoLibrary:
    def __init__(self, settings: Settings):
        self.settings = settings

    def list_videos(self) -> list[VideoSummary]:
        if not self.settings.scoopz_dir.exists():
            return []

        videos = []
        for path in sorted(self.settings.scoopz_dir.glob("*.mp4"), key=lambda item: item.name.lower()):
            stat = path.stat()
            videos.append(
                VideoSummary(
                    video_id=path.stem,
                    name=path.name,
                    path=path,
                    size_bytes=stat.st_size,
                    created_at=_timestamp(stat.st_ctime),
                    thumbnail_path=self._thumbnail_for(path),
                )
            )
        return videos

    def get_video(self, video_id: str) -> VideoDetail | None:
        path = self.settings.scoopz_dir / f"{video_id}.mp4"
        if not path.exists() or not path.is_file():
            return None

        stat = path.stat()
        return VideoDetail(
            video_id=path.stem,
            name=path.name,
            path=path,
            size_bytes=stat.st_size,
            created_at=_timestamp(stat.st_ctime),
            modified_at=_timestamp(stat.st_mtime),
            thumbnail_path=self._thumbnail_for(path),
        )

    def _thumbnail_for(self, video_path: Path) -> Path | None:
        for suffix in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = self.settings.thumb_dir / f"{video_path.stem}{suffix}"
            if candidate.exists():
                return candidate
        return None


def _timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)

