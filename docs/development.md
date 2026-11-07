# Development

## Layout

The repository root is the plugin: `plugin.yaml`, `__init__.py` and the modules. There is deliberately no `pyproject.toml`: it would make Hermes treat the directory as a Python package.

For local work, link a checkout and do not also install the plugin from the catalog, because the manifest names would collide:

```bash
ln -s /path/to/checkout ~/.hermes/plugins/rocketchat-team-gateway
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Use `unittest` from the repository root. The root's `__init__.py` confuses bare `pytest` collection.

Run with Hermes's own Python and set `HERMES_AGENT_PATH` when it is not at `~/.hermes/hermes-agent`. Without Hermes the pure tests still run and the integration tests skip. `HERMES_TESTS_REQUIRED=1` turns the skips into failures, for CI.

```bash
hermes plugins validate . --install-deps
```

## Conventions

- Hermes-independent modules stay free of Hermes imports, so they can be tested alone.
- Every text the plugin writes to a chat lives in `user_texts.py`.
- Model-facing text lives in `prompt_guidance.py` and names no specific tool.
- A new Hermes surface goes through `tests/test_hermes_contract.py`. Prefer documented surfaces.
- A change in behaviour updates the matching file in `docs/` in the same commit.
