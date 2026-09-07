"""Printing text this program did not write, on a console that cannot hold it.

Everything the queue view shows — a job title, an employer's name, a sentence hh
wrote about an application — arrives from somewhere else, and the console it is
printed on encodes cp1251. A character outside that codepage does not degrade;
it raises ``UnicodeEncodeError`` in the middle of a report, after the work is
done and before it is shown. That has already happened twice in this repository:
once on a box-drawing character in a Russian table, once on a Kazakh letter in
an employer's name — this is hh.KZ, where ә ғ қ ң ө ұ ү һ are ordinary and none
of them exist in cp1251.

Two defences, and both are here because either alone leaves a hole.

:func:`printable` reduces a string to what the console can put on screen, the
same way ``agent.state_page.printable`` does for the agent's own output. It is
applied where text is composed into a report, so the column widths are computed
on the characters that will actually be printed — cp1251 is single-byte, so one
replaced character stays one column wide.

:func:`harden` makes the stream itself replace instead of raise. That covers
what :func:`printable` was not applied to, including a traceback: the point is
that no report ever dies on the last line because of a character in somebody
else's job title.
"""

import contextlib
import io
from typing import Final, TextIO

#: What the owner's console encodes to. Used when a stream does not say what it
#: is — a pipe, a captured test stream — because being wrong in this direction
#: costs a few question marks and being wrong in the other direction costs the
#: whole report.
DEFAULT_ENCODING: Final[str] = "cp1251"


def encoding_of(stream: TextIO) -> str:
    """What this stream can encode, as far as it will say.

    ``io.StringIO`` has no encoding at all and a redirected pipe may report
    ``None``; both fall back to :data:`DEFAULT_ENCODING`.
    """
    declared = getattr(stream, "encoding", None)
    return declared if isinstance(declared, str) and declared else DEFAULT_ENCODING


def printable(text: str, encoding: str = DEFAULT_ENCODING) -> str:
    """The text, with anything the console cannot show reduced to ``?``.

    A visible degradation, and the smaller loss: Cyrillic, the guillemets, the
    em dash and the ellipsis all survive cp1251, so a real sentence still reads
    as itself.
    """
    return text.encode(encoding, errors="replace").decode(encoding)


def harden(stream: TextIO) -> None:
    """Make this stream replace unencodable characters rather than raise.

    Only a real :class:`io.TextIOWrapper` can be reconfigured — that is
    ``sys.stdout`` and ``sys.stderr`` in a terminal. Anything else (a captured
    test stream, an already-detached stream) is left alone, which is why this
    is a backstop and :func:`printable` is the actual measure.
    """
    if isinstance(stream, io.TextIOWrapper):
        # A detached or already-closed stream cannot be reconfigured, and this
        # is a backstop: failing to install it must not be what ends a run.
        with contextlib.suppress(ValueError, OSError):
            stream.reconfigure(errors="replace")
