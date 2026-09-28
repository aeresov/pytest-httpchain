import uuid
from collections import ChainMap
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from pytest_httpchain.templates import CONTEXT_HELPERS, TEMPLATE_BUILTINS, TemplatesError, contains_template, substitution, walk, walker
from tests.unit.helpers import BEYOND_RECURSION_LIMIT, LOADABLE_BUT_DEEP, nested


class SampleModel(BaseModel):
    name: str
    value: int


class TestWalk:
    def test_string_interpolation(self):
        assert walk("{{ greeting }} {{ name }}", {"greeting": "hello", "name": "World"}) == "hello World"

    def test_single_expression_preserves_type(self):
        assert walk("{{ value }}", {"value": 42}) == 42

    @pytest.mark.parametrize(("template", "expected"), [(" {{ a == b }} ", False), ("\t{{ value }}\n", 42)])
    def test_padded_single_expression_preserves_type(self, template, expected):
        """H7: padding does not demote a complete template to interpolation,
        matching is_complete_template (which types a field as
        TemplateExpression). Before, a `verify.expressions` entry like
        " {{ a == b }} " became the truthy " False " and passed."""
        result = walk(template, {"a": 1, "b": 2, "value": 42})
        assert result == expected
        assert type(result) is type(expected)

    def test_mixed_content_keeps_surrounding_whitespace(self):
        assert walk(" prefix {{ value }}", {"value": 42}) == " prefix 42"

    def test_multiline_expression_is_left_verbatim(self):
        """Single-line by design: an expression spanning a newline is not a template."""
        assert walk("{{ a\n }}", {"a": 1}) == "{{ a\n }}"

    @pytest.mark.parametrize(
        ("obj", "expected"),
        [
            pytest.param({"greeting": "Hello {{ name }}", "count": "{{ num }}"}, {"greeting": "Hello Alice", "count": 5}, id="dict"),
            pytest.param(["{{ num }}", "Value: {{ num }}"], [5, "Value: 5"], id="list"),
            # SSLConfig.cert is the one tuple-typed model field; without tuple
            # support a templated cert path is passed verbatim.
            pytest.param(("{{ num }}", "Value: {{ num }}"), (5, "Value: 5"), id="tuple"),
            pytest.param({"a": {"b": {"c": {"d": "{{ name }}"}}}}, {"a": {"b": {"c": {"d": "Alice"}}}}, id="deep-dict"),
            pytest.param({"items": [{"n": "{{ name }}"}, {"n": "{{ num }}"}]}, {"items": [{"n": "Alice"}, {"n": 5}]}, id="dicts-in-list"),
        ],
    )
    def test_containers_walked_recursively(self, obj, expected):
        assert walk(obj, {"name": "Alice", "num": 5}) == expected

    @pytest.mark.parametrize("value", [42, 3.14, -2.5, None, True, "", {}, []])
    def test_template_free_values_pass_through(self, value):
        result = walk(value, {})
        assert result == value
        assert type(result) is type(value)

    def test_pydantic_model(self):
        assert walk(SampleModel(name="User {{ id }}", value=100), {"id": "123"}) == SampleModel(name="User 123", value=100)

    def test_simple_namespace(self):
        ns = SimpleNamespace(greeting="Hello {{ name }}", details={"value": "{{ num }}"}, count=5)
        assert walk(ns, {"name": "World", "num": 42}) == SimpleNamespace(greeting="Hello World", details={"value": 42}, count=5)

    @pytest.mark.parametrize("obj", [SampleModel(name="static", value=100), SimpleNamespace(name="static", value=42)], ids=["pydantic", "namespace"])
    def test_template_free_object_returned_as_is(self, obj):
        assert walk(obj, {}) is obj

    def test_nested_namespace_walked_one_frame_per_level(self):
        """A `vars` value reaches the walk as nested namespaces. Handing vars()
        to the dict case spent two frames per level, which overflowed at a
        depth the loader accepts."""
        value: Any = "{{ x }}"
        for _ in range(LOADABLE_BUT_DEEP):
            value = SimpleNamespace(k=value)
        result = walk(value, {"x": 1})
        for _ in range(LOADABLE_BUT_DEEP):
            result = result.k
        assert result == 1

    def test_value_nested_past_the_stack_fails_as_templates_error(self):
        """The walk still recurses per level. Past the limit it fails as the
        TemplatesError the carrier reports as a stage failure, not as a bare
        RecursionError traceback."""
        with pytest.raises(TemplatesError, match=r"^Value nested too deeply to substitute \(maximum recursion depth exceeded"):
            walk(nested("{{ x }}", BEYOND_RECURSION_LIMIT), {"x": 1})

    def test_context_nested_past_the_stack_fails_as_templates_error(self):
        """Building the evaluator reads the whole context, and a ChainMap
        nested in ChainMaps resolves through one frame per level. Past the
        limit that fails the same way as a value nested too deeply."""
        context: Mapping[str, Any] = {"x": 1}
        for _ in range(BEYOND_RECURSION_LIMIT):
            context = ChainMap(context)
        with pytest.raises(TemplatesError, match=r"^Value nested too deeply to substitute \(maximum recursion depth exceeded"):
            walk("{{ x }}", context)


@pytest.mark.parametrize(
    ("expr", "context", "expected"),
    [
        ("{{ [1, 2, 3, 4] }}", {}, [1, 2, 3, 4]),
        ("{{ dict(key='value', number=42) }}", {}, {"key": "value", "number": 42}),
        ("{{ {'key': 'value', 'number': 42} }}", {}, {"key": "value", "number": 42}),
        ("{{ {} }}", {}, {}),
        ("{{ [x * 2 for x in items] }}", {"items": [1, 2, 3]}, [2, 4, 6]),
        ("{{ {k: v * 2 for k, v in data.items()} }}", {"data": {"a": 1, "b": 2}}, {"a": 2, "b": 4}),
        ("{{ dict([(k, v * 2) for k, v in data.items()]) }}", {"data": {"a": 1, "b": 2}}, {"a": 2, "b": 4}),
        ("{{ users[index]['name'] }}", {"users": [{"name": "Alice"}, {"name": "Bob"}], "index": 0}, "Alice"),
        ("{{ sum([x * multiplier for x in numbers]) }}", {"numbers": [1, 2, 3, 4, 5], "multiplier": 2}, 30),
        ("{{ str(123) }}", {}, "123"),
        ("{{ int('42') }}", {}, 42),
        ("{{ len([1, 2, 3]) }}", {}, 3),
        ("{{ max([1, 5, 3]) }}", {}, 5),
        ("{{ sum([1, 2, 3]) }}", {}, 6),
        ("{{ sorted([3, 1, 2]) }}", {}, [1, 2, 3]),
        ("{{ abs(-5) }}", {}, 5),
        ("{{ abs(-3.14) }}", {}, 3.14),
        ("{{ round(3.7) }}", {}, 4),
        ("{{ round(3.14159, 2) }}", {}, 3.14),
        ("{{ tuple([1, 2, 3]) }}", {}, (1, 2, 3)),
        ("{{ tuple('abc') }}", {}, ("a", "b", "c")),
        ("{{ set([1, 1, 2, 2, 3]) }}", {}, {1, 2, 3}),
        ("{{ list(enumerate(['a', 'b', 'c'])) }}", {}, [(0, "a"), (1, "b"), (2, "c")]),
        ("{{ list(zip([1, 2], ['a', 'b'])) }}", {}, [(1, "a"), (2, "b")]),
        ("{{ list(range(5)) }}", {}, [0, 1, 2, 3, 4]),
        ("{{ list(range(2, 5)) }}", {}, [2, 3, 4]),
        ("{{ bool(1) }}", {}, True),
        ("{{ bool(0) }}", {}, False),
        ("{{ bool([]) }}", {}, False),
        ("{{ bool([1]) }}", {}, True),
        # A ';' inside a string literal separates no statements.
        ("{{ 'a;b' + x }}", {"x": ";c"}, "a;b;c"),
    ],
)
def test_expression_value(expr, context, expected):
    result = walk(expr, context)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(
    ("expr", "context", "expected"),
    [
        ("{{ get('missing', 'default') }}", {}, "default"),
        ("{{ get('var', 'default') }}", {"var": "actual"}, "actual"),
        ("{{ get('missing') }}", {}, None),
        ("{{ get('config', {}).get('missing', 'not found') }}", {}, "not found"),
        ("{{ get('name', 'Guest').upper() }}", {}, "GUEST"),
        ("{{ get('name', 'Guest').upper() }}", {"name": "alice"}, "ALICE"),
        ("{{ [x.upper() for x in get('items', [])] }}", {"items": ["a", "b", "c"]}, ["A", "B", "C"]),
        ("{{ [x.upper() for x in get('items', [])] }}", {}, []),
    ],
)
def test_get(expr, context, expected):
    assert walk(expr, context) == expected


@pytest.mark.parametrize(
    ("expr", "context", "expected"),
    [
        ("{{ exists('var') }}", {"var": "value"}, True),
        ("{{ exists('var') }}", {}, False),
        ("{{ items[2] if exists('items') else 'no items' }}", {"items": [1, 2, 3]}, 3),
        ("{{ items[2] if exists('items') else 'no items' }}", {}, "no items"),
    ],
)
def test_exists(expr, context, expected):
    assert walk(expr, context) == expected


@pytest.mark.parametrize(
    ("expr", "context", "expected"),
    [
        pytest.param("{{ exists('x') }}", {"exists": lambda name: "user", "x": 1}, True, id="exists-not-overridable"),
        pytest.param("{{ get('missing', 'default') }}", {"get": lambda *a: "user"}, "default", id="get-not-overridable"),
        pytest.param("{{ len('abc') }}", {"len": lambda x: 99}, 99, id="user-callable-shadows-safe-function"),
        pytest.param("{{ true }}", {"true": 5}, 5, id="user-name-shadows-json-literal"),
    ],
)
def test_evaluator_merge_order(expr, context, expected):
    """functions= merges last-wins: user callables shadow the safe functions,
    and exists/get come last so no context entry can replace them. names=
    likewise lets a user name shadow a JSON literal."""
    assert walk(expr, context) == expected


@pytest.mark.parametrize(
    ("expr", "context", "expected"),
    [
        ("{{ greet('World') }}", {"greet": lambda name: f"Hello, {name}"}, "Hello, World"),
        ("{{ add(1, 2, 3) }}", {"add": lambda a, b, c: a + b + c}, 6),
        ("{{ get_config() }}", {"get_config": lambda: {"host": "localhost", "port": 8080}}, {"host": "localhost", "port": 8080}),
        ("{{ sum(get_items()) }}", {"get_items": lambda: [1, 2, 3]}, 6),
    ],
)
def test_context_callables(expr, context, expected):
    assert walk(expr, context) == expected


def test_uuid4_returns_canonical_uuid_string():
    result = walk("{{ uuid4() }}", {})
    assert str(uuid.UUID(result)) == result


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("{{ env('HTTPCHAIN_TEST_SET') }}", "test_value"),
        ("{{ env('HTTPCHAIN_TEST_UNSET', 'default') }}", "default"),
        ("{{ env('HTTPCHAIN_TEST_UNSET') }}", None),
    ],
)
def test_env(monkeypatch, expr, expected):
    monkeypatch.setenv("HTTPCHAIN_TEST_SET", "test_value")
    monkeypatch.delenv("HTTPCHAIN_TEST_UNSET", raising=False)
    assert walk(expr, {}) == expected


@pytest.mark.parametrize(
    ("obj", "expected"),
    [
        ("{{ x }}", True),
        ("prefix {{ x }}", True),
        ("plain", False),
        ({"a": ["{{ x }}"]}, True),
        ({"a": ["plain"]}, False),
        (("plain", "{{ x }}"), True),
        ([1, None], False),
        (SampleModel(name="{{ x }}", value=1), True),
        (SimpleNamespace(a="plain"), False),
        (42, False),
    ],
)
def test_contains_template(obj, expected):
    assert contains_template(obj) is expected


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no text form")


@pytest.mark.parametrize(("leaf", "expected"), [("{{ x }}", True), ("plain", False)])
def test_contains_template_at_any_depth(leaf, expected):
    """Iterative: a recursive walk spent two frames per level of nesting."""
    assert contains_template(nested(leaf, BEYOND_RECURSION_LIMIT)) is expected


class TestWalkErrorMessages:
    @pytest.mark.parametrize(
        ("expr", "context", "expected_match"),
        [
            ("{{ missing_var }}", {}, "Undefined variable"),
            ("{{ unknown_func() }}", {}, "Unknown function"),
            pytest.param("{{ __import__('os').system('ls') }}", {}, "Unknown function", id="import-blocked"),
            pytest.param("{{ open('/etc/passwd', 'r').read() }}", {}, "Unknown function", id="open-blocked"),
            ("{{ x.nonexistent }}", {"x": {"existing": 1}}, "Attribute error"),
            ("{{ a @ b }}", {"a": 1, "b": 2}, "Operator not allowed"),
            ("{{ 1 / 0 }}", {}, "ZeroDivisionError"),
            ("{{ 'text' + 5 }}", {}, "TypeError"),
            ("{{ [1, 2][10] }}", {}, "IndexError"),
            ("{{ dict(a=1)['b'] }}", {}, "KeyError"),
            ("{{ 1 + }}", {}, "Invalid expression"),
        ],
    )
    def test_error_messages(self, expr, context, expected_match):
        with pytest.raises(TemplatesError, match=expected_match):
            walk(expr, context)

    def test_error_message_keeps_braces_and_appends_cause(self):
        """M29: the expression is shown in its {{ }} form (not collapsed to
        single braces) and the simpleeval cause is appended."""
        with pytest.raises(TemplatesError, match=r"'\{\{ missing_var \}\}': .*is not defined"):
            walk("{{ missing_var }}", {})

    def test_context_callable_raising_is_wrapped_as_templates_error(self):
        """M30: an exception from a context callable (user function or factory
        fixture) surfaces as a TemplatesError naming its type, with the cause in
        the message and chained — not as a raw traceback."""

        class CustomBoom(Exception):
            pass

        def boom():
            raise CustomBoom("kaboom from user function")

        with pytest.raises(TemplatesError, match=r"CustomBoom in expression '\{\{ boom\(\) \}\}': kaboom from user function") as exc_info:
            walk("{{ boom() }}", {"boom": boom})
        assert isinstance(exc_info.value.__cause__, CustomBoom)

    @pytest.mark.parametrize(
        ("template", "context", "expected_match"),
        [
            # Python refuses to render an int past 4300 digits as text.
            pytest.param("x {{ 2 ** 100000 }}", {}, r"ValueError in expression '\{\{ 2 \*\* 100000 \}\}': Exceeds the limit", id="int-digit-limit"),
            pytest.param("x {{ value }}", {"value": _Unprintable()}, r"RuntimeError in expression '\{\{ value \}\}': no text form", id="raising-str"),
        ],
    )
    def test_interpolation_that_cannot_render_is_a_templates_error(self, template, context, expected_match):
        """Interpolating a value calls str() on it, which can raise too. That
        call ran outside the expression's error handling, so its raw exception
        escaped a stage as a plugin traceback instead of failing it."""
        with pytest.raises(TemplatesError, match=expected_match):
            walk(template, context)

    @pytest.mark.parametrize("template", ["{{ ok == True; False }}", "x {{ ok; False }}"], ids=["whole-string", "interpolated"])
    def test_multiple_statements_are_rejected(self, template):
        """simpleeval parses in exec mode and evaluated only the first of
        `a; b`, behind a mere warning — so this verify expression came out
        True, and the stage passed on half of what it asserts."""
        with pytest.raises(TemplatesError, match=r"Invalid expression '\{\{ .*; False \}\}': a template holds one expression, not 2 statements"):
            walk(template, {"ok": True})


@pytest.mark.parametrize("name", sorted(TEMPLATE_BUILTINS))
def test_advertised_builtin_resolves(name):
    """M14: every name the validator treats as engine-provided must resolve —
    to a function, or a JSON literal's value — not raise "Undefined variable".
    Read inside a list: a helper that a template renders to is refused."""
    [result] = walk("{{ [" + name + "] }}", {})
    assert callable(result) or result is None or isinstance(result, bool)


@pytest.mark.parametrize("name", sorted(CONTEXT_HELPERS))
def test_context_helpers_are_never_shadowed(name):
    """A user callable named get or exists never replaces the built-in, which
    the validator's reference model assumes: a call to either is no reference
    to a user name."""
    assert walk("{{ " + name + "('x') }}", {name: lambda *_: "user"}) != "user"


class TestChainMapContextSemantics:
    """The runtime context is a ChainMap (a layer per stage, per save step and
    per iteration), and the evaluator is built from ONE traversal of it. These
    pin what that traversal must preserve: first-layer-wins, and the
    callable/value split taken from the winning layer alone."""

    def test_first_layer_wins(self):
        context = ChainMap({"x": "top"}, {"x": "middle"}, {"x": "bottom"})
        assert walk("{{ x }}", context) == "top"
        # The helpers close over the same flattened view, so they agree.
        assert walk("{{ get('x') }}", context) == "top"

    def test_shadowed_callable_does_not_leak_into_names(self):
        """A per-layer partition would put the shadowed value in names= and the
        shadowed callable in functions= at once; the winning layer decides both."""
        callable_on_top = ChainMap({"f": lambda: "top"}, {"f": 7})
        assert walk("{{ f() }}", callable_on_top) == "top"
        # simpleeval's name lookup falls back to functions=, so a bare reference
        # resolves to the callable itself — never to the shadowed 7.
        assert callable(walk("{{ f }}", callable_on_top))

        value_on_top = ChainMap({"f": 7}, {"f": lambda: "shadowed"})
        assert walk("{{ f }}", value_on_top) == 7
        with pytest.raises(TemplatesError, match="Unknown function"):
            walk("{{ f() }}", value_on_top)

    def test_callable_named_like_a_json_literal_stays_out_of_names(self):
        """The split is by value, not by name: the literal keeps the plain-name
        slot while the callable is reachable only as a call."""
        context = {"true": lambda: "called"}
        assert walk("{{ true }}", context) is True
        assert walk("{{ true() }}", context) == "called"

    def test_helpers_report_the_winning_layer(self):
        """exists()/get() see callables too, and the copy they are bound to is
        the flattened one — a shadowed layer is invisible to them as well."""
        context = ChainMap({"fn": lambda: "top"}, {"fn": "shadowed"})
        assert walk("{{ exists('fn') }}", context) is True
        assert walk("{{ get('fn') }}", context)() == "top"

    def test_upper_layer_adds_without_hiding_lower_ones(self):
        context = ChainMap({"b": 2}, {"a": 1})
        assert walk("{{ a + b }}", context) == 3


class TestWalker:
    """``walker(context)`` is ``walk`` bound to one context, for many
    structures each substituted on its own: one evaluator serves every call."""

    def test_substitutes_as_walk_does(self):
        substitute = walker({"a": 1, "items": [1, 2]})
        assert substitute({"x": "{{ a }}", "y": ["{{ [i * 2 for i in items] }}", "n={{ a }}"]}) == {"x": 1, "y": [[2, 4], "n=1"]}

    def test_one_evaluator_serves_every_call(self, monkeypatch):
        """The evaluator is built when the walker is bound, not per call: that
        per-call pass over the context is what the walker exists to save."""
        builds: list[dict[str, Any]] = []
        build = substitution._build_evaluator

        def counting_build(context):
            builds.append(dict(context))
            return build(context)

        monkeypatch.setattr(substitution, "_build_evaluator", counting_build)
        render = walker({"x": 1})

        assert [render("{{ x }}"), render({"k": ["{{ x + 1 }}"]}), render("x={{ x }}")] == [1, {"k": [2]}, "x=1"]
        assert builds == [{"x": 1}]

    def test_context_is_read_when_bound(self):
        context = {"x": 1}
        render = walker(context)
        context["x"] = 2
        assert render("{{ x }}") == 1

    @pytest.mark.parametrize(
        "failing",
        [
            pytest.param("{{ missing }}", id="undefined"),
            pytest.param("{{ [boom() for i in items] }}", id="raised-in-a-comprehension"),
            pytest.param("{{ [i for i in range(10 ** 9)] }}", id="comprehension-too-long"),
        ],
    )
    def test_a_call_that_raises_leaves_the_next_unaffected(self, failing):
        """simpleeval swaps its name lookup in for a comprehension and restores
        it on the way out, whatever ended it; the next call sees the context
        as the first did."""

        def boom():
            raise ValueError("boom")

        substitute = walker({"items": [1, 2], "boom": boom, "i": "outer"})
        with pytest.raises(TemplatesError):
            substitute(failing)
        assert substitute("{{ [i for i in items] }}") == [1, 2]
        assert substitute("{{ i }}") == "outer"
