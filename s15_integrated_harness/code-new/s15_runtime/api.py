"""Compatibility map for the original single-module S15 API."""

from types import ModuleType

# Keep imports in the same feature order as the original code.py. Some modules
# have import-time setup such as skill scanning, hook registration, and signal
# cleanup registration.
from .foundation import bootstrap
from .workspace import tasks, worktrees
from .agent import prompting
from .tools import basic
from .collaboration import messaging, teammates
from .runtime import hooks
from .agent import subagent
from .runtime import compaction, recovery, background, cron
from .integrations import mcp
from .tools import handlers, schemas, registry
from .agent import loop
from . import cli


_MODULES: tuple[ModuleType, ...] = (
    bootstrap,
    tasks,
    worktrees,
    prompting,
    basic,
    messaging,
    teammates,
    hooks,
    subagent,
    compaction,
    recovery,
    background,
    cron,
    mcp,
    handlers,
    schemas,
    registry,
    loop,
)

_EXPORT_OWNERS: dict[str, ModuleType] = {}
for _module in _MODULES:
    for _name in getattr(_module, "__all__", ()):
        if hasattr(_module, _name):
            _EXPORT_OWNERS[_name] = _module


def has_symbol(name: str) -> bool:
    return name in _EXPORT_OWNERS


def get_symbol(name: str):
    owner = _EXPORT_OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"S15 runtime has no attribute {name!r}")
    return getattr(owner, name)


def set_symbol(name: str, value) -> None:
    """Update the owner and every module that imported the same symbol."""
    owner = _EXPORT_OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"S15 runtime has no attribute {name!r}")
    setattr(owner, name, value)
    for module in _MODULES:
        if name in vars(module):
            setattr(module, name, value)


def exported_names() -> tuple[str, ...]:
    return tuple(sorted(_EXPORT_OWNERS))


def run_cli() -> None:
    cli.run_cli()


__all__ = (
    "exported_names",
    "get_symbol",
    "has_symbol",
    "run_cli",
    "set_symbol",
)
