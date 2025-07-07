"""
Run the pytest-http MCP server as a module.

Usage: python -m pytest_http
"""

from pytest_http.mcp_server import mcp

if __name__ == "__main__":
    mcp.run()