"""Let the Home Assistant test harness run on Windows.

Home Assistant does not support Windows, and two things break before a single
test does anything:

* ``homeassistant.runner`` imports ``fcntl`` and ``resource`` unconditionally,
  for a single-instance lock file and an open-file-limit tweak that no test ever
  touches.  ``pytest_homeassistant_custom_component`` imports that module while
  setting up its time patching, so the whole plugin fails to load.
* the harness blocks every socket except unix domain sockets, which is all
  asyncio needs on POSIX -- but the Windows proactor event loop builds its
  self-pipe from a real loopback socket pair, so the ban breaks the event loop.

Both are development-environment problems, not integration problems.  This
module is loaded through ``-p tests.win_compat`` in ``pytest.ini``, which pytest
processes before entry-point plugins.  On Linux and macOS it does nothing.
"""

from __future__ import annotations

import sys
import types

WINDOWS = sys.platform == "win32"


def _install_stub(name: str, **members: object) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
    except ImportError:
        module = types.ModuleType(name)
        module.__doc__ = f"Windows test stub for the POSIX-only {name!r} module."
        for attribute, value in members.items():
            setattr(module, attribute, value)
        sys.modules[name] = module


def _unsupported(*args: object, **kwargs: object) -> None:
    raise OSError(f"not supported on {sys.platform}")


if WINDOWS:
    _install_stub(
        "fcntl",
        LOCK_EX=2,
        LOCK_NB=4,
        LOCK_SH=1,
        LOCK_UN=8,
        flock=_unsupported,
        fcntl=_unsupported,
        ioctl=_unsupported,
        lockf=_unsupported,
    )
    _install_stub(
        "resource",
        RLIMIT_NOFILE=7,
        RLIM_INFINITY=-1,
        getrlimit=lambda _limit: (1024, 1024),
        setrlimit=_unsupported,
    )
    _install_stub("pwd", getpwuid=_unsupported, getpwnam=_unsupported)
    _install_stub("grp", getgrgid=_unsupported, getgrnam=_unsupported)

    # The harness bans every socket except unix domain sockets before each test.
    # Neutralising the ban here -- this module is imported before the harness --
    # is the only reliable order, because the ban and the fixture setup that
    # trips over it both happen inside the same pytest hook.  Nothing in this
    # suite talks to a real network, so no safety is lost.
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None
