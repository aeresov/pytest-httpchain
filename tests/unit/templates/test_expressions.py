"""Tests for expressions.py - template pattern matching utilities."""

import re

import pytest

from pytest_httpchain.templates import (
    TEMPLATE_PATTERN,
    contains_escape,
    contains_template,
    escape,
    extract_template_expression,
    find_templates,
    is_complete_template,
    needs_rendering,
    unescape,
    walk,
)
from pytest_httpchain.templates.expressions import render_text


@pytest.mark.parametrize(
    ("value", "expr"),
    [
        ("{{ value }}", "value"),
        ("{{  spaced  }}", "spaced"),
        ("  {{ value }}  ", "value"),
        ("\t{{ value }}\n", "value"),
        ("{{ x + y * 2 }}", "x + y * 2"),
        ("{{ [i * 2 for i in items] }}", "[i * 2 for i in items]"),
        ("{{ {'key': value} }}", "{'key': value}"),
        ("Hello {{ name }}", None),
        ("{{ name }} there", None),
        ("{{ a }} {{ b }}", None),
        ("plain text", None),
        ("", None),
        ("{{ incomplete", None),
        ("incomplete }}", None),
        ("{ value }", None),
        # "{{ }}" carries nothing to evaluate and simpleeval raises on it at
        # runtime, so it is not a complete template — matching
        # validate_partial_template_str, which has always refused the empty form.
        ("{{}}", None),
        ("{{ }}", None),
        ("  {{   }}  ", None),
        # An escaped template is text, and so is one after a doubled
        # backslash (a backslash, then the value): only whitespace may
        # surround a complete template's braces.
        (r"\{{ value }}", None),
        (r"\\{{ value }}", None),
        (r"  \{{ value }}  ", None),
        # The expression form of a literal "{{" is a complete template.
        ("{{ '{{' }}", "'{{'"),
    ],
)
def test_complete_template_detection(value, expr):
    """Both predicates agree: a string is a complete template exactly when an
    expression can be extracted from it."""
    assert extract_template_expression(value) == expr
    assert is_complete_template(value) is (expr is not None)


# (text, what rendering makes of it with each template rendered as <expr>, the
# templates in it). The one pattern decides for every consumer, so what
# `find_templates` finds is exactly what rendering renders: an escaped `{{` is
# found by none of them, nor is any `{{` in the text it escapes, and a doubled
# backslash is one backslash before a template.
ESCAPES = [
    pytest.param(r"\{{ x }}", "{{ x }}", [], id="escaped"),
    pytest.param(r"\\{{ x }}", "\\<x>", ["x"], id="doubled-backslash-then-template"),
    pytest.param(r"\\\{{ x }}", r"\{{ x }}", [], id="backslash-then-escaped"),
    pytest.param(r"\\\\{{ x }}", r"\\<x>", ["x"], id="two-backslashes-then-template"),
    pytest.param(r"\{{ x }} and {{ y }}", "{{ x }} and <y>", ["y"], id="escape-at-start"),
    pytest.param(r"{{ x }} and \{{ y }}", "<x> and {{ y }}", ["x"], id="escape-after-a-template"),
    pytest.param(r"ends \{{", "ends {{", [], id="escape-at-end"),
    pytest.param(r"\{{", "{{", [], id="escape-alone"),
    # Braces no `}}` closes open no template, and the backslashes before them
    # are halved all the same: `\\` before `{{` is one backslash, always.
    pytest.param(r"\\{{ x", r"\{{ x", [], id="doubled-backslash-before-unclosed-braces"),
    # An escape covers the text after its braces up to the first `}}`: a
    # `{{` in it opens no template.
    pytest.param(r"\{{{{ x }}", "{{{{ x }}", [], id="adjacent-braces-are-escaped-text"),
    pytest.param(r"\{{{ x }}", "{{{ x }}", [], id="escaped-then-one-brace"),
    pytest.param(r"\{{ a {{ x }} }}", "{{ a {{ x }} }}", [], id="template-syntax-inside-escaped-text"),
    pytest.param(r"\{{ a {{ x", "{{ a {{ x", [], id="unclosed-escape-covers-the-rest"),
    pytest.param(r"\{{ '{{' }}", "{{ '{{' }}", [], id="escaped-expression-form"),
    # A `}}` ends the escaped text, and what follows is read as usual.
    pytest.param(r"\{{ a }}{{ x }}", "{{ a }}<x>", ["x"], id="template-right-after-escaped-text"),
    pytest.param(r"\{{ a }}}{{ x }}", "{{ a }}}<x>", ["x"], id="escaped-text-ends-at-the-first-closing-braces"),
    # Escaped text ends with its line, as a template does.
    pytest.param("\\{{ a\n{{ x }}", "{{ a\n<x>", ["x"], id="escape-ends-with-its-line"),
    # Backslashes before `{{` in escaped text are halved as anywhere else.
    pytest.param(r"{\{{ x }}", "{{{ x }}", [], id="brace-then-escape"),
    pytest.param(r"\{{\{{ x }}", "{{{{ x }}", [], id="escape-twice"),
    pytest.param(r"\{{ a \\{{ x }}", r"{{ a \{{ x }}", [], id="doubled-backslash-inside-escaped-text"),
    # The whitespace around an escaped template is kept: it is text.
    pytest.param(r" \{{ x }} ", " {{ x }} ", [], id="padded-escape"),
    # Handlebars and Mustache payloads, as a server expects them.
    pytest.param(r"\{{#each items}}\{{this}}\{{/each}}", "{{#each items}}{{this}}{{/each}}", [], id="handlebars-block"),
    pytest.param(r"\{{{raw}}}", "{{{raw}}}", [], id="handlebars-triple-stash"),
    pytest.param(r"\{{{{raw}}}} {{ x }} \{{{{/raw}}}}", "{{{{raw}}}} <x> {{{{/raw}}}}", ["x"], id="handlebars-raw-block"),
    # A backslash anywhere but right before `{{` is text.
    pytest.param(r"C:\dir\{x}\n", r"C:\dir\{x}\n", [], id="backslashes-elsewhere"),
    pytest.param(r"\{ {x} \}", r"\{ {x} \}", [], id="backslash-before-one-brace"),
    # The expression form renders a literal "{{" too, and what it renders is
    # not read again, so the text after it is text; a template's expression
    # is read as written, a backslash in it included.
    pytest.param("{{ '{{' }}name}}", "<'{{'>name}}", ["'{{'"], id="expression-form"),
    pytest.param(r"{{ '\{{' }}", r"<'\{{'>", [r"'\{{'"], id="escape-inside-an-expression"),
]


@pytest.mark.parametrize(("text", "rendered", "templates"), ESCAPES)
def test_escapes(text, rendered, templates):
    assert render_text(text, lambda expr: f"<{expr}>") == rendered
    assert [match.group("expr").strip() for match in find_templates(text)] == templates
    assert contains_template(text) is bool(templates)
    assert contains_escape(text) is ("\\{{" in text)
    assert needs_rendering(text) is (rendered != text)
    if not templates:
        # The text a string without templates renders to.
        assert unescape(text) == rendered


def test_find_templates_skips_escapes():
    """A plain search with TEMPLATE_PATTERN finds escapes too (it is what
    rendering rewrites); `find_templates` keeps the templates, each match
    starting where its backslashes do."""
    text = r"\{{ a }} \\{{ b }}"
    assert [match.group(0) for match in re.finditer(TEMPLATE_PATTERN, text)] == [r"\{{ a }}", r"\\{{ b }}"]
    assert [(match.start(), match.group("expr")) for match in find_templates(text)] == [(9, " b ")]


def test_unescape_leaves_templates_as_written():
    """`unescape` renders escapes only: a template, and the backslashes
    before it, are the engine's to render."""
    assert unescape(r"\{{ a }} \\{{ b }} {{ c }}") == r"{{ a }} \\{{ b }} {{ c }}"


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        pytest.param("plain", "plain", id="nothing-to-escape"),
        pytest.param("{{ x }}", r"\{{ x }}", id="template"),
        pytest.param("{{a}} and {{b}}", r"\{{a}} and \{{b}}", id="two"),
        pytest.param("{{{{raw}}}}", r"\{{\{{raw}}}}", id="adjacent-braces"),
        pytest.param("{{{x}}}", r"\{{{x}}}", id="three-braces"),
        # A backslash before `{{` is doubled, then the braces escaped.
        pytest.param(r"\{{ x }}", r"\\\{{ x }}", id="backslash-before"),
        pytest.param(r"C:\dir\{x}", r"C:\dir\{x}", id="backslashes-elsewhere"),
        pytest.param("a {{ b\n}}", "a \\{{ b\n}}", id="multiline"),
        pytest.param("}} {", "}} {", id="closing-braces"),
    ],
)
def test_escape(text, escaped):
    """`escape` writes text so that it renders as itself: never a template."""
    assert escape(text) == escaped
    assert unescape(escaped) == text
    assert render_text(escaped, lambda expr: f"<{expr}>") == text
    assert not contains_template(escaped)


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        pytest.param("plain", "plain", id="nothing-to-escape"),
        # A `}}` follows on its line: the escape ends there.
        pytest.param("{{ x }}", r"\{{ x }}", id="closed-by-its-line"),
        # None follows: a template rendering to the braces, which ends where
        # it is written, for each `{{` from there on.
        pytest.param("q={{x", "q={{ '{{' }}x", id="unclosed"),
        pytest.param("{{a}} b{{c{{", r"\{{a}} b{{ '{{' }}c{{ '{{' }}", id="closed-then-unclosed"),
        pytest.param("{{{", "{{ '{{' }}{", id="three-braces"),
        pytest.param(r"a\{{b", r"a\\{{ '{{' }}b", id="backslash-before"),
        # Line by line: an escape ends with its line.
        pytest.param("a{{\nb}}", "a{{ '{{' }}\nb}}", id="closing-on-the-next-line"),
        # Only whitespace around the braces would be one whole template,
        # which renders to its value, the whitespace dropped: one template
        # of the whole text instead.
        pytest.param(" {{", "{{ ' {{' }}", id="whitespace-before"),
        pytest.param("{{\n", "{{ '{{\\n' }}", id="line-break-after"),
    ],
)
def test_escape_closed(text, escaped):
    """`escape(closed=True)` renders to the text, and a template after it on
    its line (after a separator) is read as one, not covered by an escape."""
    assert escape(text, closed=True) == escaped
    assert walk(escaped, {}) == text
    assert walk(escaped + "&{{ x }}", {"x": "X"}) == text + "&X"


def test_escape_passes_a_run_of_backslashes_once():
    """A long run of backslashes before no braces is passed over once, not
    tried again from each backslash (100,000 took half a minute)."""
    run = "\\" * 100_000
    assert escape(run) == escape(run, closed=True) == run


@pytest.mark.parametrize("text", [param.values[0] for param in ESCAPES])
def test_escape_inverts_rendering(text):
    """Every string of the escapes table, taken as literal text, escapes to
    scenario text rendering gives back exactly: templates, escapes and
    backslashes alike."""
    assert render_text(escape(text), lambda expr: f"<{expr}>") == text
