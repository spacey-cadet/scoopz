from fastapi import APIRouter, HTTPException, status

from app.api.deps import VideoLibraryDep
from app.models.videos import VideoDetail, VideoListResponse

router = APIRouter()


@router.get("", response_model=VideoListResponse)
def list_videos(library: VideoLibraryDep) -> VideoListResponse:
    videos = library.list_videos()
    return VideoListResponse(count=len(videos), videos=videos)


@router.get("/{video_id}", response_model=VideoDetail)
def get_video(video_id: str, library: VideoLibraryDep) -> VideoDetail:
    video = library.get_video(video_id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    return video

