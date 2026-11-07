"""Shared helpers for testing the mixins split out of adapter.py.

Unlike the framework-agnostic modules (history.py, inbound.py and so on), the mixins
(``webhook_mixin.py``/``ddp_transport_mixin.py``) cannot be loaded with the other tests'
plain "sys.path.insert + flat import" pattern, for two reasons:

1. They use relative imports (``from .shared_state import ...``), which work only when the file
   is loaded as part of a REAL package, and the repository directory's name is not a valid
   Python identifier, so this goes through ``importlib.util.spec_from_file_location`` with a
   synthetic package name.
2. They import ``gateway.*`` directly (``gateway.platforms.base``, ``gateway.session``), which is
   available only with the Hermes framework's source on ``sys.path``. Its location is
   machine-specific and configurable with the ``HERMES_AGENT_PATH`` env var, defaulting to the
   usual ``~/.hermes/hermes-agent``. If neither exists (a CI machine without Hermes), the tests
   skip gracefully instead of failing; on a machine with Hermes they really run.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PACKAGE_NAME = "_hermes_rocketchat_test_pkg"

HERMES_AGENT_AVAILABLE = False
SKIP_REASON = ""


def _try_add_hermes_agent_to_path() -> None:
    global HERMES_AGENT_AVAILABLE, SKIP_REASON
    candidate = os.environ.get("HERMES_AGENT_PATH") or os.path.expanduser(
        "~/.hermes/hermes-agent"
    )
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)
    try:
        import gateway.platforms.base  # noqa: F401
    except ImportError as exc:
        SKIP_REASON = (
            "gateway.* (Hermes core) not importable - set HERMES_AGENT_PATH "
            f"or install hermes-agent locally to run this test (got: {exc})"
        )
        return
    HERMES_AGENT_AVAILABLE = True


_try_add_hermes_agent_to_path()

# ``HERMES_TESTS_REQUIRED=1``: a missing Hermes is an ERROR, not a skip. CI turns this
# on, so a Hermes upgrade that breaks an import (or a wrong ``HERMES_AGENT_PATH``)
# cannot stay silently green.
if not HERMES_AGENT_AVAILABLE and os.environ.get("HERMES_TESTS_REQUIRED") == "1":
    raise RuntimeError(f"HERMES_TESTS_REQUIRED=1, de: {SKIP_REASON}")


def _load_plugin_package():
    """Load the plugin package under a valid synthetic name.

    The directory name is not importable as a package (see the module docstring).

    The package really loads (no dangling import). The try/except stays as a safety net: if a future
    change breaks an import, this still gives a graceful skip rather than failing every test that goes
    through ``load_mixin_module()``.
    """
    if _PACKAGE_NAME in sys.modules:
        return sys.modules[_PACKAGE_NAME]
    init_path = os.path.join(_PLUGIN_DIR, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME, init_path, submodule_search_locations=[_PLUGIN_DIR]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        sys.modules.pop(_PACKAGE_NAME, None)
        if os.environ.get("HERMES_TESTS_REQUIRED") == "1":
            raise
        raise unittest.SkipTest(
            f"hermes-rocketchat package failed to load: {exc}"
        ) from exc
    return module


def load_mixin_module(name: str):
    """Load a mixin module ("webhook_mixin") through the synthetic package.

    Precondition: ``HERMES_AGENT_AVAILABLE``; callers guard with
    ``@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)``.
    """
    _load_plugin_package()
    return importlib.import_module(f"{_PACKAGE_NAME}.{name}")


def make_bare_instance(mixin_cls, **attrs):
    """An instance of a mixin class WITHOUT running ``__init__`` (and with it the real
    ``BasePlatformAdapter``/``Platform`` registration).

    The mixins cannot be instantiated through the full ``RocketChatAdapter`` in this test environment: the
    ``Platform("rocketchat")`` enum value is only taken up by the real gateway plugin registration
    (``register()``), which does not run here. We want to test the mixin methods' logic, not the whole
    lifecycle, so a bare instance with exactly the attributes the tested methods use is correct and
    sufficient.
    """
    instance = mixin_cls.__new__(mixin_cls)
    for key, value in attrs.items():
        setattr(instance, key, value)
    return instance
