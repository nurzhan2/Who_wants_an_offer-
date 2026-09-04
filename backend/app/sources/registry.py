"""The source plugin registry: discovery, registration, enablement.

Adding a connector means adding one file to this package with
``@register_source`` on the class. Nothing else, anywhere, changes — and the
three obvious ways to build this all fail that requirement, so they are worth
recording.

*An explicit ``SOURCES = [...]`` list* is exactly the file you must edit per
source. *A per-source setting* (``JSEARCH_ENABLED``) is the same edit wearing a
different hat, in ``config.py`` and ``.env.example`` and the tests for both.
*Discovery in* ``app/sources/__init__.py`` looks right and is a genuine import
cycle: a connector imports ``app.sources.base``, which executes the package
``__init__``, which imports the connector, which re-enters a half-initialised
package — and it makes ``from app.sources.base import RawPosting`` fail whenever
any single connector has a syntax error.

So discovery is lazy and lives here: the first call that needs the registry
walks the package, and a module-level flag makes it happen once. Import-time
population would also break every API test, because the test client builds the
app with a bare ``create_app()`` and never runs the lifespan.
"""

import importlib
import pkgutil
import re
import sys
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger
from app.sources.base import AccessMode, BaseSource, SourceUnavailable, Unavailable

if TYPE_CHECKING:  # pragma: no cover
    from app.sources.http import RequestHook

logger = get_logger(__name__)

#: A slug is a URL-safe token that also has to fit ``vacancy_source.source_slug``.
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_SLUG_LENGTH = 50
#: Shortest class docstring an API-mode connector may have, in non-empty lines.
#: A terms_url nobody read is the same blindness the robots exemption was meant
#: to avoid, only facing the other way, so the summary is a condition of
#: registration rather than a convention.
MIN_TERMS_SUMMARY_LINES = 3

#: Modules in this package that are framework, not connectors.
_INFRASTRUCTURE = frozenset({"base", "http", "registry"})

_REGISTRY: dict[str, type[BaseSource]] = {}
_INSTANCES: dict[str, BaseSource] = {}
_IMPORT_ERRORS: dict[str, str] = {}
_loaded = False


def register_source[S: BaseSource](cls: type[S]) -> type[S]:
    """Register a connector class, validating it while we can still fail loudly.

    Generic in ``S`` so the decorated class keeps its own type under
    ``mypy --strict``; returning ``type[BaseSource]`` would erase it.
    """
    slug = getattr(cls, "slug", None)
    if not slug or not isinstance(slug, str):
        raise ConfigurationError(f"{cls.__name__} must declare a slug")
    if not SLUG_PATTERN.match(slug):
        raise ConfigurationError(f"{cls.__name__}: slug {slug!r} must match {SLUG_PATTERN.pattern}")
    if len(slug) > MAX_SLUG_LENGTH:
        raise ConfigurationError(
            f"{cls.__name__}: slug {slug!r} is longer than "
            f"vacancy_source.source_slug ({MAX_SLUG_LENGTH})"
        )
    existing = _REGISTRY.get(slug)
    if existing is not None and existing is not cls:
        # Naming both is the point: overwriting silently makes one connector
        # vanish, and its postings simply stop being refreshed with no error
        # anywhere to explain it.
        raise ConfigurationError(
            f"duplicate source slug {slug!r}: {existing.__name__} and {cls.__name__}"
        )
    if cls.access_mode is AccessMode.API:
        if not cls.terms_url:
            raise ConfigurationError(
                f"{cls.__name__}: access_mode=API skips robots.txt, so it must declare "
                "terms_url — the terms are what governs instead"
            )
        summary = [line for line in (cls.__doc__ or "").splitlines() if line.strip()]
        if len(summary) < MIN_TERMS_SUMMARY_LINES:
            raise ConfigurationError(
                f"{cls.__name__}: an API source must summarise its terms in the class "
                f"docstring (limits, attribution, restrictions) — at least "
                f"{MIN_TERMS_SUMMARY_LINES} lines"
            )

    _REGISTRY[slug] = cls
    return cls


def load_sources() -> None:
    """Import every connector module in this package, exactly once.

    ``walk_packages`` rather than ``iter_modules`` so a future
    ``app/sources/ats/greenhouse.py`` needs no change here.

    A module that fails to import is logged with its traceback and recorded,
    and the rest still load: one typo must not disable every source. It is not
    swallowed — :func:`import_errors` reports it and the API surfaces it.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    import app.sources as package

    def _walk_failed(name: str) -> None:
        """Record a sub-package that could not be imported, and keep walking.

        Without this the guarantee above is false for exactly the case it
        anticipates. ``walk_packages`` imports a sub-package itself in order to
        descend into it, and with no ``onerror`` it re-raises anything that is
        not an ImportError — so a future ``app/sources/ats/__init__.py`` that
        raises would propagate out of the registry and disable every connector,
        not just its own. The loop's own try/except cannot catch it, because
        the failure happens inside the iterator.
        """
        _IMPORT_ERRORS[name] = repr(sys.exc_info()[1])
        logger.exception("sources.package_walk_failed", module=name)

    for info in pkgutil.walk_packages(
        package.__path__, prefix=f"{package.__name__}.", onerror=_walk_failed
    ):
        leaf = info.name.rsplit(".", 1)[-1]
        if leaf.startswith("_") or leaf in _INFRASTRUCTURE:
            continue
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # a broken connector, not a broken pipeline
            _IMPORT_ERRORS[info.name] = repr(exc)
            logger.exception("sources.import_failed", module=info.name)


def _instance(cls: type[BaseSource]) -> BaseSource:
    """One instance per slug, cached.

    Cached for the same reason the LLM router is a singleton: a source owns its
    token bucket, so handing out a fresh instance per call would reset the
    bucket every time and the configured rate would never be enforced anywhere.
    """
    if cls.slug not in _INSTANCES:
        _INSTANCES[cls.slug] = cls()
    return _INSTANCES[cls.slug]


def all_sources() -> list[BaseSource]:
    """Every registered source, enabled or not."""
    load_sources()
    return [_instance(cls) for cls in sorted(_REGISTRY.values(), key=lambda c: c.slug)]


def get_source(slug: str) -> BaseSource:
    """One source by slug."""
    load_sources()
    cls = _REGISTRY.get(slug)
    if cls is None:
        raise ConfigurationError(f"unknown source {slug!r}")
    return _instance(cls)


def disabled_reason(source: BaseSource) -> Unavailable | None:
    """Why this source will not run, config first, then the source's own answer.

    ``min_interval`` and the daily quota are deliberately not consulted: both
    need the database, and this module stays free of it so the whole registry
    can be tested without PostgreSQL.
    """
    allow = settings.sources_enabled
    if allow and source.slug not in allow:
        return Unavailable(
            code=SourceUnavailable.DISABLED_BY_CONFIG,
            detail=f"Источник «{source.name}» не входит в SOURCES_ENABLED.",
        )
    if source.slug in settings.sources_disabled:
        return Unavailable(
            code=SourceUnavailable.DISABLED_BY_CONFIG,
            detail=f"Источник «{source.name}» выключен через SOURCES_DISABLED.",
        )
    return source.unavailable()


def get_enabled_sources() -> list[BaseSource]:
    """Sources that will actually run: configured, and not switched off."""
    return [source for source in all_sources() if disabled_reason(source) is None]


def bind_sources(
    sources: list[BaseSource], *, on_request: "RequestHook | None" = None
) -> list[BaseSource]:
    """Attach the shared HTTP client to each source.

    Imported here rather than at module scope: ``http`` imports ``base``, and
    keeping the registry's own import of it lazy means a test can exercise
    registration without pulling httpx in at all.
    """
    from app.sources.http import get_client

    client = get_client()
    return [source.bind(client.bind(source, on_request=on_request)) for source in sources]


def import_errors() -> dict[str, str]:
    """Module name to exception repr, for ``GET /api/v1/sources``."""
    load_sources()
    return dict(_IMPORT_ERRORS)


def reset_registry() -> None:
    """Drop the cached instances and let discovery run again.

    Deliberately does NOT clear the class registry, and that is not an
    oversight. Registration happens as a side effect of importing a connector
    module, and a module already in ``sys.modules`` is not executed a second
    time — so clearing the classes and re-running discovery leaves the registry
    permanently empty instead of rebuilding it. What a caller actually needs to
    drop is the instances, which is what makes a settings change take effect.

    A test that registers a throwaway class and wants it gone afterwards should
    use :func:`forget_source`, which is explicit about what it removes.
    """
    global _loaded
    _loaded = False
    _INSTANCES.clear()
    _IMPORT_ERRORS.clear()


def forget_source(slug: str) -> None:
    """Remove one registered class, for a test that registered a fake."""
    _REGISTRY.pop(slug, None)
    _INSTANCES.pop(slug, None)
