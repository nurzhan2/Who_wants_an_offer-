"""The documents screen: what this project has written, and what came of it.

Two kinds of document exist in this database and the screen shows both.

**Resumes** are uploaded, not generated. Nothing here writes a CV — there is no
CV generator in this repository — so what this screen can honestly show about
one is the file that was uploaded and the ATS audit taken at that moment: what
an employer's parser sees when it reads it, and what it loses. That audit is the
closest thing to a generated document the resume side has, and it is the half a
person can act on.

**Letters** are generated, saved and never sent from here. Each row carries the
three dates the feedback loop is made of — written, sent, answered — because a
letter that was written and never sent is work waiting for a person at a
keyboard, and a letter that was sent and never answered is the measurement this
whole project exists to improve. A screen that showed only the text would answer
none of that.

The rules version is shown twice on purpose: the version recorded with each
letter, and the version in force now. When they differ, the letter was judged by
rules that no longer apply, and re-running today's guard over the stored text —
which is what :attr:`LetterDocument.problems` is — says whether that matters.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import RuleScope
from app.db.models import CandidateProfile
from app.db.repositories.application import ApplicationRepository
from app.db.repositories.profile import ProfileRepository
from app.documents import rules as document_rules
from app.letters.guard import ENGLISH, RUSSIAN, find_problems
from app.schemas.ats import ATSReport
from app.schemas.dashboard import Documents, LetterDocument, LetterProblemRead, ResumeDocument
from app.workshop import store as workshop_store


async def build(session: AsyncSession) -> Documents:
    """Every resume and every letter, with what became of each.

    ``current_rules_version`` is the fingerprint of the rules in force right now
    — the built-in guard and whatever the owner has active in the workshop — so
    the screen can put it beside the version each letter was written under and
    let a person see which ones predate an edit.
    """
    profiles = await ProfileRepository(session).list_all()
    letters = await ApplicationRepository(session).letters()
    active = await workshop_store.active_rules(session, scope=RuleScope.COVER_LETTER)
    return Documents(
        resumes=[_resume(profile) for profile in profiles],
        letters=[_with_verdict(letter) for letter in letters],
        current_rules_version=document_rules.version(active, scope=RuleScope.COVER_LETTER),
    )


def _resume(profile: CandidateProfile) -> ResumeDocument:
    """One uploaded resume and its audit.

    A stored report that no longer validates is dropped rather than raised on:
    :mod:`app.schemas.ats` says a change there is a schema change and older
    reports must keep validating, so a failure here is a bug in that promise —
    and the right place to notice it is a missing panel on one row, not a
    documents screen that will not open.
    """
    report: ATSReport | None = None
    if profile.ats_report:
        try:
            report = ATSReport.model_validate(profile.ats_report)
        except ValueError:
            report = None
    return ResumeDocument(
        profile_id=profile.id,
        filename=profile.resume_filename,
        source_format=profile.resume_format,
        size_bytes=profile.resume_size_bytes,
        is_active=profile.is_active,
        parse_status=profile.parse_status,
        parse_error=profile.parse_error,
        uploaded_at=profile.created_at,
        ats=report,
    )


def _with_verdict(letter: LetterDocument) -> LetterDocument:
    """The same row, plus what today's rules make of the text it holds.

    Recomputed rather than stored. A stored verdict would be the one thing on
    this screen that nothing keeps current, and the interesting row is exactly
    the one where the answer has changed since the letter was written.

    The length bounds are the defaults, not this vacancy's limit: the limit is
    a property of the posting and is not recorded with the letter, so applying
    a guessed one would report a letter as too long for a vacancy that never
    said so. Generation checks against the real limit — see
    ``app/letters/generator.py`` — and that is where that check belongs.
    """
    problems = find_problems(letter.text)
    return letter.model_copy(
        update={
            "problems": [
                LetterProblemRead(
                    code=problem.value,
                    # Russian: it is read by the person whose letter it is. The
                    # English form is what the model is shown on a retry.
                    message=RUSSIAN.get(problem, ENGLISH[problem]),
                )
                for problem in problems
            ]
        }
    )
