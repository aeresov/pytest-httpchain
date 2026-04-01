from pytest_httpchain_mcp.server import mcp


def serve() -> None:
    mcp.run()
