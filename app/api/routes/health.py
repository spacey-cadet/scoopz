from fastapi import APIRouter

from app.api.deps import SettingsDep
from app.models.common import HealthCheck

router = APIRouter()


@router.get("/health", response_model=HealthCheck)
def health_check(settings: SettingsDep) -> HealthCheck:
    return HealthCheck(status="ok", app=settings.app_name, version=settings.app_version)

