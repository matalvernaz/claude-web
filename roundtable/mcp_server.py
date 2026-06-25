"""FastMCP wrapper around ``roundtable.core``.

Each public operation in ``core`` is registered as an MCP tool here.
Most wrappers are trivial pass-throughs — they call straight into core
and rely on its docstring (FastMCP introspects for the tool description)
and signature (FastMCP introspects for the JSON-schema arguments). The
MCP tool name matches the core function name so existing callers
(Claude prompts that reference ``mcp__roundtable__roundtable_create``
etc.) keep working.

Exception: ``roundtable_ask`` and ``roundtable_ask_parallel`` each take
a ``tool_use_context`` parameter whose ``permission_callback`` field is
a ``Callable`` — pydantic can't generate a JSON schema for callables,
so registering the core function directly blows up at schema-discovery
time. We instead register explicit wrappers that match the
MCP-callable surface (no ``tool_use_context``) and resolve the thread's
stored repo binding server-side via ``core._effective_tool_context``. A
``readonly`` binding needs no interactive callback, so repo reads work
over stdio MCP (Anthropic via the agent SDK; Gemini/OpenAI via their
function-calling loops when ``CLAUDE_ROUNDTABLE_PANEL_TOOLS`` is set).
Only the ``ask`` policy stays web-only — it needs a UI prompt, so over
stdio it falls back to no-tools rather than auto-allowing.

Run this module to start the stdio server:

    python -m roundtable.mcp_server

…or, equivalently, run the top-level ``server.py`` launcher which just
imports ``mcp`` from this module and calls ``mcp.run()``.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from . import core

mcp = FastMCP("roundtable")


def roundtable_ask(
    thread_id: int, participant: str, prompt: str = "", effort: str = "",
    web_search: bool = False,
) -> str:
    # Resolve any thread-bound repo context (Goal 4) into a ToolUseContext
    # from its stored string policy. None when the thread isn't bound (or
    # the policy needs an interactive callback we can't supply over stdio),
    # which preserves the prior no-tools MCP behaviour.
    return core.roundtable_ask(
        thread_id=thread_id, participant=participant, prompt=prompt,
        effort=effort, web_search=web_search,
        tool_use_context=core._effective_tool_context(thread_id),
    )


roundtable_ask.__doc__ = core.roundtable_ask.__doc__


def roundtable_ask_parallel(
    thread_id: int, participants: list[str], prompt: str = "",
    effort: str = "", web_search: bool = False,
) -> dict:
    return core.roundtable_ask_parallel(
        thread_id=thread_id, participants=participants, prompt=prompt,
        effort=effort, web_search=web_search,
        tool_use_context=core._effective_tool_context(thread_id),
    )


roundtable_ask_parallel.__doc__ = core.roundtable_ask_parallel.__doc__


# Register each public operation as an MCP tool. We register by
# reference for the trivial ones (signature + docstring live in core.py)
# and register the explicit wrappers above for the two that take a
# Callable parameter pydantic can't serialise.
for _fn in (
    core.roundtable_create,
    core.roundtable_bind_repo,
    core.roundtable_bind_github,
    core.roundtable_repo_context,
    core.roundtable_repo_pack,
    core.roundtable_post,
    roundtable_ask,
    roundtable_ask_parallel,
    core.roundtable_set_artifact,
    core.roundtable_get_artifact,
    core.roundtable_fork,
    core.roundtable_history,
    core.roundtable_list,
    core.roundtable_close,
    core.roundtable_participants,
    core.roundtable_usage,
):
    mcp.tool()(_fn)
del _fn


def main() -> None:
    """Entry point used by ``server.py`` and ``python -m``."""
    logging.basicConfig(level=logging.INFO)
    core.ensure_routable()  # refuse to start the stdio server with nothing to route to
    from importlib.metadata import PackageNotFoundError, version

    def _v(pkg: str) -> str:
        try:
            return version(pkg)
        except PackageNotFoundError:
            return "n/a"

    models = ", ".join(
        f"{name}={info['model']}" for name, info in core.PARTICIPANTS.items()
    )
    logging.getLogger(__name__).info(
        "roundtable starting: participants[%s] sdks[google-genai=%s "
        "openai=%s anthropic=%s mcp=%s]",
        models, _v("google-genai"), _v("openai"), _v("anthropic"), _v("mcp"),
    )
    mcp.run()


if __name__ == "__main__":
    main()
