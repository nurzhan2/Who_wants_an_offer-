"""Documents written for one vacancy: a tailored CV, and the letter beside it.

    contacts  the contact block, read back out of the stored resume
    context   what a CV may know: the profile's jobs and skills, the vacancy's
              requirements, and the two different sets between them
    prompt    filling in app/llm/prompts/tailored_cv.md, fencing untrusted text
    generator the model call, the checks on its answer, the rule-based fallback
    guard     what a generated CV may not contain, enforced by reading it
    rules     the hard rules in force, and the identity of that set
    render    the arrangement plus the database rows, as a .docx and as text
    review    the ATS audit of the produced file, and per-vacancy coverage
    store     the versioned rows in and the documents out
    service   the sequence, for either kind of document

**What "generating a CV" means here, exactly.** It means reordering the
candidate's skills and jobs so what this vacancy asks for comes first, choosing
which jobs and which of each job's technologies to show, spelling a skill the
way the vacancy spells it when it is the same skill, and fitting the result to
the length rules. It does not mean writing prose about what somebody did at a
job, because the profile does not record that and anything written there would
be invented.

That distinction is why the model returns an *arrangement* — references, orders,
names from a closed list — rather than a document. Company names, job titles,
dates, skill levels and years are read from the database when the file is built
and there is no field in the model's answer that could carry a different one. So
"the generator does not change dates, companies or titles" is a fact about the
schema rather than a rule someone checks, and what the checks in ``guard`` are
left to enforce is the narrower thing that can still go wrong: a skill that is
not the candidate's, a technology moved between employers, and the one free-text
field in the document.

Nothing in this package sends anything. A document is generated, audited against
this project's own ATS checks, stored as a new version, and handed to the person.
Sending an application is ``agent/``'s, from a browser, under the user's own
account, and only after a human has confirmed that particular application.
"""

from app.documents.contacts import ContactBlock, from_resume_text
from app.documents.context import (
    MAX_EXPERIENCE_ENTRIES,
    MAX_SUMMARY_CHARS,
    CVContext,
    EducationEntry,
    ExperienceEntry,
    SkillChoice,
    build_context,
)
from app.documents.generator import CVDraft, CVUnwritableError, GeneratedCV, generate
from app.documents.guard import MIN_SKILLS, CVProblem, unsupported_mentions
from app.documents.render import CVArrangement, to_docx, to_text
from app.documents.review import DocumentReview, RequirementCoverage, ReviewedDocument
from app.documents.service import DocumentOutcome, write_cover_letter, write_cv

__all__ = [
    "MAX_EXPERIENCE_ENTRIES",
    "MAX_SUMMARY_CHARS",
    "MIN_SKILLS",
    "CVArrangement",
    "CVContext",
    "CVDraft",
    "CVProblem",
    "CVUnwritableError",
    "ContactBlock",
    "DocumentOutcome",
    "DocumentReview",
    "EducationEntry",
    "ExperienceEntry",
    "GeneratedCV",
    "RequirementCoverage",
    "ReviewedDocument",
    "SkillChoice",
    "build_context",
    "from_resume_text",
    "generate",
    "to_docx",
    "to_text",
    "unsupported_mentions",
    "write_cover_letter",
    "write_cv",
]
