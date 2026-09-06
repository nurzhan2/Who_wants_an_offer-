"""The deduplication key: one posting, one row, however many sources carry it.

``vacancy.fingerprint`` is ``NOT NULL UNIQUE``, so nothing can be stored without
one. That is why this module exists in phase 3 rather than in phase 4 with the
rest of normalisation: a crawler literally cannot insert a row without it.

**The algorithm is deliberately timid.** It casefolds, collapses whitespace and
strips punctuation, and it stops there. It does *not* strip company forms
(ООО, ТОО, LLC, GmbH), does not resolve ``ИП`` plus a person's name to the
business, and does not transliterate. Those are phase 4's job, and doing them
badly here would be worse than not doing them: this key decides which postings
are the same job.

The asymmetry is what makes timidity the right default. Being too strict splits
one job into two rows — visible, annoying, and repairable by merging. Being too
loose merges two different jobs into one row, and the second job is *gone*: its
title, its salary and its link were overwritten by the first, and no later pass
can tell that anything was lost. So this key splits where it is unsure, and
phase 4 merges once it knows better.

**Version 2 puts the city in.** Version 1 hashed a company and a title and
nothing else. The signature already took a ``city``; the one caller passed
``None`` for every posting, on the grounds that most sources report a place as
free text and a guessed city produces a wrong key. That reasoning holds for a
guess and only for a guess. A source that reads a city out of a structured
field on the page is not guessing, and leaving its city out cost precisely what
the asymmetry above says it costs: an employer advertising one title in four
cities hashed to one fingerprint, three of those four postings overwrote each
other as they arrived, and the vacancy count read low with nothing anywhere
recording the loss.

Version 2 is therefore the same function over the same three parts, reached by
a caller that now supplies the third. The rule against guessing is unchanged —
a caller passes a city only when its source stated one, and
``app/pipeline/runner.py`` documents which sources qualify, which do not, and
the one place where hh's connector stretches the rule further than this module
would like.

**Adding a part can only split, never merge.** Two rows that differed under
version 1 differed in their company or in their title, and both of those parts
are still in the input, so no pair of distinct version-1 rows can hash together
under version 2. That property is about pairs of version-1 rows and it does not
extend to a table that already holds version-2 ones — which is what a crawl run
against an unmigrated database leaves behind, and why
``0007_fingerprint_city`` checks for an occupied key before it writes rather
than trusting this paragraph. What no recompute can do is bring back a posting
that version 1 already overwrote: that row was replaced as it arrived, and only
a re-crawl restores it.

:data:`VERSION` is stored on every row it produces. When phase 4 improves the
normalisation it raises this number again, and its recompute walks the table,
recomputes, and merges the rows that newly collide.
"""

import hashlib
import re
import unicodedata

#: Bumped whenever what reaches the hash changes in a way that moves a
#: fingerprint — a different algorithm below, or, as in version 2, the same
#: algorithm reached by a caller that supplies a part it used to leave empty.
#: Stored in ``vacancy.fingerprint_version`` so a mixed table stays legible and
#: a recompute knows which rows it has already done.
VERSION = 2

#: Anything that is not a letter, a digit or a space. Removed rather than
#: replaced with a space, so «Пиксель-Мираж» and «Пиксель Мираж» agree while
#: "Data Engineer" and "DataEngineer" still differ.
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

#: Separates the parts inside the hashed string. A character that cannot appear
#: in a normalised part, so ("ab", "c") and ("a", "bc") cannot collide.
_SEPARATOR = "\x1f"


def normalize_part(value: str | None) -> str:
    """Reduce one component to its comparable form.

    NFKC first: the same company name typed with a full-width Latin letter, or
    with a precomposed ``й`` in one source and a combining one in another, is
    the same company, and without normalisation those are different bytes and
    therefore different rows.
    """
    if not value:
        return ""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", folded)).strip()


def fingerprint(*, company: str | None, title: str, city: str | None) -> str:
    """The 40-character key ``vacancy.fingerprint`` holds.

    ``city`` is a place the source *stated*, never one the caller worked out
    from free text. A guess that is wrong in either direction is worse than
    ``None``: too specific splits one job in two, too loose merges two jobs and
    loses one of them. ``None`` is the honest value when the source said
    nothing, and it hashes the same way an empty string does.

    That is the contract this function asks for and cannot enforce. What a
    connector actually put in the field is the connector's claim; hh's has one
    documented fallback that is the crawler's configuration rather than the
    page's own words, and ``app.pipeline.runner.stated_city`` names it.

    sha1 because the column is ``String(40)`` and this is a dedup key, not a
    security boundary. ``usedforsecurity=False`` keeps it working on a build
    where the hash is restricted for that reason.
    """
    parts = (normalize_part(company), normalize_part(title), normalize_part(city))
    digest = hashlib.sha1(_SEPARATOR.join(parts).encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()
