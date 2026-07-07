#!/usr/bin/env python3
"""
Hermes Console — thin entrypoint.

All application logic lives in src/hermes_console/.
This file only adds the src/ directory to Python's path and delegates to
the main() function inside the routes module.
"""

import os
import sys

# Make the hermes_console package importable without requiring an install step.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_console.routes.handler import main  # noqa: E402

if __name__ == "__main__":
    main()
