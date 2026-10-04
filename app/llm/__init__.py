"""LLM access layer: providers, retry policy, prompts and the LLM-backed translator.

Dependency rule: nothing in this package imports ``app.translator``; the
dependency goes one way only (``app.translator`` imports ``app.llm``).
"""
