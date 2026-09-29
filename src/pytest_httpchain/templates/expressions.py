import re
from collections.abc import Callable, Iterator

# The lookahead allows a single `}` inside the expression (dict literals), so
# `{{ {'k': v} }}` needs the space before the closing braces. Single-line by
# design: template values are JSON string scalars, and every consumer shares
# this pattern. Multi-line logic belongs in a user function.
_TEMPLATE_INNER = r"(?:(?!\}\}).)+"

# A backslash escapes the `{{` right after it: `\{{` is the text `{{`, and so
# is the text after it up to the first `}}` on its line, braces included, which
# opens no template (`\{{ a {{ b }} }}` is text throughout; with no `}}` after
# it, the rest of the line is text). Before `{{`, `\\` stands for one
# backslash, so a run of them there renders halved, and an odd one escapes the
# braces: `\\{{ x }}` is a backslash, then x's value. The halving holds in
# escaped text too (`\{{\{{ x }}` is `{{{{ x }}`); only a template's
# expression is read as it is written.
#
# TEMPLATE_PATTERN is what rendering rewrites, as tokens found left to right
# (`finditer`, `sub`), never overlapping: a template, whose match takes in the
# even run of backslashes before its braces (`backslashes`), an escape (the odd
# run as `escape`, then the braces and the text they escape, as `escaped`), or
# an even run before braces that open no template (`run`). The lookbehind
# starts each at a run's start. A template is a match with an `expr`: find
# them with `find_templates`, never with a bare `re.search`, which finds the
# escapes too. Whether a `{{` is escaped text depends on what comes before it
# on its line, which only a scan from the line's start reads, not a
# lookbehind.
_TEMPLATE = r"(?P<backslashes>(?:\\\\)*)\{\{(?P<expr>" + _TEMPLATE_INNER + r")\}\}"
_ESCAPED = r"(?P<escape>\\(?:\\\\)*)\{\{(?P<escaped>(?:(?!\}\}).)*(?:\}\})?)"
_RUN = r"(?P<run>(?:\\\\)+)(?=\{\{)"
TEMPLATE_PATTERN = rf"(?<!\\)(?:{_TEMPLATE}|{_ESCAPED}|{_RUN})"
# For JSON Schema `pattern` sites: a template's braces and expression, with no
# named group, which JS regex engines reject (and VS Code then silently drops
# the pattern). The schema uses it only for a complete template, anchored
# (``^\s*...\s*$``), where nothing but whitespace can come before the braces,
# so it needs none of TEMPLATE_PATTERN's escape handling, and none of its
# lookbehinds, which not every JSON Schema validator's regex engine takes.
TEMPLATE_PATTERN_ECMA = r"\{\{" + _TEMPLATE_INNER + r"\}\}"

_TOKENS = re.compile(TEMPLATE_PATTERN)
_COMPLETE = re.compile(rf"\s*{TEMPLATE_PATTERN}\s*")
# A run of backslashes before `{{` in escaped text, which renders halved.
_RUN_IN_TEXT = re.compile(r"(?<!\\)\\+(?=\{\{)")


def find_templates(text: str) -> Iterator[re.Match[str]]:
    """The templates in ``text``, left to right, exactly as the engine renders
    them: TEMPLATE_PATTERN's tokens with an ``expr`` (its text, unstripped),
    whose ``start()`` is where the template's backslashes begin. What every
    consumer that asks whether, where or which templates are in a string
    reads: the models, the validator, scoping and `contains_template`."""
    return (match for match in _TOKENS.finditer(text) if match.group("expr") is not None)


def is_complete_template(value: str) -> bool:
    """True when the whole string is one ``{{ }}`` expression."""
    return extract_template_expression(value) is not None


def extract_template_expression(value: str) -> str | None:
    """The expression inside a complete template string, else None.

    An empty expression (``"{{ }}"``) is not a template: it carries nothing to
    evaluate and `parse_expression` refuses it at runtime. Rejecting it
    here keeps `is_complete_template` — and so every ``TemplateExpression``
    field — in agreement with `types.validate_partial_template_str`, which has
    always refused an empty expression in the partial form.

    Nothing but whitespace may surround the braces, a backslash included: an
    escaped ``\\{{ x }}`` is text, and ``\\\\{{ x }}`` a backslash before the
    value, which is text too, never the value itself.
    """
    if (match := _COMPLETE.fullmatch(value)) and match.group("expr") is not None and not match.group("backslashes"):
        return expr if (expr := match.group("expr").strip()) else None
    return None


def contains_escape(value: str) -> bool:
    """True when rendering rewrites more of ``value`` than its templates: a
    backslash right before a ``{{`` (an escape, or a backslash doubled before
    one), which rendering halves."""
    return "\\{{" in value


def _halved(backslashes: str) -> str:
    return backslashes[: len(backslashes) // 2]


def _rendered_escape(match: re.Match[str]) -> str:
    """What rendering makes of a token that is no template: an escape's text,
    the escaping backslash dropped, or an even run halved."""
    if (escape := match.group("escape")) is not None:
        return _halved(escape) + "{{" + _RUN_IN_TEXT.sub(lambda run: _halved(run.group(0)), match.group("escaped"))
    return _halved(match.group("run"))


def render_text(value: str, render: Callable[[str], str]) -> str:
    """``value`` as the engine renders a string that is not one whole
    template: each template replaced by what ``render`` makes of its
    expression (stripped), and before each ``{{`` outside an expression,
    template or not, the run of backslashes halved, which drops the one that
    escapes a ``{{``; the text it escapes is kept, its braces unread.

    One pass, left to right: what a template renders to is not read again, so
    a value holding ``{{ x }}`` or ``\\{{`` is put in as it is, and an escape
    is removed once.
    """

    def rewrite(match: re.Match[str]) -> str:
        if (expr := match.group("expr")) is None:
            return _rendered_escape(match)
        return _halved(match.group("backslashes")) + render(expr.strip())

    return _TOKENS.sub(rewrite, value)


def unescape(value: str) -> str:
    """What rendering makes of the escapes in ``value``: ``\\{{`` becomes
    ``{{``, and ``\\\\`` before a ``{{`` one backslash. The text a string
    without templates renders to; a template is left as it is written, for
    the engine to render. For a check that judges a literal as the runtime
    will use it (`types.as_rendered`), and for ``validate --deep``, which
    looks for the file a path renders to."""
    return _TOKENS.sub(lambda match: match.group(0) if match.group("expr") is not None else _rendered_escape(match), value)


# A `{{` and the run of backslashes right before it, the only text rendering
# rewrites outside a template.
_BRACES = re.compile(r"(\\*)\{\{")
# A template rendering to the braces themselves, which ends where it is written.
_LITERAL_BRACES = "{{ '{{' }}"


def escape(text: str, *, closed: bool = False) -> str:
    """The scenario text that renders to ``text``, which holds no template
    then: each ``{{`` escaped, the backslashes right before it doubled
    (``\\{{`` is written ``\\\\\\{{``). The inverse of `unescape`, for a
    tool writing values it did not author into a scenario (``import``): a
    request body holding a Handlebars ``{{name}}`` is sent as recorded.

    An escape covers the text after its braces up to the first ``}}`` on its
    line, so text with a ``{{`` that no ``}}`` follows on its line would
    cover a template written after it on that line too. ``closed`` writes
    for text a template may follow: each such ``{{`` is instead a template
    rendering to the braces (``{{ '{{' }}``), so no escape reaches past the
    text's end, and it renders to ``text`` whatever follows it on its line
    after a separator (text ending in ``{`` or a backslash would still join
    braces right after it). It holds a template then, where it has such a
    ``{{``."""

    def escape_line(line: str) -> str:
        # An escape closes where a `}}` follows its braces: before the line's last.
        last_closing = line.rfind("}}") if closed else len(line)

        def rewrite(match: re.Match[str]) -> str:
            doubled = "\\" * (2 * len(match.group(1)))
            return doubled + (_LITERAL_BRACES if match.end() > last_closing else "\\{{")

        return _BRACES.sub(rewrite, line)

    return "\n".join(escape_line(line) for line in text.split("\n"))
