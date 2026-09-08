"""The workshop: how the owner's documents are written, without touching code.

    sections   finding a named section of a plain-text document, and its items
    rules      what a rule is, and what it says about a finished document
    truth      the boundary — a rule describes form, never facts
    references documents kept as examples of shape, and getting text out of them
    prompt     rendering rules and references into the parts of a prompt
    store      the rows in and the rules out
    service    the sequence: save, validate, list, and generate a trial letter

Two levers, and they do different jobs on purpose. A **reference** carries form
by example — structure, length, register, the order things are said in — and is
untrusted text quoted to the model as data. A **rule** carries a constraint that
a function can measure, and a hard one is enforced by throwing the document away
rather than by asking nicely. Neither carries a fact about the candidate: the
profile is the only evidence there is, and ``truth`` refuses to store a rule that
would have a document claim otherwise.

Nothing here is imported at package level. The letter generator imports
``workshop.rules`` and ``workshop`` imports the letter guard, so a re-export
here would decide which of the two packages must be imported first.
"""
