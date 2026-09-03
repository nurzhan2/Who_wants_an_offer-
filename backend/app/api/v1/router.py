"""Aggregate router for /api/v1.

Feature routers (resume, vacancies, matches, sources, pipeline, analytics,
applications) are included here as the phases that own them land.
"""

from fastapi import APIRouter

router = APIRouter()
