"""The Claude Code CLI as an LLM provider.

Why it exists: resume extraction and cover letters run rarely and want the best
answer available, and a Claude Code subscription already pays for that. Sending
them to the API instead would be paying twice.

Why it is not used for everything: a CLI call carries Claude Code's own system
prompt whatever the payload. Measured on this project — a prompt whose entire
answer is ``{"ok": true}`` reported 36,747 cache-read plus 13,857 cache-creation
tokens and ``total_cost_usd`` of $0.064. Re-rank at 120 calls a day would burn
roughly $44 a day of subscription quota before a single useful token, and
Telegram parsing is worse. Those tasks belong elsewhere; see
``app.core.config.default_routing``.

Three things here are load-bearing and easy to get wrong.

**Never a shell, and never in argv.** The prompt carries resume text and
Telegram posts — input this project did not write, about people who did not ask
to be in a process listing. ``create_subprocess_exec`` with an argument list
means there is no shell string for that input to break out of, and the prompt
goes in on **stdin** so it never becomes a command-line argument at all.
Arguments are readable from the process table by anyone on the machine, and a
resume is exactly the kind of thing that must not be there.

Passing it on stdin is also the only thing that works. ``claude`` on Windows is
a ``.CMD`` shim, so the whole invocation goes through ``cmd.exe`` and inherits
its 8,191-character limit rather than the 32,767 of a normal process. The real
extraction prompt is about 7,900 characters before the resume is added, and
measured failure was "The command line is too long" at 8,062. On stdin the
command line is 198 characters whatever the prompt contains.

**The binary is resolved, not named.** ``create_subprocess_exec("claude", ...)``
raises FileNotFoundError on Windows, where the launcher is ``claude.CMD``:
exec does not do PATHEXT resolution the way a shell does. ``shutil.which``
returns the real path, once, at startup.

**Tools are granted per task, and by a table.** See ``app.llm.base.TOOL_POLICY``.
Only resume extraction may read a file, and only inside a directory this module
creates containing exactly that one file.
"""

import asyncio
import json
import os
import re
import shutil
import time
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.llm import prompts
from app.llm.base import (
    DENIED_TOOLS,
    TOOL_POLICY,
    BatchViaLoop,
    Document,
    Effort,
    LLMResult,
    LLMTask,
    LLMUsage,
)

logger = get_logger(__name__)

BINARY_NAME = "claude"
#: Without structured outputs the model can wrap its answer in a fence however
#: firmly the prompt asks it not to.
FENCE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)
#: Two retries rather than the API path's one: nothing constrains generation
#: here, so a malformed answer is a normal event rather than a surprise.
MAX_ATTEMPTS = 3


class CLIUnavailableError(LLMError):
    """The Claude Code CLI is not installed or not runnable."""

    title = "Claude Code CLI unavailable"
    problem_type = "cli-unavailable"


def resolve_binary(explicit: str | None = None) -> str | None:
    """Full path to the CLI, or None when it is not installed.

    Resolved once and cached by the caller. An explicit setting wins, which is
    how a machine with several installs picks one; otherwise PATH decides.
    """
    candidate = explicit or shutil.which(BINARY_NAME)
    if candidate is None:
        return None
    path = Path(candidate)
    if not path.is_file():
        return None
    if os.name != "nt" and not os.access(path, os.X_OK):
        return None
    return str(path)


class ClaudeCLIProvider(BatchViaLoop):
    """Runs prompts through the local ``claude`` binary."""

    name = "cli"

    def __init__(
        self,
        binary: str | None = None,
        *,
        timeout: float = 300.0,
        max_turns: int = 6,
        concurrency: int = 2,
    ) -> None:
        # Resolved at construction, not per call: a stale path after a Claude
        # Code upgrade should surface as one clear startup failure rather than
        # a FileNotFoundError in the middle of a pipeline run.
        self._binary = resolve_binary(binary)
        self._timeout = timeout
        self._max_turns = max_turns
        # It is a local process competing for the same CPU, not a server.
        self._slots = asyncio.Semaphore(concurrency)
        logger.info("llm.cli.resolved", binary=self._binary, available=self._binary is not None)

    def is_available(self) -> bool:
        """Whether the binary was found. Cached; no process is spawned to ask."""
        return self._binary is not None

    async def complete_json[ResultT: BaseModel](
        self,
        prompt_name: str,
        response_model: type[ResultT],
        *,
        task: LLMTask,
        variables: dict[str, Any] | None = None,
        documents: Sequence[Document] = (),
        effort: Effort | None = None,
        cached_prefix: str | None = None,
    ) -> LLMResult[ResultT]:
        """Render a prompt, run it through the CLI, validate the answer.

        ``effort`` and ``cached_prefix`` are accepted and ignored: the CLI
        exposes neither. They are part of the protocol because the API provider
        needs them, and silently dropping them here is better than making every
        caller know which provider it is talking to.
        """
        if self._binary is None:
            raise CLIUnavailableError(
                f"the {BINARY_NAME!r} binary is not on PATH; install Claude Code "
                "or route this task to another provider"
            )

        allowed = TOOL_POLICY[task]
        rendered = prompts.render(prompt_name, **(variables or {}))
        instruction = _schema_instruction(response_model)

        with TemporaryDirectory(prefix="wwao-cli-") as workdir:
            # A directory of our own holding nothing but the documents this
            # call needs. The tool policy grants Read at most; the working
            # directory decides what Read can reach.
            filenames = _stage(documents, Path(workdir))
            prompt = _assemble(rendered, instruction, filenames, allowed)
            return await self._attempt_until_valid(
                prompt=prompt,
                response_model=response_model,
                task=task,
                allowed=allowed,
                workdir=workdir,
                prompt_name=prompt_name,
            )

    async def _attempt_until_valid[ResultT: BaseModel](
        self,
        *,
        prompt: str,
        response_model: type[ResultT],
        task: LLMTask,
        allowed: Sequence[str],
        workdir: str,
        prompt_name: str,
    ) -> LLMResult[ResultT]:
        """Run the CLI, validating the answer and feeding failures back."""
        total = _EMPTY_USAGE
        message = prompt

        for attempt in range(1, MAX_ATTEMPTS + 1):
            envelope, usage = await self._run(message, allowed=allowed, workdir=workdir, task=task)
            total = _merge(total, usage)

            body = _strip_fence(str(envelope.get("result") or ""))
            try:
                value = response_model.model_validate_json(body)
            except ValidationError as exc:
                if attempt == MAX_ATTEMPTS:
                    logger.warning(
                        "llm.cli.invalid_payload",
                        prompt=prompt_name,
                        task=task.value,
                        attempts=attempt,
                    )
                    raise LLMError(
                        f"{prompt_name} returned a payload that failed validation "
                        f"{MAX_ATTEMPTS} times"
                    ) from exc
                message = (
                    f"{prompt}\n\n## Your previous answer was rejected\n\n"
                    f"It did not match the required schema. The validator said:\n{exc}\n\n"
                    "Return the corrected JSON object and nothing else."
                )
                continue

            return LLMResult(value=value, usage=_finalise(total, task), attempts=attempt)

        raise LLMError(f"{prompt_name} exhausted its attempts")  # pragma: no cover

    async def _run(
        self, prompt: str, *, allowed: Sequence[str], workdir: str, task: LLMTask
    ) -> tuple[dict[str, Any], LLMUsage]:
        """One subprocess, one JSON envelope back."""
        binary = self._binary
        if binary is None:  # pragma: no cover - complete_json guards this
            raise CLIUnavailableError("the CLI binary was not resolved")
        argv = [
            binary,
            # -p with no argument: the prompt arrives on stdin. See the module
            # docstring for why it must not be in argv.
            "-p",
            "--output-format",
            "json",
            "--max-turns",
            str(self._max_turns),
            "--allowedTools",
            *(allowed or [""]),
            "--disallowedTools",
            *DENIED_TOOLS,
        ]

        started = time.perf_counter()
        async with self._slots:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                # stderr is read and dropped: it can echo the prompt, which
                # carries resume text, and nothing here may put that in a log.
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode("utf-8")), timeout=self._timeout
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise LLMError(
                    f"the CLI did not answer within {self._timeout:.0f}s and was terminated"
                ) from exc
        duration_ms = (time.perf_counter() - started) * 1000

        if process.returncode != 0:
            # stderr can echo the prompt, which carries resume text. Only the
            # exit code leaves this function.
            raise LLMError(f"the CLI exited with status {process.returncode}")

        try:
            envelope: dict[str, Any] = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise LLMError("the CLI did not return a JSON envelope") from exc

        if envelope.get("is_error"):
            denials = envelope.get("permission_denials") or []
            denied = sorted({str(d.get("tool_name")) for d in denials if isinstance(d, dict)})
            raise LLMError(
                "the CLI reported an error"
                + (f"; it was denied these tools: {denied}" if denied else "")
            )

        return envelope, _usage_from(envelope, task, duration_ms)


#: Reused so a zero-usage starting point does not allocate a dataclass per call.
_EMPTY_USAGE = LLMUsage(provider="cli", model="", task=LLMTask.TOOLING, accounting="subscription")


def _schema_instruction(response_model: type[BaseModel]) -> str:
    """Describe the required shape, since the CLI cannot constrain generation.

    The API path gets structured outputs and the schema is enforced by the
    server. Here the schema is only a request, which is also why this provider
    retries twice rather than once.
    """
    schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False, indent=None)
    return (
        "## Response format\n\n"
        "Return one JSON object and nothing else: no prose before it, no prose "
        "after it, and no markdown code fence. It must validate against this "
        f"JSON Schema:\n\n{schema}"
    )


def _stage(documents: Sequence[Document], workdir: Path) -> list[str]:
    """Write the documents into the call's own directory. Returns their names."""
    names: list[str] = []
    for index, document in enumerate(documents):
        suffix = ".pdf" if document.media_type == "application/pdf" else ".bin"
        stem = Path(document.path).stem if document.path else "document"
        # The index is always in the name. Two documents whose paths differ only
        # by directory share a basename, and the second would otherwise
        # overwrite the first silently, leaving the model told to read one file
        # twice and one document simply gone.
        name = f"{index}-{stem}{suffix}"
        (workdir / name).write_bytes(document.content)
        names.append(name)
    return names


def _assemble(rendered: str, schema: str, filenames: Sequence[str], allowed: Sequence[str]) -> str:
    """Build the prompt the CLI receives.

    The file instruction is added only when the task is actually allowed to
    read, so a prompt can never point at a file the policy would refuse — which
    would burn the whole turn budget on denials.
    """
    parts: list[str] = []
    if filenames and "Read" in allowed:
        listed = ", ".join(filenames)
        parts.append(
            "## Files\n\n"
            f"Use the Read tool on {listed} in the current directory, once each, "
            "and then answer. Do not use any other tool."
        )
    parts.append(rendered)
    parts.append(schema)
    return "\n\n".join(parts)


def _strip_fence(text: str) -> str:
    """Unwrap a markdown code fence, which the model adds despite instructions."""
    match = FENCE.match(text)
    return match.group("body") if match else text.strip()


def _usage_from(envelope: dict[str, Any], task: LLMTask, duration_ms: float) -> LLMUsage:
    """Read the CLI's own accounting out of its envelope.

    ``accounting="subscription"``: these dollars are quota, not an invoice.
    Adding them to API spend would produce a number that means nothing.
    """
    usage = envelope.get("usage") or {}
    model_costs = {
        str(model): float(entry.get("costUSD") or 0.0)
        for model, entry in (envelope.get("modelUsage") or {}).items()
        if isinstance(entry, dict)
    }
    # A CLI turn can touch more than one model; the dearest one is the one that
    # actually did the work.
    primary = max(model_costs, key=lambda name: model_costs[name], default="")
    return LLMUsage(
        provider="cli",
        model=primary,
        task=task,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        duration_ms=float(envelope.get("duration_ms") or duration_ms),
        accounting="subscription",
        model_costs=model_costs,
    )


def _merge(left: LLMUsage, right: LLMUsage) -> LLMUsage:
    """Accumulate across retries: a retried call is paid for twice."""
    costs = dict(left.model_costs)
    for model, cost in right.model_costs.items():
        costs[model] = costs.get(model, 0.0) + cost
    return LLMUsage(
        provider=right.provider,
        model=right.model or left.model,
        task=right.task,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cache_read_tokens=left.cache_read_tokens + right.cache_read_tokens,
        cache_write_tokens=left.cache_write_tokens + right.cache_write_tokens,
        cost_usd=(left.cost_usd or 0.0) + (right.cost_usd or 0.0),
        duration_ms=left.duration_ms + right.duration_ms,
        accounting="subscription",
        model_costs=costs,
    )


def _finalise(usage: LLMUsage, task: LLMTask) -> LLMUsage:
    """Stamp the task onto an accumulated usage record."""
    return LLMUsage(
        provider=usage.provider,
        model=usage.model,
        task=task,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=usage.cost_usd,
        duration_ms=usage.duration_ms,
        accounting="subscription",
        model_costs=usage.model_costs,
    )
