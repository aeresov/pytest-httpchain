from pathlib import Path
from typing import Any


class LoaderError(Exception):
    """An error parsing JSON test scenario."""


def load_json(path: Path) -> dict[str, Any]:
    raise LoaderError("not implemented")
