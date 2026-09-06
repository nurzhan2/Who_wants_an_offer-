"""The two copies of the link detector, compared as patterns rather than as examples.

``backend/app/letters/guard.py`` and ``agent/letter.py`` hold the same regular
expression twice, deliberately: the two packages may not import each other, a
test in ``agent/tests/test_isolation.py`` enforces that by walking the import
graph, and duplicating a pattern is the smaller cost.

What the duplication needs is a way to notice when the copies stop agreeing, and
the shared corpus is not it. ``backend/tests/test_letters.py`` and
``agent/tests/test_agent.py`` assert the same sixteen strings against their own
copy, and sixteen strings pin sixteen strings: drop ``uz`` from one list and not
the other, or reorder an alternation so a nested group changes meaning, and both
suites stay green while a letter written by the backend is refused at the point
of sending — after the human has read it and pressed confirm. That failure looks
like the agent malfunctioning, and the cause is three files away.

So this compares the definitions themselves. It reads ``agent/letter.py`` as
**source text parsed with :mod:`ast`, never as an import**: importing it here
would be the exact boundary violation the isolation test forbids, and the
comparison would then be measuring the version this process happened to load.
Parsing also means the file is inspected without running it, which is the
technique ``agent/tests/test_isolation.py`` and ``agent/tests/test_boundaries.py``
already use for the same reason.
"""

import ast
from pathlib import Path

import pytest

from app.letters import guard

pytestmark = pytest.mark.unit

AGENT_LETTER = Path(__file__).resolve().parents[2] / "agent" / "letter.py"


def _module() -> ast.Module:
    """``agent/letter.py`` as a syntax tree. Read, never imported, never run."""
    return ast.parse(AGENT_LETTER.read_text(encoding="utf-8"))


def _assignments(tree: ast.Module) -> dict[str, ast.expr]:
    """Every module-level ``NAME = ...``, by name.

    Module level only: a name bound inside a function is not the constant the
    detector is built from, and picking one up would make this test agree with
    something nobody uses.
    """
    bound: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            bound[node.target.id] = node.value
    return bound


def _text(node: ast.expr, bound: dict[str, ast.expr]) -> str:
    """A string built from literals, ``+`` and other module constants.

    Both copies write the pattern as literal fragments concatenated around
    ``TOP_LEVEL_DOMAINS``, so resolving exactly those three forms is enough to
    reconstruct the source string. Anything else raises rather than guessing:
    a pattern this cannot read is a pattern this cannot compare, and quietly
    returning something else would be worse than failing.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return _text(bound[node.id], bound)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left, bound) + _text(node.right, bound)
    raise AssertionError(f"cannot read {ast.dump(node)} as a string")


def _compiled_pattern(name: str, bound: dict[str, ast.expr]) -> str:
    """The pattern text passed to ``re.compile`` for this module constant."""
    call = bound[name]
    assert isinstance(call, ast.Call), f"{name} is not a re.compile(...) call"
    return _text(call.args[0], bound)


def test_the_two_copies_list_the_same_top_level_domains() -> None:
    """The list is what separates ``nurzhan.dev`` from ``и т.д.``, in both packages.

    A TLD in one list and not the other is silent in both suites unless an
    example happens to use it, and the whole list is exactly the part no example
    covers exhaustively.
    """
    bound = _assignments(_module())

    assert _text(bound["TOP_LEVEL_DOMAINS"], bound) == guard.TOP_LEVEL_DOMAINS


def test_the_two_copies_compile_the_same_link_pattern() -> None:
    """Character for character, including the flags written into the pattern.

    ``(?xi)`` is inside the string in both copies, so comparing the source text
    compares the flags too.
    """
    bound = _assignments(_module())

    assert _compiled_pattern("LINK", bound) == guard.LINK.pattern


def test_the_two_copies_compile_the_same_at_sign_pattern() -> None:
    """The full-width at-sign is the one that arrives from phones and is easy to lose."""
    bound = _assignments(_module())

    assert _compiled_pattern("AT_SIGN", bound) == guard.AT_SIGN.pattern


def test_the_technology_exception_set_matches_wherever_the_agent_has_one() -> None:
    """``TECHNOLOGY_SPELLINGS`` is backend-only today, and this is what will hold it.

    The backend needed the exception because dropping a matched skill out of the
    fallback deletes the letter's evidence; the agent needs it for the milder
    reason that ``ASP.NET`` in a letter a person wrote sends them to a
    confirmation screen for nothing. Until ``agent/letter.py`` gets it — a change
    that belongs to the agent side and cannot be made from here — the two
    detectors genuinely disagree about ``ASP.NET``, and that disagreement is
    stated in ``app/letters/guard.py`` rather than hidden.

    This asserts the part that can be asserted now: if the agent copy grows the
    set, it grows the same one. It does not claim the agent is fixed.
    """
    spellings = _assignments(_module()).get("TECHNOLOGY_SPELLINGS")
    if spellings is None:
        pytest.skip("agent/letter.py has no TECHNOLOGY_SPELLINGS yet; see guard.py's docstring")

    # ``frozenset({...})`` is a call, not a literal, so unwrap it before reading.
    if isinstance(spellings, ast.Call):
        spellings = spellings.args[0]

    assert set(ast.literal_eval(spellings)) == set(guard.TECHNOLOGY_SPELLINGS)
