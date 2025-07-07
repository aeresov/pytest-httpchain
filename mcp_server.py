#!/usr/bin/env python3
"""MCP Server launcher for pytest-http library"""

import sys
from pathlib import Path

# Add the project directory to the Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

try:
    from pytest_http.mcp_server import main
    
    if __name__ == "__main__":
        main()
except ImportError as e:
    print(f"Error importing MCP server: {e}")
    print("Please ensure the pytest-http library is installed with MCP dependencies:")
    print("pip install pytest-http[mcp]")
    sys.exit(1)