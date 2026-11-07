"""The process environment read through Hermes's profile-scoped secret reader."""

from __future__ import annotations

import os
from collections.abc import Mapping

from gateway.platforms._shared import get_scoped_secret


class ScopedEnv(Mapping):
    """The environment, read through Hermes's documented profile-scoped reader
    (``gateway.platforms._shared.get_scoped_secret``, ADDING_A_PLATFORM.md: "Adapter-side
    reads use get_scoped_secret / extra_or_secret").

    Under a multiplexed gateway the process's ``os.environ`` belongs to the DEFAULT
    profile: a raw read on a secondary profile would return another profile's token. On
    a single-profile install the behaviour is the same as ``os.environ``. Iteration walks
    the key names of the process environment (used only for prefix scans).
    """

    def __getitem__(self, key: str) -> str:
        value = get_scoped_secret(key, None)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self):
        return iter(os.environ)

    def __len__(self) -> int:
        return len(os.environ)
