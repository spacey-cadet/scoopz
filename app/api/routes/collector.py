from fastapi import APIRouter, status

from app.api.deps import CollectorServiceDep
from app.models.collector import CollectorState, LinkIngestRequest, LinkIngestResponse

router = APIRouter()


@router.get("/state", response_model=CollectorState)
def get_collector_state(service: CollectorServiceDep) -> CollectorState:
    return service.get_state()


@router.post("/links", response_model=LinkIngestResponse, status_code=status.HTTP_202_ACCEPTED)
def ingest_links(payload: LinkIngestRequest, service: CollectorServiceDep) -> LinkIngestResponse:
    return service.ingest_links(payload)

