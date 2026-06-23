from fastapi import APIRouter

from app.api.routes import captions, collector, health, videos

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(collector.router, prefix="/collector", tags=["collector"])
api_router.include_router(videos.router, prefix="/videos", tags=["videos"])
api_router.include_router(captions.router, prefix="/captions", tags=["captions"])

