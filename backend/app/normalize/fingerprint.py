"""The deduplication key: one posting, one row, however many sources carry it.

``vacancy.fingerprint`` is ``NOT NULL UNIQUE``, so nothing can be stored without
one. That is why this module exists in phase 3 rather than in phase 4 with the
rest of normalisation: a crawler literally cannot insert a row without it.

**Version 1 is deliberately timid.** It casefolds, collapses whitespace and
strips punctuation, and it stops there. It does *not* strip company forms
(ООО, ТОО, LLC, GmbH), does not resolve ``ИП`` plus a person's name to the
business, and does not transliterate. Those are phase 4's job, and doing them
badly here would be worse than not doing them: this key decides which postings
are the same job.

The asymmetry is what makes timidity the right default. Being too strict splits
one job into two rows — visible, annoying, and repairable by merging. Being too
loose merges two different jobs into one row, and the second job is *gone*: its
title, its salary and its link were overwritten by the first, and no later pass
can tell that anything was lost. So version 1 splits where it is unsure, and
phase 4 merges once it knows better.

:data:`VERSION` is stored on every row it produces. When phase 4 improves the
normalisation it raises this number, and its recompute script walks the table,
recomputes, and merges the rows that newly collide.
"""

import hashlib
import re
import unicodedata

#: Bumped whenever the algorithm below changes in a way that moves a
#: fingerprint. Stored in ``vacancy.fingerprint_version`` so a mixed table is
#: legible and a recompute knows which rows it has already done.
VERSION = 1

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

    sha1 because the column is ``String(40)`` and this is a dedup key, not a
    security boundary. ``usedforsecurity=False`` keeps it working on a build
    where the hash is restricted for that reason.
    """
    parts = (normalize_part(company), normalize_part(title), normalize_part(city))
    digest = hashlib.sha1(_SEPARATOR.join(parts).encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()
