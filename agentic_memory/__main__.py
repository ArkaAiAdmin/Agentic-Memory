"""Module entry point: ``python -m agentic_memory ...``.

Defers to :func:`agentic_memory.main` so the package is invocable as
both a console script (``agentic-memory``) and a module
(``python -m agentic_memory``).
"""


from agentic_memory import main

if __name__ == "__main__":
    raise SystemExit(main())
