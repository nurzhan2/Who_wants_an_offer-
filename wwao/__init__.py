"""The one entry point, and the reason it is a package of its own.

``python -m wwao <subcommand>`` drives the whole thing: crawl the sources, score
what came back, write the letters, look at the queue, and — only with a person
at the keyboard — apply.

**Why this is not in ``backend/`` and not in ``agent/``.** Those two are kept
apart on purpose, and the separation is enforced rather than agreed:
``agent/tests/test_isolation.py`` parses every file under ``agent/`` for an
import of ``app`` and every file under ``backend/app/`` for an import of
``agent`` or ``playwright``. A single command that can both crawl and apply has
to reach both worlds, so it cannot live inside either tree without breaking the
test that keeps them apart — and the test is right. The crawler is anonymous,
read-only and runs on a server; the agent acts under a person's own hh account
on their laptop and sends things. One process holding both is how a scheduler
comes to send an application.

So this package is the seam, and it holds the boundary by holding nothing: it
imports neither world at any point, under any subcommand. Every subcommand runs
its work in a child process that imports exactly one of them —
``scripts/run_pipeline.py`` and friends on the backend side, ``python -m
agent.run`` on the agent side. ``wwao/tests/test_separation.py`` asserts both
halves, statically and by starting a real process and looking at what it loaded.

The only subcommand that sends anything is ``apply``, and it refuses to start
without a terminal. See :mod:`wwao.cli`.
"""
