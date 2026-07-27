"""The CLI dump tool must keep working without Home Assistant.

Phase 3 adds ``custom_components/fujitsu_waterstage/__init__.py``, which imports
Home Assistant.  ``tools/dump.py`` sidesteps that ``__init__`` on purpose; this
test fails loudly if that ever stops being true.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DUMP = REPO_ROOT / "tools" / "dump.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DUMP), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )


def test_help() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "--slave" in result.stdout


def test_groups_only_needs_no_hardware() -> None:
    result = _run("--host", "192.0.2.1", "--groups-only")
    assert result.returncode == 0, result.stderr
    assert "204 registers in" in result.stderr
    assert "9900..9921" in result.stderr


def test_block_filter() -> None:
    result = _run("--host", "192.0.2.1", "--groups-only", "--block", "swimming_pool")
    assert result.returncode == 0, result.stderr
    assert "3 registers in 1 read request(s): 90..92" in result.stderr


def test_unknown_block_is_reported() -> None:
    result = _run("--host", "192.0.2.1", "--groups-only", "--block", "nope")
    assert result.returncode == 2
    assert "known blocks" in result.stderr


def test_does_not_import_home_assistant() -> None:
    result = _run("--host", "192.0.2.1", "--groups-only")
    assert result.returncode == 0
    assert "homeassistant" not in result.stderr.lower()
