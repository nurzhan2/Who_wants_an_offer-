"""Resume content must never reach the logs.

A resume is the most personal document this service handles: a full name, a
phone number, an email address and a home city, all in one file. Logs go to
aggregators, are read by whoever is on call, and outlive the resume itself — so
one well-meant ``logger.info("parsed", text=document.raw_text)`` turns a debug
line into a privacy incident that nothing downstream will ever notice.

Every test here runs a real fixture through the real code — extraction, the API
provider, the profile builder, the upload endpoint, the contact block — with the
model and the embedding provider faked, captures everything that reaches the log
stream, and asserts the identities carried by those fixtures are nowhere in it.
Several distinct strings are checked each time (a name, an email, a phone
number, a city, and the links a contact block holds), because a formatter that
truncates or a field that carries only part of the document still leaks the part
it carries.

The contact block deserves its own mention. It is the one place where these
strings stop being a by-product of parsing and become first-class columns that a
person edits, an endpoint returns and a service logs about — which is exactly
the shape of change that adds a ``logger.info("saved", phone=phone)`` without
anybody thinking of it as a resume any more.

Two tests exist to keep the rest honest, and they must never be deleted:

* ``test_the_capture_sees_what_the_application_logs`` logs the very strings the
  other tests search for and asserts they ARE found. Without it a silently
  broken capture makes this whole file pass while the service logs everything.
* ``test_fixtures_really_carry_the_identities_searched_for`` pins the fixtures
  to those strings. Searching the logs for text the document never contained
  would prove nothing either.
"""

import base64
import io
import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
import structlog
from anthropic.types import TextBlock
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import logging as logging_module
from app.core.config import settings
from app.core.exceptions import ParsingError
from app.core.logging import configure_logging, get_logger
from app.db.enums import ParseStatus
from app.db.repositories.contact import ContactRepository
from app.db.repositories.profile import ProfileRepository
from app.llm import usage as usage_ledger
from app.llm.base import Document, LLMResult, LLMTask, LLMUsage
from app.llm.providers.anthropic_api import AnthropicAPIProvider
from app.llm.router import LLMRouter
from app.matching import embeddings
from app.resume import extractor, profile_builder
from app.schemas.llm import (
    ExtractedLanguage,
    ExtractedSkill,
    ProfileExtraction,
    WorkPeriod,
)
from app.services import resume as resume_service
from factories import make_profile

FIXTURES = Path(__file__).parent / "fixtures" / "resumes"

#: A log field longer than this is prose, and the only prose within reach of
#: this pipeline is the rendered prompt or the resume itself. Comfortably above
#: the longest hand-written message in the codebase (~200 characters) and well
#: below a rendered prompt (~3 kB) or an extracted resume (~1.3 kB).
MAX_REASONABLE_FIELD = 400

#: Keys whose value is a rendered traceback, which is long by nature.
TRACEBACK_KEYS = frozenset({"exception", "stack"})


@dataclass(frozen=True, slots=True)
class Identity:
    """The personal details one fixture carries. None of them may be logged.

    ``links`` is empty for the resume files in this repository — none of them
    carries a GitHub or a Telegram address — and populated for the contact
    block below, which does. It defaults to empty rather than being a separate
    type so that the guard test pinning fixtures to their strings keeps passing
    unchanged: a fixture claims only what it actually contains.
    """

    name: str
    email: str
    phone: str
    city: str
    links: tuple[str, ...] = ()

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every string that must not appear in a log line."""
        return (self.name, self.email, self.phone, self.city, *self.links)


#: The invented people in ``tests/fixtures/resumes``. Every string here is
#: asserted to be present in the extracted text by the guard test below.
IDENTITIES: dict[str, Identity] = {
    "plain.txt": Identity("Кирилл Макетов", "k.maketov@example.com", "+7 700 000 00 15", "Астана"),
    "single_column_ru.pdf": Identity(
        "Игорь Образцов", "i.obraztsov@example.com", "+7 700 000 00 11", "Алматы"
    ),
    "two_column_ru.pdf": Identity(
        "Дмитрий Вымыслов", "d.vymyslov@example.com", "+7 700 000 00 12", "Астана"
    ),
    "english.pdf": Identity(
        "Avery Testwood", "a.testwood@example.com", "+995 500 00 00 13", "Tbilisi"
    ),
    "mixed_ru_en.pdf": Identity(
        "Елена Примерова", "e.primerova@example.com", "+995 500 00 00 14", "Тбилиси"
    ),
    "with_table.docx": Identity(
        "Наталья Шаблонова", "n.shablonova@example.com", "+7 700 000 00 16", "Караганда"
    ),
}

PLAIN_TEXT_RESUME = "plain.txt"


def read_fixture(filename: str) -> bytes:
    """The bytes of one resume fixture."""
    return (FIXTURES / filename).read_bytes()


# ── capturing everything the application logs ─────────────────────────


class LogSink:
    """Every line the application logged during one test.

    Two channels on purpose. The first is the real handler ``configure_logging``
    installs, with the real production renderer, pointed at a buffer instead of
    stderr — so it holds every key of every structlog call exactly as a log
    aggregator would receive it, and can be parsed back into records. ``caplog``
    is the second opinion, catching anything that reaches the stdlib tree should
    that handler ever be replaced.

    Deliberately not ``capsys``: it swaps in a fresh stream for every test
    phase, so a handler bound to ``sys.stderr`` during fixture setup spends the
    test writing to a closed file — and a leak test whose capture is empty
    passes no matter what the code logs.
    """

    def __init__(self, buffer: io.StringIO, caplog: pytest.LogCaptureFixture) -> None:
        self._buffer = buffer
        self._caplog = caplog

    @property
    def stream(self) -> str:
        """The raw JSON log stream so far."""
        return self._buffer.getvalue()

    @property
    def caplog_text(self) -> str:
        """The stdlib capture on its own, for asserting a channel works."""
        return self._caplog.text

    @property
    def text(self) -> str:
        """Everything logged, from both channels, as one searchable string."""
        return f"{self.stream}\n{self.caplog_text}"

    def records(self) -> list[dict[str, Any]]:
        """The log stream parsed back into structured records."""
        parsed: list[dict[str, Any]] = []
        for line in self.stream.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:  # a line no logger of ours wrote
                continue
            if isinstance(record, dict):
                parsed.append(record)
        return parsed

    def events(self) -> set[str]:
        """The event names logged, to prove a test exercised what it claims."""
        return {str(record["event"]) for record in self.records() if "event" in record}


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[LogSink]:
    """Configure real production logging and capture all of it.

    Production rather than development rendering because JSON is what a log
    aggregator stores, and because it can be parsed back into fields. INFO
    because that is the level the service ships with, and therefore the set of
    records a real deployment writes.

    Not DEBUG, and that is worth knowing rather than assuming: ``pdfplumber``'s
    parser logs every token it reads, so a resume's text goes to the log stream
    verbatim as soon as the root level is lowered — the whole stdlib tree is
    routed through this handler. That is a hole in the logging configuration
    (nothing quietens third-party loggers), not something these tests can fix.
    """
    monkeypatch.setattr(logging_module.settings, "environment", "production")
    monkeypatch.setattr(logging_module.settings, "log_level", "INFO")

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    configure_logging()
    buffer = io.StringIO()
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(buffer)
    # configure_logging clears the root handlers, caplog's included.
    root.addHandler(caplog.handler)
    caplog.set_level(logging.INFO)
    try:
        yield LogSink(buffer, caplog)
    finally:
        root.removeHandler(caplog.handler)
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        structlog.reset_defaults()


def disguises(secret: str) -> tuple[str, ...]:
    """Every form one personal string can wear in a log stream.

    Searching for the literal characters alone is not enough, and this is not
    theoretical: the production renderer is ``json.dumps`` with its default
    ``ensure_ascii=True``, so ``Кирилл Макетов`` reaches the JSON stream as
    ``\\u041a\\u0438...`` and a search for the name finds nothing. A byte string
    interpolated into a message — ``f"... starts with {content[:80]!r}"``, the
    kind of line added while debugging a rejected upload — arrives as
    ``\\xd0\\x9a\\xd0\\xb8...``. Both decode straight back to the candidate's
    name in front of whoever reads the log, so both are leaks, and a check that
    only looks for the literal form silently misses both.
    """
    return (
        secret,
        json.dumps(secret)[1:-1],  # the \\uXXXX escapes the JSON renderer emits
        repr(secret.encode())[2:-1],  # the \\xNN escapes repr(bytes) emits
    )


def assert_absent(text: str, identity: Identity, *extra: str) -> None:
    """Fail naming the leaked string and the disguise it arrived in."""
    for secret in (*identity.secrets, *extra):
        for form in disguises(secret):
            assert form not in text, f"{secret!r} reached the logs, encoded as {form!r}"


def assert_no_prose(records: Sequence[dict[str, Any]]) -> None:
    """No log field is long enough to be a document or a prompt.

    The identity checks catch the resumes in this repository; this catches the
    shape of the mistake for every other resume — a field holding the rendered
    prompt, the raw text or a base64 document is long, whoever uploaded it.
    """
    for record in records:
        for key, value in record.items():
            if key in TRACEBACK_KEYS or not isinstance(value, str):
                continue
            assert len(value) <= MAX_REASONABLE_FIELD, (
                f"log field {key!r} of event {record.get('event')!r} carries "
                f"{len(value)} characters; that is prose, not metadata"
            )


# ── the two guards that keep the rest of the file honest ──────────────


@pytest.mark.unit
def test_the_capture_sees_what_the_application_logs(logs: LogSink) -> None:
    """The mandatory control. Every other test asserts an absence, and an
    absence proves nothing unless a presence can be demonstrated the same way:
    a capture that silently stopped working would make this file pass while the
    service logged whole resumes.

    Each channel is proved on its own rather than through the combined ``text``.
    The two encode differently — the JSON stream escapes ``Кирилл`` to
    ``\\u041a\\u0438...`` while ``caplog`` keeps the characters — so a combined
    search passes as long as *either* channel works, and the Russian half of
    every identity in this file would go unverified the moment ``caplog``
    stopped being attached."""
    identity = IDENTITIES[PLAIN_TEXT_RESUME]

    get_logger("tests.pii").info(
        "pii.canary",
        name=identity.name,
        email=identity.email,
        phone=identity.phone,
        city=identity.city,
    )

    assert "pii.canary" in logs.events()
    for channel, content in (("json stream", logs.stream), ("caplog", logs.caplog_text)):
        for secret in identity.secrets:
            assert any(form in content for form in disguises(secret)), (
                f"{secret!r} was logged but the {channel} capture did not see it "
                "in any encoding; every absence assertion in this file is vacuous"
            )


@pytest.mark.unit
@pytest.mark.parametrize("filename", sorted(IDENTITIES))
def test_fixtures_really_carry_the_identities_searched_for(filename: str) -> None:
    """The second guard. Searching the logs for a name the resume never
    contained would pass for ever; this pins each fixture to the strings the
    leak tests look for, so re-generating a fixture breaks here and loudly
    rather than weakening every assertion in the file."""
    document = extractor.extract(read_fixture(filename), filename)

    for secret in IDENTITIES[filename].secrets:
        assert secret in document.raw_text


# ── extraction ────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("filename", sorted(IDENTITIES))
def test_extraction_logs_metadata_but_never_the_document(filename: str, logs: LogSink) -> None:
    """Extraction is the first place the resume exists as text, and it logs a
    line for every upload. Format, sizes and counts are what operating the
    service needs; the text they were counted from is not."""
    document = extractor.extract(read_fixture(filename), filename)

    assert document.raw_text  # the resume really was parsed, not skipped
    assert "resume.extracted" in logs.events()
    assert_absent(logs.text, IDENTITIES[filename])
    assert_no_prose(logs.records())


@pytest.mark.unit
def test_an_unreadable_upload_is_rejected_without_quoting_it(logs: LogSink) -> None:
    """A rejection message travels further than a success: it reaches the API
    response, the client and the logs. Explaining *what* could not be read must
    not mean repeating the bytes that could not be read — and "which bytes?" is
    the first question anyone debugging this branch asks, so ``starts with
    {content[:80]!r}`` is a plausible thing to find here one day. This fixture
    opens with the candidate's own name, and ``repr`` of its bytes spells that
    name out in ``\\xNN`` escapes rather than hiding it."""
    identity = IDENTITIES[PLAIN_TEXT_RESUME]
    disguised = read_fixture(PLAIN_TEXT_RESUME)

    with pytest.raises(ParsingError) as failure:
        extractor.extract(disguised, "resume.bin")

    message = str(failure.value)
    # An empty message would satisfy every absence check below by saying nothing.
    assert "PDF, DOCX, TXT or Markdown" in message
    assert_absent(message, identity)
    assert_absent(logs.text, identity)


@pytest.mark.unit
def test_an_oversized_upload_is_reported_by_size_only(
    monkeypatch: pytest.MonkeyPatch, logs: LogSink
) -> None:
    """The one rejection that has the whole document in hand and a number to
    report instead. The message must be about megabytes, not about the person
    whose resume happened to be too big."""
    monkeypatch.setattr(settings, "resume_max_file_size_mb", 1)
    identity = IDENTITIES[PLAIN_TEXT_RESUME]
    oversized = read_fixture(PLAIN_TEXT_RESUME) * 600

    with pytest.raises(ParsingError) as failure:
        extractor.extract(oversized, PLAIN_TEXT_RESUME)

    message = str(failure.value)
    assert "MB" in message and "1 MB limit" in message
    assert_absent(message, identity)
    assert_absent(logs.text, identity)


# ── the LLM wrapper ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StubUsage:
    """The usage block of a stubbed response, with recognisable numbers."""

    input_tokens: int = 4321
    output_tokens: int = 87
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class StubMessage:
    """Just enough of an SDK ``Message`` for the wrapper to read."""

    content: list[TextBlock]
    usage: StubUsage = field(default_factory=StubUsage)
    stop_reason: str = "end_turn"


class StubMessages:
    """The ``messages`` namespace of a stubbed Anthropic client."""

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> StubMessage:
        """Record the request and answer with the next canned reply."""
        self.calls.append(kwargs)
        reply = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        return StubMessage(content=[TextBlock(type="text", text=reply)])


class StubAnthropic:
    """A transport that never touches the network. There is no API key here."""

    def __init__(self, replies: Sequence[str]) -> None:
        self.messages = StubMessages(replies)


class KeylessAPIProvider(AnthropicAPIProvider):
    """The real API provider over a stub transport, minus the key check.

    ``is_available`` reports whether ``ANTHROPIC_API_KEY`` is set, and there is
    no key in this environment — so a router asked to serve a task would skip
    the real provider and these tests would exercise the availability gate
    instead of the wrapper whose logging they are about.
    """

    def is_available(self) -> bool:
        """Yes: the transport underneath is a stub and needs no credentials."""
        return True


def router_over(provider: AnthropicAPIProvider) -> LLMRouter:
    """A real router that resolves every task to one provider.

    Binding all three names keeps the test indifferent to which provider
    ``LLM_ROUTING`` sends resume extraction to today.
    """
    return LLMRouter({name: provider for name in ("api", "cli", "ollama")})


def extraction_for(identity: Identity) -> ProfileExtraction:
    """What the model would return for a fixture, identity and all.

    Deliberately populated with the real personal details: an extraction that
    carried none would make every assertion downstream vacuous.
    """
    return ProfileExtraction(
        full_name=identity.name,
        headline="Python-разработчик",
        summary=f"Бэкенд-разработчик из города {identity.city}, почта {identity.email}.",
        city=identity.city,
        country="KZ",
        relocation=True,
        remote_pref="full",
        salary_expectation=1_400_000.0,
        salary_currency="KZT",
        work_periods=[
            WorkPeriod(
                company="«Медный Грифон»",
                title="Python-разработчик",
                start="2022-07",
                end=None,
                is_current=True,
                stack=["Python", "FastAPI"],
                domains=["fintech"],
            )
        ],
        skills=[
            ExtractedSkill(
                name="Python",
                mentioned_in="work_description",
                companies=["«Медный Грифон»"],
            ),
            ExtractedSkill(name="FastAPI", mentioned_in="skills_block"),
        ],
        languages=[ExtractedLanguage(code="ru", level="native")],
        stated_total_years=6.0,
    )


async def test_the_llm_call_log_carries_cost_not_the_prompt(logs: LogSink) -> None:
    """The request is the one place the resume is handed over verbatim, and the
    call that sends it is also the call worth logging — tokens and dollars are
    the only reason the line exists. Logging the request alongside them would
    put a resume in the log stream on every upload.

    The request carries the resume twice over, so both are checked: the rendered
    prompt (asserted through a sentence only the template contains) and the
    attached PDF, whose base64 is asserted directly because a document block
    leaks as base64, in which no name is searchable."""
    identity = IDENTITIES[PLAIN_TEXT_RESUME]
    document = extractor.extract(read_fixture(PLAIN_TEXT_RESUME), PLAIN_TEXT_RESUME)
    attachment = read_fixture("english.pdf")
    extraction = extraction_for(identity)
    transport = StubAnthropic([extraction.model_dump_json()])
    provider = AnthropicAPIProvider(client=transport)  # type: ignore[arg-type]  # stub transport

    result = await provider.complete_json(
        "extract_profile",
        ProfileExtraction,
        task=LLMTask.RESUME_EXTRACTION,
        effort="high",
        variables={"today": "2026-09-01", "resume_text": document.raw_text},
        documents=(Document(content=attachment),),
    )

    assert result.value.full_name == identity.name  # the resume did go through
    sent = transport.messages.calls[0]["messages"][0]["content"]
    encoded = sent[0]["source"]["data"]  # the PDF really was attached as base64
    assert encoded == base64.standard_b64encode(attachment).decode()

    call = next(record for record in logs.records() if record["event"] == "llm.call")
    assert call["input_tokens"] == 4321
    assert call["output_tokens"] == 87
    assert call["cost_usd"] > 0
    assert_absent(logs.text, identity, "Do not invent anything", encoded[:64])
    assert_no_prose(logs.records())


# ── the profile-building pipeline ─────────────────────────────────────


class FakeRouter:
    """A router that answers instantly with a fixed extraction.

    There is no API key in this environment and there never will be, and the
    provider resume extraction is routed to by default is a CLI subprocess that
    costs real money and forty seconds per call — so the pipeline tests below
    stand in for the router itself rather than for a transport under it.
    """

    def __init__(self, extraction: ProfileExtraction) -> None:
        self._extraction = extraction
        self.calls = 0

    async def complete_json(self, *_: Any, **__: Any) -> LLMResult[ProfileExtraction]:
        """Return the canned extraction with plausible usage."""
        self.calls += 1
        return LLMResult(
            value=self._extraction,
            usage=LLMUsage(
                provider="fake",
                model=settings.anthropic_model_heavy,
                task=LLMTask.RESUME_EXTRACTION,
                input_tokens=4321,
                output_tokens=87,
                cost_usd=0.03,
            ),
            attempts=1,
        )


@pytest.fixture(autouse=True)
def clean_ledger() -> Iterator[None]:
    """Leave the process-wide usage ledger as this file found it.

    The pipeline records every call it makes into a module-level singleton, so
    the two tests below would otherwise add a stub provider and its dollars to
    the totals ``/metrics`` reports for the rest of the run.
    """
    usage_ledger.ledger.reset()
    try:
        yield
    finally:
        usage_ledger.ledger.reset()


@pytest.fixture
def fake_embeddings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Deterministic vectors, and a cache directory of this test's own.

    The [embeddings] extra is not installed, so without this the provider is
    the one that only raises. Nothing here may load the real model.
    """
    monkeypatch.setattr(settings, "embedding_provider", "fake")
    monkeypatch.setattr(settings, "embedding_cache_dir", tmp_path / "vectors")
    embeddings.get_provider.cache_clear()
    try:
        yield
    finally:
        embeddings.get_provider.cache_clear()


async def test_a_successful_parse_stores_the_identity_and_logs_none_of_it(
    logs: LogSink,
    fake_embeddings: None,
    db_session: AsyncSession,
    profiles: ProfileRepository,
) -> None:
    """The whole pipeline in one run: extract, model, enrich, persist, embed.
    The profile row is *supposed* to hold the candidate's name, city and resume
    text — that is the product. The log line about it is supposed to hold an id,
    a format, sizes and counts, and this is what separates the two."""
    identity = IDENTITIES[PLAIN_TEXT_RESUME]
    document = extractor.extract(read_fixture(PLAIN_TEXT_RESUME), PLAIN_TEXT_RESUME)
    profile = await profiles.create_pending(
        filename=PLAIN_TEXT_RESUME,
        size_bytes=document.size_bytes,
        source_format=document.source_format,
        started_at=datetime.now(UTC),
    )
    router = FakeRouter(extraction_for(identity))

    result = await profile_builder.build_profile(
        document,
        session=db_session,
        profile_id=profile.id,
        router=cast(LLMRouter, router),  # fake router, no API key here
        today=date(2026, 9, 1),
    )

    assert router.calls == 1  # the model step really did run
    assert result.status is ParseStatus.READY
    # The writes went through Core statements, so the ORM instance is stale.
    await db_session.refresh(profile)
    assert profile.name == identity.name
    assert identity.email in (profile.raw_text or "")

    assert "resume.parsed" in logs.events()
    assert_absent(logs.text, identity)
    assert_no_prose(logs.records())


async def test_a_failed_parse_records_a_reason_free_of_resume_text(
    logs: LogSink,
    fake_embeddings: None,
    db_session: AsyncSession,
    profiles: ProfileRepository,
) -> None:
    """``parse_error`` is written to the database, returned by the API and
    logged, so it is the widest-travelling string in the pipeline. A failure
    handler that quotes what it choked on would publish exactly the part of the
    resume the model was in the middle of reading."""
    identity = IDENTITIES[PLAIN_TEXT_RESUME]
    document = extractor.extract(read_fixture(PLAIN_TEXT_RESUME), PLAIN_TEXT_RESUME)
    profile = await profiles.create_pending(
        filename=PLAIN_TEXT_RESUME,
        size_bytes=document.size_bytes,
        source_format=document.source_format,
        started_at=datetime.now(UTC),
    )
    # A model that answers with something unparseable twice: a real failure
    # built by the real code, not an exception a test invented.
    transport = StubAnthropic(["not json at all"])
    router = router_over(KeylessAPIProvider(client=transport))  # type: ignore[arg-type]

    result = await profile_builder.build_profile(
        document,
        session=db_session,
        profile_id=profile.id,
        router=router,
        today=date(2026, 9, 1),
    )

    assert result.status is ParseStatus.FAILED
    await db_session.refresh(profile)
    assert profile.parse_status is ParseStatus.FAILED
    assert profile.parse_error  # a reason was recorded at all
    assert_absent(profile.parse_error, identity)

    assert "resume.parse_failed" in logs.events()
    assert_absent(logs.text, identity)
    assert_no_prose(logs.records())


# ── the upload endpoint ───────────────────────────────────────────────


UPLOAD_URL = f"{settings.api_v1_prefix}/resume/upload"


async def test_the_endpoint_rejects_an_oversized_upload_by_size_alone(
    monkeypatch: pytest.MonkeyPatch, logs: LogSink, client_without_db: AsyncClient
) -> None:
    """The size check runs before anything parses the file, and the message it
    produces is logged by the error handler and returned to the client. Both
    must describe the file, not the person in it."""
    monkeypatch.setattr(settings, "resume_max_file_size_mb", 1)
    identity = IDENTITIES[PLAIN_TEXT_RESUME]
    oversized = read_fixture(PLAIN_TEXT_RESUME) * 600

    response = await client_without_db.post(
        UPLOAD_URL, files={"file": ("resume.txt", oversized, "text/plain")}
    )

    assert response.status_code == 422
    assert "1 MB limit" in response.json()["detail"]
    assert "app_error" in logs.events()
    assert_absent(response.text, identity)
    assert_absent(logs.text, identity)
    assert_no_prose(logs.records())


async def test_the_upload_endpoint_logs_ids_formats_and_sizes_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    logs: LogSink,
    async_client: AsyncClient,
    profiles: ProfileRepository,
) -> None:
    """The filename is the trap here. It is user-controlled, routinely contains
    the candidate's full name, and is stored on the profile row — one step from
    being logged with the id it is stored next to."""
    identity = IDENTITIES[PLAIN_TEXT_RESUME]
    filename = f"{identity.name} резюме.txt"
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")

    async def no_background_parse(*_: Any, **__: Any) -> None:
        """Stand in for the background task: it would need a real model."""

    monkeypatch.setattr(resume_service, "parse_in_background", no_background_parse)

    response = await async_client.post(
        UPLOAD_URL,
        files={"file": (filename, read_fixture(PLAIN_TEXT_RESUME), "text/plain")},
    )

    assert response.status_code == 202
    profile_id = response.json()["profile_id"]
    stored = await profiles.get(UUID(profile_id))
    assert stored is not None
    assert stored.resume_filename == filename  # the name really did arrive

    assert {"resume.accepted", "request_handled"} <= logs.events()
    accepted = next(record for record in logs.records() if record["event"] == "resume.accepted")
    assert accepted["profile_id"] == profile_id
    assert accepted["source_format"] == "txt"
    assert accepted["size_bytes"] == len(read_fixture(PLAIN_TEXT_RESUME))
    assert_absent(logs.text, identity, filename)
    assert_no_prose(logs.records())


# ── the contact block ─────────────────────────────────────────────────


#: A resume carrying the one thing the checked-in fixtures do not: links. It is
#: written here rather than added to ``tests/fixtures/resumes`` because those
#: are generated files whose bytes several other suites assert on, and because
#: an inline constant cannot drift from the strings the tests below search for
#: — the drift risk that ``test_fixtures_really_carry_the_identities_searched_for``
#: exists to catch for the files does not exist for this one.
LINKED_RESUME = """\
Тимур Черновиков
Backend-разработчик

Город: Шымкент, Казахстан
Email: t.chernovikov@example.com
Телефон: +7 700 000 00 17
GitHub: https://github.com/chernovikov
Telegram: https://t.me/chernovikov

ОПЫТ РАБОТЫ
ТОО «Бумажный Лис» — Шымкент
Backend-разработчик
05.2021 — по настоящее время
Сервисы на FastAPI, очереди, интеграции.
"""

LINKED_IDENTITY = Identity(
    name="Тимур Черновиков",
    email="t.chernovikov@example.com",
    phone="+7 700 000 00 17",
    city="Шымкент",
    links=("https://github.com/chernovikov", "https://t.me/chernovikov"),
)

CONTACTS_URL = f"{settings.api_v1_prefix}/profile/{{profile_id}}/contacts"


@pytest.mark.unit
def test_the_linked_resume_really_carries_the_identity_searched_for() -> None:
    """The same guard the file fixtures get, for the inline one. An absence
    assertion against strings the document never contained would pass for ever,
    links included."""
    for secret in LINKED_IDENTITY.secrets:
        assert secret in LINKED_RESUME


async def stored_profile(
    profiles: ProfileRepository, session: AsyncSession, identity: Identity, raw_text: str
) -> UUID:
    """A parsed profile carrying one identity, ready for the contact step."""
    profile = await profiles.create(make_profile(name=identity.name, raw_text=raw_text))
    profile.locations = [identity.city]
    await session.flush()
    return profile.id


async def test_prefilling_a_contact_block_logs_field_names_and_counts_only(
    logs: LogSink, db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    """The step that turns resume text into contact columns.

    It has every one of these strings in hand at once — that is its whole job —
    and it logs a line about what it did on every upload. What that line may
    say is *which* fields were filled and how many links were found; naming the
    fields is what makes the log useful for debugging, and it is also the exact
    point where writing the values instead would feel natural.
    """
    profile_id = await stored_profile(profiles, db_session, LINKED_IDENTITY, LINKED_RESUME)

    await resume_service.fill_contacts(db_session, profile_id)

    # Anchor the negative: the block really was filled in, so "no phone number
    # in the logs" is a statement about the logging and not about a no-op.
    contact = await ContactRepository(db_session).get(profile_id)
    assert contact is not None
    assert contact.phone == LINKED_IDENTITY.phone
    assert contact.email == LINKED_IDENTITY.email
    assert {link.url for link in contact.links} == set(LINKED_IDENTITY.links)

    prefilled = next(record for record in logs.records() if record["event"] == "contacts.prefilled")
    assert prefilled["fields"] == ["full_name", "phone", "email", "city"]
    assert prefilled["link_count"] == 2
    assert_absent(logs.text, LINKED_IDENTITY)
    assert_no_prose(logs.records())


async def test_correcting_a_contact_block_by_hand_logs_none_of_what_was_typed(
    logs: LogSink,
    async_client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
) -> None:
    """The endpoint the owner types their own details into.

    Every value in this request is personal data, it arrives as a request body
    that middleware could log wholesale, and it goes back out in the response —
    which is correct, because the response is the owner's own screen. The log
    line is the only part of the round trip that must not carry it.
    """
    profile_id = await stored_profile(profiles, db_session, LINKED_IDENTITY, LINKED_RESUME)

    response = await async_client.patch(
        CONTACTS_URL.format(profile_id=profile_id),
        json={
            "full_name": LINKED_IDENTITY.name,
            "phone": LINKED_IDENTITY.phone,
            "email": LINKED_IDENTITY.email,
            "city": LINKED_IDENTITY.city,
            "links": [{"kind": "github", "url": LINKED_IDENTITY.links[0]}],
        },
    )

    assert response.status_code == 200
    # The owner gets their own details back; that is the product, not a leak.
    assert response.json()["phone"] == LINKED_IDENTITY.phone
    assert {"contacts.updated", "request_handled"} <= logs.events()
    updated = next(record for record in logs.records() if record["event"] == "contacts.updated")
    assert updated["fields"] == ["full_name", "phone", "email", "city", "links"]
    assert_absent(logs.text, LINKED_IDENTITY)
    assert_no_prose(logs.records())


async def test_reading_a_contact_block_logs_nothing_about_its_contents(
    logs: LogSink,
    async_client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
) -> None:
    """A GET is the request most likely to be logged in full one day, because
    it looks harmless: no body, no mutation, nothing to audit. Its *response*
    is a whole contact block."""
    profile_id = await stored_profile(profiles, db_session, LINKED_IDENTITY, LINKED_RESUME)
    await resume_service.fill_contacts(db_session, profile_id)

    response = await async_client.get(CONTACTS_URL.format(profile_id=profile_id))

    assert response.status_code == 200
    assert response.json()["email"] == LINKED_IDENTITY.email  # there was something to leak
    assert "request_handled" in logs.events()
    assert_absent(logs.text, LINKED_IDENTITY)
    assert_no_prose(logs.records())


async def test_a_contact_value_rejected_by_prefill_is_reported_without_quoting_it(
    logs: LogSink, db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    """Prefill drops a candidate that does not fit its column — a city field
    holding a whole address line, say — and says so in the log. "Which value?"
    is the first question anyone debugging that line asks, and the answer is
    the one string this module may not print."""
    overlong_city = f"{LINKED_IDENTITY.city}, " * 40
    profile = await profiles.create(make_profile(name=LINKED_IDENTITY.name, raw_text=LINKED_RESUME))
    profile.locations = [overlong_city]
    await db_session.flush()

    await resume_service.fill_contacts(db_session, profile.id)

    rejected = next(
        record for record in logs.records() if record["event"] == "contacts.prefill_rejected"
    )
    assert rejected["field"] == "city"
    contact = await ContactRepository(db_session).get(profile.id)
    assert contact is not None
    assert contact.city is None  # dropped rather than truncated into something wrong
    assert contact.phone == LINKED_IDENTITY.phone  # and the rest was still filled in
    assert_absent(logs.text, LINKED_IDENTITY)
    assert_no_prose(logs.records())
