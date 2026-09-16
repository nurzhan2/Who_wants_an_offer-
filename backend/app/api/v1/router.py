"""Aggregate router for /api/v1.

Feature routers are included here as the phases that own them land.

Order matters in exactly one place and it is worth naming: ``applications`` is
the token-guarded seam to the local apply agent, while the dashboard's own
read of the tracker lives under ``tracker``. They are separate prefixes on
purpose — see ``app/api/v1/tracker.py`` — so that nothing unauthenticated ever
shares a namespace whose documented promise is that the token is required.
"""

from fastapi import APIRouter

from app.api.v1 import (
    applications,
    documents,
    operations,
    overview,
    pipeline,
    profile,
    resume,
    sources,
    tracker,
    vacancies,
    workshop,
)

router = APIRouter()
router.include_router(resume.router)
router.include_router(profile.router)
router.include_router(sources.router)
router.include_router(pipeline.router)
router.include_router(applications.router)
router.include_router(overview.router)
router.include_router(operations.router)
router.include_router(vacancies.router)
router.include_router(tracker.router)
router.include_router(documents.router)
router.include_router(workshop.router)
