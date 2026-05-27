"""Roundtable — multi-AI threaded conversation library + MCP server.

Public API for in-process callers (e.g. a webapp wanting to drive the same
operations the MCP tools expose) lives in ``roundtable.core``. The stdio
MCP server is ``roundtable.mcp_server``; importing it as a side effect
of ``import roundtable`` would force FastMCP into every web-side caller,
so the package init stays empty and callers pick the layer they want.
"""
