import ast
import re
import uuid
from collections import ChainMap
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from simpleeval import InvalidExpression

from pytest_httpchain.models.types import VarsNamespace, convert_dict_to_namespace
from pytest_httpchain.templates import CONTEXT_HELPERS, TEMPLATE_BUILTINS, TemplatesError, contains_template, parse_expression, substitution, walk, walker
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
            # Named as the attribute path names an attribute, not as a bare KeyError.
            ("{{ dict(a=1)['b'] }}", {}, r"^Key error in expression '\{\{ dict\(a=1\)\['b'\] \}\}': Key 'b' does not exist"),
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

    @pytest.mark.parametrize(
        ("template", "reason"),
        [
            # `=` for `==`: simpleeval evaluated the right-hand side behind an
            # AssignmentAttempted warning, so this verify expression came out
            # True whatever `user.active` was.
            pytest.param("{{ user.active = True }}", "not an assignment; to compare two values, write '=='", id="assign"),
            pytest.param("n={{ count = 3 }}", "not an assignment; to compare two values, write '=='", id="assign-interpolated"),
            pytest.param("{{ user['active'] = flag = True }}", "not an assignment; to compare two values, write '=='", id="assign-chained"),
            # Evaluated to the right-hand side too: 1, not count + 1.
            pytest.param("{{ count += 1 }}", "not an assignment", id="augmented"),
            # simpleeval refused these two itself, but only at evaluation, as a
            # feature it does not have: now the one reason, known to validate.
            pytest.param("{{ count: int = 1 }}", "not an assignment", id="annotated"),
            pytest.param("{{ [(n := x) for x in items] }}", r"a template cannot assign a name \(':='\)", id="walrus"),
            pytest.param("{{ import os }}", "not a statement", id="import"),
            pytest.param("{{ del count }}", "not a statement", id="del"),
        ],
    )
    def test_assignments_and_statements_are_rejected(self, template, reason):
        expr = template.partition("{{ ")[2].removesuffix(" }}")
        with pytest.raises(TemplatesError, match=rf"^Invalid expression '{re.escape('{{ ' + expr + ' }}')}': (a template holds one expression, )?{reason}$"):
            walk(template, {"user": SimpleNamespace(active=False), "flag": False, "count": 0, "items": [1]})

    @pytest.mark.parametrize(
        ("template", "reason"),
        [
            # Python's own reason, without its "(<unknown>, line 1)": a template is one line.
            pytest.param("{{ 1 + }}", "invalid syntax", id="syntax"),
            # The dict literal's `}` ran into the template's `}}`: the template ended a brace early.
            pytest.param("{{ {'a': 1}}}", "'{' was never closed", id="brace-before-closing-braces"),
            pytest.param("x {{ }} y", "a template holds one expression, and this one is empty", id="empty"),
            # CPython's parser runs out of stack on these, as a MemoryError and a RecursionError.
            pytest.param("{{ " + "-" * 100_000 + "1 }}", "the expression is too complex to parse", id="parser-stack"),
            pytest.param("{{ a" + ".b" * 100_000 + " }}", "the expression is too complex to parse", id="ast-recursion"),
            # A lone surrogate, one JSON \u escape away: the parser raised a
            # UnicodeEncodeError, which escaped walk() as it was.
            pytest.param("{{ 'a\ud800' }}", "the expression holds '\\ud800', which is not valid text (surrogates not allowed)", id="lone-surrogate"),
        ],
    )
    def test_text_that_does_not_parse_says_why(self, template, reason):
        with pytest.raises(TemplatesError, match=rf"^Invalid expression '.*': {re.escape(reason)}$"):
            walk(template, {"a": SimpleNamespace(b=1)})

    def test_a_lone_surrogate_is_written_escaped(self):
        """As the scenario's JSON writes it: no UTF-8 stream can print the
        character itself, and `validate` crashed printing it."""
        with pytest.raises(TemplatesError) as excinfo:
            walk("{{ 'a\ud800' }}", {})
        assert str(excinfo.value).startswith("Invalid expression '{{ 'a\\ud800' }}': ")

    # Each template the engine refuses from its text alone, and the reason.
    # simpleeval refused each too ("Sorry, Lambda is not available in this
    # evaluator"), but only once evaluation reached it, so `validate` never
    # saw it.
    _REFUSED_FROM_TEXT = [
        pytest.param("{{ sorted(items, key=lambda row: row) }}", "does not evaluate a lambda", id="lambda"),
        pytest.param("{{ {x for x in items} }}", "does not evaluate a set comprehension; write set(... for ...)", id="set-comprehension"),
        pytest.param("{{ max(*items) }}", "does not evaluate '*' unpacking outside a list literal ([*a, *b])", id="starred-argument"),
        pytest.param("{{ (*items, 1) }}", "does not evaluate '*' unpacking outside a list literal ([*a, *b])", id="starred-in-tuple"),
        pytest.param("{{ [x for *x, y in pairs] }}", "does not evaluate '*' unpacking outside a list literal ([*a, *b])", id="starred-target"),
        pytest.param("{{ (yield) }}", "does not evaluate 'yield'", id="yield"),
        pytest.param("{{ (yield from items) }}", "does not evaluate 'yield from'", id="yield-from"),
        pytest.param("{{ await items }}", "does not evaluate 'await'", id="await"),
        # A key of JSON data read as an attribute: MongoDB's `_id`.
        pytest.param("{{ doc._id }}", "does not read an attribute named '_id'; for a key of that name, write ['_id']", id="underscore-attribute"),
        pytest.param("{{ doc.__class__ }}", "does not read an attribute named '__class__'", id="dunder-attribute"),
        pytest.param("{{ items.func_name }}", "does not read an attribute named 'func_name'; for a key of that name, write ['func_name']", id="func-attribute"),
        pytest.param("{{ 'id-{}'.format(1) }}", "does not read the attribute 'format'; build the text with an f-string or +", id="format"),
        pytest.param("{{ items.mro }}", "does not read the attribute 'mro'", id="disallowed-attribute"),
        # simpleeval took the function for a lambda: "Lambda Functions not implemented".
        pytest.param("{{ fns[0]() }}", "calls only a name or an attribute (f(), obj.method())", id="call-of-a-subscript"),
        pytest.param("{{ get('f')() }}", "calls only a name or an attribute (f(), obj.method())", id="call-of-a-call"),
        pytest.param("{{ (lambda: 1)() }}", "does not evaluate a lambda", id="call-of-a-lambda"),
    ]
    _CONTEXT = {"items": [1, 2], "pairs": [[1, 2]], "doc": {"_id": 1}, "fns": [len], "f": len}

    @pytest.mark.parametrize(("template", "reason"), _REFUSED_FROM_TEXT)
    def test_what_the_engine_refuses_from_the_text_is_refused_up_front(self, template, reason):
        with pytest.raises(TemplatesError, match=rf"^Invalid expression '.*': the template engine {re.escape(reason)}$"):
            walk(template, self._CONTEXT)

    @pytest.mark.parametrize(("template", "reason"), _REFUSED_FROM_TEXT)
    def test_simpleeval_refuses_each_template_refused(self, template, reason):
        """Never stricter than the engine where it evaluates the expression:
        should simpleeval learn one of these (outside the lists read, as it
        spreads a list's ``*`` outside its node map), this fails."""
        expr = template.removeprefix("{{ ").removesuffix(" }}")
        # FeatureNotAvailable ("Sorry, Lambda is not available ..."), or for a
        # starred comprehension target an AttributeError of its own.
        with pytest.raises((InvalidExpression, AttributeError)):
            substitution._build_evaluator(self._CONTEXT).eval(expr)

    def test_an_unevaluated_kind_is_refused_where_evaluation_would_not_reach_it(self):
        """Refused from its text, as the validator refuses it, not only when
        the branch holding it is taken."""
        with pytest.raises(TemplatesError, match="does not evaluate a lambda"):
            walk("{{ 1 if ok else (lambda: 2) }}", {"ok": True})

    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            # The one `*` the engine takes, spread by the list itself.
            pytest.param("{{ [*a, *b] }}", [1, 2, 3], id="list-spread"),
            pytest.param("{{ {**d, 'k': 2} }}", {"k": 2}, id="dict-spread"),
            pytest.param("{{ set(x for x in a) }}", {1, 2}, id="set-of-a-generator"),
        ],
    )
    def test_spreads_and_generators_evaluate(self, template, expected):
        assert walk(template, {"a": [1, 2], "b": [3], "d": {"k": 1}}) == expected

    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            # The `=` of a keyword argument and a comparison assign nothing;
            # nor does a `;` or an `=` inside a string.
            pytest.param("{{ dict(a=count) }}", {"a": 0}, id="keyword-argument"),
            pytest.param("{{ count == 0 }}", True, id="comparison"),
            pytest.param("{{ 'a = b; c' }}", "a = b; c", id="string"),
            # One statement: a trailing `;` ends it, and is no second one.
            pytest.param("{{ count; }}", 0, id="trailing-semicolon"),
        ],
    )
    def test_equals_signs_that_assign_nothing_evaluate(self, template, expected):
        assert walk(template, {"count": 0}) == expected

    @pytest.mark.parametrize("expr", ["user.active = True", "1 +", "a; b", "", "(n := 1)"])
    def test_parse_expression_gives_the_reason_the_runtime_gives(self, expr):
        """The validator reports the reason `parse_expression` gives
        (HTTPCHAIN037); the stage fails with the same one."""
        with pytest.raises(TemplatesError) as parsed:
            parse_expression(expr)
        with pytest.raises(TemplatesError) as rendered:
            walk("x {{ " + expr + " }}", {})
        assert str(rendered.value) == f"Invalid expression '{{{{ {expr} }}}}': {parsed.value}"

    def test_parse_expression_returns_the_expression_evaluated(self):
        """Not the statement around it: the validator walks this tree for the
        names a template reads."""
        tree = parse_expression("  sorted(rows, key=len)  ")
        assert isinstance(tree, ast.Call)
        assert ast.unparse(tree) == "sorted(rows, key=len)"


class TestObjectAccess:
    """A `vars` object (the models' `VarsNamespace`) read by attribute and by
    key, as a dict saved from a response is read."""

    @staticmethod
    def _context() -> dict[str, Any]:
        return {
            "user": convert_dict_to_namespace({"name": "Alice", "Content-Type": "json", "address": {"city": "Oslo"}, "roles": [{"id": 1}, {"id": 2}]}),
            "order": convert_dict_to_namespace({"items": [1, 2], "keys": "k", "_id": 7}),
            "saved": {"name": "Bob", "X-Request-Id": "r-1"},
        }

    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            pytest.param("{{ user.name }}", "Alice", id="attribute"),
            pytest.param("{{ user['name'] }}", "Alice", id="subscript"),
            pytest.param("{{ user['Content-Type'] }}", "json", id="key-that-is-no-identifier"),
            pytest.param("{{ user['address']['city'] }}", "Oslo", id="nested-subscript"),
            pytest.param("{{ user.address['city'] + user['address'].city }}", "OsloOslo", id="mixed"),
            pytest.param("{{ user.roles[1]['id'] }}", 2, id="object-in-a-list"),
            pytest.param("{{ [role['id'] for role in user.roles] }}", [1, 2], id="objects-in-a-comprehension"),
            pytest.param("{{ 'name' in user }}", True, id="in"),
            pytest.param("{{ 'nick' not in user }}", True, id="not-in"),
            pytest.param("{{ len(user) }}", 4, id="len"),
            pytest.param("{{ [k for k in user] }}", ["name", "Content-Type", "address", "roles"], id="iteration-in-order"),
            pytest.param("{{ sorted(user)[0] }}", "Content-Type", id="sorted"),
            pytest.param("{{ list(user.keys()) }}", ["name", "Content-Type", "address", "roles"], id="keys"),
            pytest.param("{{ list(user.values())[:2] }}", ["Alice", "json"], id="values"),
            pytest.param("{{ {k: v for k, v in user.items() if k == 'name'} }}", {"name": "Alice"}, id="items"),
            pytest.param("{{ user.get('nick', 'anon') }}", "anon", id="get-default"),
            pytest.param("{{ user.get('nick') }}", None, id="get-none"),
            pytest.param("{{ user.get('name') }}", "Alice", id="get"),
            pytest.param("{{ dict(user)['name'] }}", "Alice", id="dict"),
            pytest.param("{{ {**user}['Content-Type'] }}", "json", id="dict-spread"),
            pytest.param("{{ 'x' if user.get('address') else 'y' }}", "x", id="truth"),
            # A dict saved from a response reads the same way.
            pytest.param("{{ saved['X-Request-Id'] + saved.name }}", "r-1Bob", id="saved-dict"),
        ],
    )
    def test_access_forms(self, template, expected):
        assert walk(template, self._context()) == expected

    def test_key_named_like_a_method(self):
        """The attribute reads the data, as it always did; so does every key
        form. The shadowed method, called, calls the data."""
        context = self._context()
        assert walk("{{ order.items }}", context) == [1, 2]
        assert walk("{{ order['items'] }}", context) == [1, 2]
        assert walk("{{ order.keys }}", context) == "k"
        assert walk("{{ [order[k] for k in order][0] }}", context) == [1, 2]
        with pytest.raises(TemplatesError, match=r"^TypeError in expression '\{\{ order.items\(\) \}\}': 'list' object is not callable$"):
            walk("{{ order.items() }}", context)

    def test_underscore_key_reads_by_subscript_only(self):
        """simpleeval refuses an attribute starting with `_`, from the text,
        but never a key: `_eval_subscript` checks nothing of the key."""
        context = self._context()
        assert walk("{{ order['_id'] }}", context) == 7
        with pytest.raises(TemplatesError, match=r"for a key of that name, write \['_id'\]$"):
            walk("{{ order._id }}", context)

    def test_subscript_reaches_the_data_only(self):
        with pytest.raises(TemplatesError, match=r"^Key error in expression .*: Key '__class__' does not exist"):
            walk("{{ user['__class__'] }}", self._context())

    @pytest.mark.parametrize(
        ("template", "message"),
        [
            pytest.param(
                "{{ user['nick'] }}",
                "Key error in expression '{{ user['nick'] }}': Key 'nick' does not exist in expression 'user['nick']'",
                id="vars-object",
            ),
            pytest.param(
                "{{ user['address']['zip'] == 1 }}",
                "Key error in expression '{{ user['address']['zip'] == 1 }}': Key 'zip' does not exist in expression 'user['address']['zip'] == 1'",
                id="nested",
            ),
            pytest.param(
                "Name: {{ saved['nick'] }}",
                "Key error in expression '{{ saved['nick'] }}': Key 'nick' does not exist in expression 'saved['nick']'",
                id="saved-dict-interpolated",
            ),
            # As the attribute path names a missing attribute.
            pytest.param(
                "{{ user.nick }}",
                "Attribute error in expression '{{ user.nick }}': Attribute 'nick' does not exist in expression 'user.nick'",
                id="attribute",
            ),
        ],
    )
    def test_missing_key_names_it(self, template, message):
        with pytest.raises(TemplatesError) as excinfo:
            walk(template, self._context())
        assert str(excinfo.value) == message

    def test_key_error_of_a_function_is_not_taken_for_a_missing_key(self):
        """Only the subscript's own lookup is: a user function's KeyError is
        its own error, named by its type as any other."""

        def lookup():
            return {}["gone"]

        with pytest.raises(TemplatesError, match=r"^KeyError in expression '\{\{ lookup\(\)\['x'\] \}\}': 'gone'$"):
            walk("{{ lookup()['x'] }}", {"lookup": lookup})

    @pytest.mark.parametrize(
        ("template", "expression", "call"),
        [
            pytest.param("{{ user.keys }}", "user.keys", ".keys()", id="keys"),
            pytest.param("{{ user.items }}", "user.items", ".items()", id="items"),
            pytest.param("Values: {{ user.values }}", "user.values", ".values()", id="interpolated"),
            pytest.param("{{ user.address.get }}", "user.address.get", ".get(...)", id="get-takes-a-key"),
            # Inside an expression too: the method is a value there, always
            # true and never equal to data, so each of these passed as a check.
            pytest.param("{{ user.items != [] }}", "user.items != []", ".items()", id="compared"),
            pytest.param("{{ user.get is not None }}", "user.get is not None", ".get(...)", id="is-not-none"),
            pytest.param("{{ bool(user.values) }}", "bool(user.values)", ".values()", id="truth"),
            pytest.param("{{ 'x' if user.keys else 'y' }}", "'x' if user.keys else 'y'", ".keys()", id="condition"),
            pytest.param("{{ 'k=' + str(user.keys) }}", "'k=' + str(user.keys)", ".keys()", id="str"),
            pytest.param("{{ [user.items][0] }}", "[user.items][0]", ".items()", id="in-a-list"),
            pytest.param("{{ [r.get for r in user.roles] }}", "[r.get for r in user.roles]", ".get(...)", id="in-a-comprehension"),
            # Handed to a call, but not as its key=: the call is not the method's.
            pytest.param("{{ dict(at=user.get) }}", "dict(at=user.get)", ".get(...)", id="other-keyword"),
            pytest.param("{{ sorted(user, user.get) }}", "sorted(user, user.get)", ".get(...)", id="positional"),
        ],
    )
    def test_method_read_as_a_value_is_refused(self, template, expression, call):
        """An attribute where the object has no key of that name was a missing
        attribute; now that the object has methods, it would read one, a value
        that compares as no data does and whose repr holds the whole object.
        It stays the missing attribute wherever it sits, and the message adds
        how the method is called."""
        name = call.split("(")[0].removeprefix(".")
        with pytest.raises(TemplatesError) as excinfo:
            walk(template, self._context())
        assert str(excinfo.value) == (
            f"Attribute error in expression '{{{{ {expression} }}}}': Attribute '{name}' does not exist in expression '{expression}'; "
            f"the object has no key '{name}'; to call its method, write {call}"
        )

    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            pytest.param("{{ user.get('nick', 'anon') }}", "anon", id="called"),
            pytest.param("{{ user.get('address').get('city') }}", "Oslo", id="chained-calls"),
            pytest.param("{{ [r.get('id') for r in user.roles] }}", [1, 2], id="called-in-a-comprehension"),
            pytest.param("{{ max(scores, key=scores.get) }}", "b", id="key-of-a-built-in"),
            pytest.param("{{ sorted(scores, key=scores.get) }}", ["c", "a", "b"], id="sorted-by-value"),
        ],
    )
    def test_method_called_or_handed_as_a_key(self, template, expected):
        """What a method is for: a call, or a ``key=``, which calls it."""
        context = self._context() | {"scores": convert_dict_to_namespace({"a": 2, "b": 5, "c": 1})}
        assert walk(template, context) == expected

    def test_method_of_a_saved_object_is_the_dicts(self):
        """A saved object is a dict, whose attribute simpleeval reads before
        its key, as it always has: the docs point to the key forms for a key
        named like a method."""
        saved = {"items": 3}
        assert walk("{{ saved.items }}", {"saved": saved}) == saved.items
        assert walk("{{ saved['items'] }}", {"saved": saved}) == 3

    def test_method_of_another_object_is_left_alone(self):
        """A fixture's object may hand out a method on purpose: only a `vars`
        object's are refused."""

        class Helper(SimpleNamespace):
            def ping(self) -> str:
                return "pong"

        helper = Helper()
        assert walk("{{ helper.ping }}", {"helper": helper}) == helper.ping

    def test_walk_keeps_the_type_at_every_depth(self):
        """Rendered, a `vars` object keeps its key access: `_walk` rebuilt it
        as a plain SimpleNamespace."""
        value = convert_dict_to_namespace({"id": "{{ n }}", "inner": {"k": "{{ n }}"}, "list": [{"k": "{{ n }}"}]})
        result = walk(value, {"n": 1})
        assert (type(result), type(result.inner), type(result.list[0])) == (VarsNamespace, VarsNamespace, VarsNamespace)
        assert (result["id"], result["inner"]["k"], result["list"][0]["k"]) == (1, 1, 1)
        assert list(result) == ["id", "inner", "list"]

    def test_object_nested_hundreds_deep_interpolates(self):
        """Text reads the object's repr, which spent three frames a level
        while it recursed, and failed the stage at a depth the loader
        accepts."""
        deep = convert_dict_to_namespace({"k": nested("x", LOADABLE_BUT_DEEP)})
        assert walk("v={{ deep }}", {"deep": deep}) == f"v={deep!r}"
        assert walk("v={{ deep }}", {"deep": deep}).startswith("v=namespace(k=[namespace(k=[")

    def test_dict_copy_of_an_object_with_a_keys_key(self):
        """`dict()` and `**` call `.keys()`, which such a key's data shadows;
        a comprehension over the keys, or a JSON round trip, copies it."""
        context = self._context()
        with pytest.raises(TemplatesError, match=r"^TypeError in expression '\{\{ dict\(order\) \}\}': 'str' object is not callable$"):
            walk("{{ dict(order) }}", context)
        assert walk("{{ {k: order[k] for k in order} }}", context) == {"items": [1, 2], "keys": "k", "_id": 7}
        assert walk("{{ json_loads(json_dumps(order)) }}", context) == {"items": [1, 2], "keys": "k", "_id": 7}

    def test_walk_keeps_a_plain_namespace_plain(self):
        result = walk(SimpleNamespace(a="{{ n }}"), {"n": 1})
        assert type(result) is SimpleNamespace
        assert result == SimpleNamespace(a=1)


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
