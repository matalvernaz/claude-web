"""Find the `claude` CLI to run: the portable build's managed copy first, the
machine's own install second, the SDK's bundled copy last.

The managed copy (``tools/claude``, see portable_tools) is downloaded from
Anthropic's release bucket and kept current by the app, so it is preferred
wherever it exists. An installed CLI comes next because it keeps itself
current (and the app's update timer updates it); the copy vendored inside
claude-agent-sdk is frozen at whatever the SDK shipped, and spawn flags newer
than it die with "unknown option". But the bundled copy is the only CLI a
desktop-binary user may have, so every place that runs `claude` falls back to
it rather than reporting it missing. Before this, sign-in looked on PATH
alone and failed on a fresh Windows machine even though the binary carried a
working claude.exe.

On Windows an npm install puts a `claude.cmd` batch shim on PATH. The Agent SDK
refuses to spawn batch scripts (cmd.exe re-parses their arguments, the
CVE-2024-27980 class), so a shim only counts when nothing native exists: as the
last resort it still gets the SDK's explanatory refusal instead of "not found".
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

import portable_tools

MANAGED = "managed"
SYSTEM = "system"
BUNDLED = "bundled"
SHIM = "shim"


def _is_windows() -> bool:
    return os.name == "nt"


def _home() -> Path:
    return Path.home()


def _is_native_exe(path: Optional[str]) -> bool:
    """Whether CreateProcess can run ``path`` directly: a .exe or .com.

    Mirrors the SDK's own check, including PATHEXT handing back names such as
    ``claude.exe.cmd``, which end in .cmd and so don't count.
    """
    if not path:
        return False
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rstrip(". ").lower().endswith((".exe", ".com"))


def _bundled_dir() -> Optional[Path]:
    """Where claude-agent-sdk keeps its vendored CLI; the same directory the
    SDK's _find_bundled_cli looks in."""
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    return Path(claude_agent_sdk.__file__).parent / "_bundled"


def bundled() -> Optional[str]:
    """The SDK's bundled CLI, if this install has one. Wheels carry it; the
    sdist doesn't, and neither do SDK releases too big for PyPI's per-file
    limit, which is why the release build installs the SDK wheel-only."""
    directory = _bundled_dir()
    if directory is None:
        return None
    path = directory / ("claude.exe" if _is_windows() else "claude")
    return str(path) if path.is_file() else None


def managed() -> Optional[str]:
    """The copy the portable build downloads and updates itself, if any."""
    if not portable_tools.enabled():
        return None
    path = portable_tools.managed_exe("claude")
    return str(path) if path is not None else None


def system() -> Optional[str]:
    """A `claude` installed on this machine that can be spawned directly."""
    found = shutil.which("claude")
    if not _is_windows() or _is_native_exe(found):
        return found
    # PATH is searched directory by directory, so npm's shim in an early
    # directory hides a native claude.exe in a later one.
    exe = shutil.which("claude.exe")
    if _is_native_exe(exe):
        return exe
    native = _home() / ".local" / "bin" / "claude.exe"
    return str(native) if native.is_file() else None


def find() -> tuple[Optional[str], Optional[str]]:
    """``(path, source)`` for the CLI to run; ``(None, None)`` when there is none.

    ``source`` is MANAGED, SYSTEM, BUNDLED, or SHIM (Windows: only a batch
    shim or an extensionless wrapper script was found).
    """
    path = managed()
    if path:
        return path, MANAGED
    path = system()
    if path:
        return path, SYSTEM
    path = bundled()
    if path:
        return path, BUNDLED
    path = shutil.which("claude")
    if path:
        return path, SHIM
    return None, None


def resolve() -> Optional[str]:
    """Path of the CLI to run, or None when this machine has none at all."""
    return find()[0]


def install_instructions() -> str:
    """How to install Claude Code natively on this platform. Not npm: on
    Windows that yields the claude.cmd shim the SDK refuses to run."""
    if _is_windows():
        return "In PowerShell: irm https://claude.ai/install.ps1 | iex"
    return "curl -fsSL https://claude.ai/install.sh | bash"
