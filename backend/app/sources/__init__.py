"""Job source connectors.

Every connector subclasses ``BaseSource`` and registers itself; the pipeline
only ever talks to the registry, never to a connector module directly.
"""
