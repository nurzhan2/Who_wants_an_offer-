"""The source registry: what it refuses to register, and what it refuses to forget.

Every connector reaches the pipeline through this module and through nothing
else, which makes registration the last moment a mistake in a connector is
loud. That is what these tests defend.

**A refusal has to happen at registration.** A slug that overflows
``vacancy_source.source_slug``, or a second class quietly taking a slug that is
already taken, produces no error later on at all: the connector simply stops
appearing, its postings stop being refreshed, and nothing anywhere explains it.

**The robots.txt exemption has to cost something.** ``access_mode=API`` says the
vendor's published terms govern instead of the crawler file, so the registry
demands both the link to those terms and a summary of them in the class
docstring. Drop either check and the flag becomes a free bypass of the only
automated politeness this project has.

**Discovery must survive one broken file.** A connector with a typo takes itself
out of the run and stays reported; it must not take the working sources with it.

The registry is process-global state, so every test here puts back what it took:
``forget_source`` for a class it registered, and an explicit restore around the
laziness test, which reloads the module and rebinds its globals wholesale.
"""

import importlib
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from app import sources as sources_package
from app.core.config import settings
from app.core.exceptions import ConfigurationError
from app.sources import registry
from app.sources.base import (
    AccessMode,
    BaseSource,
    RawPosting,
    SearchQuery,
    SourceUnavailable,
)

pytestmark = pytest.mark.unit

#: The connectors that really ship. Asserted as a subset rather than as an
#: equality so adding a fourth source does not fail this file — but removing one
#: by accident still does.
REAL_SLUGS = frozenset({"jsearch", "arbeitnow", "remotive"})

#: Modules the laziness test drops from ``sys.modules`` so discovery has real
#: work to do. Infrastructure (``base``, ``http``, ``registry``) is deliberately
#: left alone: re-importing ``base`` would mint a second ``BaseSource`` class and
#: every identity check in the process would start lying.
CONNECTOR_MODULES = ("app.sources.jsearch", "app.sources.arbeitnow", "app.sources.remotive")

#: Name the broken-connector test writes onto the package path.
BROKEN_MODULE = "app.sources.exploding_connector"
BROKEN_MESSAGE = "this connector is broken"

#: What a fake connector yields. Empty on purpose: nothing here fetches, and
#: iterating an empty tuple keeps ``search`` an async generator without an
#: unreachable ``yield`` to explain.
NO_POSTINGS: tuple[RawPosting, ...] = ()


class FakeSource(BaseSource):
    """Base for the throwaway connectors below.

    Declares no slug, so the subclass under examination is the only place a slug
    comes from — and so the "author forgot the slug" case is this class itself
    rather than another copy of the same three lines.
    """

    name = "Fake source"

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield nothing: registration, not fetching, is what is under test."""
        for posting in NO_POSTINGS:
            yield posting


@pytest.fixture(autouse=True)
def forget_test_classes() -> Iterator[None]:
    """Drop whatever a test registered, so the next one sees the real set.

    The registry is one dict for the whole process and ``reset_registry`` will
    not clear it (it cannot: see the test below). A fake left behind would show
    up in another test's ``get_enabled_sources()`` as a source nobody wrote.

    Discovery is forced first so the snapshot is the real set. Taken before it,
    the snapshot would be empty and this fixture would "clean up" every shipped
    connector the moment a test caused them to be discovered — after which
    nothing could bring them back, because their modules are already imported.
    """
    registry.load_sources()
    before = set(registry._REGISTRY)
    yield
    for slug in set(registry._REGISTRY) - before:
        registry.forget_source(slug)


def declared_credentials() -> dict[str, SecretStr]:
    """Every credential every registered source declares, all present.

    So an enablement test turns only on the flag it is about, and does not
    quietly change meaning on a machine whose ``.env`` happens to hold a key.
    """
    return {
        key: SecretStr("test-value")
        for source in registry.all_sources()
        for key in source.required_credentials
    }


# ── what registration refuses ─────────────────────────────────────────


def test_a_connector_without_a_slug_is_refused() -> None:
    """The slug is the primary key of everything this source ever stores.

    Registered without one, the class would be keyed on ``None`` and its
    postings could not be upserted, deduplicated or attributed to anything.
    """
    with pytest.raises(ConfigurationError) as caught:
        registry.register_source(FakeSource)

    assert "FakeSource" in str(caught.value)


def test_an_empty_slug_is_refused_like_a_missing_one() -> None:
    """A slug left as an empty placeholder reads as declared and behaves as absent."""

    class BlankSlugSource(FakeSource):
        """A connector whose slug was never filled in."""

        slug = ""

    with pytest.raises(ConfigurationError):
        registry.register_source(BlankSlugSource)


@pytest.mark.parametrize("slug", ["JSearch", "hh-ru", "hh.ru", "hh ru", "9to5", "_private"])
def test_a_slug_outside_the_pattern_is_refused(slug: str) -> None:
    """Slugs travel in URLs, env vars and a database column, all of which read
    them literally.

    ``hh-ru`` and ``hh_ru`` become two names for one source the moment somebody
    writes the other spelling into ``SOURCES_DISABLED``, and the source keeps
    running with no error anywhere to explain why the switch did nothing.
    """
    bad = type(
        "BadlyNamedSource",
        (FakeSource,),
        {"slug": slug, "__doc__": "A connector with an unusable slug."},
    )

    with pytest.raises(ConfigurationError) as caught:
        registry.register_source(bad)

    assert repr(slug) in str(caught.value)


def test_a_slug_longer_than_its_column_is_refused() -> None:
    """The column holds 50 characters; a longer slug fails at write time, not here.

    And it fails inside ``bulk_upsert``, taking the whole batch of postings with
    it rather than the one row — hundreds of vacancies lost to a name.
    """

    class VerboseSource(FakeSource):
        """A connector whose slug is longer than the column that stores it."""

        slug = "a" * (registry.MAX_SLUG_LENGTH + 1)

    with pytest.raises(ConfigurationError) as caught:
        registry.register_source(VerboseSource)

    message = str(caught.value)
    assert "source_slug" in message
    assert str(registry.MAX_SLUG_LENGTH) in message


def test_a_duplicate_slug_is_refused_and_names_both_classes() -> None:
    """Overwriting silently makes one connector vanish.

    Its postings stop being refreshed and nothing reports it, so the error has
    to name both classes: the slug alone does not say which two files collided,
    and the loser of the collision is the one nobody thinks to look at.
    """

    class FirstSource(FakeSource):
        """The connector that got there first."""

        slug = "fake_duplicate"

    class SecondSource(FakeSource):
        """A connector reusing a slug that is already taken."""

        slug = "fake_duplicate"

    registry.register_source(FirstSource)
    with pytest.raises(ConfigurationError) as caught:
        registry.register_source(SecondSource)

    message = str(caught.value)
    assert "FirstSource" in message
    assert "SecondSource" in message
    assert registry._REGISTRY["fake_duplicate"] is FirstSource


def test_registering_the_same_class_twice_is_not_a_collision() -> None:
    """A reload re-executes the decorator; that is not two connectors.

    Treating it as a duplicate would make a connector module unimportable a
    second time and break exactly the reload the laziness test performs.
    """

    class IdempotentSource(FakeSource):
        """A connector whose module gets imported twice."""

        slug = "fake_idempotent"

    assert registry.register_source(IdempotentSource) is IdempotentSource
    assert registry.register_source(IdempotentSource) is IdempotentSource


# ── the price of skipping robots.txt ──────────────────────────────────


def test_an_api_source_without_terms_is_refused() -> None:
    """``access_mode=API`` is a claim that published terms govern instead.

    Unenforced, the flag is a one-line bypass of the robots.txt check with
    nothing put in its place — and the next connector reaches for it because it
    is the quickest way to stop the crawler check complaining.
    """

    class UntermedSource(FakeSource):
        """A connector claiming API access.

        It therefore skips the robots.txt check.

        But it names no terms that would govern instead.
        """

        slug = "fake_untermed"
        access_mode = AccessMode.API

    with pytest.raises(ConfigurationError) as caught:
        registry.register_source(UntermedSource)

    assert "terms_url" in str(caught.value)


def test_an_api_source_that_does_not_summarise_its_terms_is_refused() -> None:
    """A link nobody read is the same blindness, facing the other way.

    The summary in the docstring is the evidence that somebody opened the terms
    before deciding the crawler file did not apply to them; a bare URL is a
    promise to read them later.
    """

    class TerseSource(FakeSource):
        """A one-line docstring is not a summary of anybody's terms."""

        slug = "fake_terse"
        access_mode = AccessMode.API
        terms_url = "https://example.invalid/terms"

    with pytest.raises(ConfigurationError) as caught:
        registry.register_source(TerseSource)

    message = str(caught.value)
    assert "docstring" in message
    assert str(registry.MIN_TERMS_SUMMARY_LINES) in message


def test_an_api_source_that_does_both_registers() -> None:
    """The control for the two refusals above.

    Without it they would prove only that an API-mode fake gets rejected, not
    that the terms link and the summary are what the registry is asking for.
    """

    class WellDocumentedSource(FakeSource):
        """A connector calling a documented endpoint under its published terms.

        Limits: one request a second, 100 a day, counted on our side.

        Attribution: a link back to the posting is required on every card.

        Restrictions: results may not be republished as a competing job board.
        """

        slug = "fake_documented"
        access_mode = AccessMode.API
        terms_url = "https://example.invalid/terms"

    assert registry.register_source(WellDocumentedSource) is WellDocumentedSource
    assert registry.get_source("fake_documented").slug == "fake_documented"


# ── discovery ─────────────────────────────────────────────────────────


def test_the_shipped_connectors_are_all_discovered() -> None:
    """Nothing imports a connector by name, so an undiscovered one is invisible.

    It raises no error and writes no log line: the run is simply smaller than it
    should be, which looks exactly like a quiet week on the job market.
    """
    slugs = {source.slug for source in registry.all_sources()}

    assert slugs >= REAL_SLUGS
    assert registry.import_errors() == {}


def test_getting_a_source_twice_hands_back_the_same_instance() -> None:
    """A source owns its token bucket, and a bucket is only a limit if it is shared.

    A fresh instance per call would hand every caller a full bucket, so the
    configured rate would never be enforced anywhere and the first source with a
    real quota would be throttled by its vendor instead of by us.
    """
    first = registry.get_source("arbeitnow")
    second = registry.get_source("arbeitnow")

    assert first is second
    # The pipeline collects its sources through all_sources(), so that path has
    # to yield the same object rather than a parallel set of buckets.
    assert any(source is first for source in registry.all_sources())


def test_an_unknown_slug_is_a_configuration_error() -> None:
    """A slug that names nothing is a typo in a run request or in the environment.

    Returning None instead would push the failure into the pipeline as an
    AttributeError that never mentions the name that was wrong.
    """
    with pytest.raises(ConfigurationError) as caught:
        registry.get_source("no_such_source")

    assert "no_such_source" in str(caught.value)


def test_reset_registry_keeps_the_classes_it_could_never_rebuild() -> None:
    """Clearing the classes would empty the registry permanently.

    Registration is a side effect of importing a connector module, and a module
    already in ``sys.modules`` is not executed again — so a reset that dropped
    the classes and re-ran discovery would leave nothing behind at all.
    Instances are what a reset exists to drop, because dropping them is what
    makes a settings change take effect.
    """
    before = {source.slug for source in registry.all_sources()}
    stale = registry.get_source("arbeitnow")

    registry.reset_registry()

    assert {source.slug for source in registry.all_sources()} == before
    assert before >= REAL_SLUGS
    assert registry.get_source("arbeitnow") is not stale


def test_discovery_is_lazy_and_runs_on_the_first_call_that_needs_it() -> None:
    """Populating the registry at import time breaks the app before it starts.

    ``app.sources.base`` is imported by every connector, so importing the
    connectors from the package ``__init__`` is a genuine cycle; and the API
    tests build the app with a bare ``create_app()`` that never runs the
    lifespan, so a registry filled by the lifespan would be empty for all of
    them. Lazy means the first call that needs the registry is what fills it.
    """
    saved_classes = dict(registry._REGISTRY)
    saved_modules = {name: sys.modules[name] for name in CONNECTOR_MODULES if name in sys.modules}
    try:
        for name in saved_modules:
            del sys.modules[name]
        # Rebinds the module's globals: _REGISTRY becomes a new empty dict, and
        # every function in the module — including the decorator each connector
        # already holds a reference to — writes into that one instead.
        importlib.reload(registry)

        assert registry._REGISTRY == {}
        assert not registry._loaded
        assert not any(name in sys.modules for name in CONNECTOR_MODULES)

        slugs = {source.slug for source in registry.all_sources()}

        assert slugs >= REAL_SLUGS
        assert registry._loaded
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved_classes)
        registry._INSTANCES.clear()
        registry._IMPORT_ERRORS.clear()
        registry._loaded = False
        sys.modules.update(saved_modules)


def test_one_broken_connector_does_not_hide_the_working_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One typo must not disable every source, and must not be swallowed either.

    Discovery imports whatever is in the package, so a connector that raises on
    import would abort the walk and take the other sources down with it. Logging
    it and moving on is only half the answer: an error nobody can see turns a
    dead connector into a source that "has not returned much lately".
    """
    (tmp_path / "exploding_connector.py").write_text(
        '"""A connector that fails on import, the way a syntax error would."""\n'
        "\n"
        f'raise RuntimeError("{BROKEN_MESSAGE}")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sources_package, "__path__", [*sources_package.__path__, str(tmp_path)])
    importlib.invalidate_caches()
    registry.reset_registry()
    try:
        slugs = {source.slug for source in registry.all_sources()}
        errors = registry.import_errors()

        assert slugs >= REAL_SLUGS
        assert BROKEN_MODULE in errors
        assert BROKEN_MESSAGE in errors[BROKEN_MODULE]
    finally:
        sys.modules.pop(BROKEN_MODULE, None)
        registry.reset_registry()


# ── enablement ────────────────────────────────────────────────────────


def test_a_disabled_source_is_excluded_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switched off has to be reported, not hidden.

    A source missing from the dashboard with no reason beside it reads as a
    broken connector, and the next hour goes into debugging a deployment that is
    behaving exactly as it was configured to.
    """
    monkeypatch.setattr(settings, "source_credentials", declared_credentials())
    monkeypatch.setattr(settings, "sources_enabled", frozenset())
    monkeypatch.setattr(settings, "sources_disabled", frozenset({"remotive"}))

    enabled = {source.slug for source in registry.get_enabled_sources()}

    assert "remotive" not in enabled
    assert enabled >= REAL_SLUGS - {"remotive"}

    reason = registry.disabled_reason(registry.get_source("remotive"))
    assert reason is not None
    assert reason.code is SourceUnavailable.DISABLED_BY_CONFIG
    assert "SOURCES_DISABLED" in reason.detail


def test_a_non_empty_allow_list_wins_over_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SOURCES_ENABLED`` is the debugging switch: only these, nothing else.

    If it merely added to the default set, a session aimed at one connector
    would still spend the whole day's metered quota on the others.
    """
    monkeypatch.setattr(settings, "source_credentials", declared_credentials())
    monkeypatch.setattr(settings, "sources_disabled", frozenset())
    monkeypatch.setattr(settings, "sources_enabled", frozenset({"arbeitnow"}))

    assert {source.slug for source in registry.get_enabled_sources()} == {"arbeitnow"}

    reason = registry.disabled_reason(registry.get_source("jsearch"))
    assert reason is not None
    assert reason.code is SourceUnavailable.DISABLED_BY_CONFIG
    assert "SOURCES_ENABLED" in reason.detail


def test_a_source_missing_its_credentials_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise every run starts by spending its rate limit on 401s.

    The reason names the keys and only the keys: a value, a prefix, even a
    length in an API response or a log line narrows the secret for whoever
    reads it.
    """
    monkeypatch.setattr(settings, "source_credentials", {})
    # jsearch also accepts the older RAPIDAPI_KEY, which a developer's .env sets.
    monkeypatch.setattr(settings, "rapidapi_key", None)
    monkeypatch.setattr(settings, "sources_enabled", frozenset())
    monkeypatch.setattr(settings, "sources_disabled", frozenset())

    jsearch = registry.get_source("jsearch")
    enabled = {source.slug for source in registry.get_enabled_sources()}

    assert "jsearch" not in enabled
    reason = registry.disabled_reason(jsearch)
    assert reason is not None
    assert reason.code is SourceUnavailable.MISSING_CREDENTIALS
    assert reason.missing_credentials
    assert reason.missing_credentials == jsearch.required_credentials
