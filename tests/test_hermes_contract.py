"""Tests of conformance with Hermes's plugin contract.

Principle: the plugin builds only on Hermes's documented, contract-protected extension points, and anything a later Hermes
version could break must be flagged. These tests make that machine-checkable:

1. **Static import check** (runs without Hermes): every Hermes import of the plugin is on a surface Hermes documents as public,
   or among the known exceptions in ``KNOWN_PRIVATE_IMPORTS`` below. A new internal import fails the test; so does an exception
   no longer used (the list can only shrink).
2. **Hermes's own validator** (``hermes plugins validate --json``): its "private core imports" warning must list exactly the
   exception list, and the other checks must be green.
3. **Runtime check against the installed Hermes:** every ``BasePlatformAdapter`` member the plugin overrides or calls exists; the
   overrides' signatures accept the base class's; the ``MessageEvent`` fields and ``register_platform`` parameters exist.

Parts 2 and 3 need an installed Hermes. Without one the test skips, unless ``HERMES_TESTS_REQUIRED=1`` (see
``_adapter_test_support.py``): then it fails, so a Hermes upgrade that breaks an import cannot go unnoticed.

Sources:
- ``hermes_cli/plugin_validate_private_imports.py`` ``_PUBLIC``: modules and names public to plugins, collected from the developer
  documentation;
- ``website/docs/developer-guide/adding-platform-adapters.md`` and ``gateway/platforms/ADDING_A_PLATFORM.md``: the platform adapter
  contract;
- ``website/docs/developer-guide/plugins/index.md`` "Native plugin compatibility contract".
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _adapter_test_support import (  # noqa: E402
    HERMES_AGENT_AVAILABLE,
    SKIP_REASON,
    load_mixin_module,
)

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The top-level names of Hermes's own packages (those that can occur in a platform
# plugin at all).
HERMES_TOP_LEVEL = frozenset({
    "agent", "cli", "cron", "gateway", "hermes_cli", "hermes_constants",
    "hermes_platform", "hermes_state", "plugins", "pm", "providers", "tools",
    "utils",
})

# The Hermes surface public to plugins that this plugin draws on. Source:
# hermes_cli/plugin_validate_private_imports.py ``_PUBLIC``. ``module`` = every
# non-underscore name of the module; ``module:Name`` = only that name.
PUBLIC_SURFACE = frozenset({
    "gateway.config",
    "gateway.platform_registry",
    "gateway.platforms._shared",
    "gateway.platforms.base",
    "gateway.platforms.event",
    "gateway.platforms.helpers",
    "hermes_cli.cli_output",
    "hermes_cli.config",
    "hermes_cli.gateway_setup_wizard:confirm_reconfigure",
    "hermes_cli.gateway_setup_wizard:print_setup_header",
    "hermes_cli.gateway_setup_wizard:prompt_csv",
    "hermes_cli.gateway_setup_wizard:save_env_values",
    "hermes_cli.setup:prompt",
    "hermes_cli.setup:prompt_yes_no",
    "tools.url_safety:is_safe_url",
})

# Known internal imports (target as module.name, as ``hermes plugins validate`` writes
# it) -> why. The command guard's imports have no documented successor: Hermes's own
# command tiers do not cover what the guard intercepts (see docs/commands.md), so they
# stay until Hermes offers a documented path. The list can only shrink.
KNOWN_PRIVATE_IMPORTS = {
    "agent.skill_commands.resolve_skill_command_key": "command guard",
    "agent.skill_bundles.resolve_bundle_command_key": "command guard",
    "hermes_cli.commands.is_gateway_known_command": "command guard",
    "hermes_cli.commands.resolve_command": "command guard",
}

# The ``BasePlatformAdapter`` members the documentation offers a platform plugin to
# override or call, with the source.
DOCUMENTED_MEMBERS = {
    # adding-platform-adapters.md "Architecture Overview" and "adapter.py"
    "__init__": "adding-platform-adapters.md",
    "connect": "adding-platform-adapters.md",
    "disconnect": "adding-platform-adapters.md",
    "send": "adding-platform-adapters.md",
    "send_typing": "adding-platform-adapters.md",
    "get_chat_info": "adding-platform-adapters.md",
    "handle_message": "adding-platform-adapters.md",
    "build_source": "adding-platform-adapters.md / ADDING_A_PLATFORM.md",
    "_mark_connected": "adding-platform-adapters.md",
    "_mark_disconnected": "adding-platform-adapters.md",
    "_keep_typing": "adding-platform-adapters.md (Slow-LLM UX)",
    # ADDING_A_PLATFORM.md 1. „Required/Optional methods”, „Key patterns”
    "send_image": "ADDING_A_PLATFORM.md",
    "send_document": "ADDING_A_PLATFORM.md",
    "send_voice": "ADDING_A_PLATFORM.md",
    "send_video": "ADDING_A_PLATFORM.md",
    "send_animation": "ADDING_A_PLATFORM.md",
    "send_image_file": "ADDING_A_PLATFORM.md",
    "format_message": "ADDING_A_PLATFORM.md (sibling adapters)",
    "_source_session_key": "ADDING_A_PLATFORM.md",
    "_event_session_key": "ADDING_A_PLATFORM.md",
    # user guide: capabilities that can be implemented in an adapter
    "edit_message": "user-guide/features/plugins.md",
    "delete_message": "user-guide/messaging/index.md",
}

# Known members not in the plugin guide -> why. The startup self-check (adapter.py)
# watches that they exist. The list can only shrink.
KNOWN_UNDOCUMENTED_MEMBERS = {
    "stop_typing": "public member, mentioned only on the platform side",
    "prefers_fresh_final_streaming": "public member",
    "truncate_message": "public member; built-in adapters chunk this way too",
    "splits_long_messages": "public class attribute",
    "_set_fatal_error": "private; bundled plugins call it too",
    "_notify_fatal_error": "private",
}


def _plugin_sources() -> dict[str, ast.AST]:
    """The plugin's non-test Python sources, file name -> AST."""
    trees = {}
    for name in sorted(os.listdir(_PLUGIN_DIR)):
        if name.endswith(".py"):
            with open(os.path.join(_PLUGIN_DIR, name), encoding="utf-8") as fh:
                trees[name] = ast.parse(fh.read(), filename=name)
    return trees


def _hermes_imports() -> list[tuple[str, int, str, str]]:
    """``(file, line, module, name)`` for every Hermes import (empty name for ``import module``)."""
    found = []
    for fname, tree in _plugin_sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pairs = [(a.name, "") for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                pairs = [(node.module, a.name) for a in node.names]
            else:
                continue
            for module, name in pairs:
                if module.split(".")[0] in HERMES_TOP_LEVEL:
                    found.append((fname, node.lineno, module, name))
    return found


def _is_public(module: str, name: str) -> bool:
    if f"{module}:{name}" in PUBLIC_SURFACE:
        return True
    return module in PUBLIC_SURFACE and not name.startswith("_")


def _target(module: str, name: str) -> str:
    return f"{module}.{name}" if name else module


class StaticImportContractTests(unittest.TestCase):
    """Runs without Hermes."""

    def test_every_hermes_import_is_public_or_a_known_exception(self):
        offenders = [
            f"{f}:{line} {_target(m, n)}"
            for f, line, m, n in _hermes_imports()
            if not _is_public(m, n) and _target(m, n) not in KNOWN_PRIVATE_IMPORTS
        ]
        self.assertEqual(
            offenders, [],
            "Internal Hermes import: Hermes does not guarantee this. "
            "Use a documented surface, "
            "or record it here with a reason.",
        )

    def test_known_exceptions_are_still_used(self):
        used = {_target(m, n) for _f, _l, m, n in _hermes_imports()}
        stale = sorted(set(KNOWN_PRIVATE_IMPORTS) - used)
        self.assertEqual(stale, [], "Exception no longer used: remove it from KNOWN_PRIVATE_IMPORTS.")


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class HermesValidatorTests(unittest.TestCase):
    """Hermes's own plugin validator, on this plugin."""

    @classmethod
    def setUpClass(cls):
        hermes = os.environ.get("HERMES_BIN") or shutil.which("hermes") or os.path.join(
            os.path.dirname(sys.executable), "hermes")
        if not os.path.exists(hermes):
            if os.environ.get("HERMES_TESTS_REQUIRED") == "1":
                raise AssertionError(f"hermes CLI not found ({hermes})")
            raise unittest.SkipTest("hermes CLI not found (HERMES_BIN)")
        proc = subprocess.run(
            [hermes, "plugins", "validate", "--json", _PLUGIN_DIR],
            capture_output=True, text=True, timeout=180,
        )
        start = proc.stdout.find("{")
        if start < 0:
            raise AssertionError(f"hermes plugins validate: no JSON ({proc.stderr[-500:]})")
        cls.report = json.loads(proc.stdout[start:])

    def test_validation_passes(self):
        failed = [c for c in self.report["checks"] if not c.get("ok")]
        self.assertTrue(self.report.get("ok"), failed)

    def test_private_imports_match_the_known_exceptions(self):
        reported = set()
        for warning in self.report.get("warnings", []):
            if warning.startswith("private core imports"):
                reported |= {t.rstrip(".") for t in re.findall(r"\S+\.py:\d+ ([\w.]+)", warning)}
        self.assertEqual(sorted(reported), sorted(KNOWN_PRIVATE_IMPORTS))

    def test_no_core_override(self):
        names = {c["name"]: c for c in self.report["checks"]}
        self.assertTrue(names.get("no core override", {}).get("ok"), names.get("no core override"))


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class RuntimeContractTests(unittest.TestCase):
    """Against the installed Hermes: the names used exist and fit."""

    @classmethod
    def setUpClass(cls):
        from gateway.platforms.base import BasePlatformAdapter

        cls.base = BasePlatformAdapter
        cls.adapter_cls = load_mixin_module("adapter").RocketChatAdapter

    def _plugin_member_names(self) -> tuple[set[str], set[str]]:
        """``(overridden, called)`` base-class members from the source."""
        base_names = set(dir(self.base))
        defined, used = set(), set()
        # Only the adapter and its mixins: other classes (a dataclass's ``name`` field)
        # are not part of BasePlatformAdapter.
        classes = [
            node for tree in _plugin_sources().values() for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name.endswith(("Adapter", "Mixin"))
        ]
        for cls_node in classes:
            for node in ast.walk(cls_node):
                if node is cls_node:
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            defined.add(item.name)
                        elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                            defined |= {t.id for t in targets if isinstance(t, ast.Name)}
                elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                      and node.value.id == "self"):
                    used.add(node.attr)
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id in ("hasattr", "getattr") and len(node.args) >= 2
                      and isinstance(node.args[1], ast.Constant)):
                    used.add(node.args[1].value)
        return defined & base_names, (used & base_names) - defined

    def test_base_members_are_documented_or_known(self):
        overridden, called = self._plugin_member_names()
        allowed = set(DOCUMENTED_MEMBERS) | set(KNOWN_UNDOCUMENTED_MEMBERS)
        self.assertEqual(sorted((overridden | called) - allowed), [],
                         "Undocumented BasePlatformAdapter member: record it with a reason.")

    def test_known_undocumented_members_are_still_used(self):
        overridden, called = self._plugin_member_names()
        stale = sorted(set(KNOWN_UNDOCUMENTED_MEMBERS) - overridden - called)
        self.assertEqual(stale, [], "Exception no longer used: remove it from KNOWN_UNDOCUMENTED_MEMBERS.")

    def test_listed_members_exist_on_the_base_class(self):
        missing = sorted(
            name for name in set(DOCUMENTED_MEMBERS) | set(KNOWN_UNDOCUMENTED_MEMBERS)
            if not hasattr(self.base, name)
        )
        self.assertEqual(missing, [], "Hermes removed or renamed these.")

    def test_overrides_accept_the_base_signature(self):
        overridden, _called = self._plugin_member_names()
        problems = []
        for name in sorted(overridden):
            base_attr = getattr(self.base, name)
            ours = getattr(self.adapter_cls, name)
            if not callable(base_attr) or name == "__init__":
                continue
            base_sig, our_sig = inspect.signature(base_attr), inspect.signature(ours)
            our_params = our_sig.parameters
            has_var_kw = any(p.kind is p.VAR_KEYWORD for p in our_params.values())
            for pname, param in base_sig.parameters.items():
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                if pname not in our_params and not has_var_kw:
                    problems.append(f"{name}: missing parameter {pname!r}")
            for pname, param in our_params.items():
                if (param.default is param.empty
                        and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
                        and pname not in base_sig.parameters):
                    problems.append(f"{name}: Hermes does not pass the required {pname!r}-t")
        self.assertEqual(problems, [])

    def test_startup_self_check_lists_exactly_what_the_source_relies_on(self):
        adapter = load_mixin_module("adapter")
        overridden, called = self._plugin_member_names()
        relied = (overridden | called) - {"__init__"}
        self.assertEqual(sorted(adapter._RELIED_BASE_MEMBERS), sorted(relied))
        self.assertEqual(adapter.check_hermes_contract(), [])

    def test_startup_self_check_reports_a_missing_member(self):
        from unittest import mock

        adapter = load_mixin_module("adapter")
        with mock.patch.object(adapter, "_RELIED_BASE_MEMBERS", ("send", "no_such_member")), \
                self.assertLogs(adapter.logger, "ERROR") as logs:
            self.assertEqual(adapter.check_hermes_contract(), ["BasePlatformAdapter.no_such_member"])
        self.assertIn("no_such_member", logs.output[0])

    def test_message_event_fields_exist(self):
        import dataclasses

        from gateway.platforms.event import MessageEvent

        fields = {f.name for f in dataclasses.fields(MessageEvent)}
        used = set()
        for tree in _plugin_sources().values():
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "MessageEvent"):
                    used |= {kw.arg for kw in node.keywords if kw.arg}
        self.assertTrue(used, "no MessageEvent(...) call found")
        self.assertEqual(sorted(used - fields), [])
        self.assertEqual(sorted(load_mixin_module("adapter")._RELIED_EVENT_FIELDS), sorted(used))

    def test_register_platform_kwargs_exist(self):
        import dataclasses

        from gateway.platform_registry import PlatformEntry

        fields = {f.name for f in dataclasses.fields(PlatformEntry)}
        used = set()
        for tree in _plugin_sources().values():
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "register_platform"):
                    used |= {kw.arg for kw in node.keywords if kw.arg}
        self.assertTrue(used, "no register_platform(...) call found")
        self.assertEqual(sorted(used - fields), [])


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class DocumentedHelperTests(unittest.TestCase):
    """The documented helpers, with this plugin's settings."""

    @classmethod
    def setUpClass(cls):
        cls.adapter = load_mixin_module("adapter")

    def test_settings_are_read_through_the_scoped_secret_reader(self):
        import sys
        from unittest import mock

        scoped_env = sys.modules[self.adapter._ScopedEnv.__module__]
        values = {"ROCKETCHAT_URL": "https://chat.example/", "ROCKETCHAT_TOKEN": "t",
                  "ROCKETCHAT_USER_ID": "u", "ROCKETCHAT_BOT_USERNAME": "bot"}
        with mock.patch.object(scoped_env, "get_scoped_secret",
                               side_effect=lambda name, default=None: values.get(name, default)) as reader:
            cfg = self.adapter._config_from_platform(mock.Mock(extra={}, token=None))
        self.assertEqual((cfg.base_url, cfg.token, cfg.user_id, cfg.bot_username),
                         ("https://chat.example", "t", "u", "bot"))
        reader.assert_any_call("ROCKETCHAT_TOKEN", None)

    def test_is_connected_uses_the_documented_env_probe(self):
        from unittest import mock

        import hermes_cli.gateway as gateway_mod

        present = {"ROCKETCHAT_TOKEN": "t", "ROCKETCHAT_URL": "u",
                   "ROCKETCHAT_USER_ID": "i", "ROCKETCHAT_BOT_USERNAME": "b"}
        with mock.patch.object(gateway_mod, "get_env_value", side_effect=present.get):
            self.assertTrue(self.adapter._is_connected(None))
        with mock.patch.object(gateway_mod, "get_env_value",
                               side_effect={**present, "ROCKETCHAT_URL": " "}.get):
            self.assertFalse(self.adapter._is_connected(None))


class PlaintextApprovalWordsTests(unittest.TestCase):
    """The guard's list of textual persistent approvals covers Hermes's own synonyms.

    On a pending approval Hermes translates a bare "always"/"session" (and the configured language's synonyms) to
    ``/approve always|session``, without its own command tiers (``_plaintext_approval_words``, from
    ``approval.inputs.always|session`` in ``locales/<language>.yaml``). The plugin's guard intercepts these with a hand-kept list
    (``builtin_commands`` ``_PERSISTENT_APPROVAL_REPLIES``); this test reports when a Hermes upgrade brings a new English word. Other
    languages are not covered: see docs/commands.md.
    """

    LANGUAGES = ("en",)

    @classmethod
    def setUpClass(cls):
        root = os.environ.get("HERMES_AGENT_PATH") or os.path.expanduser("~/.hermes/hermes-agent")
        cls.locales = os.path.join(root, "locales")
        if not os.path.isdir(cls.locales):
            if os.environ.get("HERMES_TESTS_REQUIRED") == "1":
                raise AssertionError(f"Hermes's locales folder not found ({cls.locales})")
            raise unittest.SkipTest("Hermes's locales folder not found (HERMES_AGENT_PATH)")

    def _inputs(self, lang: str, key: str) -> set[str]:
        with open(os.path.join(self.locales, f"{lang}.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        block = re.search(r"^  inputs:\n((?:    .*\n)+)", text, re.MULTILINE)
        self.assertIsNotNone(block, f"{lang}.yaml: no approval.inputs block")
        line = re.search(rf'^    {key}: "([^"]*)"', block.group(1), re.MULTILINE)
        self.assertIsNotNone(line, f"{lang}.yaml: no approval.inputs.{key}")
        return {w.strip().lower() for w in line.group(1).split(",") if w.strip()}

    def test_guard_covers_hermes_persistent_approval_words(self):
        sys.path.insert(0, _PLUGIN_DIR)
        from builtin_commands import PERSISTENT_APPROVAL_KEY, blocked_persistent_approval

        words = set()
        for lang in self.LANGUAGES:
            for key in ("always", "session"):
                words |= self._inputs(lang, key)
        missed = sorted(w for w in words
                        if blocked_persistent_approval(w, frozenset()) != PERSISTENT_APPROVAL_KEY)
        self.assertEqual(missed, [], "Hermes knows a new persistent approval word: "
                                     "add it to builtin_commands._PERSISTENT_APPROVAL_REPLIES.")


if __name__ == "__main__":
    unittest.main()
