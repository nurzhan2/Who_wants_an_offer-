"""Turning a CV context into prompt variables, and fencing the untrusted part.

The same three-part defence the letter package uses, for the same reason and
against a slightly worse case. A vacancy description is text somebody else wrote
and published, and ``app/llm/base.py`` already treats it as the base case for
prompt injection — the tool policy denies this task every tool it could use, and
this task, like ``COVER_LETTER``, gets none.

1. **The description arrives fenced and labelled as data**, last, after every
   instruction.
2. **The fence cannot be closed from inside.** Any occurrence of the markers in
   the description is neutralised before rendering.
3. **The answer is checked, not trusted** — :mod:`app.documents.guard`, reading
   the arrangement and then the finished document.

What differs from the letter is how much a successful injection could achieve.
The letter's prompt asks for prose, so "say the candidate has twelve years of
Kubernetes" is a sentence away from working. This prompt asks for an
arrangement: references to jobs, names from a closed skill list, a headline from
a closed set of strings. There is no field in the answer for a description to
put a claim into. An injection that persuades the model completely still cannot
express the thing it was trying to say — which is the strongest form the defence
takes, and the reason the schema is shaped the way it is.

The markers themselves are shared with :mod:`app.letters.prompt` rather than
redefined, so a description crafted against one prompt is defanged in the other.

The prompt itself lives in ``app/llm/prompts/tailored_cv.md``, per CLAUDE.md;
this module only fills it in.
"""

from app.documents.context import MAX_SUMMARY_CHARS, CVContext
from app.documents.guard import MIN_SKILLS
from app.letters.context import LetterContext, clip_description
from app.letters.prompt import FENCE_CLOSE, FENCE_OPEN, defang, overlap_block, vacancy_block

#: Shown where a vacancy has no description at all — a real case, because a
#: sitemap crawl can store a posting whose body never came back.
_NO_DESCRIPTION = "(no description was stored for this vacancy)"


def fenced_description(context: CVContext) -> str:
    """The description, bounded, defanged, and wrapped in its markers."""
    description = clip_description(context.vacancy.description)
    body = defang(description) if description else _NO_DESCRIPTION
    return f"{FENCE_OPEN}\n{body}\n{FENCE_CLOSE}"


def candidate_block(context: CVContext) -> str:
    """What the CV may say about the person, minus their contact details.

    The contact block is deliberately absent. A phone number and an email
    address are the owner's personal data, they are rendered into the document
    from the database by :mod:`app.documents.render`, and there is no decision
    about them for a model to make — so they are not sent to one. That is not
    only prudence: everything in this prompt is material an injected instruction
    could try to have repeated back, and the smallest such surface is the one
    that carries nothing personal.
    """
    profile = context.profile
    lines: list[str] = []
    if profile.headline:
        lines.append(f"- Headline in the resume: {profile.headline}")
    if profile.seniority:
        lines.append(f"- Seniority: {profile.seniority}")
    if profile.total_years is not None:
        lines.append(f"- Total experience, computed from the dates: {profile.total_years:g} years")
    if profile.locations:
        lines.append(f"- Based in: {', '.join(profile.locations)}")
    if profile.languages:
        lines.append(f"- Languages: {', '.join(profile.languages)}")
    if profile.summary:
        lines.append(f"- Summary from the resume: {profile.summary}")
    return "\n".join(lines) if lines else "- (the profile records nothing but skills and jobs)"


def skills_block(context: CVContext) -> str:
    """Every skill the candidate has, with the depth the profile recorded.

    The complete list, and it is labelled as complete in the template, because
    the failure mode this feature has to avoid is a model deciding a nearby
    technology is close enough. Years and level are shown so the arrangement can
    lead with the strong ones — and they are shown as notes rather than as
    fields to copy, since the document prints them from the database.
    """
    if not context.profile.skills:
        return "(the profile records no skills)"
    lines: list[str] = []
    for skill in context.profile.skills:
        detail = ", ".join(
            part
            for part in (
                f"{skill.years:g} years" if skill.years is not None else None,
                skill.level or None,
                f'resume writes it "{skill.spelling}"'
                if skill.spelling.lower() != skill.canonical_name.lower()
                else None,
            )
            if part
        )
        lines.append(f"- {skill.canonical_name}{f' — {detail}' if detail else ''}")
    return "\n".join(lines)


def experience_block(context: CVContext) -> str:
    """Every job, by ref, with the stack the resume attributed to it.

    Company, title and dates are printed here so the model can judge relevance
    and order. They are *not* fields it fills in: the answer carries a ref, and
    the renderer reads these strings from the database. Showing them is what
    lets an arrangement be sensible; not accepting them back is what makes it
    honest.
    """
    if not context.experience:
        return "(the profile records no jobs)"
    lines: list[str] = []
    for entry in context.experience:
        period = "current" if entry.is_current else f"{entry.start or '?'}..{entry.end or '?'}"
        header = f"- ref {entry.ref}: {entry.title} at {entry.company} ({period})"
        lines.append(header)
        if entry.stack:
            lines.append(
                f"    stack, and the only technologies this job may show: {', '.join(entry.stack)}"
            )
        else:
            lines.append("    stack: none recorded, so this job shows no technologies")
        if entry.domains:
            lines.append(f"    domains: {', '.join(entry.domains)}")
    return "\n".join(lines)


def headlines_block(context: CVContext) -> str:
    """The closed set of strings the headline may be.

    Rendered as a list to copy from rather than described, because "choose one
    of the candidate's job titles" is an instruction a model can interpret and a
    list of four strings is one it can only obey. The check on the answer is
    equality against this same set.
    """
    if not context.allowed_headlines:
        return "(none — leave the headline empty)"
    return "\n".join(f"- {title}" for title in context.allowed_headlines)


def variables(context: CVContext, *, feedback: str = "") -> dict[str, str]:
    """Everything ``tailored_cv.md`` needs, and nothing it does not.

    ``app.llm.prompts.render`` refuses both a missing placeholder and an unused
    variable, so this dict and the template check each other on every call —
    which is why a test renders it rather than only inspecting it.
    """
    return {
        "cv_language": context.language,
        "max_summary_chars": str(MAX_SUMMARY_CHARS),
        "min_skills": str(MIN_SKILLS),
        "candidate_facts": candidate_block(context),
        "skills_block": skills_block(context),
        "experience_block": experience_block(context),
        "headlines_block": headlines_block(context),
        "vacancy_facts": vacancy_block(_as_letter_context(context)),
        "skill_overlap": overlap_block(_as_letter_context(context)),
        "fence_open": FENCE_OPEN,
        "fence_close": FENCE_CLOSE,
        "description": fenced_description(context),
        "feedback": feedback,
    }


def _as_letter_context(context: CVContext) -> LetterContext:
    """A letter context over the same facts, to reuse two rendering functions.

    ``vacancy_block`` and ``overlap_block`` render exactly the blocks a CV needs
    — the vacancy's own facts, and the computed intersection — and they already
    handle the awkward parts, such as an employer whose name reads as a web
    address. Copying them here would create two renderings of one thing that
    would eventually disagree, and the disagreement would show up as a CV and a
    letter describing the same overlap differently to the same employer.

    The conversion is free: both carry the same ``VacancyFacts``, the same
    ``ProfileFacts`` and the same ``SkillOverlap``, so this is a re-wrapping and
    not a second computation of anything.
    """
    return LetterContext(
        vacancy=context.vacancy,
        profile=context.profile,
        overlap=context.overlap,
        language=context.language,
    )
