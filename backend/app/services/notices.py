"""Things about the owner's account that every screen has to show.

There is one today, and it is the reason this module exists: hh's sentence
about the resume's visibility. On the first real ``--send`` (2026-09-16) hh
showed it on all four applications — «поменяйте видимость резюме на „Видно всем
работодателям, зарегистрированным на headhunter.com.kz“». The applications
went out (hh accepts them under that notice, measured 2026-09-07), but the
sentence is about the *resume*: while the setting stands, every application is
sent with it, and an employer may never see the resume behind it.

So it cannot be something the owner learns at the moment of sending, one
vacancy at a time, in a terminal. It is read here from what the agent recorded
and shown on every screen until the evidence says it has gone.

**"Gone" is decided by evidence, not by a dismiss button.** hh shows the line on
every response form while the setting stands, and the agent records what the
form said on every send. So if the newest send carries no such line while an
older one did, the owner has most likely changed the setting — and the notice
says exactly that, rather than disappearing silently or staying for ever.

hh's words are quoted, never paraphrased: the setting has to be found by its
exact name in hh's own interface.
"""

import re
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Application
from app.schemas.notices import Notice, NoticeKind, Notices

#: hh's notice, however it is inflected or wrapped. Deliberately loose on the
#: rest of the sentence: the target setting's name differs by site (hh.ru,
#: hh.kz, headhunter.com.kz) and the owner needs whichever one hh wrote.
VISIBILITY: Final[re.Pattern[str]] = re.compile(r"видимост\w*\s+резюме", re.IGNORECASE)

#: What to do, without inventing a path through hh's interface that nobody here
#: has measured. The setting's exact target is the quoted part of hh's sentence.
WHERE: Final[str] = (
    "откройте это резюме на hh, найдите настройку видимости и выберите вариант, "
    "который hh называет в кавычках."
)


async def build(session: AsyncSession) -> Notices:
    """Every notice that currently applies. Empty when there is nothing to say."""
    notice = await _resume_visibility(session)
    return Notices(items=[notice] if notice is not None else [])


async def _resume_visibility(session: AsyncSession) -> Notice | None:
    """hh's visibility sentence, if the sends recorded it, and whether it still holds."""
    rows = (
        await session.execute(
            select(
                Application.sent_at,
                Application.hh_warning,
                Application.hh_blocking_warning,
            )
            .where(Application.sent_at.is_not(None))
            .order_by(Application.sent_at.desc())
        )
    ).all()
    carrying: list[tuple[datetime, str]] = []
    for row in rows:
        line = quoted_visibility(row.hh_blocking_warning) or quoted_visibility(row.hh_warning)
        if line is not None and row.sent_at is not None:
            carrying.append((row.sent_at, line))
    if not carrying:
        return None

    newest_send: datetime = rows[0].sent_at
    last_seen, quote = carrying[0]
    first_seen = carrying[-1][0]
    resolved = newest_send > last_seen
    return Notice(
        kind=NoticeKind.RESUME_VISIBILITY,
        title=(
            "Похоже, видимость резюме уже исправлена"
            if resolved
            else "hh ограничивает видимость вашего резюме"
        ),
        quote=quote,
        body=(
            "Последний отклик ушёл уже без этого предупреждения. Если вы меняли "
            "настройку — всё в порядке; если нет, проверьте её."
            if resolved
            else (
                "Это про само резюме, а не про конкретную вакансию: пока настройка не "
                "изменена, все отклики уходят с ним, и работодатель может резюме не "
                f"увидеть. Где поменять: {WHERE}"
            )
        ),
        resolved=resolved,
        applications=len(carrying),
        first_seen_at=first_seen,
        last_seen_at=last_seen,
    )


def quoted_visibility(text: str | None) -> str | None:
    """The line of ``text`` that is hh's visibility notice, verbatim, or None."""
    if not text:
        return None
    for line in text.splitlines():
        if VISIBILITY.search(line):
            return line.strip()
    return None
