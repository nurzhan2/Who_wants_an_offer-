"""LLM provider implementations.

One module per provider, all conforming to ``app.llm.base.LLMProvider``. The
router picks between them per task; nothing else imports a provider directly.
"""
