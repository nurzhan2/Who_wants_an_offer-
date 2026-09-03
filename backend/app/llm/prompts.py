"""Prompt loading.

Prompts live as files under ``app/llm/prompts/`` and never inline in a call
site: they are the part of an LLM feature most likely to be edited by someone
who is not editing code, and a diff of a .md file is readable.

Placeholders are ``{{name}}``. Deliberately not ``str.format`` — prompts contain
JSON examples full of braces, and every one of them would have to be escaped.
"""

import re
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class PromptNotFoundError(LookupError):
    """A prompt was requested by a name that has no file."""


@lru_cache(maxsize=32)
def load(name: str) -> str:
    """Read a prompt template by name, without the .md suffix."""
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        available = sorted(p.stem for p in PROMPT_DIR.glob("*.md"))
        raise PromptNotFoundError(f"no prompt named {name!r}; available: {available}")
    return path.read_text(encoding="utf-8")


def render(name: str, **variables: object) -> str:
    """Fill a template's ``{{placeholders}}``.

    An unknown placeholder is an error rather than an empty string: a prompt
    silently missing half its context produces plausible nonsense, which is the
    hardest kind of bug to notice.
    """
    template = load(name)
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            missing.append(key)
            return match.group(0)
        return str(variables[key])

    rendered = PLACEHOLDER.sub(substitute, template)
    if missing:
        raise KeyError(
            f"prompt {name!r} needs variables that were not given: {sorted(set(missing))}"
        )

    # The other direction matters just as much. A variable the template never
    # uses means the caller believes it is sending something the model will
    # never see — which is how the resume text itself once went missing for
    # every non-PDF upload, with the prompt still claiming it was included.
    unused = sorted(set(variables) - set(PLACEHOLDER.findall(template)))
    if unused:
        raise KeyError(f"prompt {name!r} has no placeholder for: {unused}")
    return rendered
