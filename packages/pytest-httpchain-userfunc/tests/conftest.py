"""Test fixtures for pytest-httpchain-userfunc tests."""

import sys
from pathlib import Path

# Add tests directory to sys.path so test helper modules can be imported
# via importlib.import_module (e.g., "test_helpers:func_name")
_tests_dir = Path(__file__).parent
if str(_tests_dir) not in sys.path:
    sys.path.insert(0, str(_tests_dir))
