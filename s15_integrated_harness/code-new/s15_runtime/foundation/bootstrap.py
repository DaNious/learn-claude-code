

#!/usr/bin/env python3
"""
s15: Integrated Harness - combine the course mechanisms in one runtime.

Run:  python s15_integrated_harness/code.py
Need: pip install anthropic python-dotenv pyyaml + .env with ANTHROPIC_API_KEY

    scheduled work ----+                    +---- team events
                       v                    v
    +---------------------------------------------------+
    | Agent loop                                        |
    | prompt -> model -> tool calls -> results -> prompt |
    +-------------------------+-------------------------+
                              |
          +-------------------+-------------------+
          |                   |                   |
          v                   v                   v
    built-in tools      persistent teams      MCP tools
"""

import ast
import atexit
import fcntl as _fcntl  # pyright: ignore[reportMissingImports]
import importlib.util
import json
import os
import random
import re
import secrets
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Any
import yaml  # pyright: ignore[reportMissingModuleSource]

fcntl: Any = _fcntl
readline: Any = None
try:
    import readline as _readline  # pyright: ignore[reportMissingImports]
    readline = _readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

from anthropic import Anthropic  # pyright: ignore[reportMissingImports]
from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36ms15 >> \033[0m"
CLI_ACTIVE = False


def load_memory_runtime():
    """Load s09 once and share this host's client, model, and workspace."""
    path = Path(__file__).resolve().parents[4] / "s09_memory" / "code.py"
    spec = importlib.util.spec_from_file_location(
        f"integrated_memory_{id(client)}", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load memory runtime from {path}")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    setattr(runtime, "WORKDIR", WORKDIR)
    setattr(runtime, "MEMORY_DIR", WORKDIR / ".memory")
    setattr(runtime, "MEMORY_INDEX", WORKDIR / ".memory" / "MEMORY.md")
    setattr(runtime, "client", client)
    setattr(runtime, "MODEL", MODEL)
    return runtime


MEMORY_RUNTIME = load_memory_runtime()


class ConsoleBroker:
    """Serialize normal prompts and worker permission questions on one stdin."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reader = None

    def ask(self, prompt: str) -> str:
        with self._lock:
            return (self.reader or input)(prompt)


CONSOLE = ConsoleBroker()


def terminal_print(text: str):
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        print(text)
        return
    line = ""
    if READLINE_AVAILABLE:
        try:
            line = readline.get_line_buffer()
        except Exception:
            line = ""
    print(f"\r\033[K{text}")
    print(PROMPT + line, end="", flush=True)


__all__ = (
    "ast",
    "atexit",
    "fcntl",
    "importlib",
    "json",
    "os",
    "random",
    "re",
    "secrets",
    "signal",
    "subprocess",
    "threading",
    "time",
    "contextmanager",
    "Path",
    "datetime",
    "dataclass",
    "asdict",
    "field",
    "yaml",
    "readline",
    "Anthropic",
    "load_dotenv",
    "READLINE_AVAILABLE",
    "load_memory_runtime",
    "ConsoleBroker",
    "terminal_print",
    "WORKDIR",
    "client",
    "MODEL",
    "PRIMARY_MODEL",
    "FALLBACK_MODEL",
    "SKILLS_DIR",
    "TRANSCRIPT_DIR",
    "TOOL_RESULTS_DIR",
    "DEFAULT_MAX_TOKENS",
    "ESCALATED_MAX_TOKENS",
    "MAX_RETRIES",
    "MAX_CONSECUTIVE_529",
    "MAX_RECOVERY_RETRIES",
    "BASE_DELAY_MS",
    "CONTEXT_LIMIT",
    "KEEP_RECENT_TOOL_RESULTS",
    "PERSIST_THRESHOLD",
    "CONTINUATION_PROMPT",
    "PROMPT",
    "CLI_ACTIVE",
    "MEMORY_RUNTIME",
    "CONSOLE",
)
