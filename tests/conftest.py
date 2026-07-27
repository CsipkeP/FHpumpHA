"""Make the integration importable without a Home Assistant installation.

``custom_components/fujitsu_waterstage/__init__.py`` will import Home Assistant
once phase 3 lands.  Registering a stand-in package object with a ``__path__``
lets the submodules resolve their relative imports while that ``__init__`` is
never executed, so ``codec``, ``registers`` and ``hub`` stay testable on their
own.  ``tools/dump.py`` does the same thing for the same reason.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "fujitsu_waterstage"

if "fujitsu_waterstage" not in sys.modules:
    _package = types.ModuleType("fujitsu_waterstage")
    _package.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
    sys.modules["fujitsu_waterstage"] = _package

from fujitsu_waterstage.registers import load_register_map  # noqa: E402


@pytest.fixture(scope="session")
def register_map():
    """The packaged register map."""
    return load_register_map()
