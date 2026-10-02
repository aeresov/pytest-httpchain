"""MessagePack's wire representation, shared by expressions and HTTP steps."""

from types import SimpleNamespace
from typing import Any

import msgpack

MSGPACK_DECODE_ERRORS = (ValueError, TypeError, RecursionError, msgpack.UnpackException)


def _pack_default(value: Any) -> Any:
    # Scenario vars are namespaces so templates can use attribute access.
    if isinstance(value, SimpleNamespace):
        return vars(value)
    raise TypeError(f"msgpack_pack() cannot encode {type(value).__name__}")


def pack_msgpack(value: Any) -> bytes:
    """Pack a value, keeping text and binary distinct on the wire."""
    return msgpack.packb(value, use_bin_type=True, default=_pack_default)


def unpack_msgpack(data: bytes) -> Any:
    """Decode one value; MessagePack bin values stay Python bytes."""
    return msgpack.unpackb(data, raw=False, strict_map_key=True)


def is_msgpack_content_type(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type in {"application/msgpack", "application/x-msgpack"} or media_type.endswith("+msgpack")
