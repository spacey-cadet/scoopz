import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    app_name: str = "Scoopz"
    app_version: str = "0.1.0"
    api_v1_prefix: str = "/api/v1"

    scoopz_dir: Path = Field(default=Path("/sdcard/Pictures/scoopz"))
    download_dir: Path = Field(default=Path("/sdcard/Download"))
    inbox_path: Path = Field(default=Path("/sdcard/Download/links_inbox.txt"))
    history_file: Path = Field(default=Path("/sdcard/Download/scoopz_history.log"))
    thumb_dir: Path = Field(default=Path("/sdcard/Pictures/scoopz/.thumbs"))
    temp_dir: Path = Field(default=Path("/sdcard/Download/scoopz_temp"))

    max_batch_size: int = 7
    num_frames: int = 4
    extraction_workers: int = 3
    api_workers: int = 3


def _path_env(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


@lru_cache
def get_settings() -> Settings:
    download_dir = _path_env("SCOOPZ_DOWNLOAD_DIR", Path("/sdcard/Download"))
    scoopz_dir = _path_env("SCOOPZ_DIR", Path("/sdcard/Pictures/scoopz"))
    return Settings(
        app_name=os.environ.get("SCOOPZ_APP_NAME", "Scoopz"),
        app_version=os.environ.get("SCOOPZ_APP_VERSION", "0.1.0"),
        api_v1_prefix=os.environ.get("SCOOPZ_API_V1_PREFIX", "/api/v1"),
        scoopz_dir=scoopz_dir,
        download_dir=download_dir,
        inbox_path=_path_env("SCOOPZ_INBOX_PATH", download_dir / "links_inbox.txt"),
        history_file=_path_env("SCOOPZ_HISTORY_FILE", download_dir / "scoopz_history.log"),
        thumb_dir=_path_env("SCOOPZ_THUMB_DIR", scoopz_dir / ".thumbs"),
        temp_dir=_path_env("SCOOPZ_TEMP_DIR", download_dir / "scoopz_temp"),
        max_batch_size=int(os.environ.get("SCOOPZ_MAX_BATCH_SIZE", "7")),
        num_frames=int(os.environ.get("SCOOPZ_NUM_FRAMES", "4")),
        extraction_workers=int(os.environ.get("SCOOPZ_EXTRACTION_WORKERS", "3")),
        api_workers=int(os.environ.get("SCOOPZ_API_WORKERS", "3")),
    )

