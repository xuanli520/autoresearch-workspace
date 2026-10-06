"""Compatibility import for the shared workspace process-control module.

Standalone handoff bundles embed the canonical implementation at this path.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.process_control import processes

sys.modules[__name__] = processes
