"""Reading a document against one vacancy's requirement list.

Three things are defended here, and the third is the one the feature exists for.

**That the match is literal.** An employer's keyword filter searches for the
string the posting carries. If this module were clever about synonyms it would
report a resume as covered by a filter that is about to drop it, which is a
worse answer than no answer: the candidate stops editing.

**That literal does not mean brittle.** ``C++`` has to match ``C++``, ``Go``
must not match ``Google``, and a line break between two words of one requirement
must not turn a hit into a miss. Every one of those is a real spelling in this
market's postings.

**That the three buckets stay three.** "Held but not named here" and "not held"
are the difference between a document to rewrite and a skill to learn, and
collapsing them is the difference between an audit and an instruction to lie.
The tests below assert that the second bucket carries its evidence and the third
carries no suggestion at all.
"""

import pytest

from app.db.enums import RequirementSource
from app.resume.ats_keywords import HeldSkill, literal_pattern, match_requirements
from app.schemas.ats import KeywordStatus

pytestmark = pytest.mark.unit

#: The candidate, as the profile records them: canonical key plus the spellings
#: they actually wrote. The evidence for calling anything "unstated".
HELD = (
    HeldSkill(canonical_name="python", spellings=("Python", "Python 3")),
    HeldSkill(canonical_name="postgresql", spellings=("PostgreSQL", "Postgres")),
    HeldSkill(canonical_name="docker", spellings=("Docker",)),
)


def statuses(text: str, requirements: tuple[str, ...]) -> dict[str, KeywordStatus]:
    """The verdict per requirement, for the tests that only care about that."""
    keywords = match_requirements(text, requirements, HELD)
    return {item.requirement: item.status for item in keywords.requirements}


def test_a_requirement_spelled_the_posting_s_way_is_present() -> None:
    """The whole point: the filter finds this one."""
    verdicts = statuses("Опыт: Python, PostgreSQL, Docker.", ("Python", "PostgreSQL", "Docker"))

    assert set(verdicts.values()) == {KeywordStatus.PRESENT}


def test_a_skill_the_candidate_has_but_this_variant_calls_something_else() -> None:
    """«постгрес» is PostgreSQL to a person and to nobody else in the pipeline.

    This is the bucket the feature exists for. The document says the thing; it
    says it in words the filter does not search for, so the finding is a line to
    rewrite rather than a skill to acquire — and both spellings are reported, so
    the candidate can see exactly what to change.
    """
    keywords = match_requirements("Стек: постгрес, python, docker.", ("PostgreSQL",), HELD)
    (item,) = keywords.requirements

    assert item.status is KeywordStatus.UNSTATED
    assert item.found_as is not None, "the document's own spelling is the actionable half"
    assert item.held_as == "PostgreSQL", "the profile is the evidence that this invents nothing"


def test_a_skill_held_and_simply_not_mentioned_is_also_unstated() -> None:
    """No spelling in the document at all, but the profile has it."""
    keywords = match_requirements("Пишу на Python.", ("Docker",), HELD)
    (item,) = keywords.requirements

    assert item.status is KeywordStatus.UNSTATED
    assert item.found_as is None
    assert item.held_as == "Docker"


def test_a_requirement_nobody_holds_is_absent_and_carries_no_suggestion() -> None:
    """The boundary, in the data rather than in the wording.

    Nothing on an ``absent`` requirement can be used to write it into a document:
    there is no held spelling, because there is no held skill. An audit that
    filled this field in would be handing over the sentence to paste.
    """
    keywords = match_requirements("Пишу на Python.", ("Kubernetes",), HELD)
    (item,) = keywords.requirements

    assert item.status is KeywordStatus.ABSENT
    assert item.held_as is None
    assert item.found_as is None


def test_the_three_buckets_are_reported_separately() -> None:
    """One list in, three answers out, and the report keeps them apart."""
    keywords = match_requirements(
        "Стек: Python, постгрес.",
        ("Python", "PostgreSQL", "Kubernetes"),
        HELD,
    )

    assert [item.requirement for item in keywords.present] == ["Python"]
    assert [item.requirement for item in keywords.unstated] == ["PostgreSQL"]
    assert [item.requirement for item in keywords.absent] == ["Kubernetes"]
    # Rounded on the model, so the JSON carries a share rather than a float
    # nobody asked for; compared at the precision it is actually stored to.
    assert keywords.literal_coverage == pytest.approx(1 / 3, abs=1e-4)


def test_coverage_of_a_vacancy_that_lists_nothing_is_not_zero() -> None:
    """A posting cannot be failed on requirements it never stated."""
    assert match_requirements("что угодно", (), HELD).literal_coverage == 1.0


@pytest.mark.parametrize(
    ("requirement", "text", "found"),
    [
        # Case is folded; every filter worth modelling folds it.
        ("PostgreSQL", "стек: postgresql, redis", True),
        # The punctuation between a requirement's own words is not load-bearing.
        ("CI/CD", "настраивал CI / CD в GitLab", True),
        # Punctuation *inside* one token is part of it: a filter searching for
        # "Node.js" does not find "node js", and the audit is supposed to be as
        # dumb as the filter. The fold catches this case as `unstated` and tells
        # the candidate which spelling to change — see the test above.
        ("Node.js", "писал на node js", False),
        ("Node.js", "писал на Node.js", True),
        # A line break inside a phrase is a layout accident, not a miss.
        ("Machine Learning", "Machine\nLearning инженер", True),
        # ...but the words themselves are. A missing one is a miss.
        ("Machine Learning", "Machine инженер", False),
        # The names this market writes, which \b gets wrong.
        ("C++", "C++ разработчик", True),
        ("C++", "писал на C и на Java", False),
        ("Go", "Google Analytics", False),
        ("Go", "сервисы на Go", True),
        ("SQL", "PostgreSQL и MySQL", False),
    ],
)
def test_literal_matching_is_literal_without_being_brittle(
    requirement: str, text: str, found: bool
) -> None:
    """Every row is a spelling that appears in real postings and real resumes."""
    pattern = literal_pattern(requirement)
    assert pattern is not None
    assert (pattern.search(text) is not None) is found


def test_a_sentence_typed_into_the_skills_field_is_not_searched_for() -> None:
    """Some employers type prose into ``keySkills``.

    Looking for it literally would always fail and always report the same
    unfixable gap, so it is not looked for as a phrase at all.
    """
    long_one = "умение работать в команде и брать на себя ответственность за результат"
    assert literal_pattern(long_one) is None


def test_two_spellings_of_one_requirement_are_one_gap() -> None:
    """hh postings do list «PostgreSQL» and «Postgres» side by side.

    Reporting both would double-count one fact, and on a card that reads as a
    candidate missing twice as much as they do.
    """
    keywords = match_requirements("Пишу на Python.", ("PostgreSQL", "Postgres"), HELD)

    assert len(keywords.requirements) == 1


def test_requirement_order_follows_the_posting() -> None:
    """A candidate reading this is looking at the employer's priorities."""
    asked = ("Docker", "Python", "Kubernetes")
    keywords = match_requirements("Python, Docker", asked, HELD)

    assert [item.requirement for item in keywords.requirements] == list(asked)


def test_hardness_travels_with_the_requirement() -> None:
    """Nice-to-haves are reported, at their own weight."""
    keywords = match_requirements("Python", ("Python", "Kubernetes"), HELD, required=[True, False])

    assert [item.is_required for item in keywords.requirements] == [True, False]


def test_hardness_defaults_to_required_when_the_source_does_not_say() -> None:
    """Better to report a vacancy as asking for everything than for nothing."""
    keywords = match_requirements("Python", ("Python", "Kubernetes"), HELD)

    assert all(item.is_required for item in keywords.requirements)


def test_where_a_requirement_came_from_travels_with_it() -> None:
    """The report's third state, and it is about the vacancy rather than the CV.

    ``present`` / ``unstated`` / ``absent`` all answer "what does this document
    do with this requirement". None of them answers "did anybody actually ask
    for it", and a candidate rewriting a CV around a requirement inferred from
    prose is entitled to that answer.
    """
    keywords = match_requirements(
        "Python",
        ("Python", "Kubernetes"),
        HELD,
        sources=[RequirementSource.EMPLOYER_FIELD, RequirementSource.DESCRIPTION_TEXT],
    )

    assert [item.source for item in keywords.requirements] == [
        RequirementSource.EMPLOYER_FIELD,
        RequirementSource.DESCRIPTION_TEXT,
    ]


def test_provenance_defaults_to_the_employer_when_the_caller_does_not_say() -> None:
    """A caller passing a structured list is a caller reading the employer's field.

    Defaulting the other way would relabel every requirement in the queue card
    — which reads ``key_skills`` straight out of the payload — as an inference.
    """
    keywords = match_requirements("Python", ("Python",), HELD)

    assert keywords.requirements[0].source is RequirementSource.EMPLOYER_FIELD


def test_a_document_naming_a_gap_does_not_thereby_cover_it() -> None:
    """The case that set the precedence, and it came from a real letter.

    The project's own fallback cover letter names the requirements the candidate
    does not meet — deliberately, because hiding them is lying to an employer.
    That puts the word in the text, so a keyword filter matches it. A reading
    that took the match at face value would report the requirement as covered by
    somebody who has just written that they do not have it.

    So "not in the profile" outranks a literal hit. The hit is still recorded,
    because "the filter will find this word and there is nothing behind it" is a
    true and useful thing to be able to say.
    """
    letter = "Работал с Python. С Kubernetes не работал — готов разбираться."
    keywords = match_requirements(letter, ("Python", "Kubernetes"), HELD)
    by_name = {item.requirement: item for item in keywords.requirements}

    assert by_name["Python"].status is KeywordStatus.PRESENT
    assert by_name["Kubernetes"].status is KeywordStatus.ABSENT
    assert by_name["Kubernetes"].found_as == "Kubernetes"
    assert by_name["Kubernetes"].held_as is None
    assert keywords.literal_coverage == 0.5
