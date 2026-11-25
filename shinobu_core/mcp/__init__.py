# -*- coding: utf-8 -*-
"""
Pure-stdlib bidirectional MCP (Model Context Protocol) bridge for Shinobu.

Provides:
  - Inbound stdio MCP client manager (`McpClientManager`, `McpStdioClient`)
    that loads `<home>/mcp_servers.json` and exposes remote tools as `ToolSpec`.
  - Outbound stdio MCP server (`run_mcp_stdio_server`, `McpStdioServer`,
    `LoopbackDaemonBackend`) that exposes a running local Shinobu daemon as
    MCP tools (`shinobu_bubble`, `shinobu_state`, `shinobu_recall`, `shinobu_chat`).
"""

from __future__ import annotations

from .client import (
    DEFAULT_TIMEOUT_SEC,
    MCP_CONFIG_FILENAME,
    MCP_PROTOCOL_VERSION,
    McpClientError,
    McpClientManager,
    McpProtocolError,
    McpServerConfig,
    McpServerStatus,
    McpStdioClient,
    McpTimeoutError,
    McpToolInfo,
    format_mcp_tool_result,
)
from .community import (
    COMMUNITY_MCP_PRESETS,
    install_mcp_preset,
    list_community_presets,
    register_shinobu_onto_agy,
)
from .server import (
    DaemonBackendError,
    LoopbackDaemonBackend,
    McpStdioServer,
    OUTBOUND_TOOLS,
    ShinobuBackendProtocol,
    run_mcp_stdio_server,
)

__all__ = [
    "COMMUNITY_MCP_PRESETS",
    "DEFAULT_TIMEOUT_SEC",
    "DaemonBackendError",
    "LoopbackDaemonBackend",
    "MCP_CONFIG_FILENAME",
    "MCP_PROTOCOL_VERSION",
    "McpClientError",
    "McpClientManager",
    "McpProtocolError",
    "McpServerConfig",
    "McpServerStatus",
    "McpStdioClient",
    "McpStdioServer",
    "McpTimeoutError",
    "McpToolInfo",
    "OUTBOUND_TOOLS",
    "ShinobuBackendProtocol",
    "format_mcp_tool_result",
    "install_mcp_preset",
    "list_community_presets",
    "register_shinobu_onto_agy",
    "run_mcp_stdio_server",
]
