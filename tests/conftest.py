"""Test bootstrap.

The integration is importable under two names and both must resolve to the
*same* module objects, or a ``Register`` built by one half of the test suite
would not be recognised by the other:

* ``custom_components.fujitsu_waterstage`` -- what Home Assistant loads.
* ``fujitsu_waterstage`` -- the short name the codec, register and hub tests
  use, and the one ``tools/dump.py`` synthesises so it can run without Home
  Assistant at all.

With Home Assistant installed the short name is aliased onto the real package.
Without it, a stand-in package object is registered instead so the layers below
Home Assistant stay testable on their own.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "fujitsu_waterstage"

#: Modules that must be reachable under both names.
_SHARED_MODULES = (
    "codec",
    "const",
    "coordinator",
    "discovery",
    "entity",
    "hub",
    "registers",
)

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import homeassistant  # noqa: F401
except ImportError:  # pragma: no cover - exercised only without Home Assistant
    HOMEASSISTANT_INSTALLED = False
else:
    HOMEASSISTANT_INSTALLED = True

if "fujitsu_waterstage" not in sys.modules:
    if HOMEASSISTANT_INSTALLED:
        _package = importlib.import_module("custom_components.fujitsu_waterstage")
        sys.modules["fujitsu_waterstage"] = _package
        for _name in _SHARED_MODULES:
            sys.modules[f"fujitsu_waterstage.{_name}"] = importlib.import_module(
                f"custom_components.fujitsu_waterstage.{_name}"
            )
    else:
        _package = types.ModuleType("fujitsu_waterstage")
        _package.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
        sys.modules["fujitsu_waterstage"] = _package

from fujitsu_waterstage.registers import load_register_map  # noqa: E402

requires_homeassistant = pytest.mark.skipif(
    not HOMEASSISTANT_INSTALLED, reason="Home Assistant is not installed"
)


@pytest.fixture(scope="session")
def register_map():
    """The packaged register map."""
    return load_register_map()


@pytest.fixture(autouse=True)
def _isolate_gateway_registry():
    """The shared (host, port) gateway registry must not leak between tests."""
    from fujitsu_waterstage import hub

    hub._GATEWAYS.clear()  # noqa: SLF001 - test isolation
    yield
    hub._GATEWAYS.clear()  # noqa: SLF001


if HOMEASSISTANT_INSTALLED:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Home Assistant only loads custom components when asked to."""
        return enable_custom_integrations
