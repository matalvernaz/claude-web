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

The CLI builds that list one of two ways. With the server's model catalog
switched on for the account it lists every model, each named with its
version ("Opus 5.5", then "Opus 5", "Opus 4.8", ...). Without it, it falls
back to a list compiled into the binary: the family aliases only, named
"Opus" or "Sonnet" with the version left in the description. The switch is
decided per account and only once the CLI knows which account it runs as,
so each account's list is fetched under its own identity and the picker
offers every row any of a user's accounts lists (``union_rows``).
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
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
# claude-opus-5-5 → ("opus", "5", "5"); claude-opus-5 → ("opus", "5", None).
_MODEL_ID_RE = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?$")


def canonical_model_id(model_id: str) -> str:
    """``claude-haiku-4-5-20251001`` and ``claude-haiku-4-5`` compare equal.

    The CLI resolves Haiku to its dated id while every other model resolves to
    a bare one, and a 1M-context request may carry a ``[1m]`` suffix.
    """
    text = (model_id or "").strip().lower()
    if text.endswith("[1m]"):
        text = text[: -len("[1m]")]
    return _DATE_SUFFIX_RE.sub("", text)


def model_family(model_id: str) -> Optional[str]:
    """``opus`` for ``opus``, ``claude-opus-4-8`` or ``claude-opus-4-8[1m]``."""
    canon = canonical_model_id(model_id)
    for family in FAMILY_ALIASES:
        if canon == family or f"-{family}-" in f"-{canon}-":
            return family
    return None


def _name_from_id(model_id: str) -> str:
    """"Opus 5.5" for ``claude-opus-5-5``; "" for anything else."""
    match = _MODEL_ID_RE.match(canonical_model_id(model_id))
    if not match:
        return ""
    family, major, minor = match.groups()
    return f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")


def _model_name(row: dict) -> str:
    """The model a non-default row runs, named with its version.

    The served list names rows "Opus 5.5" already. The compiled one names the
    alias rows "Opus" and keeps the version in the description ("Opus 5.5 ·
    Best for everyday, complex tasks"), and a screen-reader user choosing
    between "Opus" and "Sonnet" can't tell which models those are.
    """
    name = (row.get("displayName") or "").strip()
    if not name:
        return row["value"]
    if any(ch.isdigit() for ch in name):
        return name
    lead = (row.get("description") or "").split("·")[0].strip()
    if lead.lower().startswith(name.lower() + " ") and _VERSION_RE.match(lead[len(name):].strip()):
        return lead
    derived = _name_from_id(row.get("resolvedModel") or "")
    if derived.lower().startswith(name.lower() + " "):
        return derived
    return name


def row_label(row: dict) -> str:
    """The picker label for a non-default row.

    A ``[1m]`` value asks for the 1M-context window, which the compiled list
    offers under the plain family name; said so, it can't read the same as
    the row for the same model at its usual window.
    """
    name = _model_name(row)
    if row["value"].lower().endswith("[1m]") and "1m" not in name.lower():
        name += " (1M context)"
    return name


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
    own split entry, built from the ``opus`` row and the newest Fable row
    when both exist so it tracks the newest of each too. The compiled list
    can carry Fable as an explicit ``claude-fable-5-1[1m]`` rather than the
    ``fable`` alias, so the Fable row is found by family, not by value.
    """
    observed = observed_context or {}
    rows = usable_rows(rows) or list(FALLBACK_ROWS)
    by_value = {row["value"]: row for row in rows}
    fable_row = by_value.get("fable") or next(
        (row for row in rows if row["value"] != "default" and "fable" in (
            model_family(row["value"]), model_family(row.get("resolvedModel") or ""))),
        None,
    )

    def name_of(resolved: str) -> str:
        # The default row names no model; borrow the name of the sibling row
        # that resolves to the same one.
        canon = canonical_model_id(resolved)
        for row in rows:
            if row["value"] != "default" and canon and (
                    canonical_model_id(row.get("resolvedModel") or "") == canon):
                return _model_name(row)
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
            "label": row_label(row),
            "efforts": _efforts(row), "context": window(resolved),
            "betas": [],
        })
        if row is fable_row and "opus" in by_value:
            opus = by_value["opus"]
            opus_resolved = opus.get("resolvedModel") or "opus"
            fable_efforts = set(_efforts(row))
            add({
                # Split entry (the CLI's opusplan pattern, pointed at Fable):
                # plan_model runs in plan mode, model the rest of the time.
                "key": "fableplan", "model": "opus", "plan_model": value,
                "resolved": opus_resolved,
                "label": f"Fableplan ({_model_name(row)} plans, {_model_name(opus)} builds)",
                "efforts": [e for e in _efforts(opus) if e in fable_efforts],
                "context": window(opus_resolved),
                "betas": [],
            })
    return entries


def union_rows(row_lists: Any) -> list[dict]:
    """Every row any of the lists offers, once each, for a single picker.

    A user's accounts can list different models (one has the server's catalog
    switched on, another does not yet), and failover moves a chat between
    them. Offering only the current account's rows would make a chat on Opus
    4.8 fall back to the family alias when it moves to an account that doesn't
    list 4.8; with every row offered it keeps the model it asked for, and an
    account that refuses it says so through the usual model error.

    The longest list leads, since it carries the CLI's own ordering of the most
    models; ties keep the order given. A row several lists carry offers only
    the effort levels all of them accept, so a level picked on one account
    can't be one another refuses.
    """
    lists = [usable_rows(rows) for rows in (row_lists or [])]
    lists = sorted((rows for rows in lists if rows), key=len, reverse=True)
    merged: dict[str, dict] = {}
    for rows in lists:
        for row in rows:
            value = row["value"]
            if value not in merged:
                merged[value] = dict(row)
                continue
            seen = merged[value]
            if not (seen.get("supportsEffort") and row.get("supportsEffort")):
                seen["supportsEffort"] = False
                continue
            accepted = set(_efforts(row))
            seen["supportedEffortLevels"] = [e for e in _efforts(seen) if e in accepted]
    return list(merged.values())


# ─── Cache ────────────────────────────────────────────────────────────────────


def load_cache(path: Path) -> Optional[dict]:
    """``{"accounts": {account: rows}, "cli_version", "fetched_at"}`` from the
    last fetch, or None.

    A cache written before lists were kept per account holds one ``rows``
    list, which was the shared account's.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {"shared": data.get("rows")}
    accounts = {
        key: usable_rows(rows) for key, rows in accounts.items()
        if isinstance(key, str) and usable_rows(rows)
    }
    if not accounts:
        return None
    return {"accounts": accounts, "cli_version": data.get("cli_version"),
            "fetched_at": data.get("fetched_at")}


def save_cache(path: Path, accounts: dict[str, list[dict]], cli_version: str) -> None:
    """Atomically replace the cache. Best-effort: a failed write only means
    the next boot starts from the fallback list until the fetch lands."""
    path = Path(path)
    payload = {"version": 2, "accounts": accounts, "cli_version": cli_version,
               "fetched_at": time.time()}
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
