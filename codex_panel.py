"""Per-run Codex MCP tool and hook transport to the app's review panel.

Codex runs stdio MCP servers and command hooks out of process. A private Unix
socket keeps both attached to the same ActiveRun without copying credentials,
editing a user's config, or opening an unauthenticated HTTP endpoint.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
import shlex
import socket
import sys
import tempfile
from typing import Awaitable, Callable

import panel

log = logging.getLogger(__name__)
MAX_REQUEST = 8 * 1024 * 1024
HOOK_TIMEOUT = 20


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _without_null_fields(value):
    # config/read includes null optional fields. Codex's JSON→TOML override
    # conversion turns null into an empty string, invalid for e.g. timeout.
    if isinstance(value, dict):
        return {k: _without_null_fields(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_null_fields(v) for v in value]
    return value


class PanelBridge:
    def __init__(self, dispatch: Callable[[dict], Awaitable[dict]]) -> None:
        self.dispatch = dispatch
        self.directory = tempfile.TemporaryDirectory(prefix="cw-panel-")
        self.path = str(Path(self.directory.name) / "panel.sock")
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(
            self._handle, path=self.path, limit=MAX_REQUEST,
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            payload = json.loads(await asyncio.wait_for(reader.readline(), HOOK_TIMEOUT))
            if not isinstance(payload, dict):
                raise ValueError("panel request must be an object")
            result = await self.dispatch(payload)
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except Exception:
            log.exception("Codex panel bridge request failed")
            with contextlib.suppress(Exception):
                writer.write(json.dumps({"error": "The review panel connection failed. Try again."}).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.tasks.discard(task)

    async def close(self) -> None:
        if self.server:
            self.server.close()
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.server:
            await self.server.wait_closed()
        self.directory.cleanup()

    def command(self, mode: str) -> list[str]:
        return [sys.executable, str(Path(__file__).resolve()), mode, self.path]

    async def configuration(self, server, cwd: Path) -> dict:
        """Add our hooks, then trust only our exact generated definitions.

        The ephemeral thread discovers Codex's own hook hashes. It never runs
        a model or writes a session. Other hook definitions and trust stay as
        configured by the user; the overrides live only in this chat process.
        """
        existing = (await server.request("config/read", {"cwd": str(cwd)})).get("config") or {}
        hooks = _without_null_fields(existing.get("hooks") or {})
        command = shlex.join(self.command("hook"))
        group = {"matcher": "^(Bash|apply_patch)$", "hooks": [{
            "type": "command", "command": command, "timeout": HOOK_TIMEOUT + 5,
        }]}
        # Codex accumulates hooks from every config source. Repeating the
        # effective user hooks in sessionFlags would run each of them twice.
        config = {
            "features.hooks": True,
            **{f"hooks.{event}": [group]
               for event in ("PreToolUse", "PostToolUse")},
        }
        probe = await server.request("thread/start", {
            "cwd": str(cwd), "ephemeral": True, "config": config,
        })
        probe_id = probe["thread"]["id"]
        try:
            listed = await server.request("hooks/list", {"threadId": probe_id})
        finally:
            await server.request("thread/unsubscribe", {"threadId": probe_id})
        own = [h for row in listed["data"] for h in row["hooks"]
               if h.get("source") == "sessionFlags" and h.get("command") == command]
        if {h["eventName"] for h in own} != {"preToolUse", "postToolUse"}:
            raise RuntimeError("Codex did not register the review panel hooks")
        config["hooks.state"] = {
            **hooks.get("state", {}),
            **{h["key"]: {"enabled": True, "trusted_hash": h["currentHash"]} for h in own},
        }
        config["mcp_servers.panel"] = {
            "command": sys.executable,
            "args": self.command("mcp")[1:],
            "enabled": True,
            # The panel's off-mode question card can wait for the user.
            "tool_timeout_sec": 3600,
        }
        return config

    async def verify_hooks(self, server, thread_id: str) -> None:
        listed = await server.request("hooks/list", {"threadId": thread_id})
        command = shlex.join(self.command("hook"))
        own = [h for row in listed["data"] for h in row["hooks"]
               if h.get("source") == "sessionFlags" and h.get("command") == command
               and h.get("enabled") and h.get("trustStatus") in ("trusted", "managed")]
        if {h["eventName"] for h in own} != {"preToolUse", "postToolUse"}:
            raise RuntimeError("Codex review panel hooks are disabled or untrusted")


def request(path: str, payload: dict, timeout: float | None = None) -> dict:
    encoded = json.dumps(payload).encode() + b"\n"
    if len(encoded) > MAX_REQUEST:
        raise ValueError("panel request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(path)
        connection.sendall(encoded)
        with connection.makefile("rb") as stream:
            result = json.loads(stream.readline(MAX_REQUEST + 1))
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


def main() -> None:
    mode, path = sys.argv[1:]
    if mode == "hook":
        try:
            payload = json.loads(sys.stdin.buffer.read(MAX_REQUEST + 1))
            result = request(path, {"kind": "hook", "input": payload}, HOOK_TIMEOUT)
        except Exception:
            # A disconnected pre-hook must not silently let a commit through.
            result = deny("The review panel connection failed. Resume the chat before changing files or committing.")
        print(json.dumps(result))
    elif mode == "mcp":
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("panel")

        @mcp.tool(name="review", description=panel.TOOL_DESCRIPTION)
        def review(stage: str, repo: str, message: str, reason: str = "") -> str:
            result = request(path, {"kind": "review", "arguments": {
                "stage": stage, "repo": repo, "message": message, "reason": reason,
            }})
            if result["is_error"]:
                raise ValueError(result["text"])
            return result["text"]

        mcp.run()
    else:
        raise ValueError("unknown panel bridge mode")


if __name__ == "__main__":
    main()
