"""Skill canonicalisation: one name per skill, so two of them can be compared.

A resume says "PostgreSQL", a vacancy says "Postgres", a third document uses
the Cyrillic spelling. Compared as raw strings those are three unrelated
skills and the match score is wrong in a way nobody notices. This module maps
a spelling onto a canonical name plus a coarse group.

The dictionary shipped alongside it (``skills_min.yaml``) is deliberately
small. The full dictionary, and a relatedness graph behind ``group_of``,
belong to phase 4; what is meant to survive that swap is the
:class:`SkillCanonicalizer` protocol, so callers never learn where the data
came from.

An unrecognised spelling returns ``None``. It is neither dropped nor guessed
at here: keeping it verbatim, queueing it for review, or ignoring it is a
policy decision that belongs to the caller, and inventing a canonical name for
it would quietly pollute the dictionary's job.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

# PyYAML ships no type information and this project does not depend on the
# stub package, so the import is untyped and ``safe_load`` returns ``Any``.
# That Any is confined to ``_read_entries``, which narrows the document to
# concrete types before anything else touches it.
import yaml  # type: ignore[import-untyped]

#: Shipped next to this module rather than under a configured data directory:
#: the dictionary is code, versioned with the code that reads it.
DEFAULT_DICTIONARY = Path(__file__).with_name("skills_min.yaml")

#: Everything people use to glue a name together. Dropping these outright
#: rather than collapsing them to a single space is what makes "Node.js",
#: "node js" and "nodejs" one key.
_SEPARATORS = re.compile(r"[\s._\-]+")


def normalize(spelling: str) -> str:
    """Fold away the spelling noise that carries no meaning."""
    return _SEPARATORS.sub("", spelling.strip().casefold())


def fold(spelling: str) -> str:
    """The key two spellings of one skill must share to be the same skill.

    The dictionary first, so "Node.js" and "nodejs" meet at the canonical name
    it chose, then normalised; a normalised fold second, so two spellings of a
    skill the dictionary has never heard of still meet. Falling back rather than
    dropping matters: the bundled dictionary is small, and a miss here silently
    shrinks every set intersection built on it.

    Lives here rather than in the two packages that need it —
    :mod:`app.letters.context` computes the overlap a letter is written around,
    :mod:`app.resume.ats_keywords` computes the one an audit reports — because
    two copies of this rule drifting apart means a letter claiming a skill the
    audit says is unstated, on the same document.
    """
    canonical = default_canonicalizer().canonicalize(spelling)
    return normalize(canonical if canonical is not None else spelling)


class SkillDictionaryError(ValueError):
    """The skill dictionary is malformed and cannot be used.

    Raised on load, not on lookup. A dictionary that maps one spelling to two
    skills would canonicalise by file order, which is a silently wrong match
    score rather than a visible failure.
    """


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One dictionary entry: the canonical name and how it may be written."""

    canonical: str
    group: str
    aliases: tuple[str, ...] = ()


class SkillCanonicalizer(Protocol):
    """What the rest of the codebase is allowed to assume about canonicalisation."""

    def canonicalize(self, raw: str) -> str | None:
        """Return the canonical name for ``raw``, or None if it is unknown."""
        ...

    def group_of(self, canonical: str) -> str | None:
        """Return the group of a canonical name, or None if it is unknown."""
        ...


@dataclass(frozen=True, slots=True)
class _Index:
    """The three lookups a canonicalisation needs, tried in this order."""

    by_canonical: dict[str, SkillEntry]
    by_alias: dict[str, str]
    by_normalized: dict[str, str]


class YamlSkillCanonicalizer:
    """Canonicaliser backed by a YAML dictionary file.

    The file is read on first lookup and kept: reading it at import time would
    make every test that imports this module pay for the parse, and a caller
    that only wants ``group_of`` on a hit it already has pays nothing at all.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_DICTIONARY
        self._cache: _Index | None = None

    def canonicalize(self, raw: str) -> str | None:
        """Map any spelling of a skill onto its canonical name.

        Exact canonical name first, then exact alias, then the normalised
        comparison. The exact passes come first so a dictionary entry always
        wins over a fold that happens to collide with it.
        """
        text = raw.strip()
        if not text:
            return None
        index = self._index()
        if text in index.by_canonical:
            return text
        alias = index.by_alias.get(text)
        if alias is not None:
            return alias
        return index.by_normalized.get(normalize(text))

    def group_of(self, canonical: str) -> str | None:
        """Group of a canonical name — the output of :meth:`canonicalize`, not raw input."""
        entry = self._index().by_canonical.get(canonical)
        return entry.group if entry is not None else None

    def _index(self) -> _Index:
        if self._cache is None:
            self._cache = _build_index(_read_entries(self._path), source=self._path)
        return self._cache


@lru_cache(maxsize=1)
def default_canonicalizer() -> SkillCanonicalizer:
    """The process-wide canonicaliser over the bundled dictionary.

    A singleton because the dictionary never changes at runtime and callers
    reach for it inside per-skill loops. Typed as the protocol so phase 4 can
    return something else from here without touching a single call site.
    """
    return YamlSkillCanonicalizer()


def _read_entries(path: Path) -> list[SkillEntry]:
    """Parse the dictionary file into entries, rejecting anything unexpected."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # A missing or unreadable file is a malformed dictionary too. Letting
        # FileNotFoundError escape would leave a caller guarding the load with
        # `except SkillDictionaryError` catching four failure modes out of five.
        raise SkillDictionaryError(f"{path}: cannot be read: {exc}") from exc

    try:
        document: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # A syntax error is a malformed dictionary like any other, so it leaves
        # here as SkillDictionaryError. Letting PyYAML's own exception escape
        # would mean a caller guarding the load could not catch half of it.
        raise SkillDictionaryError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise SkillDictionaryError(f"{path}: expected a mapping of canonical name to definition")
    return [_entry(name, body, source=path) for name, body in document.items()]


def _entry(name: object, body: object, *, source: Path) -> SkillEntry:
    """Validate one ``name: {group, aliases}`` pair."""
    if not isinstance(name, str) or not name.strip():
        raise SkillDictionaryError(f"{source}: canonical names must be non-empty strings: {name!r}")
    if not isinstance(body, dict):
        raise SkillDictionaryError(f"{source}: {name!r} must map to 'group' and optional 'aliases'")

    group = body.get("group")
    if not isinstance(group, str) or not group.strip():
        raise SkillDictionaryError(f"{source}: {name!r} has no 'group'")

    aliases = body.get("aliases") or []
    if not isinstance(aliases, list) or not all(
        isinstance(alias, str) and alias.strip() for alias in aliases
    ):
        raise SkillDictionaryError(f"{source}: {name!r} has a bad 'aliases': {aliases!r}")

    return SkillEntry(canonical=name, group=group, aliases=tuple(aliases))


def _build_index(entries: Iterable[SkillEntry], *, source: Path) -> _Index:
    """Turn entries into lookups, refusing a dictionary that contradicts itself."""
    by_canonical: dict[str, SkillEntry] = {}
    by_alias: dict[str, str] = {}
    by_normalized: dict[str, str] = {}

    for entry in entries:
        # YAML itself keeps only the last of two identical keys, so this guard
        # is for entries assembled any other way — a phase-4 loader, a test.
        if entry.canonical in by_canonical:
            raise SkillDictionaryError(f"{source}: {entry.canonical!r} is defined twice")
        by_canonical[entry.canonical] = entry
        # The canonical name goes into the normalised map as well. That is
        # what turns an alias repeating another skill's name into a load-time
        # error, instead of dead text no lookup ever reaches.
        _claim(by_normalized, normalize(entry.canonical), entry.canonical, "spelling", source)
        for alias in entry.aliases:
            _claim(by_alias, alias, entry.canonical, "alias", source)
            _claim(by_normalized, normalize(alias), entry.canonical, "spelling", source)

    return _Index(by_canonical=by_canonical, by_alias=by_alias, by_normalized=by_normalized)


def _claim(owner: dict[str, str], key: str, canonical: str, kind: str, source: Path) -> None:
    """Record ``key -> canonical``, refusing a key two different skills both claim."""
    taken = owner.get(key)
    if taken is not None and taken != canonical:
        raise SkillDictionaryError(
            f"{source}: {kind} {key!r} is claimed by both {taken!r} and {canonical!r}; "
            "canonicalisation would depend on the order of the file"
        )
    owner[key] = canonical
