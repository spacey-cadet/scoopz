from typing import Annotated

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.services.captions import CaptionJobStore, CaptionService
from app.services.collector import CollectorService
from app.services.videos import VideoLibrary

SettingsDep = Annotated[Settings, Depends(get_settings)]

_caption_jobs = CaptionJobStore()


def get_video_library(settings: SettingsDep) -> VideoLibrary:
    return VideoLibrary(settings=settings)


def get_collector_service(settings: SettingsDep) -> CollectorService:
    return CollectorService(settings=settings)


def get_caption_service(
    settings: SettingsDep,
    video_library: Annotated[VideoLibrary, Depends(get_video_library)],
) -> CaptionService:
    return CaptionService(settings=settings, video_library=video_library, jobs=_caption_jobs)


VideoLibraryDep = Annotated[VideoLibrary, Depends(get_video_library)]
CollectorServiceDep = Annotated[CollectorService, Depends(get_collector_service)]
CaptionServiceDep = Annotated[CaptionService, Depends(get_caption_service)]

