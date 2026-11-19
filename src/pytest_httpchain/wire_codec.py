"""MessagePack's wire representation, shared by expressions and HTTP steps,
and which codec reads an HTTP response's body (`selected_codec`), for the
stage's checks and for the report alike."""

from types import SimpleNamespace
from typing import Any, Literal

import msgpack

MSGPACK_DECODE_ERRORS = (ValueError, TypeError, RecursionError, msgpack.UnpackException)

# A stage's ``response_codec``: the codec its response body is read with, or
# ``auto``, chosen by the body's Content-Type (`selected_codec`).
type ResponseCodec = Literal["auto", "json", "msgpack"]

# The ``httpx.Response.extensions`` key under which the carrier hands the
# report a stage's ``response_codec``, so that the report decodes the body as
# the stage's checks did.
RESPONSE_CODEC_EXTENSION = "httpchain_response_codec"


def _pack_default(value: Any) -> Any:
    # Scenario vars are namespaces so templates can use attribute access.
    if isinstance(value, SimpleNamespace):
        return vars(value)
    # Naming no caller: a body.msgpack packs it as the msgpack_pack() helper
    # does, and the helper names itself (`templates.functions`).
    raise TypeError(f"cannot encode {type(value).__name__} as MessagePack")


def pack_msgpack(value: Any) -> bytes:
    """Pack a value, keeping text and binary distinct on the wire. A value
    of no type MessagePack has is a TypeError saying so."""
    return msgpack.packb(value, use_bin_type=True, default=_pack_default)


def unpack_msgpack(data: bytes) -> Any:
    """Decode one value; MessagePack bin values stay Python bytes."""
    return msgpack.unpackb(data, raw=False, strict_map_key=True)


def is_msgpack_content_type(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type in {"application/msgpack", "application/x-msgpack"} or media_type.endswith("+msgpack")


def selected_codec(codec: ResponseCodec, content_type: str) -> Literal["json", "msgpack"]:
    """The codec a response body sent as ``content_type`` is read with under
    ``codec``: the one it names, or for ``auto`` MessagePack where the type is
    a MessagePack one (`is_msgpack_content_type`), else JSON."""
    if codec == "auto":
        return "msgpack" if is_msgpack_content_type(content_type) else "json"
    return codec
