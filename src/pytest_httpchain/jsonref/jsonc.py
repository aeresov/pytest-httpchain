"""JSON with comments (JSONC): the one parser of the JSON files read from disk.

A scenario, a file it includes and a body schema file may carry ``//`` line
comments, ``/* */`` block comments and a trailing comma before ``]`` or ``}``.
`strip_jsonc` turns such text into strict JSON of the same length, which
``json.loads`` then parses: each comment becomes whitespace, its newlines kept,
and each trailing comma a space, so every character stays at its offset and a
`json.JSONDecodeError`'s line and column point into the file as written.

Strictly valid JSON is left as it is, so every reader goes through here with no
opt-in: `loads_jsonc` is what the reference resolver parses a file with and what
a body schema file is read with (``utils.read_json_schema_file``), so they agree
on what a file may hold. What arrives over HTTP (a response body) is never
passed through here: it stays plain JSON, as ``json.loads`` reads it.
"""

import json
import math
import re
from typing import Any, NoReturn

# JSON's whitespace, and nothing else: Python's \s would take a no-break space
# the JSON parser refuses as whitespace.
_WHITESPACE = "[ \t\n\r]"

# Whitespace and comments between a comma and whatever follows it, which makes
# it a trailing comma when that is a closing bracket. A lookahead from each
# comma scans only as far as the next significant character, so the scans of
# all commas together cover the text about once.
_GAP = rf"(?:{_WHITESPACE}++|//[^\r\n]*+|/\*.*?\*/)*+"

# One match per comment or trailing comma: everything up to it (``skip``, which
# has strings in it whole, so a comment marker or a comma inside one is never
# seen), then the comment or comma, or the end of the text. Possessive, so a
# text without one fails no alternative and backtracks nowhere: the scan is
# linear. Every position starts a match, so the matches tile the text.
_SCAN = re.compile(
    r"""
    (?P<skip>(?:
        [^"/,]++
      | "[^"\\]*+(?:\\.[^"\\]*+)*+"?+   # a string, escapes and all, to its closing quote or the end of the text
      | /(?![/*])                       # a slash that starts no comment: the JSON parser's to refuse
      | ,(?!"""
    + _GAP
    + r"""[\]}])                        # a comma something other than a closing bracket follows
    )*+)
    (?:
        (?P<line>//[^\r\n]*+)
      | (?P<block>/\*.*?\*/)
      | (?P<unterminated>/\*)
      | (?P<comma>,)
      | \Z
    )
    """,
    re.DOTALL | re.VERBOSE,
)

# A comma before a closing bracket, whitespace between. Text with no ``/`` has
# no comment, and text with no match of this no trailing comma either, so the
# scan is skipped for it: most strictly valid JSON, returned as it is. (A match
# inside a string only costs the scan, which leaves it alone.)
_TRAILING_COMMA = re.compile(rf",{_WHITESPACE}*[\]}}]")

_NOT_A_NEWLINE = re.compile(r"[^\r\n]+")

# The characters after which a comma has no value before it: a leading comma
# (``[,1]``, ``{,}``), a second one (``[1,,]``) or one after a key's colon. Such
# a comma is left in place for the JSON parser to refuse, at its position.
_NO_VALUE_BEFORE = frozenset("[{,:")


def strip_jsonc(text: str) -> str:
    """``text`` with its comments and trailing commas blanked out, as strict JSON.

    A comment becomes spaces, keeping each ``\\r`` and ``\\n`` in it, and a comma
    followed (past whitespace and comments) by ``]`` or ``}`` becomes a space,
    when a value is before it: one trailing comma is accepted, never a leading or
    a doubled one. Nothing inside a string is touched. The result is as long as
    ``text``, every character at its offset, so the JSON parser's error positions
    are the file's. Anything else that is not JSON, a lone ``/`` included, is
    left for the parser to refuse.

    Raises `json.JSONDecodeError` for a ``/*`` that is never closed, at its
    position.
    """
    if "/" not in text and _TRAILING_COMMA.search(text) is None:
        return text
    parts: list[str] = []
    copied = 0
    # The last character before the current match that is neither whitespace
    # nor in a comment, "" at the start of the text.
    previous = ""
    for match in _SCAN.finditer(text):
        if significant := match["skip"].rstrip(" \t\n\r"):
            previous = significant[-1]
        kind = match.lastgroup
        if kind == "unterminated":
            raise json.JSONDecodeError("Unterminated comment", text, match.start(kind))
        if kind == "line":
            blank = " " * len(match[kind])
        elif kind == "block":
            blank = _NOT_A_NEWLINE.sub(lambda run: " " * len(run[0]), match[kind])
        elif kind == "comma" and previous and previous not in _NO_VALUE_BEFORE:
            blank = " "
        else:
            if kind == "comma":
                previous = ","
            continue
        parts.append(text[copied : match.start(kind)])
        parts.append(blank)
        copied = match.end(kind)
    parts.append(text[copied:])
    return "".join(parts)


# A string, whole, or a word ``json.loads`` reads as a number JSON cannot
# write: ``NaN`` or ``Infinity``, or a number too large for a float, which it
# reads as infinity. Words are found only outside strings, and the first one
# spelled as the parser handed it over is where the parser met it.
_WORD = re.compile(r'"[^"\\]*+(?:\\.[^"\\]*+)*+"|(?P<word>-?Infinity|NaN|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)')


class _NotJSON(Exception):
    """A word the parser read that is no JSON number, raised out of it with
    why."""


def _refuse_constant(word: str) -> NoReturn:
    raise _NotJSON(word, f"{word} is not valid JSON")


def _finite(word: str) -> float:
    # 1e400 is JSON, but a float past the largest is infinity, which is not.
    if math.isinf(value := float(word)):
        raise _NotJSON(word, f"{word} is too large a number (it reads as infinity)")
    return value


def loads_jsonc(text: str, **kwargs: Any) -> Any:
    """``json.loads`` of JSONC text (`strip_jsonc`), with ``json.loads``' keyword
    arguments but ``parse_constant`` and ``parse_float``, which it sets.
    Raises `json.JSONDecodeError` as ``json.loads`` does, for an
    unterminated comment too, and for ``NaN``, ``Infinity`` and
    ``-Infinity``, which ``json.loads`` reads as numbers though they are no
    JSON, and a number too large for a float (``1e400``), which it reads as
    infinity: a file holding one is not a JSON file, or holds a number no
    JSON can write, and ``resolve``, which prints strict JSON, would print
    ``Infinity``."""
    stripped = strip_jsonc(text)
    try:
        return json.loads(stripped, parse_constant=_refuse_constant, parse_float=_finite, **kwargs)
    except _NotJSON as e:
        word, message = e.args
        position = next(match.start("word") for match in _WORD.finditer(stripped) if match["word"] == word)
        raise json.JSONDecodeError(message, stripped, position) from None
