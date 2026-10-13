"""Entry: `python3 -I tools/g03_conductor <cmd>` (directory form keeps -I isolation and still finds the package)."""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.g03_conductor.cli import main
else:
    from .cli import main

raise SystemExit(main())
