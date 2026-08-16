#!/usr/bin/env python3
"""Microsoft To Do CLI — entry point.

Standard library only, so it runs from a bare `python3 scripts/todo.py` with no
install step, no virtualenv and no network fetch beyond Graph itself.

    python3 scripts/todo.py --help
"""

from __future__ import annotations

import sys
from pathlib import Path

# Deliberately kept despite the declared 3.9 floor: this guard only ever runs on
# interpreters *below* that floor, which is precisely when it is not dead code.
# Nothing above this line uses 3.9-only syntax, so the message always gets out.
if sys.version_info < (3, 9):  # pragma: no cover
    sys.stderr.write("ms-todo-skill requires Python 3.9 or newer\n")
    raise SystemExit(2)

# Make the sibling package importable regardless of the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mstodo.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
