"""The Claude Code CLI provider: how it spawns, what it hands the process, what it refuses to leak.

The real ``claude`` binary must never run from here. It is installed on the
development machine, a single extraction call costs roughly $0.20 and takes
forty seconds, and a test suite that quietly spends money is a test suite people
stop running. Every test in this file replaces ``asyncio.create_subprocess_exec``
with a recording fake, and an autouse fixture makes *any* unfaked spawn — exec or
shell — fail loudly rather than reach the machine.

What is actually under test is not "does the CLI answer". It is the handling
around the CLI, and almost all of it is security or money:

* the prompt carries resume text and scraped vacancy descriptions, so it must
  never become a shell string and never become a command-line argument;
* tools are granted by task from a table, and the working directory is what
  bounds the one tool that is ever granted;
* a retried call is paid for twice, and the ledger has to say so;
* the failure paths must name the problem without echoing the prompt.
"""

import ast
import asyncio
import io
import json
import shutil
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.core.exceptions import LLMError
from app.llm import prompts
from app.llm.base import DENIED_TOOLS, TOOL_POLICY, Document, LLMTask
from app.llm.providers import claude_cli
from app.llm.providers.claude_cli import (
    BINARY_NAME,
    MAX_ATTEMPTS,
    ClaudeCLIProvider,
    CLIUnavailableError,
    resolve_binary,
)

pytestmark = pytest.mark.unit

PROMPT_NAME = "demo"
PROMPT_TEMPLATE = "Extract structured data about {{subject}}."
#: Stands in for resume content. If this string turns up in argv, in an error
#: message or in a log line, the provider is leaking the document it was given
#: to anyone who can run `ps` on the machine.
SUBJECT = "Ivan Petrov, ivan.petrov@example.com, +7 700 000 00 00"

VALID_ANSWER = '{"name": "Ivan", "score": 7}'
#: Missing the required ``name``, so pydantic rejects it and the provider retries.
INVALID_ANSWER = '{"score": 7}'


class Answer(BaseModel):
    """Minimal response schema: one required field is enough to fail validation."""

    name: str
    score: int = 0


# ── the fake CLI ──────────────────────────────────────────────────────


@dataclass(slots=True)
class Spawn:
    """Everything one invocation was given, recorded at the moment it happened."""

    argv: list[str]
    cwd: str
    stdin: str = ""
    #: Names in the working directory while the process was "running".
    listing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Exit:
    """Scripted outcome: the process exits non-zero, with something on stderr."""

    returncode: int
    stderr: bytes = b""


@dataclass(frozen=True, slots=True)
class Raw:
    """Scripted outcome: exit 0, but stdout is whatever this says."""

    stdout: str


@dataclass(frozen=True, slots=True)
class Hang:
    """Scripted outcome: the process never answers, so the timeout fires."""


class FakeProcess:
    """The parts of ``asyncio.subprocess.Process`` this provider touches."""

    def __init__(self, cli: "FakeCLI", spawn: Spawn, outcome: object) -> None:
        self._cli = cli
        self._spawn = spawn
        self._outcome = outcome
        self.returncode: int | None = None
        self.killed = False

    async def communicate(self, data: bytes = b"") -> tuple[bytes, bytes]:
        """Record the stdin payload and the working directory, then answer."""
        self._spawn.stdin = data.decode("utf-8")
        self._spawn.listing = tuple(sorted(p.name for p in Path(self._spawn.cwd).iterdir()))
        self._cli.active += 1
        self._cli.peak = max(self._cli.peak, self._cli.active)
        try:
            if self._cli.delay:
                await asyncio.sleep(self._cli.delay)
            if isinstance(self._outcome, Hang):
                await asyncio.Event().wait()  # cancelled by wait_for
        finally:
            self._cli.active -= 1

        if isinstance(self._outcome, Exit):
            self.returncode = self._outcome.returncode
            return b"", self._outcome.stderr
        if isinstance(self._outcome, Raw):
            self.returncode = 0
            return self._outcome.stdout.encode("utf-8"), b""
        self.returncode = 0
        return json.dumps(self._outcome).encode("utf-8"), b""

    def kill(self) -> None:
        """Record that the provider terminated us; a real kill sends SIGKILL."""
        self.killed = True

    async def wait(self) -> int:
        """Reap the killed process, as the provider does after a timeout."""
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


@dataclass(slots=True)
class FakeCLI:
    """Stand-in for ``asyncio.create_subprocess_exec``, one scripted outcome per call."""

    outcomes: list[object] = field(default_factory=list)
    spawns: list[Spawn] = field(default_factory=list)
    processes: list[FakeProcess] = field(default_factory=list)
    active: int = 0
    peak: int = 0
    delay: float = 0.0

    def script(self, *outcomes: object) -> "FakeCLI":
        """Queue the answers this fake will give, in order."""
        self.outcomes.extend(outcomes)
        return self

    async def __call__(self, *argv: object, **kwargs: Any) -> FakeProcess:
        """Accept the invocation the way ``create_subprocess_exec`` does."""
        # An argument list, never one string: this is the property the whole
        # file exists to protect, so the fake refuses anything else.
        assert all(isinstance(item, str) for item in argv), f"non-string argv: {argv!r}"
        spawn = Spawn(argv=[str(item) for item in argv], cwd=str(kwargs["cwd"]))
        self.spawns.append(spawn)
        if not self.outcomes:
            raise AssertionError(f"unscripted CLI call number {len(self.spawns)}")
        process = FakeProcess(self, spawn, self.outcomes.pop(0))
        self.processes.append(process)
        return process

    @property
    def spawn(self) -> Spawn:
        """The only invocation, asserting that there was exactly one."""
        assert len(self.spawns) == 1, f"expected one spawn, got {len(self.spawns)}"
        return self.spawns[0]


def envelope(result: str = VALID_ANSWER, **overrides: Any) -> dict[str, Any]:
    """A ``--output-format json`` envelope, shaped like a real measured one."""
    data: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result,
        "usage": {
            "input_tokens": 4,
            "output_tokens": 3395,
            "cache_read_input_tokens": 55781,
            "cache_creation_input_tokens": 39370,
        },
        "total_cost_usd": 0.2059,
        "duration_ms": 38300,
        "num_turns": 3,
        "modelUsage": {"claude-sonnet-5": {"costUSD": 0.2026}},
        "permission_denials": [],
    }
    data.update(overrides)
    return data


def flags(argv: list[str]) -> dict[str, list[str]]:
    """Group argv into ``flag -> values``, so assertions read like the CLI docs."""
    grouped: dict[str, list[str]] = {}
    current: str | None = None
    for item in argv[1:]:
        if item.startswith("-"):
            current = item
            grouped.setdefault(current, [])
        elif current is not None:
            grouped[current].append(item)
    return grouped


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def forbid_real_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any unfaked spawn fail instead of reaching the real binary.

    Without this, a test that forgets to install the fake would silently invoke
    the installed ``claude``: forty seconds and about $0.20 of subscription
    quota, per call, per run.
    """

    async def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"a test tried to spawn a real process: {args!r}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse)


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FakeCLI:
    """The recording fake, installed over ``asyncio.create_subprocess_exec``."""
    fake = FakeCLI()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return fake


@pytest.fixture(autouse=True)
def prompt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A throwaway prompt, so these tests do not depend on the shipped one."""
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / f"{PROMPT_NAME}.md").write_text(PROMPT_TEMPLATE, encoding="utf-8")
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "PROMPT_DIR", directory)
    yield directory
    prompts.load.cache_clear()


@pytest.fixture
def binary(tmp_path: Path) -> str:
    """A file that looks like an installed CLI. It is never executed."""
    path = tmp_path / BINARY_NAME
    path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def provider(binary: str) -> ClaudeCLIProvider:
    """A provider pointed at the fake binary, with a short timeout."""
    return ClaudeCLIProvider(binary, timeout=1.0, max_turns=6, concurrency=2)


async def run(
    provider: ClaudeCLIProvider,
    *,
    task: LLMTask = LLMTask.RESUME_EXTRACTION,
    documents: tuple[Document, ...] = (),
) -> Any:
    """Shorthand for the one call every test makes."""
    return await provider.complete_json(
        PROMPT_NAME, Answer, task=task, variables={"subject": SUBJECT}, documents=documents
    )


# ── no shell, ever ────────────────────────────────────────────────────


def test_the_module_never_reaches_a_shell_in_executable_code() -> None:
    """The single most important property here. The prompt contains resume text
    and vacancy descriptions scraped from job boards — text this project did not
    write. Handed to a shell as one string, a backtick or a ``;`` in that text is
    command execution on the developer's machine.

    So: no ``create_subprocess_shell``, no ``subprocess.run(..., shell=True)``,
    and no identifier mentioning a shell at all. The word may appear in prose
    explaining why, which is exactly the distinction this test draws — hits are
    allowed in comments and docstrings, and nowhere else.
    """
    source = Path(claude_cli.__file__).read_text(encoding="utf-8")
    prose = {tokenize.COMMENT, tokenize.STRING} | {
        value
        for name, value in vars(tokenize).items()
        if name.startswith("FSTRING") and isinstance(value, int)
    }

    code = [
        token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in prose
    ]
    offenders = [token.string for token in code if "shell" in token.string.lower()]
    assert offenders == [], f"executable code mentions a shell: {offenders}"

    tree = ast.parse(source)
    keywords = [
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    ]
    assert "shell" not in keywords


async def test_the_binary_is_spawned_with_an_argument_list(
    provider: ClaudeCLIProvider, cli: FakeCLI, binary: str
) -> None:
    """``create_subprocess_exec`` with separate string arguments is what makes the
    "no shell" property structural rather than a habit: there is no command line
    for untrusted text to break out of, because there is no command line."""
    cli.script(envelope())

    await run(provider)

    argv = cli.spawn.argv
    assert argv[0] == binary
    assert all(isinstance(item, str) for item in argv)
    assert len(argv) > 1, "one joined string would mean a shell parsed it"


# ── the prompt goes on stdin, not into argv ───────────────────────────


async def test_the_prompt_is_written_to_stdin_and_never_appears_in_argv(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """Two independent reasons, either one sufficient.

    Privacy: command-line arguments are readable from the process table by any
    user on the machine. A resume in argv is a resume published to every process
    on the box, and the person it describes never agreed to that.

    Windows: ``claude`` is a ``.CMD`` shim, so the whole invocation goes through
    ``cmd.exe`` and inherits its 8,191-character limit rather than the 32,767 of
    a normal process. The real extraction prompt is about 7,900 characters before
    the resume is appended; the measured failure was "The command line is too
    long" at 8,062. On stdin the command line stays around 200 characters no
    matter how long the resume is.
    """
    cli.script(envelope())

    await run(provider)

    spawn = cli.spawn
    assert SUBJECT not in " ".join(spawn.argv)
    assert not any("Extract structured data" in item for item in spawn.argv)
    assert "-p" in spawn.argv, "the bare -p flag is what makes the CLI read stdin"

    assert SUBJECT in spawn.stdin
    assert "Extract structured data about" in spawn.stdin
    assert len(" ".join(spawn.argv)) < 1000, "argv must not grow with the prompt"


async def test_argv_length_does_not_grow_with_the_resume(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """The regression guard for the cmd.exe limit: a resume ten thousand
    characters long must leave the command line exactly as short as an empty one,
    because none of it is on the command line."""
    cli.script(envelope(), envelope())

    await run(provider)
    short = len(" ".join(cli.spawns[0].argv))

    await provider.complete_json(
        PROMPT_NAME,
        Answer,
        task=LLMTask.RESUME_EXTRACTION,
        variables={"subject": "x" * 10_000},
    )

    assert len(" ".join(cli.spawns[1].argv)) == short
    assert len(cli.spawns[1].stdin) > 10_000


# ── tool policy ───────────────────────────────────────────────────────


@pytest.mark.parametrize("task", list(LLMTask))
async def test_tools_are_granted_per_task_from_the_policy_table(
    provider: ClaudeCLIProvider, cli: FakeCLI, task: LLMTask
) -> None:
    """A CLI call is an agent with a filesystem, and some prompts carry text from
    the open internet. Only resume extraction may read a file; every other task
    runs with nothing. Parametrised over every task so adding one to the enum
    without adding it to ``TOOL_POLICY`` fails here instead of silently
    inheriting whatever the last branch did."""
    cli.script(envelope())

    await run(provider, task=task)

    granted = flags(cli.spawn.argv)["--allowedTools"]
    expected = list(TOOL_POLICY[task]) or [""]
    assert granted == expected
    if task is LLMTask.RESUME_EXTRACTION:
        assert granted == ["Read"]
    else:
        assert granted == [""], f"{task.value} must be granted no tool at all"


@pytest.mark.parametrize("task", list(LLMTask))
async def test_the_denied_tools_are_named_on_every_call(
    provider: ClaudeCLIProvider, cli: FakeCLI, task: LLMTask
) -> None:
    """Belt and braces. With only Read permitted the model was still observed
    reaching for Bash and burning its whole turn budget on the denial; naming the
    denied tools up front turns a wasted call into a refusal it can act on."""
    cli.script(envelope())

    await run(provider, task=task)

    assert flags(cli.spawn.argv)["--disallowedTools"] == list(DENIED_TOOLS)
    assert "Bash" in DENIED_TOOLS


# ── the working directory is the sandbox ──────────────────────────────


async def test_the_working_directory_holds_only_the_staged_document(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """The tool policy grants Read; the working directory decides what Read can
    reach. A call run in the repository root would let a prompt-injected model
    read .env and put the API key in its answer, and the answer is JSON we then
    store. One fresh directory holding exactly the uploaded file is the bound."""
    cli.script(envelope())
    document = Document(content=b"%PDF-1.7 resume bytes", path="resume.pdf")

    await run(provider, documents=(document,))

    spawn = cli.spawn
    # The staged name carries the document's index. Two documents whose paths
    # differ only by directory share a basename, and the second would otherwise
    # overwrite the first with nothing to show for it.
    assert spawn.listing == ("0-resume.pdf",)
    assert Path(spawn.cwd).name.startswith("wwao-cli-")
    assert "Use the Read tool on 0-resume.pdf" in spawn.stdin


async def test_documents_sharing_a_basename_do_not_overwrite_each_other(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """Two paths differing only by directory still name two documents.

    Staging them under their bare basename put both in one file: the second
    silently replaced the first, the model was told to read the same name
    twice, and one document vanished without an error anywhere.
    """
    cli.script(envelope())
    documents = (
        Document(content=b"%PDF-1.7 first", path="old/resume.pdf"),
        Document(content=b"%PDF-1.7 second", path="new/resume.pdf"),
    )

    await run(provider, documents=documents)

    spawn = cli.spawn
    # Two files on disk, not one, and the model is told to read both. A
    # collision would leave a single name here and in the prompt.
    assert len(set(spawn.listing)) == 2
    for name in spawn.listing:
        assert name in spawn.stdin


async def test_the_working_directory_is_removed_after_the_call(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """A resume left behind in the system temp directory is a copy of someone's
    personal data outliving the request that produced it."""
    cli.script(envelope())

    await run(provider, documents=(Document(content=b"%PDF-1.7", path="resume.pdf"),))

    assert not Path(cli.spawn.cwd).exists()


async def test_a_task_without_read_is_never_told_to_open_a_file(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """Pointing a model at a file the policy will refuse burns the entire turn
    budget on denials and returns nothing, so the file instruction is added only
    when Read is actually granted."""
    cli.script(envelope())

    await run(
        provider,
        task=LLMTask.COVER_LETTER,
        documents=(Document(content=b"%PDF-1.7", path="resume.pdf"),),
    )

    assert "Use the Read tool" not in cli.spawn.stdin


# ── reading the answer ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "wrapped",
    [
        f"```json\n{VALID_ANSWER}\n```",
        f"```\n{VALID_ANSWER}\n```",
        f"  ```json\n{VALID_ANSWER}\n```  \n",
        VALID_ANSWER,
    ],
)
async def test_a_fenced_answer_is_unwrapped(
    provider: ClaudeCLIProvider, cli: FakeCLI, wrapped: str
) -> None:
    """Nothing constrains generation on this path — there are no structured
    outputs behind a CLI — so the model wraps its JSON in a markdown fence however
    firmly the prompt asks it not to. Treating that as invalid would spend two
    extra calls, at CLI prices, on an answer that was already correct."""
    cli.script(envelope(wrapped))

    result = await run(provider)

    assert result.value == Answer(name="Ivan", score=7)
    assert result.attempts == 1


# ── retries, and what they cost ───────────────────────────────────────


async def test_an_invalid_answer_is_retried_with_the_validator_complaint(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """The retry has to tell the model what was wrong. Re-sending the identical
    prompt asks a non-deterministic process to differ by luck, at $0.20 a go."""
    cli.script(envelope(INVALID_ANSWER), envelope(VALID_ANSWER))

    result = await run(provider)

    assert result.attempts == 2
    retry = cli.spawns[1].stdin
    assert "previous answer was rejected" in retry
    assert "name" in retry and "Field required" in retry
    assert SUBJECT in retry, "the retry must still carry the original prompt"


async def test_three_invalid_answers_raise_an_error_naming_the_prompt(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """Two retries and then a real failure. The error names the prompt because
    that is the only actionable part: the fix is always a prompt or schema edit,
    and an anonymous "LLM error" sends someone reading logs to find out which."""
    cli.script(*[envelope(INVALID_ANSWER)] * MAX_ATTEMPTS)

    with pytest.raises(LLMError) as excinfo:
        await run(provider)

    assert PROMPT_NAME in str(excinfo.value)
    assert len(cli.spawns) == MAX_ATTEMPTS
    assert SUBJECT not in str(excinfo.value), "the error must not echo the prompt"


async def test_usage_from_every_attempt_is_summed(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """A retried call is paid for twice, and the ledger has to say so. Reporting
    only the successful attempt would understate CLI spend by exactly the amount
    the failures cost — which is the number someone is looking at when they
    decide whether this task belongs on the subscription at all."""
    cli.script(
        envelope(INVALID_ANSWER, total_cost_usd=0.10, modelUsage={"m": {"costUSD": 0.10}}),
        envelope(INVALID_ANSWER, total_cost_usd=0.20, modelUsage={"m": {"costUSD": 0.20}}),
        envelope(VALID_ANSWER, total_cost_usd=0.30, modelUsage={"m": {"costUSD": 0.30}}),
    )

    result = await run(provider)

    assert result.attempts == 3
    assert result.usage.cost_usd == pytest.approx(0.60)
    assert result.usage.model_costs == pytest.approx({"m": 0.60})
    assert result.usage.input_tokens == 4 * 3
    assert result.usage.output_tokens == 3395 * 3


# ── failure paths ─────────────────────────────────────────────────────


async def test_a_timeout_kills_the_process_and_says_how_long_it_waited(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """A CLI call is a local process holding a semaphore slot. Left alive it keeps
    that slot forever and the pipeline stops after two hangs, so the process is
    killed rather than merely abandoned. The message carries the limit because
    the fix is usually to raise it for a genuinely slow document."""
    cli.script(Hang())

    with pytest.raises(LLMError) as excinfo:
        await run(provider)

    assert cli.processes[0].killed
    assert "1s" in str(excinfo.value)
    assert "did not answer" in str(excinfo.value)


async def test_a_timeout_releases_the_concurrency_slot(binary: str, cli: FakeCLI) -> None:
    """The slot is held by an ``async with``, so a raise inside it must still
    give the slot back. A leaked slot is a pipeline that wedges after as many
    bad documents as there are slots, with no error to point at.

    ``concurrency=1`` on purpose: with the shared two-slot provider a leak would
    still leave a spare, the follow-up call would succeed, and this test would
    pass while the bug it names was present. One slot means the second call can
    only run if the first gave its slot back. The ``wait_for`` turns the leak
    into a failure in a second rather than a suite that hangs forever.
    """
    provider = ClaudeCLIProvider(binary, timeout=0.05, concurrency=1)
    cli.script(Hang(), envelope())

    with pytest.raises(LLMError):
        await run(provider)
    result = await asyncio.wait_for(run(provider), timeout=5.0)

    assert result.value.name == "Ivan"


async def test_a_non_zero_exit_reports_the_status_and_nothing_else(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """stderr is read and dropped on purpose. The CLI echoes its input when it
    fails, so stderr can contain the whole resume — and an exception message ends
    up in logs, in an HTTP problem document, and in a bug report."""
    cli.script(Exit(3, stderr=f"Traceback: refused to process {SUBJECT}".encode()))

    with pytest.raises(LLMError) as excinfo:
        await run(provider)

    message = str(excinfo.value)
    assert "3" in message
    assert SUBJECT not in message
    assert "Traceback" not in message


async def test_a_non_json_envelope_is_reported_as_such(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """``--output-format json`` is a request, not a guarantee: a CLI that prints
    an update notice or a login prompt exits 0 with prose on stdout. That has to
    fail as "no envelope" rather than as a confusing schema error."""
    cli.script(Raw("Claude Code needs to be updated."))

    with pytest.raises(LLMError) as excinfo:
        await run(provider)

    assert "JSON envelope" in str(excinfo.value)


async def test_a_refused_tool_is_named_in_the_error(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """``is_error`` with permission denials means the model spent the call
    knocking on a door the policy holds shut. Naming the tool turns "the CLI
    failed" into either a prompt fix or a deliberate policy decision."""
    cli.script(
        envelope(
            is_error=True,
            permission_denials=[{"tool_name": "Bash"}, {"tool_name": "WebFetch"}],
        )
    )

    with pytest.raises(LLMError) as excinfo:
        await run(provider)

    message = str(excinfo.value)
    assert "Bash" in message and "WebFetch" in message


# ── usage accounting ──────────────────────────────────────────────────


async def test_usage_is_read_from_the_envelope_and_marked_as_subscription(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """CLI dollars are quota already paid for; API dollars are an invoice
    arriving. Summing them produces a number that means nothing, so the
    accounting tag travels with every record and the /metrics endpoint keeps the
    two totals apart."""
    cli.script(envelope())

    result = await run(provider)

    usage = result.usage
    assert usage.provider == "cli"
    assert usage.accounting == "subscription"
    assert usage.task is LLMTask.RESUME_EXTRACTION
    assert usage.input_tokens == 4
    assert usage.output_tokens == 3395
    assert usage.cache_read_tokens == 55781
    assert usage.cache_write_tokens == 39370
    assert usage.cost_usd == pytest.approx(0.2059)
    assert usage.duration_ms == pytest.approx(38300)
    assert usage.total_tokens == 4 + 3395 + 55781 + 39370


async def test_the_primary_model_is_the_dearest_one_in_the_breakdown(
    provider: ClaudeCLIProvider, cli: FakeCLI
) -> None:
    """One CLI turn touches more than one model — a small one for routing and
    summarising, the real one for the answer. Reporting whichever came first in a
    dict would attribute extraction quality to the cheap model and make the cost
    per task unreadable, so the breakdown is kept whole and the expensive model
    is the one named."""
    cli.script(
        envelope(
            modelUsage={
                "claude-haiku-4": {"costUSD": 0.0033},
                "claude-sonnet-5": {"costUSD": 0.2026},
            }
        )
    )

    result = await run(provider)

    assert result.usage.model == "claude-sonnet-5"
    assert result.usage.model_costs == pytest.approx(
        {"claude-haiku-4": 0.0033, "claude-sonnet-5": 0.2026}
    )


# ── availability ──────────────────────────────────────────────────────


async def test_an_unresolvable_binary_is_reported_without_spawning_anything(
    tmp_path: Path, cli: FakeCLI
) -> None:
    """The router falls back on "this provider is not here", never on "it
    answered badly". So being absent has to be a distinct, cheap, spawn-free
    signal — and the check happens before the temporary directory is created and
    the documents are written to disk."""
    provider = ClaudeCLIProvider(str(tmp_path / "not-installed"))

    assert provider.is_available() is False

    with pytest.raises(CLIUnavailableError) as excinfo:
        await run(provider)

    assert cli.spawns == []
    assert BINARY_NAME in str(excinfo.value)
    assert isinstance(excinfo.value, LLMError), "the router routes on LLMError subclasses"


def test_an_explicit_binary_path_wins_over_path(
    tmp_path: Path, binary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine with a global install and an nvm-managed one resolves whichever
    PATH happens to reach first, and they are not the same version. The setting
    is how a deployment pins it."""
    other = tmp_path / "other-claude"
    other.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    other.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda _name: str(other))

    assert resolve_binary(binary) == binary
    assert resolve_binary(None) == str(other)


def test_a_missing_or_non_file_path_resolves_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale setting after an uninstall must read as "not available" so the
    router falls back, rather than as FileNotFoundError in the middle of a
    pipeline run. A directory counts as missing too."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert resolve_binary(str(tmp_path / "gone")) is None
    assert resolve_binary(str(tmp_path)) is None
    assert resolve_binary(None) is None


# ── concurrency ───────────────────────────────────────────────────────


async def test_parallel_calls_are_capped_at_the_configured_concurrency(
    binary: str, cli: FakeCLI
) -> None:
    """Each call is a local Claude Code process, not a request to a server: it
    competes for this machine's CPU and memory. Unbounded fan-out from a batch
    would spawn one per item and make the laptop unusable, so the semaphore is
    the only thing between a ten-item batch and ten agents."""
    provider = ClaudeCLIProvider(binary, timeout=5.0, concurrency=2)
    cli.delay = 0.02
    cli.script(*[envelope()] * 5)

    # Bounded: a semaphore that never gives a slot back deadlocks the gather,
    # and a test that hangs forever is a CI job someone has to go and kill.
    results = await asyncio.wait_for(
        asyncio.gather(*(run(provider) for _ in range(5))), timeout=10.0
    )

    assert len(results) == 5
    assert cli.peak == 2, f"{cli.peak} processes ran at once with concurrency=2"
