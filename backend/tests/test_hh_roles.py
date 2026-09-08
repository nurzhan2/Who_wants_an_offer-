"""The chain from a profile to the catalogue pages hh publishes for it.

Three steps, and each one is a place a mistake would be silent rather than
loud: a profile that matches no family still crawls, a family that names no
role still crawls, and a role that matches no slug still crawls. What changes
in every one of those cases is only which postings a short run spends its
budget on — which is exactly the failure this whole path exists to fix, so the
tests here are about the quiet cases as much as the working one.

Nothing here is a fixture of hh's live data. The role names are the shapes hh
uses — a comma-separated pair of synonyms, a Latin product name with a Russian
noun after it — because those shapes are what the matching has to survive, and
they were confirmed against the directory on 2026-09-08. The slugs are
plausible rather than measured, and the file says so: what these prove is that
the rules do what they say, and the live check is
``scripts/probe_hh_roles.py --keyword ...``, which runs this same code against
the real slug list and prints what each role found.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.exceptions import SourceError
from app.sources.hh_roles import (
    EXPERIENCE_WORDS,
    FOLD,
    GENERIC_ROLE_WORDS,
    TRANSLIT,
    DirectoryRole,
    RoleFamily,
    carries,
    families_for,
    fold,
    intent_words,
    load_families,
    read_directory,
    roles_for,
    slugs_for,
)

pytestmark = pytest.mark.unit

#: The Russian alphabet, as a transliteration table has to cover it.
ALPHABET = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"

DIRECTORY: dict[str, Any] = {
    "categories": [
        {
            "id": "11",
            "name": "Информационные технологии",
            "roles": [
                {"id": "96", "name": "Программист, разработчик"},
                {"id": "160", "name": "DevOps-инженер"},
                {"id": "11", "name": "Аналитик данных"},
                {"id": "124", "name": "Тестировщик"},
            ],
        },
        {
            "id": "3",
            "name": "Строительство",
            "roles": [{"id": "34", "name": "Инженер-проектировщик"}],
        },
    ]
}

#: Invented, and deliberately mixed: development, adjacent, and the professions
#: that a careless rule would sweep in with them.
SLUGS = (
    "programmist",
    "razrabotchik",
    "python-razrabotchik",
    "devops-inzhener",
    "analitik-dannyh",
    "testirovshchik",
    "inzhener-stroitel",
    "prorab",
    "buhgalter",
    "menedzher-po-prodazham",
    "uchitel-matematiki",
    "junior-python-developer",
)


# -- the tables --------------------------------------------------------


def test_the_transliteration_table_is_cyrillic_in_and_ascii_out() -> None:
    """The protection RUF001 gives, restored as an assertion.

    ``hh_roles.py`` switches that rule off, because a transliteration table IS a
    table of look-alikes and ruff is right about every one of its keys. What the
    rule would have caught is a Cyrillic letter hiding on the WRONG side — in a
    value, or in one of the Latin-only tables beside it — where it would never
    match anything and nothing would say so. That is checked here instead.
    """
    assert "".join(sorted(TRANSLIT)) == "".join(sorted(ALPHABET))
    for letter, latin in TRANSLIT.items():
        assert len(letter) == 1
        assert "Ѐ" <= letter <= "ӿ", f"{letter!r} is not a Cyrillic letter"
        assert latin.isascii(), f"{letter!r} transliterates to something non-ASCII"
        assert latin.islower() or latin == "", f"{letter!r} transliterates to {latin!r}"

    for before, after in FOLD:
        assert before.isascii() and after.isascii()
    for word in GENERIC_ROLE_WORDS:
        assert word.isascii(), f"{word!r} would never match a transliterated name"


@pytest.mark.parametrize(
    ("cyrillic", "expected"),
    [
        ("программист", "programmist"),
        ("разработчик", "razrabotchik"),
        ("аналитик", "analitik"),
        ("маркетолог", "marketolog"),
        ("инженер", "inzhener"),
    ],
)
def test_transliteration_produces_the_slugs_hh_writes(cyrillic: str, expected: str) -> None:
    """``marketolog`` and ``analitik`` are measured; the rest follow the same table."""
    assert fold(cyrillic) == expected


def test_two_honest_spellings_of_one_word_fold_together() -> None:
    """Nobody has to know which transliteration scheme hh used.

    ``щ`` is ``shch`` to one convention and ``sch`` to another, and a match that
    depended on picking right would fail on a whole family of professions.
    """
    assert fold("тестировщик") == fold("testirovshchik") == fold("testirovschik")


# -- matching terms ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "term", "expected"),
    [
        ("python-razrabotchik", "разработчик", True),
        ("python-razrabotchik", "python", True),
        ("html-verstalshchik", "ml", False),
        ("sql-razrabotchik", "sql", True),
        ("kredit-menedzher", "it", False),
    ],
)
def test_a_short_term_matches_a_word_and_a_long_one_matches_inside(
    name: str, term: str, expected: bool
) -> None:
    """``ml`` inside ``html`` is noise; ``разработчик`` inside a slug is not."""
    assert bool(carries(name, [term])) is expected


# -- profile to families -----------------------------------------------


def test_the_shipped_families_recognise_a_python_backend_profile() -> None:
    """Against ``hh_roles.yaml`` as it ships, not a fixture standing in for it.

    A config nothing exercises is a config that is wrong for a year. These are
    the keywords the planner produces from the owner's own resume, and the
    families they must pick out are the ones the brief asked for: broad, not
    "python".
    """
    keywords = ("python", "fastapi", "postgresql", "docker", "pytest", "llm")

    picked = {family.key for family in families_for(keywords, load_families())}

    assert {"backend", "devops", "qa", "ml"} <= picked


def test_a_profile_from_another_trade_is_given_no_families_at_all() -> None:
    """And not the developer ones as a default.

    Handing a bookkeeper the families in this file because the file happens to
    be written by developers would be the hardcoding the whole chain avoids. It
    still crawls; its own words still pick catalogue pages.
    """
    assert families_for(("бухучёт", "1С:Зарплата", "МСФО"), load_families()) == ()


def test_a_family_names_roles_by_what_hh_calls_them() -> None:
    """The directory decides which roles exist, not this repository."""
    families = (RoleFamily(key="dev", when=("python",), roles=("разработчик", "devops")),)

    roles = roles_for(families, read_directory(DIRECTORY))

    assert {role.name for role in roles} == {"Программист, разработчик", "DevOps-инженер"}


def test_a_category_id_and_a_role_id_do_not_collide() -> None:
    """They are separate numbering spaces, and this payload proves it matters.

    Category 11 is IT and role 11 is a data analyst. Reading both into one table
    by id loses whichever arrives second — and the one lost would be a
    development role, which is the only kind this exercise is about.
    """
    directory = read_directory(DIRECTORY)

    assert len(directory) == 5
    assert DirectoryRole(id=11, name="Аналитик данных", category="Информационные технологии") in (
        directory
    )


# -- roles to slugs ----------------------------------------------------


def test_a_role_finds_the_slug_that_is_one_of_its_synonyms() -> None:
    """«Программист, разработчик» is two professions in one name.

    ``/vacancies/programmist`` is the page the whole catalogue path was measured
    on, and a rule that required both halves of the name would not have found
    it.
    """
    roles = (DirectoryRole(id=96, name="Программист, разработчик"),)

    assert set(slugs_for(roles, (), SLUGS)) == {
        "programmist",
        "razrabotchik",
        "python-razrabotchik",
    }


def test_a_generic_word_in_a_role_name_does_not_claim_the_trade_that_owns_it() -> None:
    """«DevOps-инженер» must not bring in every ``inzhener-`` slug on the site.

    Almaty's catalogue has far more construction engineers than devops ones, and
    a role that matched on ``инженер`` alone would spend the run's catalogue
    budget on building sites. The distinctive half of the name is what may stand
    alone.
    """
    roles = (DirectoryRole(id=160, name="DevOps-инженер"),)

    picked = slugs_for(roles, (), SLUGS)

    assert picked == ("devops-inzhener",)
    assert "inzhener-stroitel" not in picked


def test_keywords_find_pages_no_role_named_and_outrank_the_ones_that_do() -> None:
    """The fallback that makes this work for a profile no family describes.

    And it is not a second-class one. A page carrying the candidate's own words
    beats a page that merely belongs to the right trade, because the crawl opens
    a handful of these and ``junior-python-developer`` is a better use of one
    than ``programmist`` is. Being named by hh's directory only breaks ties
    among pages that say nothing about this profile in particular.
    """
    roles = (DirectoryRole(id=96, name="Программист, разработчик"),)

    picked = slugs_for(roles, ("python", "developer"), SLUGS)

    assert picked[0] == "junior-python-developer"
    assert picked.index("python-razrabotchik") < picked.index("programmist")


def test_nothing_a_profile_did_not_ask_for_is_picked() -> None:
    """The corpus this replaces was sales managers and teachers, measured."""
    roles = roles_for(
        (RoleFamily(key="dev", when=("python",), roles=("разработчик",)),),
        read_directory(DIRECTORY),
    )

    picked = slugs_for(roles, ("python",), SLUGS)

    assert not {"buhgalter", "menedzher-po-prodazham", "uchitel-matematiki", "prorab"} & set(picked)


# -- the file itself ---------------------------------------------------


def test_the_shipped_config_is_wide_rather_than_one_language() -> None:
    """The brief's instruction, as an assertion.

    "Do not narrow it to Python" is the kind of requirement that survives review
    and then quietly loses to the next person tidying a config, so it is written
    down where a change has to argue with it.
    """
    families = load_families()

    assert {family.key for family in families} >= {"backend", "data", "ml", "devops", "qa"}
    backend = next(family for family in families if family.key == "backend")
    assert len(backend.when) > 10
    assert {"java", "golang", "php"} <= {term.casefold() for term in backend.when}


def test_a_config_that_does_not_parse_names_the_file(tmp_path: Path) -> None:
    """A crawl that silently loses its role config is a crawl of sales managers."""
    broken = tmp_path / "hh_roles.yaml"
    broken.write_text("families:\n  - when: [python]\n", encoding="utf-8")

    with pytest.raises(SourceError) as raised:
        load_families(broken)

    assert "hh_roles.yaml" in str(raised.value)


# -- the order the pages are opened in ---------------------------------


#: What role 96 «Программист, разработчик» really matched on almaty.hh.kz, in
#: the shapes that matter: the profile's own technology, the same trade at the
#: level the candidate is at, and the dozens of pages for other people's
#: technologies that share the word ``programmist``.
LIVE_SLUGS = (
    "programmist_1c",
    "programmist-1s-buhgalteriya",
    "programmist_1szup",
    "programmist-1c-82",
    "programmist-abap",
    "programmist-navision",
    "programmist-chpu",
    "programmist-asu-tp",
    "programmist",
    "python-razrabotchik",
    "backend-razrabotchik-python",
    "mladshij-programmist",
    "junior-programmist",
    "programmist_stazher",
    "nachinayushiy_programmist",
)


def test_the_profiles_own_technology_outranks_everybody_elses() -> None:
    """Measured, and the reason this ranking exists at all.

    Role 96 matches over a hundred slugs on the live site and dozens of them are
    1C, ABAP, Navision and CNC. A run opens eight. Sorted alphabetically all
    eight are 1C — and the crawl hands back the same irrelevant corpus it was
    rewritten to stop handing back, by a different route.
    """
    roles = (DirectoryRole(id=96, name="Программист, разработчик"),)
    families = families_for(("python",), load_families())

    picked = slugs_for(roles, ("python",), LIVE_SLUGS, families=families)

    assert picked[:2] == ("python-razrabotchik", "backend-razrabotchik-python")
    assert not any(slug.startswith("programmist_1") for slug in picked[:8])
    assert not any(slug.startswith("programmist-1") for slug in picked[:8])


def test_a_level_word_is_not_another_trade() -> None:
    """``junior`` and ``1c`` are both words the profile never said.

    One of them means the same job earlier in a career and the other means
    somebody else's job, and nothing but :data:`EXPERIENCE_WORDS` can tell them
    apart. There is no list of bad words anywhere in this module — such a list
    would be endless and out of date the week it was written.
    """
    roles = (DirectoryRole(id=96, name="Программист, разработчик"),)
    families = families_for(("python",), load_families())

    picked = slugs_for(roles, ("python",), LIVE_SLUGS, families=families)

    for level in ("mladshij-programmist", "junior-programmist", "programmist_stazher"):
        assert picked.index(level) < picked.index("programmist_1c"), level


def test_hh_spells_the_same_level_word_two_ways_and_both_are_recognised() -> None:
    """``mladshij`` and ``mladshiy`` are one profession, and so are three ways of ``щ``.

    A fold that left them apart would leave ``nachinayushiy_programmist`` looking
    like a page about a technology nobody here has heard of, and rank it with the
    ABAP pages.
    """
    assert fold("mladshij-programmist") == fold("mladshiy-programmist")
    assert fold("начинающий") == fold("nachinayushiy") == fold("nachinayuschiy")


def test_every_word_table_is_stored_folded() -> None:
    """A word left unfolded here is a word that silently never matches anything.

    ``veduschiy`` is the example: the fold turns it into ``veduschy``, so an
    entry written the first way sits in the table looking correct and matching
    nothing for as long as nobody checks.
    """
    for word in EXPERIENCE_WORDS | GENERIC_ROLE_WORDS:
        assert word == fold(word), f"{word!r} is not stored in its folded form"


# -- intent, which is not the same thing as skill ----------------------


#: The owner's, verbatim. Everything below is what it has to survive.
HEADLINE = "Python Developer — Backend / AI-интеграции"

#: The sixteen keywords the planner produced from a resume that lists Java, Go,
#: JavaScript and C beside Python — all of them true, all of them equal.
FLAT_KEYWORDS = (
    "python",
    "java",
    "go",
    "javascript",
    "c",
    "c#",
    "linux",
    "docker",
    "git",
    "sql",
    "postgresql",
    "fastapi",
    "pytest",
    "react",
    "bash",
    "nginx",
)

POLYGLOT_SLUGS = (
    "go-razrabotchik",
    "c-razrabotchik",
    "javascript-razrabotchik",
    "razrabotchik-c-sharp",
    "linux-administrator",
    "java-razrabotchik",
    "testirovshchik",
    "python-razrabotchik",
    "backend-razrabotchik-python",
    "backend-razrabotchik",
    "programmist",
    "razrabotchik-integraciy",
)


def _polyglot_roles() -> tuple[DirectoryRole, ...]:
    """The roles a Python backend profile selects out of hh's directory."""
    return read_directory(DIRECTORY)


def test_a_flat_keyword_list_cannot_say_which_language_is_wanted() -> None:
    """The failure this weight was added for, kept as the control.

    Measured on the live run of 2026-09-08: ranked by keywords alone the crawl
    opened Go, C, JavaScript, Linux and C# pages and not one Python page. Every
    one of those words is genuinely on the resume, which is exactly why the
    keyword list cannot arbitrate between them — and why deleting the headline
    weight has to fail a test rather than quietly restore that run.
    """
    families = families_for(FLAT_KEYWORDS, load_families())

    picked = slugs_for(_polyglot_roles(), FLAT_KEYWORDS, POLYGLOT_SLUGS, families=families)

    assert "python" not in picked[0]


def test_the_headline_outranks_every_skill_the_resume_happens_to_list() -> None:
    """What the candidate says they are beats what they can also do.

    ``python`` and ``linux`` are both on this resume. Only one of them is in the
    headline, and a run opens eight pages.
    """
    families = families_for(FLAT_KEYWORDS, load_families())

    picked = slugs_for(
        _polyglot_roles(), FLAT_KEYWORDS, POLYGLOT_SLUGS, families=families, headline=HEADLINE
    )

    assert all("python" in slug or "backend" in slug for slug in picked[:3])
    for other in ("linux-administrator", "razrabotchik-c-sharp", "javascript-razrabotchik"):
        assert picked.index("python-razrabotchik") < picked.index(other), other


def test_the_family_the_headline_named_outranks_the_one_a_skill_dragged_in() -> None:
    """``qa`` got in on ``pytest``; ``backend`` is what the headline says.

    Both are crawled — the brief asks for breadth and a QA-automation posting is
    in scope — but a backend page is bought before a testing one, and that is the
    difference between the two weights rather than a filter.
    """
    families = families_for(FLAT_KEYWORDS, load_families())

    picked = slugs_for(
        _polyglot_roles(), FLAT_KEYWORDS, POLYGLOT_SLUGS, families=families, headline=HEADLINE
    )

    assert "testirovshchik" in picked
    assert picked.index("backend-razrabotchik") < picked.index("testirovshchik")


def test_the_headline_finds_its_own_subject_in_another_grammatical_form() -> None:
    """«AI-интеграции» in a resume, ``razrabotchik-integraciy`` on hh.

    Russian inflects and the two do not inflect the same way, so an exact match
    would read the profile's own subject as somebody else's technology and rank
    it with the ABAP pages.
    """
    families = families_for(FLAT_KEYWORDS, load_families())

    picked = slugs_for(
        _polyglot_roles(), FLAT_KEYWORDS, POLYGLOT_SLUGS, families=families, headline=HEADLINE
    )

    assert picked.index("razrabotchik-integraciy") < picked.index("go-razrabotchik")


def test_a_rank_word_in_a_headline_is_not_an_intent_word() -> None:
    """«Ведущий инженер-программист» means ``programmist``.

    Letting ``inzhener`` carry the heaviest weight in the ranking would hand
    every construction page on the site the top of the queue.
    """
    assert intent_words("Ведущий инженер-программист") == frozenset({"programmist"})
    assert intent_words(None) == frozenset()


def test_the_focus_weight_changes_direction_with_the_headline() -> None:
    """Both halves of one key, because they pull opposite ways and both are right.

    A page that already names what the candidate asked for is worse for naming
    somebody else's language too. A page that names none of it is better for
    naming their kind of work than for naming a trade they never mentioned.
    """
    roles = (DirectoryRole(id=96, name="Программист, разработчик"),)
    families = families_for(FLAT_KEYWORDS, load_families())
    named = ("python-developer", "java-backend-developer")
    unnamed = ("go-razrabotchik", "linux-administrator")

    picked = slugs_for(roles, FLAT_KEYWORDS, named + unnamed, families=families, headline=HEADLINE)

    assert picked.index("python-developer") < picked.index("java-backend-developer")
    assert picked.index("go-razrabotchik") < picked.index("linux-administrator")


# -- against the measurement itself ------------------------------------


MEASUREMENT = Path(__file__).resolve().parents[2] / "docs" / "hh-probe-plan.json"


def _measured_slugs() -> tuple[tuple[str, ...], tuple[DirectoryRole, ...]]:
    """The live slug list and role set, out of the committed measurement."""
    payload = json.loads(MEASUREMENT.read_text(encoding="utf-8"))
    slugs = tuple(payload["index"]["slugs"])
    roles = tuple(
        DirectoryRole(id=role["id"], name=role["name"]) for role in payload["plan"]["roles"]
    )
    return slugs, roles


def test_the_ranking_puts_python_first_on_the_slugs_hh_really_publishes() -> None:
    """The whole feature, against 10 435 real slugs rather than a dozen invented ones.

    ``docs/hh-probe-plan.json`` is the measurement the catalogue path rests on,
    and it is in the repository precisely so this can be asserted without a
    network: every slug here is one hh published on 2026-09-08, including the
    198 that role 96 matches and the JavaScript pages that took the top of the
    list on the live run.
    """
    slugs, roles = _measured_slugs()
    families = families_for(FLAT_KEYWORDS, load_families())

    picked = slugs_for(roles, FLAT_KEYWORDS, slugs, families=families, headline=HEADLINE)

    # The four the crawl re-reads on every run. Nothing else in this file is
    # worth as much as these four being right.
    assert all("python" in slug for slug in picked[:4])
    assert picked[0] == "python-backend-developer"


def test_without_the_headline_the_same_real_list_leads_with_javascript() -> None:
    """The live run of 2026-09-08, reproduced from the data it was measured on.

    It opened JavaScript, Go, C, Linux and C# pages and no Python one. This is
    that run, and it is here so that losing the headline weight fails a test
    instead of quietly costing a week of the wrong corpus.
    """
    slugs, roles = _measured_slugs()
    families = families_for(FLAT_KEYWORDS, load_families())

    picked = slugs_for(roles, FLAT_KEYWORDS, slugs, families=families)

    assert "python" not in picked[0]
    assert not any("python" in slug for slug in picked[:5])
