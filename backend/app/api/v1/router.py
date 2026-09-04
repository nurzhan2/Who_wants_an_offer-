"""Aggregate router for /api/v1.

Feature routers are included here as the phases that own them land.
"""

from fastapi import APIRouter

from app.api.v1 import pipeline, profile, resume, sources

router = APIRouter()
router.include_router(resume.router)
router.include_router(profile.router)
router.include_router(sources.router)
router.include_router(pipeline.router)
