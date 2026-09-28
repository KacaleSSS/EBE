"""Load the retained engine without copying private project data."""
import importlib
import sys
from pathlib import Path


def modules():
    location = str(Path(__file__).parent / "legacy")
    if location not in sys.path:
        sys.path.insert(0, location)
    return tuple(importlib.import_module(name) for name in ("engine", "research", "pipeline"))
