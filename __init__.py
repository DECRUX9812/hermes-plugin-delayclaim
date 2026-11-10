"""Delayclaim — EU261/UK261 flight compensation, computed and tracked.

Six tools, one deterministic engine, one local case file. Nothing is sent to an
airline: the plugin assesses, drafts and tracks, and the person sends.

Import-safe: the data directory resolves per call (one process serves several
profiles, and the capability probe imports this module in a bare interpreter).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

try:  # loaded as a package by the plugin loader / capability probe
    from .delayclaim.tools import SCHEMAS, make_handlers  # type: ignore
except ImportError:  # loaded as a bare module
    from delayclaim.tools import SCHEMAS, make_handlers  # type: ignore

__all__ = ["register"]


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def register(ctx: Any) -> None:
    """Wire the agent half: six tools in their own toolset."""
    handlers = make_handlers(_PLUGIN_DIR, _home)
    for name, handler in handlers.items():
        ctx.register_tool(
            name=name,
            toolset="delayclaim",
            schema=SCHEMAS[name],
            handler=handler,
            description=SCHEMAS[name]["description"].split(".")[0],
            emoji="✈️",
        )
