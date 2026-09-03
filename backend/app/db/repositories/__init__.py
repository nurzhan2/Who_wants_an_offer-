"""Typed repositories.

Services call these; they never build SQL themselves. Repositories know about
SQL and nothing about scoring, connectors or HTTP.
"""

from app.db.repositories.match import MatchRepository
from app.db.repositories.pipeline_run import PipelineRunRepository
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.vacancy import VacancyRepository

__all__ = [
    "MatchRepository",
    "PipelineRunRepository",
    "ProfileRepository",
    "VacancyRepository",
]
