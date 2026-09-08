"""Aggregate router for /api/v1.

Feature routers are included here as the phases that own them land.
"""

from fastapi import APIRouter

from app.api.v1 import applications, pipeline, profile, resume, sources, workshop

router = APIRouter()
router.include_router(resume.router)
router.include_router(profile.router)
router.include_router(sources.router)
router.include_router(pipeline.router)
router.include_router(applications.router)
router.include_router(workshop.router)
