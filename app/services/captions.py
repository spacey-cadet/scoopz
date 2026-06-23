from datetime import datetime, timezone

from app.core.config import Settings
from app.models.captions import CaptionJob, CaptionJobCreate
from app.services.videos import VideoLibrary


class CaptionJobStore:
    def __init__(self):
        self._jobs: dict[str, CaptionJob] = {}

    def add(self, job: CaptionJob) -> CaptionJob:
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> CaptionJob | None:
        return self._jobs.get(job_id)


class CaptionService:
    def __init__(self, settings: Settings, video_library: VideoLibrary, jobs: CaptionJobStore):
        self.settings = settings
        self.video_library = video_library
        self.jobs = jobs

    def create_job(self, payload: CaptionJobCreate) -> CaptionJob:
        job = CaptionJob(video_ids=payload.video_ids, prompt=payload.prompt)
        job.updated_at = datetime.now(timezone.utc)
        return self.jobs.add(job)

    def get_job(self, job_id: str) -> CaptionJob | None:
        return self.jobs.get(job_id)

