from fastapi import APIRouter, HTTPException, status

from app.api.deps import CaptionServiceDep
from app.models.captions import CaptionJob, CaptionJobCreate, CaptionJobStatus

router = APIRouter()


@router.post("/jobs", response_model=CaptionJob, status_code=status.HTTP_202_ACCEPTED)
def create_caption_job(payload: CaptionJobCreate, service: CaptionServiceDep) -> CaptionJob:
    return service.create_job(payload)


@router.get("/jobs/{job_id}", response_model=CaptionJob)
def get_caption_job(job_id: str, service: CaptionServiceDep) -> CaptionJob:
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Caption job not found")
    return job


@router.get("/jobs/{job_id}/status", response_model=CaptionJobStatus)
def get_caption_job_status(job_id: str, service: CaptionServiceDep) -> CaptionJobStatus:
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Caption job not found")
    return CaptionJobStatus(job_id=job.job_id, status=job.status, progress=job.progress)

