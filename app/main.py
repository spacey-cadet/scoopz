from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="API scaffold for the Scoopz collector and caption workflow.",
    )
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()

