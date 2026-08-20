#!/usr/bin/env python3
"""Pylance-friendly standard-package entry point for the S15 runtime."""

import sys as _sys
from pathlib import Path as _Path
from types import ModuleType as _ModuleType

_PACKAGE_ROOT = str(_Path(__file__).resolve().parent)
if _PACKAGE_ROOT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_ROOT)

from s15_runtime import api as _api  # noqa: E402


class _CompatibilityModule(_ModuleType):
    """Forward the original module API to symbols in their owning modules."""

    def __getattr__(self, name: str):
        return _api.get_symbol(name)

    def __setattr__(self, name: str, value) -> None:
        if _api.has_symbol(name):
            _api.set_symbol(name, value)
            return
        super().__setattr__(name, value)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(_api.exported_names()))


_current_module = _sys.modules.get(__name__)
if _current_module is not None:
    _current_module.__class__ = _CompatibilityModule


if __name__ == "__main__":
    _api.run_cli()
