"""Shared building blocks for the API contracts."""

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

#: Active ISO 4217 alphabetic codes. Kept as data rather than a native enum:
#: currencies are added and retired, and ``ALTER TYPE`` on a PostgreSQL enum is
#: a migration hazard. Deliberately includes both members of the recently
#: renamed pairs (ANG/XCG, SLL/SLE, VES/VED) so a posting fetched before a
#: source updates its own data does not fail ingestion.
_ISO_4217_TABLE = """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB
    BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP
    DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF
    IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK
    LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN
    NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF
    SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT
    TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VED VES VND VUV WST XAF XCD XCG
    XDR XOF XPF YER ZAR ZMW ZWG
"""

ISO_4217_CODES: frozenset[str] = frozenset(_ISO_4217_TABLE.split())

#: Currencies the salary normaliser is expected to have a rate for. Postings in
#: anything else are stored with their original currency and simply do not get
#: a normalised amount, so they sort last rather than sorting wrong.
PRIMARY_CURRENCIES: frozenset[str] = frozenset({"USD", "EUR", "KZT", "RUB", "GBP", "PLN", "GEL"})


def validate_currency(value: str) -> str:
    """Reject anything that is not an active ISO 4217 alphabetic code."""
    if value not in ISO_4217_CODES:
        raise ValueError(f"{value!r} is not an active ISO 4217 currency code")
    return value


CurrencyCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=3, max_length=3),
    AfterValidator(validate_currency),
    Field(description="ISO 4217 alphabetic code."),
]

# Country and language are shape-checked only. ISO 3166 and ISO 639 change more
# often than currencies and a wrong country code cannot corrupt an ordering the
# way a wrong currency can.

CountryCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=2),
    Field(description="ISO 3166-1 alpha-2 code."),
]

LanguageCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, min_length=2, max_length=2),
    Field(description="ISO 639-1 code."),
]


#: Rows per page when the caller does not say, and the most it may ask for.
#: Here rather than in the repository because they are part of what the API
#: promises — the list endpoint validates ``limit`` against them and the
#: repository clamps to them — and a contract cannot be defined inside the layer
#: that happens to enforce it. The repository imports them back, so its own
#: name for them keeps working.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class ReadModel(BaseModel):
    """Base for every response model; reads straight off ORM instances."""

    model_config = ConfigDict(from_attributes=True)


class SortField(StrEnum):
    """Sortable columns of the vacancy list.

    ``SALARY`` sorts by the normalised monthly USD amount, never by the
    advertised figure: 500000 KZT would otherwise outrank 4000 USD.
    """

    SCORE = "score"
    PUBLISHED_AT = "published_at"
    SALARY = "salary"


class MatchMode(StrEnum):
    """Which stored number ``SortField.SCORE`` orders the list by.

    A mode never computes anything: every one of these numbers was written to
    ``match`` by the scoring pass, and a mode only picks which of them to rank
    by. Filters are the same in every mode — a mode changes the order of the
    list, not what is in it.
    """

    #: ``match.score`` as the scoring pass wrote it: 0.7 title + 0.3 description.
    COMBINED = "combined"
    #: ``component_scores.title_similarity``: the title against the headline.
    TITLE = "title"
    #: ``component_scores.semantic_similarity``: the description against the profile.
    DESCRIPTION = "description"
    #: ``component_scores.skill_coverage_required``: requirements the profile
    #: covers. An overlap of requirements, not fitness for the job, and it
    #: overrates a posting whose list is short — see docs/MATCHING.md.
    SKILLS = "skills"


class SortDirection(StrEnum):
    """Sort direction."""

    ASC = "asc"
    DESC = "desc"


class Facets(BaseModel):
    """Counts for the dashboard's filter sidebar, computed in one query."""

    sources: dict[str, int] = Field(default_factory=dict)
    buckets: dict[str, int] = Field(default_factory=dict)
    cities: dict[str, int] = Field(default_factory=dict)


class CursorPage[T](BaseModel):
    """One page of a keyset-paginated list.

    There is no page number and no offset: ``next_cursor`` encodes the sort
    value and the id of the last row, so inserts and deletes between requests
    cannot make rows repeat or disappear.
    """

    items: list[T]
    next_cursor: str | None = None
    total: int | None = Field(
        default=None,
        description="Total matching rows. Only filled when the caller asks for it.",
    )
    facets: Facets | None = None
