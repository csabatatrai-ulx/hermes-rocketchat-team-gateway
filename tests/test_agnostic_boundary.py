"""Guards the agnostic/framework boundary this package relies on.

The modules listed in AGNOSTIC_MODULES know NOTHING about Hermes: no ``gateway.*`` import, no ``hermes_cli``.
That separation keeps them testable without Hermes. This test fails a run if a future edit quietly imports a
Hermes-only name into one of these files.

Static text scan, not an AST walk: good enough to catch an import statement, which is the only realistic way
Hermes-specific code enters one of these files.
"""

from __future__ import annotations

import os
import re
import unittest

_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every module in this package except the deliberately Hermes-aware ones
# (adapter.py, __init__.py and the two transport mixins, see
# _FRAMEWORK_EXEMPT below).
AGNOSTIC_MODULES = [
    "approval_buttons.py",
    "asker.py",
    "builtin_commands.py",
    "config.py",
    "context_refs.py",
    "dedup.py",
    "dm_session.py",
    "history.py",
    "inbound.py",
    "media.py",
    "prompt_guidance.py",
    "rc_markdown.py",
    "reasoning_format.py",
    "room_session.py",
    "reply_reference.py",
    "shared_state.py",
    "rest_client.py",
    "user_texts.py",
    "whoami.py",
]

# webhook_mixin.py and ddp_transport_mixin.py are Hermes-aware glue just like
# adapter.py: webhook_mixin imports gateway.* directly (MessageEvent/MessageType),
# ddp_transport_mixin relies on the BasePlatformAdapter lifecycle contract
# (``self._set_fatal_error``/``self._notify_fatal_error``) even with no literal
# "gateway" among its imports.
_FRAMEWORK_EXEMPT = {"scoped_env.py", "setup_wizard.py", "standalone.py", 
    "adapter.py",
    "__init__.py",
    "webhook_mixin.py",
    "ddp_transport_mixin.py",
}

# The only two namespaces adapter.py itself imports from Hermes - see this
# package's __init__.py / adapter.py module docstring.
_FORBIDDEN_IMPORT_TARGETS = ("gateway", "hermes_cli")
_IMPORT_LINE = re.compile(r"^\s*(?:import|from)\s+(\S+)")


def _forbidden_imports(source: str) -> list:
    hits = []
    for line in source.splitlines():
        match = _IMPORT_LINE.match(line)
        if not match:
            continue
        target = match.group(1)
        for token in _FORBIDDEN_IMPORT_TARGETS:
            if target == token or target.startswith(token + "."):
                hits.append(line.strip())
    return hits


class TestAgnosticBoundary(unittest.TestCase):
    """Every agnostic module must import zero Hermes-specific names."""

    def test_no_hermes_imports_in_agnostic_modules(self):
        for filename in AGNOSTIC_MODULES:
            path = os.path.join(_PACKAGE_DIR, filename)
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
            hits = _forbidden_imports(source)
            self.assertFalse(
                hits,
                f"{filename} imports Hermes-specific module(s), breaking the "
                f"agnostic/framework boundary (see this test's docstring): "
                f"{hits}",
            )

    def test_agnostic_module_list_is_complete(self):
        """Catch a new file added to the package without deciding its side.

        A new module landing here silently, classified as neither agnostic
        nor exempt, is exactly how the boundary erodes over time - this
        fails loudly instead, forcing the choice to be made explicitly.
        """
        actual = {
            name
            for name in os.listdir(_PACKAGE_DIR)
            if name.endswith(".py") and not name.startswith("test_")
        }
        expected = set(AGNOSTIC_MODULES) | _FRAMEWORK_EXEMPT
        self.assertEqual(
            actual,
            expected,
            "A module was added to or removed from the repo root "
            "without updating this test: classify it as agnostic "
            "(AGNOSTIC_MODULES) or framework-specific (_FRAMEWORK_EXEMPT).",
        )


if __name__ == "__main__":
    unittest.main()
