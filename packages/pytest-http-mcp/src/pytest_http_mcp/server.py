from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

mcp = FastMCP("pytest-http")


class VerifyJsonResult(BaseModel):
    valid: bool
    errors: list[str]
    scenario_info: dict[str, Any]


@mcp.tool()
def validate(path: Path) -> VerifyJsonResult:
    raise NotImplementedError()
