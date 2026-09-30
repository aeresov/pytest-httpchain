"""strip_jsonc / loads_jsonc: comments and trailing commas blanked out of JSON
text, nothing else touched, every character left at its offset."""

import json

import pytest

from pytest_httpchain.jsonref import loads_jsonc, strip_jsonc


def _newlines(text):
    return [(index, char) for index, char in enumerate(text) if char in "\r\n"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param('{"a": 1} // note', {"a": 1}, id="line-comment-at-end-without-newline"),
        pytest.param('// heading\n{"a": 1}', {"a": 1}, id="line-comment-first"),
        pytest.param('{"a": /* inline */ 1}', {"a": 1}, id="block-comment-inline"),
        pytest.param('/*\n * spans\n * lines\n */\n{"a": 1}', {"a": 1}, id="block-comment-over-lines"),
        pytest.param('{"a": 1}\r\n// windows\r\n', {"a": 1}, id="line-comment-crlf"),
        pytest.param('/* one\r\n two */{"a": 1}', {"a": 1}, id="block-comment-crlf"),
        pytest.param("[1 /* /* */ ]", [1], id="block-comments-do-not-nest"),
        pytest.param("[1 /* // */ ]", [1], id="line-marker-inside-block-comment"),
        pytest.param("[1 // /* \n]", [1], id="block-marker-inside-line-comment"),
        pytest.param("[1 /* ] */, 2]", [1, 2], id="bracket-inside-comment"),
        pytest.param('[1] // an "unclosed quote', [1], id="quote-inside-comment"),
        pytest.param("[1, /* **/ 2]", [1, 2], id="stars-before-the-close"),
        pytest.param("[1/**/]", [1], id="empty-block-comment"),
        pytest.param("[1 //\n]", [1], id="empty-line-comment"),
        pytest.param('{"a": "caf\u00e9"} // caf\u00e9 \u2028 \U0001f600', {"a": "caf\u00e9"}, id="non-ascii-comment"),
        # Nothing inside a string is a comment or a trailing comma.
        pytest.param('{"url": "http://example.com/*x*/ // y"}', {"url": "http://example.com/*x*/ // y"}, id="markers-in-string"),
        pytest.param('{"a": ",]", "b": ",}"}', {"a": ",]", "b": ",}"}, id="comma-and-bracket-in-string"),
        pytest.param('{"a": "x\\"// y"}', {"a": 'x"// y'}, id="escaped-quote-then-marker"),
        pytest.param('{"a": "x\\\\"} // y', {"a": "x\\"}, id="escaped-backslash-closes-the-string"),
        pytest.param('{"a": "x\\\\\\"// y"}', {"a": 'x\\"// y'}, id="odd-backslash-run-escapes-the-quote"),
        pytest.param('{"a": "\\u002f/ not a comment"}', {"a": "// not a comment"}, id="unicode-escape-then-slash"),
        # One trailing comma before a closing bracket, past whitespace and comments.
        pytest.param("[1, 2,]", [1, 2], id="trailing-comma-array"),
        pytest.param('{"a": 1,}', {"a": 1}, id="trailing-comma-object"),
        pytest.param('{"a": [1,], "b": {"c": true,},}', {"a": [1], "b": {"c": True}}, id="trailing-commas-nested"),
        pytest.param("[1,\n\t \r\n]", [1], id="trailing-comma-then-whitespace"),
        pytest.param("[1, // last\n]", [1], id="trailing-comma-then-line-comment"),
        pytest.param("[1, /* last */]", [1], id="trailing-comma-then-block-comment"),
        pytest.param("[1 /* c */ ,]", [1], id="comment-then-trailing-comma"),
        pytest.param('["a",]', ["a"], id="trailing-comma-after-string"),
        pytest.param("[[],{},null,]", [[], {}, None], id="trailing-comma-after-each-kind"),
        pytest.param("\ufeff[1,]", None, id="byte-order-mark-is-the-reader-s"),
    ],
)
def test_accepted(text, expected):
    stripped = strip_jsonc(text)
    # Same length, newlines where they were: the parser's positions are the file's.
    assert len(stripped) == len(text)
    assert _newlines(stripped) == _newlines(text)
    if expected is None:
        # A byte-order mark is the file reader's to drop (utf-8-sig), not the scanner's.
        with pytest.raises(json.JSONDecodeError, match="Unexpected UTF-8 BOM"):
            json.loads(stripped)
        return
    assert json.loads(stripped) == expected
    assert loads_jsonc(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        '{"a": [1, 2], "b": {"c": null}}',
        '{"url": "https://example.com/a/b", "path": "/x"}',
        '{"a": ",]"}',
        "[]",
        "",
    ],
)
def test_strict_json_is_left_as_it_is(text):
    """Every strictly valid JSON text is unchanged, so it parses as before:
    no opt-in is needed for the .json files that were already there."""
    assert strip_jsonc(text) == text


@pytest.mark.parametrize(
    ("text", "message", "position"),
    [
        # The comment's opening, not the end of the file the scan reached.
        pytest.param('{\n  "a": 1 /* never\n closed', "Unterminated comment", (2, 10), id="unterminated-comment"),
        pytest.param("[1] /*", "Unterminated comment", (1, 5), id="unterminated-comment-at-end"),
        pytest.param("[1, /* unclosed", "Unterminated comment", (1, 5), id="unterminated-comment-after-comma"),
        pytest.param("[1, /* a */ /* b", "Unterminated comment", (1, 13), id="unterminated-after-closed"),
        # The comment ends at its first "*/"; what follows is not JSON.
        pytest.param("[1 /* /* */ */]", "Expecting ',' delimiter", (1, 13), id="nested-block-comment"),
        # A slash that starts no comment stays for the parser to refuse, there.
        pytest.param("[1 / 2]", "Expecting ',' delimiter", (1, 4), id="lone-slash"),
        pytest.param("/* c */ /", "Expecting value", (1, 9), id="lone-slash-after-comment"),
        pytest.param('{"a": 1}/', "Extra data", (1, 9), id="lone-slash-at-end"),
        # Only one comma with a value before it is a trailing comma.
        pytest.param("[,1]", "Expecting value", (1, 2), id="leading-comma"),
        pytest.param("[,]", "Expecting value", (1, 2), id="lone-comma"),
        pytest.param("{,}", "Expecting property name", (1, 2), id="leading-comma-object"),
        pytest.param("[1,,]", "Expecting value", (1, 4), id="double-comma"),
        pytest.param("[1, /* c */ ,]", "Expecting value", (1, 13), id="double-comma-around-comment"),
        pytest.param('{"a":,}', "Expecting value", (1, 6), id="comma-after-colon"),
        pytest.param("1,", "Extra data", (1, 2), id="comma-before-no-bracket"),
        # An error after a comment is at its place in the text as written.
        pytest.param('/* one\n   two */ // three\n{"a": }', "Expecting value", (3, 7), id="error-after-comments"),
        pytest.param('// one\r\n{"a": 1,\r\n "b": ]}', "Expecting value", (3, 7), id="error-after-crlf-comment"),
        # Words json.loads reads as numbers, which JSON has none of: at the
        # first outside a string, as written, a comment's included.
        pytest.param('{"a": NaN}', "NaN is not valid JSON", (1, 7), id="nan"),
        pytest.param("[1,\n -Infinity]", "-Infinity is not valid JSON", (2, 2), id="negative-infinity"),
        pytest.param('{"s": "NaN \\" Infinity", /* NaN */ "b": Infinity}', "Infinity is not valid JSON", (1, 41), id="infinity-after-strings-and-comments"),
        # JSON, but too large for a float, which json.loads reads as infinity:
        # the file holds a number no JSON can write back.
        pytest.param('{"s": "1e400", "b": 1.5, "c": 1e400}', r"1e400 is too large a number \(it reads as infinity\)", (1, 31), id="number-too-large"),
        pytest.param("[\n  -1E+400]", r"-1E\+400 is too large a number", (2, 3), id="negative-number-too-large"),
    ],
)
def test_refused_at_its_position(text, message, position):
    with pytest.raises(json.JSONDecodeError, match=message) as excinfo:
        loads_jsonc(text)
    assert (excinfo.value.lineno, excinfo.value.colno) == position


def test_numbers_a_float_holds_read_as_before():
    """Only a number past the largest float is refused: the largest, one
    that rounds to zero and a large integer read as ``json.loads`` reads
    them."""
    assert loads_jsonc("[1.7976931348623157e308, 1e-400, 100000000000000000000]") == [1.7976931348623157e308, 0.0, 10**20]


def test_unterminated_comment_names_the_text():
    """The error carries the text as written, as ``json.loads``' own do."""
    with pytest.raises(json.JSONDecodeError) as excinfo:
        strip_jsonc("[1] /* x")
    assert (excinfo.value.doc, excinfo.value.pos, str(excinfo.value)) == ("[1] /* x", 4, "Unterminated comment: line 1 column 5 (char 4)")


def test_keyword_arguments_reach_the_parser():
    pairs = loads_jsonc('{"a": 1, /* c */ "a": 2,}', object_pairs_hook=list)
    assert pairs == [("a", 1), ("a", 2)]


@pytest.mark.parametrize(
    "text",
    [
        # A comment first and a long tail: a scan that failed at the tail would
        # try again from each of its characters.
        "/**/" + "1" * 1_000_000,
        "[" + "1, " * 300_000 + "1,]",
        "[1," + " " * 1_000_000 + "]",
        "[" + "/* c */ 1," * 100_000 + "]",
        json.dumps(["a/b,]"] * 200_000),
        "[" * 500_000 + "//\n" + "]" * 500_000,
    ],
    ids=["long-tail", "many-commas", "long-gap", "many-comments", "many-strings", "deep"],
)
def test_linear_time(text):
    """Inputs a quadratic scan would take minutes over (and the nesting the
    scan itself never recurses into)."""
    assert len(strip_jsonc(text)) == len(text)


def test_unterminated_comment_after_many_commas_is_found_once():
    text = "[" + "1, " * 300_000 + "/*" + " x" * 500_000
    with pytest.raises(json.JSONDecodeError, match="Unterminated comment") as excinfo:
        strip_jsonc(text)
    assert excinfo.value.pos == text.index("/*")
