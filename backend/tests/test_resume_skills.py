"""Skill canonicalisation, and the dictionary it reads.

Two failures live here and both are silent. The first: a resume says
"PostgreSQL", a vacancy says "Postgres", and the matcher compares raw strings —
the candidate loses points for a skill they have. The second is worse, because
it survives every unit test of the matcher: the *dictionary* contradicts
itself. One spelling claimed by two skills canonicalises by the order of the
YAML file, so a harmless-looking edit silently re-points every resume that
mentions it. That is why half of this file tests data rather than code.

No database, no model, no network: parsing a bundled YAML file and looking
things up in dicts.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.resume.skills import (
    DEFAULT_DICTIONARY,
    SkillDictionaryError,
    YamlSkillCanonicalizer,
    default_canonicalizer,
    normalize,
)

pytestmark = pytest.mark.unit

#: The shipped dictionary, read straight from disk rather than through the
#: loader. The loader already rejects several of the shapes asserted below, so
#: going through it would make those assertions test the loader twice and the
#: data not at all.
RAW: dict[str, Any] = yaml.safe_load(DEFAULT_DICTIONARY.read_text(encoding="utf-8"))

#: Every (alias, owning canonical name) pair declared in the file.
ALIAS_OWNERS: list[tuple[str, str]] = [
    (alias, canonical) for canonical, body in RAW.items() for alias in (body.get("aliases") or [])
]


@pytest.fixture
def skills() -> YamlSkillCanonicalizer:
    """A canonicaliser over the bundled dictionary, unshared between tests."""
    return YamlSkillCanonicalizer()


def write_dictionary(tmp_path: Path, body: str, name: str = "skills.yaml") -> Path:
    """Put a dictionary of our own on disk, never touching the shipped one."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ── canonicalisation ──────────────────────────────────────────────────


@pytest.mark.parametrize("canonical", ["python", "postgresql", "docker", "machine learning"])
def test_a_canonical_name_maps_to_itself(skills: YamlSkillCanonicalizer, canonical: str) -> None:
    """Canonicalisation has to be idempotent: profiles are stored canonicalised
    and re-canonicalised on every re-match, so a name that moved on the second
    pass would drift a little further on every run."""
    assert skills.canonicalize(canonical) == canonical


def test_every_canonical_name_in_the_dictionary_maps_to_itself(
    skills: YamlSkillCanonicalizer,
) -> None:
    """The same guarantee across the whole file, so a future entry cannot be
    added in a shape that only its own aliases can reach."""
    drifted = {name: skills.canonicalize(name) for name in RAW}
    assert {name: got for name, got in drifted.items() if got != name} == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Postgres", "postgresql"),
        ("POSTGRES", "postgresql"),
        ("  postgres  ", "postgresql"),
        ("\tK8s\n", "kubernetes"),
        ("PyTest", "pytest"),
        ("Spring Boot", "spring"),
        ("Amazon Web Services", "aws"),
    ],
)
def test_aliases_resolve_whatever_the_case_and_padding(
    skills: YamlSkillCanonicalizer, raw: str, expected: str
) -> None:
    """Skills arrive from a PDF bullet list and an HTML vacancy body. Leading
    whitespace and title case are properties of the layout, not of the skill,
    and must not decide whether two documents mention the same thing."""
    assert skills.canonicalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("питон", "python"),
        ("Питон", "python"),
        ("  ПИТОН  ", "python"),
        ("кубер", "kubernetes"),
        ("Машинное обучение", "machine learning"),
        ("вёрстка", "html"),
        ("микросервисная архитектура", "microservices"),
    ],
)
def test_russian_spellings_resolve_to_the_english_canonical_name(
    skills: YamlSkillCanonicalizer, raw: str, expected: str
) -> None:
    """hh.ru resumes and postings are written in Russian. If the Cyrillic
    spellings did not fold onto the English canonical names, the entire local
    market would score as having no overlap with itself."""
    assert skills.canonicalize(raw) == expected


def test_every_alias_in_the_dictionary_reaches_its_owner(
    skills: YamlSkillCanonicalizer,
) -> None:
    """An alias nobody can reach is worse than a missing one: it looks like
    coverage in review and provides none at match time."""
    unreachable = [
        (alias, owner) for alias, owner in ALIAS_OWNERS if skills.canonicalize(alias) != owner
    ]
    assert unreachable == []


def test_every_alias_still_reaches_its_owner_when_shouted(
    skills: YamlSkillCanonicalizer,
) -> None:
    """Aliases are matched exactly first and only then by the normalised fold.
    An alias that resolves lower case but not upper would mean the fold has a
    hole exactly where a section heading ("PYTHON") lands."""
    unreachable = [
        (alias, owner)
        for alias, owner in ALIAS_OWNERS
        if skills.canonicalize(f"  {alias.upper()}  ") != owner
    ]
    assert unreachable == []


@pytest.mark.parametrize("raw", ["nodejs", "Node.js", "node js", "NODEJS", "node-js", "node_js"])
def test_punctuation_between_words_never_changes_the_skill(
    skills: YamlSkillCanonicalizer, raw: str
) -> None:
    """The dot, the space, the hyphen and the underscore in "Node.js" are
    typography. Six spellings of one runtime must not become six skills, and
    listing all six as aliases is the maintenance burden normalisation exists
    to remove."""
    assert skills.canonicalize(raw) == "nodejs"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Scikit Learn", "scikit-learn"),
        ("scikitlearn", "scikit-learn"),
        ("Machine-Learning", "machine learning"),
        ("machinelearning", "machine learning"),
        ("DockerCompose", "docker compose"),
        ("GitHub-Actions", "github actions"),
        ("power_bi", "power bi"),
    ],
)
def test_a_multi_word_canonical_name_is_reached_however_its_words_are_joined(
    skills: YamlSkillCanonicalizer, raw: str, expected: str
) -> None:
    """Six entries in the file are two words long, and a word boundary is
    written four different ways in the wild: "scikit-learn", "scikit learn",
    "ScikitLearn", "power_bi". A canonical name that itself contains a
    separator is where the fold is easiest to break, because the entry has to
    be reachable both by its own spelling and by every joining of its words —
    and none of these spellings is listed as an alias to fall back on."""
    assert skills.canonicalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "quantum alchemy",
        "Senior Software Engineer",
        "python3000",
        "postgre",
        "nodejsx",
        "опыт работы в команде",
    ],
)
def test_an_unknown_spelling_returns_none_rather_than_a_guess(
    skills: YamlSkillCanonicalizer, raw: str
) -> None:
    """The one behaviour the module docstring promises. A near miss must not be
    rounded to the nearest entry: inventing a canonical name here would put a
    skill the candidate never claimed into their profile, and the caller — not
    this module — decides whether to keep the raw text, queue it, or drop it."""
    assert skills.canonicalize(raw) is None


@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
def test_blank_input_is_unknown_even_when_a_spelling_folds_to_nothing(
    tmp_path: Path, raw: str
) -> None:
    """Empty cells and stray bullet characters come out of PDF extraction all
    the time, and they have to be refused before the dictionary is consulted.
    A spelling made of nothing but separators folds to the empty key, so a
    blank line that reached the fold would inherit whatever owns that key and
    put a skill nobody wrote into a candidate's profile. The trap below is
    armed first, so this fails if the blank check is dropped."""
    path = write_dictionary(tmp_path, 'python:\n  group: language\n  aliases: ["--"]\n')
    canonicalizer = YamlSkillCanonicalizer(path)
    assert canonicalizer.canonicalize("--") == "python"

    assert canonicalizer.canonicalize(raw) is None


# ── groups ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("canonical", "group"),
    [
        ("python", "language"),
        ("nodejs", "backend"),
        ("postgresql", "database"),
        ("kafka", "queue"),
        ("github actions", "ci"),
    ],
)
def test_group_of_returns_the_group_declared_in_the_file(
    skills: YamlSkillCanonicalizer, canonical: str, group: str
) -> None:
    """The group is the coarse relatedness signal the matcher falls back on when
    two skills are not equal — a Django role should not read as a total miss for
    a Flask candidate. A wrong group is a wrong score."""
    assert skills.group_of(canonical) == group


def test_group_of_an_unknown_name_is_none(skills: YamlSkillCanonicalizer) -> None:
    """No group is a fact the caller can act on; a default group would quietly
    make every unrecognised skill related to every other one."""
    assert skills.group_of("quantum alchemy") is None


def test_group_of_takes_a_canonical_name_and_not_a_raw_spelling(
    skills: YamlSkillCanonicalizer,
) -> None:
    """Documented contract worth pinning: group_of consumes the *output* of
    canonicalize. Making it canonicalise on the way in would hide the caller's
    mistake of never having canonicalised at all."""
    assert skills.canonicalize("Postgres") == "postgresql"
    assert skills.group_of("Postgres") is None


# ── the dictionary itself is part of the contract ─────────────────────


def test_the_dictionary_has_at_least_sixty_entries() -> None:
    """A floor, not a target: the number only ever grows. It exists so that a
    botched merge or a truncated file fails here rather than showing up as
    every candidate suddenly matching nothing."""
    assert len(RAW) >= 60


def test_every_entry_declares_a_tidy_group() -> None:
    """Groups are compared to each other, so casing and stray padding in the
    YAML would split one group into two that never match."""
    untidy = {
        name: body.get("group")
        for name, body in RAW.items()
        if not isinstance(body.get("group"), str)
        or body["group"] != body["group"].strip().casefold()
        or not body["group"]
    }
    assert untidy == {}


def test_canonical_names_are_lower_case_and_carry_no_stray_padding() -> None:
    """Canonical names are written into the profile and compared as strings by
    everything downstream. "Python" and "python" as two entries would be two
    skills, and a trailing space is invisible in review."""
    untidy = [
        name
        for name in RAW
        if name != name.casefold() or name != name.strip() or "  " in name or "\t" in name
    ]
    assert untidy == []


def test_no_alias_is_claimed_by_two_different_skills() -> None:
    """THE data test. Two entries listing the same alias makes canonicalisation
    depend on the order of the file: reordering the YAML, or PyYAML changing how
    it preserves order, silently re-points that spelling at another skill."""
    owners: dict[str, list[str]] = {}
    for alias, canonical in ALIAS_OWNERS:
        owners.setdefault(alias, []).append(canonical)
    assert {alias: names for alias, names in owners.items() if len(names) > 1} == {}


def test_no_alias_repeats_another_entrys_canonical_name() -> None:
    """The same collision wearing a different hat. The exact-canonical lookup
    runs before the alias lookup, so such an alias is dead text that reads as
    working coverage."""
    canonicals = set(RAW)
    stolen = {
        alias: owner for alias, owner in ALIAS_OWNERS if alias in canonicals and alias != owner
    }
    assert stolen == {}


def test_no_two_spellings_collide_once_normalisation_has_run() -> None:
    """Uniqueness has to hold after the fold, not before it: "scikit-learn" and
    "scikit learn" are distinct YAML keys and the same normalised key. Checking
    the raw strings only would let that pair through."""
    owners: dict[str, list[str]] = {}
    for canonical in RAW:
        owners.setdefault(normalize(canonical), []).append(canonical)
    for alias, canonical in ALIAS_OWNERS:
        owners.setdefault(normalize(alias), []).append(canonical)
    collisions = {key: names for key, names in owners.items() if len(set(names)) > 1}
    assert collisions == {}


# ── loading: lazy, cached, and redirectable ───────────────────────────


def test_the_file_is_not_read_until_the_first_lookup(tmp_path: Path) -> None:
    """Laziness is a documented property, and constructing a canonicaliser for a
    file that does not exist yet proves it: an eager read would raise here,
    before the dictionary is ever written."""
    path = tmp_path / "written-later.yaml"
    canonicalizer = YamlSkillCanonicalizer(path)

    write_dictionary(tmp_path, "rust:\n  group: language\n", name="written-later.yaml")

    assert canonicalizer.canonicalize("rust") == "rust"


def test_the_parsed_dictionary_is_kept_after_the_first_lookup(tmp_path: Path) -> None:
    """Callers canonicalise inside a per-skill loop over every vacancy in a run.
    Re-reading and re-parsing the YAML on each call would be invisible in tests
    and very visible in a pipeline; changing the file underneath is the only way
    to observe that it does not."""
    path = write_dictionary(tmp_path, "rust:\n  group: language\n")
    canonicalizer = YamlSkillCanonicalizer(path)
    assert canonicalizer.canonicalize("rust") == "rust"

    path.write_text("cobol:\n  group: language\n", encoding="utf-8")

    assert canonicalizer.canonicalize("rust") == "rust"
    assert canonicalizer.canonicalize("cobol") is None


def test_an_explicit_path_replaces_the_bundled_dictionary(tmp_path: Path) -> None:
    """The path argument is how phase 4 swaps the data without touching a call
    site. If the bundled file leaked in, a test dictionary would be a superset
    of production data instead of a replacement for it."""
    path = write_dictionary(tmp_path, "brainfuck:\n  group: esolang\n  aliases: [bf]\n")
    canonicalizer = YamlSkillCanonicalizer(path)

    assert canonicalizer.canonicalize("BF") == "brainfuck"
    assert canonicalizer.group_of("brainfuck") == "esolang"
    assert canonicalizer.canonicalize("python") is None


def test_the_default_canonicalizer_is_a_single_shared_instance() -> None:
    """A new instance per call would mean a fresh parse of the YAML per call
    site, which is exactly the cost the cache on this function exists to avoid."""
    assert default_canonicalizer() is default_canonicalizer()


def test_the_default_canonicalizer_reads_the_bundled_dictionary() -> None:
    """The singleton is the entry point everything else uses; pointing it at the
    wrong file would be caught by nothing above."""
    assert default_canonicalizer().canonicalize("Postgres") == "postgresql"


# ── a malformed dictionary fails loudly ───────────────────────────────


def test_an_alias_claimed_twice_is_refused_and_both_entries_are_named(tmp_path: Path) -> None:
    """The failure mode the whole design is built around. Refusing the file is
    only half of it: whoever added the second entry needs the message to say
    which two skills collide, or they are diffing a hundred-line YAML by eye."""
    path = write_dictionary(
        tmp_path,
        """
python:
  group: language
  aliases: [py]
pyton:
  group: language
  aliases: [py]
""",
    )

    with pytest.raises(SkillDictionaryError) as error:
        YamlSkillCanonicalizer(path).canonicalize("python")

    message = str(error.value)
    assert "py" in message
    assert "python" in message
    assert "pyton" in message


def test_an_alias_repeating_another_canonical_name_is_refused(tmp_path: Path) -> None:
    """Same contradiction, expressed differently in the file: without the
    canonical names being folded into the same table as the aliases, this one
    would load fine and the alias would simply never fire."""
    path = write_dictionary(
        tmp_path, "go:\n  group: language\ngolang:\n  group: language\n  aliases: [go]\n"
    )

    with pytest.raises(SkillDictionaryError) as error:
        YamlSkillCanonicalizer(path).canonicalize("go")

    assert "golang" in str(error.value)


def test_an_entry_without_a_group_is_refused_by_name(tmp_path: Path) -> None:
    """group_of is part of the protocol, so an entry with no group is a hole in
    the contract rather than a missing nicety."""
    path = write_dictionary(tmp_path, "rust:\n  aliases: [раст]\n")

    with pytest.raises(SkillDictionaryError, match="rust"):
        YamlSkillCanonicalizer(path).canonicalize("rust")


def test_a_yaml_syntax_error_arrives_as_a_dictionary_error(tmp_path: Path) -> None:
    """A caller guarding the load catches SkillDictionaryError. If PyYAML's own
    exception escaped, that guard would cover half the failures and the other
    half would take down the pipeline."""
    path = write_dictionary(tmp_path, "python:\n  group: language\n aliases: [py]\n")

    with pytest.raises(SkillDictionaryError, match="not valid YAML"):
        YamlSkillCanonicalizer(path).canonicalize("python")


def test_a_dictionary_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    """A YAML list of skill names is the obvious wrong guess at this file's
    shape; it must fail on load rather than half-work."""
    path = write_dictionary(tmp_path, "- python\n- go\n")

    with pytest.raises(SkillDictionaryError, match="mapping"):
        YamlSkillCanonicalizer(path).canonicalize("python")
