"""The Claude model picker, read from the installed Claude CLI.

The CLI's SDK initialize response lists exactly what its own ``/model``
picker offers: a default row, the family aliases (``opus``, ``fable``,
``sonnet``, ``haiku``) that move to the newest model of the family on every
CLI update, then explicit ids for older models, each with the effort levels
it accepts. claude-web used to carry that list by hand and fell behind
(Sonnet 5.5 shipped in CLI 2.1.289 and never appeared in the picker), so the
list now comes from the CLI and is cached per CLI version.

Spawning and switching pass a row's ``value`` straight to the CLI, so an
alias row keeps resolving to the newest model even between refreshes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("claude-web.models")

_ALL_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Used until the CLI has been asked (first boot, no CLI installed, or a fetch
# that failed). Aliases only, which the CLI resolves to the newest model of
# each family, so even this list never pins an old model.
FALLBACK_ROWS: list[dict] = [
    {"value": "default", "displayName": "Default", "description": "",
     "supportsEffort": True, "supportedEffortLevels": _ALL_EFFORTS},
    {"value": "opus", "displayName": "Opus", "description": "",
     "supportsEffort": True, "supportedEffortLevels": _ALL_EFFORTS},
    {"value": "fable", "displayName": "Fable", "description": "",
     "supportsEffort": True, "supportedEffortLevels": _ALL_EFFORTS},
    {"value": "sonnet", "displayName": "Sonnet", "description": "",
     "supportsEffort": True, "supportedEffortLevels": _ALL_EFFORTS},
    {"value": "haiku", "displayName": "Haiku", "description": ""},
]

FAMILY_ALIASES = ("opus", "fable", "sonnet", "haiku")

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def canonical_model_id(model_id: str) -> str:
    """``claude-haiku-4-5-20251001`` and ``claude-haiku-4-5`` compare equal.

    The CLI resolves Haiku to its dated id while every other model resolves to
    a bare one, and a 1M-context request may carry a ``[1m]`` suffix.
    """
    text = (model_id or "").strip().lower()
    if text.endswith("[1m]"):
        text = text[: -len("[1m]")]
    return _DATE_SUFFIX_RE.sub("", text)


def usable_rows(rows: Any) -> list[dict]:
    """Rows a picker can offer: well-formed, selectable, no duplicate values.

    ``disabled`` rows are ones the CLI shows but will not run (an org policy
    excludes them), so offering them here would only produce a failed turn.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("disabled"):
            continue
        value = row.get("value")
        if not isinstance(value, str) or not value.strip() or value in seen:
            continue
        seen.add(value)
        out.append(row)
    return out


def _efforts(row: dict) -> list[str]:
    if not row.get("supportsEffort"):
        return []
    levels = row.get("supportedEffortLevels") or []
    return [lvl for lvl in levels if isinstance(lvl, str)]


def build_entries(rows: Any, observed_context: Optional[dict] = None) -> list[dict]:
    """Turn the CLI's rows into claude-web picker entries.

    Each entry carries ``key`` (the picker value; ``""`` for the default row,
    which spawns with no ``--model`` at all), ``model`` (what reaches the CLI:
    the row's own value, alias or id), ``resolved`` (the id it runs today),
    ``label``, ``efforts``, and ``context`` (a window seen in an earlier
    turn's usage, else ``None`` until one is). ``fableplan`` is claude-web's
    own split entry, built from the ``fable`` and ``opus`` rows when both
    exist so it tracks the newest of each too.
    """
    observed = observed_context or {}
    rows = usable_rows(rows) or list(FALLBACK_ROWS)
    by_value = {row["value"]: row for row in rows}

    def name_of(resolved: str) -> str:
        # A row's displayName names the model it resolves to ("Opus 5.5"),
        # which the default row lacks; borrow it from its sibling.
        canon = canonical_model_id(resolved)
        for row in rows:
            if row["value"] != "default" and canon and (
                    canonical_model_id(row.get("resolvedModel") or "") == canon):
                return row.get("displayName") or ""
        return ""

    def window(resolved: str) -> Optional[int]:
        value = observed.get(canonical_model_id(resolved)) if resolved else None
        return value if isinstance(value, int) and value > 0 else None

    entries: list[dict] = []
    labels: set[str] = set()

    def add(entry: dict) -> None:
        # Two rows reading the same under a screen reader is a trap, so a
        # repeated label gets the value appended.
        if entry["label"] in labels:
            entry["label"] = f"{entry['label']} ({entry['key'] or 'default'})"
        labels.add(entry["label"])
        entries.append(entry)

    for row in rows:
        value = row["value"]
        if value == "default":
            resolved = row.get("resolvedModel") or ""
            name = name_of(resolved)
            add({
                "key": "", "model": resolved, "resolved": resolved,
                "label": f"Default ({name})" if name else "Default",
                "efforts": _efforts(row), "context": window(resolved),
                "betas": [],
            })
            continue
        resolved = row.get("resolvedModel") or value
        add({
            "key": value, "model": value, "resolved": resolved,
            "label": row.get("displayName") or value,
            "efforts": _efforts(row), "context": window(resolved),
            "betas": [],
        })
        if value == "fable" and "opus" in by_value:
            opus = by_value["opus"]
            opus_resolved = opus.get("resolvedModel") or "opus"
            fable_efforts = set(_efforts(row))
            add({
                # Split entry (the CLI's opusplan pattern, pointed at Fable):
                # plan_model runs in plan mode, model the rest of the time.
                "key": "fableplan", "model": "opus", "plan_model": "fable",
                "resolved": opus_resolved,
                "label": (f"Fableplan ({row.get('displayName') or 'Fable'} plans, "
                          f"{opus.get('displayName') or 'Opus'} builds)"),
                "efforts": [e for e in _efforts(opus) if e in fable_efforts],
                "context": window(opus_resolved),
                "betas": [],
            })
    return entries


# ─── Cache ────────────────────────────────────────────────────────────────────


def load_cache(path: Path) -> Optional[dict]:
    """``{"rows", "cli_version", "fetched_at"}`` from the last fetch, or None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not usable_rows(data.get("rows")):
        return None
    return data


def save_cache(path: Path, rows: list[dict], cli_version: str) -> None:
    """Atomically replace the cache. Best-effort: a failed write only means
    the next boot starts from the fallback list until the fetch lands."""
    path = Path(path)
    payload = {"rows": rows, "cli_version": cli_version, "fetched_at": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cli_models.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)


# ─── Fetch ────────────────────────────────────────────────────────────────────


async def fetch_rows(cli_path: str, *, cwd: Path, env: Optional[dict] = None,
                     timeout: float = 90.0) -> list[dict]:
    """Ask the CLI for its picker rows through the SDK initialize handshake.

    No prompt is sent, so nothing is billed and no turn runs. No setting
    sources (so no hooks, MCP servers or plugins start) and no session file,
    so the probe never shows up in the sidebar.
    """
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    options = ClaudeAgentOptions(
        cli_path=cli_path, cwd=str(cwd), setting_sources=[],
        env=dict(env or {}),
        extra_args={"no-session-persistence": None},
    )

    async def _ask() -> list[dict]:
        async with ClaudeSDKClient(options=options) as client:
            info = await client.get_server_info() or {}
        return info.get("models") or []

    return await asyncio.wait_for(_ask(), timeout=timeout)
