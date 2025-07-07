#!/usr/bin/env python3
"""
MCP Server for pytest-http

This script provides an MCP (Model Context Protocol) server that exposes
pytest-http functionality to AI tools like Cursor.

Usage:
    python mcp_server.py

Or to install in Claude Desktop, add this to your claude_desktop_config.json:
{
  "mcpServers": {
    "pytest-http": {
      "command": "python",
      "args": ["/path/to/this/project/mcp_server.py"],
      "env": {}
    }
  }
}
"""

from pytest_http.mcp_server import mcp

if __name__ == "__main__":
    mcp.run()