"""wire_codec.py: MessagePack's wire representation (text and binary kept
apart, a ``vars`` object packed as its dict, a value it cannot encode
refused, a map's keys held to text), which Content-Type is a MessagePack
one, and which codec reads a response body under a stage's
``response_codec``.

How a stage's checks and the report read a body with these is pinned in
test_response_steps.py and test_report_formatter.py.
"""

import re
from types import SimpleNamespace

import msgpack
import pytest

from pytest_httpchain.wire_codec import RESPONSE_CODEC_EXTENSION, is_msgpack_content_type, pack_msgpack, selected_codec, unpack_msgpack

# Why an int MessagePack cannot hold is refused.
INT_OUT_OF_RANGE = "cannot encode int outside MessagePack's range, -2**63 to 2**64 - 1"


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        pytest.param("application/msgpack", True, id="msgpack"),
        pytest.param("application/x-msgpack", True, id="x-msgpack"),
        # A structured syntax suffix: a vendor's type, encoded as MessagePack.
        pytest.param("application/vnd.x+msgpack", True, id="suffix"),
        # Its parameters dropped, the media type compared case-insensitively.
        pytest.param("Application/MsgPack; charset=binary", True, id="mixed-case-with-parameters"),
        pytest.param("application/json", False, id="json"),
        # A response that sent no Content-Type.
        pytest.param("", False, id="empty"),
    ],
)
def test_msgpack_content_type(content_type, expected):
    assert is_msgpack_content_type(content_type) is expected


def test_round_trip_keeps_text_and_binary_apart():
    value = {"text": "abc", "binary": b"abc", "nested": [{"b": b"\x00\xff"}, "s"]}
    unpacked = unpack_msgpack(pack_msgpack(value))
    assert unpacked == value
    assert (type(unpacked["text"]), type(unpacked["binary"])) == (str, bytes)


def test_vars_object_packs_as_its_dict():
    """Scenario ``vars`` turn each object into a SimpleNamespace, the ones
    nested in it too, for templates' attribute access."""
    value = SimpleNamespace(id=1, owner=SimpleNamespace(name="a"), tags=[SimpleNamespace(t="x")])
    assert unpack_msgpack(pack_msgpack(value)) == {"id": 1, "owner": {"name": "a"}, "tags": [{"t": "x"}]}


def test_value_it_cannot_encode_is_refused_by_its_type():
    """Naming no caller: a body.msgpack packs as the msgpack_pack() helper
    does, and the helper names itself (test_functions.py)."""
    with pytest.raises(TypeError, match=r"^cannot encode set as MessagePack$"):
        pack_msgpack({1, 2})


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1, [1, 2**70], {"id": 2**70}], ids=["past-the-top", "past-the-bottom", "in-a-list", "in-a-map"])
def test_int_outside_its_range_is_refused_for_its_size(value):
    """An int is a type MessagePack has: one it cannot hold is refused as
    out of its range, as msgpack refuses it, never as a type it lacks. The
    codec's hook, which msgpack calls for such an int too, said `cannot
    encode int`."""
    with pytest.raises(OverflowError, match=rf"^{re.escape(INT_OUT_OF_RANGE)}$"):
        pack_msgpack(value)


@pytest.mark.parametrize("value", [2**64 - 1, -(2**63)], ids=["top", "bottom"])
def test_int_at_its_range_s_ends_is_encoded(value):
    assert unpack_msgpack(pack_msgpack(value)) == value


def test_map_key_that_is_not_text_is_refused():
    """``strict_map_key``: a body's map whose key is not text is refused,
    one of the same map keyed by text taken. The refusal's message is
    msgpack's own, and its C extension and pure-Python fallback word it
    differently, so only the words both share are matched."""
    assert unpack_msgpack(msgpack.packb({"1": "one"})) == {"1": "one"}
    with pytest.raises(ValueError, match="is not allowed for map key"):
        unpack_msgpack(msgpack.packb({1: "one"}))


@pytest.mark.parametrize(
    ("codec", "content_type", "expected"),
    [
        # auto reads the body as its Content-Type says ...
        pytest.param("auto", "application/msgpack", "msgpack", id="auto-msgpack"),
        pytest.param("auto", "application/json", "json", id="auto-json"),
        # ... and a codec the stage names reads it whatever it says.
        pytest.param("json", "application/msgpack", "json", id="json-over-msgpack"),
        pytest.param("json", "application/json", "json", id="json"),
        pytest.param("msgpack", "application/msgpack", "msgpack", id="msgpack"),
        pytest.param("msgpack", "application/json", "msgpack", id="msgpack-over-json"),
    ],
)
def test_selected_codec(codec, content_type, expected):
    assert selected_codec(codec, content_type) == expected


def test_response_codec_extension():
    """The key of ``httpx.Response.extensions`` under which the carrier hands
    the report a stage's ``response_codec``."""
    assert RESPONSE_CODEC_EXTENSION == "httpchain_response_codec"
