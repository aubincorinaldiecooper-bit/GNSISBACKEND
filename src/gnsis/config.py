"""Lightweight, MMEngine-style configuration.

A :class:`Config` is a thin wrapper around a plain ``dict`` that supports both
attribute and item access. Configs can be loaded from a Python module (the
upstream Autogenesis style), a JSON file, or an in-memory dict. Keeping this
dependency-free is deliberate: the runtime must compose reliably with nothing
installed but the standard library.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, Dict, Iterator


class Config(Mapping):
    """An immutable-ish mapping with attribute access.

    Examples
    --------
    >>> cfg = Config({"provider": "mock", "agent": {"max_steps": 4}})
    >>> cfg.provider
    'mock'
    >>> cfg["agent"]["max_steps"]
    4
    >>> cfg.get("missing", "default")
    'default'
    """

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        object.__setattr__(self, "_data", dict(data or {}))

    # -- Mapping protocol -------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    # -- attribute access -------------------------------------------------
    def __getattr__(self, key: str) -> Any:
        try:
            return self._data[key]
        except KeyError as exc:  # pragma: no cover - mirrors normal attr error
            raise AttributeError(key) from exc

    def __setattr__(self, key: str, value: Any) -> None:  # pragma: no cover
        raise AttributeError("Config is read-only; build a new one with merge().")

    # -- helpers ----------------------------------------------------------
    def merge(self, other: Mapping[str, Any]) -> "Config":
        """Return a new config with ``other`` shallow-merged on top."""
        merged = dict(self._data)
        merged.update(other)
        return Config(merged)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Config({self._data!r})"

    # -- loaders ----------------------------------------------------------
    @classmethod
    def fromfile(cls, path: str) -> "Config":
        """Load a config from a ``.py`` or ``.json`` file.

        For Python files, every module-level variable that does not start with
        an underscore is collected into the config (MMEngine convention).
        """
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        if path.endswith(".json"):
            with open(path, "r", encoding="utf-8") as handle:
                return cls(json.load(handle))
        if path.endswith(".py"):
            namespace: Dict[str, Any] = {}
            with open(path, "r", encoding="utf-8") as handle:
                code = compile(handle.read(), path, "exec")
            exec(code, {"__file__": path}, namespace)  # noqa: S102 - trusted config
            data = {k: v for k, v in namespace.items() if not k.startswith("_")}
            return cls(data)
        raise ValueError(f"Unsupported config extension: {path}")
