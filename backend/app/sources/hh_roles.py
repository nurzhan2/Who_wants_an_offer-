"""Which hh professional roles a profile asks for, and which catalogue pages that is.

**The measurement this rests on** (2026-09-08, ``scripts/probe_hh_roles.py``, see
docs/SOURCES.md § «Обход по профессиям»). ``almaty.hh.kz`` publishes 15
``vacancies{N}.xml`` files holding 10 435 catalogue slugs;
``/vacancies/programmist`` carries the frontend's boot state with 50 vacancy ids
on it, both in the markup and in the state. Paging exists only as ``?page=0..3``
— under hh's ``Disallow: *?*`` and therefore closed to us — so depth comes from
the breadth of the slug set rather than from pagination, and the duplicates that
breadth produces are collapsed by the fingerprint that already runs.
``api.hh.ru/professional_roles`` answers with 194 roles, of which id 96 is
«Программист, разработчик».

**Why the chain is three steps and not one.** The obvious shortcut is a list of
slugs in a file. It is wrong twice over: it hardcodes one person's job search
into the repository, which CLAUDE.md forbids for the city and forbids here for
the same reason, and it goes stale silently — a slug hh retires becomes a 404
nobody notices. So:

1. *Profile to families.* The planner already turns a resume into keywords, one
   group of skills at a time (``app/sources/query_planner.py``, groups declared
   in ``app/resume/skills_min.yaml``). ``hh_roles.yaml`` says which family of
   work each of those keyword sets means. That file is the whole of the
   per-deployment configuration, it sits beside ``hh_sites.yaml``, and it names
   no person.
2. *Families to roles.* A family names roles in words; the words are matched
   against **hh's own directory**, live. Nothing here holds a copy of hh's role
   list, so a renamed role stops matching visibly — as a count in a log line —
   instead of an id in a config quietly pointing at something else.
3. *Roles to slugs.* A catalogue slug is a transliteration of a Russian role
   name, so the matching is done on a transliterated, folded form of both. This
   is the only guessy step in the chain, and it is deliberately the last one:
   its input is hh's own vocabulary at both ends, and the probe prints the
   mapping it produced so a person can check it against the live site rather
   than trust this docstring.

**Keywords also match slugs directly**, without going through the directory, and
that is not redundancy. It is what makes the mechanism work for a profile whose
family this file has never heard of: an accountant's keywords find the
accountant's catalogue pages, badly but honestly, where a dev-shaped default
would hand them a corpus of jobs they cannot do. A profile that matches no
family is not given somebody else's roles; it is given its own words.

**Being wide is a decision, not an accident.** The brief asks for the whole
neighbouring circle — backend in any language, intern and junior developer,
data/ML, DevOps, integrations, automation, QA automation — because a posting a
narrow filter drops is dropped for good, while a posting a wide one lets in
costs one scoring pass that is already written and already honest.
"""

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import SourceError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: The profile-to-roles mapping. A file rather than constants for the reason
#: CLAUDE.md gives about the city: which work a candidate is looking for is a
#: property of the deployment and not of the code.
ROLES_FILE = Path(__file__).with_name("hh_roles.yaml")

#: Everything that is not a Latin letter or a digit separates one word from the
#: next. Latin only, and not by omission: every caller runs :func:`fold` first,
#: so by the time a name reaches this it has no Cyrillic left in it.
WORD_BREAK = re.compile(r"[^0-9a-z]+")

#: Cyrillic to Latin, in the shape hh's own slugs use: ``маркетолог`` is
#: ``marketolog`` and ``аналитик`` is ``analitik``, both measured. The three
#: letters where two honest transliteration schemes legitimately disagree are
#: normalised afterwards by :func:`fold` rather than guessed at here; see
#: :data:`FOLD` for which they are.
TRANSLIT: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

#: The spellings two honest transliterations disagree about, collapsed to one so
#: that ``testirovshchik`` and ``testirovschik`` are the same word. Applied to
#: both sides of every comparison, so it does not matter which scheme hh used.
FOLD: tuple[tuple[str, str], ...] = (
    # ``j`` first, and the order is load-bearing rather than tidy. hh writes the
    # same word both ways — ``mladshij-programmist`` and ``mladshiy-…`` are one
    # profession — and folding ``iy`` before ``j`` turns the first into
    # ``mladshiy`` and the second into ``mladshy``, which then never match.
    ("j", "y"),
    # ``щ`` collapses all the way to ``sh``. Three spellings of one sound reach
    # us — ``shch`` from a strict transliteration, ``sch`` from ours, and plain
    # ``sh`` from hh, which writes «начинающий» as ``nachinayushiy`` — and a fold
    # that stopped at ``sch`` would leave that slug unrecognised as a level word.
    ("shch", "sch"),
    ("sch", "sh"),
    ("kh", "h"),
    ("ts", "c"),
    ("iy", "y"),
    ("yy", "y"),
    ("q", "k"),
    ("x", "ks"),
)

#: One slug's place in the queue, lowest first: the three weights of what the
#: profile said (negated), then words belonging to no vocabulary it has, whether
#: hh's own directory named it, how many words it has, and the slug itself for a
#: stable tie-break.
type SlugRank = tuple[int, int, int, int, int, int, str]

#: Shortest word that may stand for a role on its own. Below it a token is a
#: preposition or an abbreviation whose collisions cost more than it finds.
MIN_ROLE_TOKEN = 5

#: Characters two words must share from the front to count as the same word for
#: the two intent weights. Russian inflects, and the headline and the slug rarely
#: inflect the same way: «AI-интеграции» folds to ``integracii`` while the slug
#: hh publishes is ``razrabotchik-integraciy``, and an exact match reads the
#: profile's own subject as somebody else's technology. Six is long enough that
#: ``backend`` and ``backup`` stay apart and short enough that every case of one
#: Russian noun in two forms lands together. Only the intent weights use it;
#: :func:`carries` and the known-word test stay exact, because a false match
#: there costs a wrongly-ranked page rather than a wrongly-weighted profile.
MIN_STEM = 6

#: Below this a term is matched as a whole word rather than as a substring. Long
#: enough to be distinctive is the rule; ``ml`` inside ``html`` is the reason.
MIN_SUBSTRING_TERM = 4


def tokens(text: str) -> tuple[str, ...]:
    """A name split into the words that may be matched against, folded."""
    return tuple(part for part in WORD_BREAK.split(fold(text)) if part)


def translit(text: str) -> str:
    """Cyrillic as hh writes it in a slug. Latin passes through untouched."""
    return "".join(TRANSLIT.get(char, char) for char in text.casefold())


def fold(text: str) -> str:
    """One spelling of a word, whichever transliteration produced it."""
    folded = translit(text)
    for before, after in FOLD:
        folded = folded.replace(before, after)
    return folded


#: How far into a trade somebody is, in the words hh's own slugs use. Not a
#: preference and not tied to one candidate: a level word names the SAME job as
#: the slug without it. That is what puts ``mladshij-programmist`` above
#: ``programmist_1c`` in :func:`_rank` — ``junior`` is a word the profile did not
#: say, and so is ``1c``, but only one of them means a different trade.
EXPERIENCE_WORDS: frozenset[str] = frozenset(
    fold(word)
    for word in (
        "junior",
        "middle",
        "senior",
        "lead",
        "intern",
        "trainee",
        "младший",
        "старший",
        "ведущий",
        "главный",
        "стажёр",
        "стажировка",
        "практикант",
        "начинающий",
        "без",
        "опыта",
    )
)

#: Words that name a rank rather than a trade. A role's own distinctive word may
#: stand alone when it is matched against a slug; these may not, or «DevOps-
#: инженер» would claim every ``inzhener-`` slug on the site, most of which are
#: construction.
GENERIC_ROLE_WORDS: frozenset[str] = EXPERIENCE_WORDS | frozenset(
    fold(word)
    for word in (
        "инженер",
        "менеджер",
        "специалист",
        "консультант",
        "оператор",
        "ассистент",
        "руководитель",
        "администратор",
        "директор",
        "техник",
        "мастер",
        "сотрудник",
        "работник",
        "начальник",
        "по",
        "и",
        "в",
        "с",
        "для",
        "или",
    )
)

#: Everything above is written in the words hh uses and stored in the one form
#: everything is compared in. Folding the tables rather than the literals is the
#: point: a word left unfolded here — ``veduschiy`` where the fold produces
#: ``veduschy`` — is a word that silently never matches anything, and
#: ``test_every_word_table_is_stored_folded`` is what stops one appearing.


def carries(text: str, terms: Sequence[str]) -> tuple[str, ...]:
    """Which of these terms this name carries, and by which rule.

    Long terms match as substrings, so ``razrabotchik`` finds
    ``razrabotchik-python``. Short ones match a whole word only; see
    :data:`MIN_SUBSTRING_TERM`. Both sides are folded, so a term written in
    Russian finds a slug written in Latin.
    """
    lowered = fold(text)
    words = set(tokens(text))

    def carried(term: str) -> bool:
        needle = fold(term)
        if len(needle) < MIN_SUBSTRING_TERM:
            return needle in words
        return needle in lowered

    return tuple(term for term in terms if carried(term))


class RoleFamily(BaseModel):
    """One kind of work, and the two vocabularies that recognise it."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1, max_length=40)
    #: Matched against the keywords the planner derived from the profile.
    when: tuple[str, ...] = ()
    #: Matched against the names in ``api.hh.ru/professional_roles``.
    roles: tuple[str, ...] = ()


class DirectoryRole(BaseModel):
    """One entry of hh's professional-role directory."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str = Field(min_length=1, max_length=200)
    #: The group hh files it under. Nothing here decides anything by it — a
    #: family names roles, not categories — but a person checking why a slug was
    #: chosen reads it, and it costs one field to carry.
    category: str | None = Field(default=None, max_length=200)


def load_families(path: Path | None = None) -> tuple[RoleFamily, ...]:
    """The configured families. Read on demand, not at import.

    The default is resolved in the body rather than in the signature, for the
    reason ``load_sites`` states: a default argument is evaluated once and would
    pin the module constant forever.
    """
    path = path or ROLES_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SourceError(
            f"hh: не читается {path.name} с соответствием «профиль -> роли»: {exc}",
            source_slug="hh",
        ) from exc
    try:
        return tuple(RoleFamily.model_validate(entry) for entry in raw.get("families", []))
    except ValidationError as exc:
        raise SourceError(
            f"hh: {path.name} не описывает семейства ролей: {exc.errors()}", source_slug="hh"
        ) from exc


def read_directory(payload: Any) -> tuple[DirectoryRole, ...]:
    """``api.hh.ru/professional_roles``, read without assuming its nesting.

    Documented as categories holding roles, and walked for any object carrying
    an integer-shaped ``id`` and a textual ``name`` instead of relying on that:
    the point is what the endpoint returns today, and a shape change should show
    up as a different count rather than as an empty result that reads like "hh
    has no developer roles".

    A category is told from a role by its shape — it holds a list of other named
    objects — and never by the key it hangs under. That matters because the two
    are separate numbering spaces: hh has both a category 11 and a role 11, and
    reading them into one table by id loses whichever arrives second.

    ``Any`` on the way in because the payload is hh's; it is validated into
    :class:`DirectoryRole` here and nothing else escapes.
    """
    roles: dict[int, DirectoryRole] = {}

    def visit(value: Any, category: str | None) -> None:
        if isinstance(value, dict):
            name = value.get("name")
            named = isinstance(name, str) and bool(name.strip())
            group = _is_category(value)
            if not group and named and isinstance(name, str):
                number = _as_int(value.get("id"))
                if number is not None:
                    roles.setdefault(
                        number, DirectoryRole(id=number, name=name.strip(), category=category)
                    )
            inner = str(name).strip() if group and named else category
            for item in value.values():
                visit(item, inner)
        elif isinstance(value, list):
            for item in value:
                visit(item, category)

    visit(payload, None)
    return tuple(sorted(roles.values(), key=lambda role: role.id))


def _is_category(value: dict[str, Any]) -> bool:
    """Whether this object holds other named objects rather than being one."""
    return any(
        isinstance(item, list) and any(isinstance(element, dict) for element in item)
        for item in value.values()
    )


def _as_int(value: Any) -> int | None:
    """A role id, however hh spelled it. hh has sent these as strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def families_for(keywords: Sequence[str], families: Sequence[RoleFamily]) -> tuple[RoleFamily, ...]:
    """Which families this profile's keywords ask for.

    A profile matching none of them gets none, and that is deliberate: the
    fallback is its own keywords against the catalogue, not somebody else's
    roles. Handing an accountant the developer families because the file happens
    to be written by developers would be the hardcoding this whole chain exists
    to avoid.
    """
    return tuple(
        family for family in families if any(carries(word, family.when) for word in keywords)
    )


def roles_for(
    families: Sequence[RoleFamily], directory: Sequence[DirectoryRole]
) -> tuple[DirectoryRole, ...]:
    """The directory entries those families name, deduplicated by id."""
    terms = tuple({term for family in families for term in family.roles})
    if not terms:
        return ()
    return tuple(role for role in directory if carries(role.name, terms))


def _alternatives(name: str) -> tuple[tuple[str, ...], ...]:
    """The word groups a slug may match this role by, most specific first.

    Two rules, and both are needed. hh writes a role's synonyms into one name
    separated by commas — «Программист, разработчик» — and each of those alone
    is a slug: ``/vacancies/programmist`` is the page this whole path was
    measured on. And a multi-word role has one word that carries it —
    ``devops`` in «DevOps-инженер» — which is why a distinctive word may stand
    alone while :data:`GENERIC_ROLE_WORDS` may not.
    """
    groups: list[tuple[str, ...]] = []
    for alternative in name.split(","):
        words = tuple(word for word in tokens(alternative) if word not in GENERIC_ROLE_WORDS)
        if words:
            groups.append(words)
    groups.extend(
        (word,)
        for group in list(groups)
        for word in group
        if len(word) >= MIN_ROLE_TOKEN and (word,) not in groups
    )
    return tuple(groups)


class Vocabulary(BaseModel):
    """What one profile said, in three weights, plus everything it recognises.

    Three and not one, because a resume says three different kinds of thing and
    a flat keyword list flattens them into one. Measured on the live run of
    2026-09-08: a profile listing Python, Java, Go, JavaScript and C produced
    sixteen equal keywords, and the crawl opened catalogue pages for Go, C,
    JavaScript, Linux and C# — not one for Python — off a resume headed «Python
    Developer — Backend / AI-интеграции». Nothing was wrong with the ranking; it
    was ranking by the wrong thing, because knowing a language and wanting to be
    hired for it had the same weight.

    ``headline`` is what the candidate says they ARE. ``focus`` is the rest of
    the vocabulary of the families their headline named — a backend profile's
    ``django``, ``api``, ``rest``, and its ``golang`` too, because the brief asks
    for backend in any language and a Go backend page is a better use of a
    request than a Linux administration one. ``keywords`` is the flat skill list,
    which is what they happen to know.

    ``known`` is the union of all of it with hh's own role words and the words
    for experience levels: a slug word in none of them belongs to a different
    trade. See :func:`_rank`.
    """

    model_config = ConfigDict(frozen=True)

    headline: frozenset[str] = frozenset()
    focus: frozenset[str] = frozenset()
    keywords: tuple[str, ...] = ()
    known: frozenset[str] = frozenset()


def intent_words(headline: str | None) -> frozenset[str]:
    """The words of a headline that say what work it is.

    Rank words are dropped — a headline reading «Ведущий инженер-программист»
    means ``programmist``, and letting ``inzhener`` through would hand every
    construction page on the site the profile's strongest weight.
    """
    if not headline:
        return frozenset()
    return frozenset(
        word for word in tokens(headline) if len(word) > 1 and word not in GENERIC_ROLE_WORDS
    )


def vocabulary_for(
    roles: Sequence[DirectoryRole],
    keywords: Sequence[str],
    families: Sequence[RoleFamily],
    headline: str | None = None,
) -> Vocabulary:
    """Everything the ranking knows about one profile, in the weights it uses.

    A family counts as named by the headline when the headline carries one of
    its ``when`` terms — the same test that selected it in the first place — so a
    profile headed «Python Developer» makes ``backend`` a focus family and leaves
    ``qa``, which got in on ``pytest``, in the ordinary keyword weight where it
    belongs.
    """
    headline_words = intent_words(headline)
    focus: set[str] = set()
    for family in families:
        if headline and carries(headline, family.when):
            for term in family.when:
                focus.update(tokens(term))
    return Vocabulary(
        headline=headline_words,
        focus=frozenset(focus) - headline_words,
        keywords=tuple(keywords),
        known=_known_words(roles, keywords, families) | headline_words | frozenset(focus),
    )


def _same_word(one: str, other: str) -> bool:
    """Whether these two are the same word in different grammatical clothes."""
    if one == other:
        return True
    return len(one) >= MIN_STEM and len(other) >= MIN_STEM and one[:MIN_STEM] == other[:MIN_STEM]


def _weight(words: Iterable[str], vocabulary: frozenset[str]) -> int:
    """How many of a slug's words this part of the profile said."""
    return sum(1 for word in words if any(_same_word(word, said) for said in vocabulary))


def _known_words(
    roles: Sequence[DirectoryRole], keywords: Sequence[str], families: Sequence[RoleFamily]
) -> frozenset[str]:
    """Every word that describes the work this profile is looking for.

    Four vocabularies, and the union is what makes the ranking below need no
    list of things to avoid. hh's role names say what the trade is called, the
    profile's keywords say what it uses, the matched families say what that kind
    of work is made of, and :data:`EXPERIENCE_WORDS` say how far into it somebody
    is. A word in none of them is a DIFFERENT job — that is the whole rule.
    """
    words: set[str] = set(EXPERIENCE_WORDS) | set(GENERIC_ROLE_WORDS)
    for role in roles:
        words.update(tokens(role.name))
    for term in keywords:
        words.update(tokens(term))
    for family in families:
        for term in family.when:
            words.update(tokens(term))
    return frozenset(words)


def _rank(slug: str, *, vocabulary: Vocabulary, by_role: bool) -> SlugRank:
    """Where this slug goes in the queue. Lower sorts first.

    Measured 2026-09-08, twice, and each measurement added a key.

    The first: role 96 «Программист, разработчик» matches over a hundred slugs
    on ``almaty.hh.kz``, and dozens of them are ``programmist_1c``,
    ``programmist-1s-buhgalteriya``, ``programmist_1szup``, ``programmist-1c-82``
    and their ABAP, Navision, Bitrix and CNC cousins. A run opens eight.
    Alphabetically, all eight are 1C.

    The second: ranked by the profile's keywords alone, a run opened Go, C,
    JavaScript, Linux and C# and not one Python page — because the resume lists
    those languages beside Python, and a flat keyword list has no way to say
    which of them the candidate wants to be hired for. The headline says it, in
    their own words, and now outranks everything.

    The keys, most significant first:

    1. **Words from the headline.** What the candidate says they are. A slug
       carrying ``python`` or ``backend`` beats one carrying ``linux`` or
       ``c-sharp`` even though the profile claims all four.
    2. **Words from the families the headline named, counted in the direction
       the first key leaves them.** On a page that already names what the
       candidate asked for, another technology is a distraction:
       ``python-developer`` is a better request than ``java-backend-developer``,
       and both carry exactly one headline word. On a page that names none of
       it, the same words are the best thing left: ``go-razrabotchik`` is a
       better request than ``linux-administrator``, because backend in any
       language is in scope and administration is not what this resume is for.
       So the weight is a bonus when the headline found nothing and a demerit
       when it found something — measured against the live slug list, where
       without it half the four pages a run re-reads every time were Java.
    3. **Words from the flat skill list.** What they happen to know.
    4. **Words belonging to no vocabulary this profile has.** ``1c`` is not on a
       list of bad words anywhere — there is no such list, it would be endless
       and out of date the week it was written. It is simply a word the profile
       never said, and ``junior`` is one it did not say either but which
       :data:`EXPERIENCE_WORDS` recognises as a level rather than another trade.
    5. **Named by hh's directory before named by the candidate's words.**
    6. **Shorter, then alphabetical.** Fewer words is the more general page and
       the larger pool; alphabetical last, so a stored plan means the same thing
       tomorrow.
    """
    words = set(tokens(slug))
    headline = _weight(words, vocabulary.headline)
    focus = _weight(words, vocabulary.focus)
    return (
        -headline,
        focus if headline else -focus,
        -len(carries(slug, vocabulary.keywords)),
        sum(1 for word in words if word not in vocabulary.known),
        0 if by_role else 1,
        len(words),
        slug,
    )


def slugs_for(
    roles: Sequence[DirectoryRole],
    keywords: Sequence[str],
    slugs: Iterable[str],
    *,
    families: Sequence[RoleFamily] = (),
    headline: str | None = None,
) -> tuple[str, ...]:
    """The catalogue pages worth opening, nearest to the profile first.

    Ranked rather than merely collected, because the crawl opens a bounded
    number of them per run: which eight of a hundred it takes decides what the
    whole run collects. See :func:`_rank` for the order and for the two
    measurements that made it necessary.
    """
    groups = tuple(group for role in roles for group in _alternatives(role.name))
    vocabulary = vocabulary_for(roles, keywords, families, headline)
    ranked: list[tuple[SlugRank, str]] = []
    for slug in slugs:
        folded = fold(slug)
        by_role = any(all(word in folded for word in group) for group in groups)
        if not by_role and not (keywords and carries(slug, keywords)):
            continue
        ranked.append((_rank(slug, vocabulary=vocabulary, by_role=by_role), slug))
    return tuple(slug for _, slug in sorted(ranked))


def rank_slugs(
    slugs: Iterable[str],
    keywords: Sequence[str],
    *,
    families: Sequence[RoleFamily] = (),
    headline: str | None = None,
) -> tuple[str, ...]:
    """Re-order an already chosen slug list for a new profile input, offline.

    :func:`slugs_for` needs hh's live role directory to decide which slugs are
    in; this one takes the list a previous run stored and only orders it, with
    the same :func:`_rank`. It is what lets a person change their job titles
    and see which pages will be opened first without a request being made.
    Every slug counts as named by a role, which is how the stored list got in.
    """
    vocabulary = vocabulary_for((), keywords, families, headline)
    return tuple(sorted(slugs, key=lambda slug: _rank(slug, vocabulary=vocabulary, by_role=True)))
