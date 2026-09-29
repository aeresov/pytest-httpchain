"""Scenario scope resolution: which names are visible to which phase.

The single encoding of the visibility rules, in three parts: name extraction
from a scenario (or its raw JSON), the static per-stage `StageScopes` used by
``validation`` and ``dataflow``, and the runtime ``ChainMap`` builders used by
``carrier``. Each context builder names the static phase it realizes, so a
change to either view is visibly a change to both.

The rules, in resolution order within a stage:

========================  ====================================================
Phase                     In scope
========================  ====================================================
``always_run``            fixtures, parametrize parameters, scenario
                          substitutions, earlier stages' saves
stage ``substitutions``   same as ``always_run``, plus PRIOR steps' names
                          (steps resolve strictly in order)
``skip_if``               the above plus this stage's substitutions
``parallel`` config       same as ``skip_if``
``retry`` config          same as ``skip_if``
request (per iteration)   the above plus ``foreach`` parameters
response (per iteration)  the above plus the ``response`` metadata namespace,
                          plus PRIOR steps' saves (steps resolve in order)
========================  ====================================================

A stage's ``parallel.stats_as`` is saved with its response steps' saves, once
every iteration has ended: the stages after it see it, none of its own phases.

Stage ``parametrize`` *values* are the exception: they resolve at collection
time against scenario substitutions only, which is why `StageScopes` exposes
``scenario_substitutions`` separately.

An earlier stage's saves are in scope because it passed: one that failed
aborted the chain, so only an ``always_run`` stage runs without them. One
that skipped (``skip_if``) leaves the chain healthy, so every later stage runs
without them; `StageScopes.skippable_saves` holds the names only such stages
save, and `StageScopes.when_skipped` the scope without them.

The template built-ins (``now()``, ``len()``, ``true``, ...) are in scope in
every phase, beneath the user's names: a user name shadows a built-in of the
same name, except that a call to ``get()`` or ``exists()`` always reaches the
built-in (see `extract_template_variables` and `extract_builtin_stand_ins`).
"""

import ast
from collections import ChainMap
from collections.abc import Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

from pytest_httpchain.models import (
    CombinationsParameter,
    FunctionsSubstitution,
    IndividualParameter,
    JMESPathSave,
    ParallelConfig,
    ParallelForeachConfig,
    Parameters,
    RegexSave,
    ResponseStep,
    SaveStep,
    Scenario,
    Stage,
    Substitutions,
    SubstitutionsSave,
    VarsSubstitution,
    normalize_list_input,
)
from pytest_httpchain.templates import CALL_ONLY_BUILTINS, CONTEXT_HELPERS, TEMPLATE_BUILTINS, TemplatesError, find_templates, parse_expression, template_form

# The name under which response metadata is injected into response-step contexts.
RESPONSE_META_NAME = "response"

# Fields resolved once per scenario, against only the scenario substitutions.
SCENARIO_TEMPLATE_FIELDS = ("substitutions", "auth", "ssl", "client")

# --------------------------------------------------------------------------- #
# Name extraction: which names a scenario fragment defines or references.
# --------------------------------------------------------------------------- #


class _TemplateNames(NamedTuple):
    """The free identifiers of some template text, by how each is used. A read
    name is in each set of the ways it is used (see
    `_extract_names_from_expr`)."""

    read: set[str]
    """Names used as a value anywhere but as a call's function."""
    called: set[str]
    """Names called (``now()``)."""
    loose: set[str]
    """The read names used where no function may take one: ``{{ now }}``,
    ``str(now)``, ``dict(at=now)``, ``', '.join(now)``."""
    keys: set[str]
    """The read names handed as the ``key=`` of a built-in or a method
    (``sorted(rows, key=len)``, ``rows.sort(key=len)``), which takes a
    function."""
    to_functions: set[str]
    """The read names handed to a user's function (``sign(now)``,
    ``sign(clock=now)``), which may take a function."""
    to_methods: set[tuple[str, str]]
    """``(receiver, name)`` for a read name handed to a method, not as its
    ``key=``, of an object reached from the name ``receiver``
    (``helper.ids(uuid4)``, ``helper().ids(uuid4)``: ``("helper",
    "uuid4")``). It may take a function where ``receiver`` may be the user's
    object (`DefinedNames.callables`); a method of data (a save, a variable)
    takes none (`_used_as_values`)."""

    @classmethod
    def empty(cls) -> "_TemplateNames":
        return cls(set(), set(), set(), set(), set(), set())

    def update(self, other: "_TemplateNames") -> None:
        """Add ``other``'s names, each to its set."""
        self.read.update(other.read)
        self.called.update(other.called)
        self.loose.update(other.loose)
        self.keys.update(other.keys)
        self.to_functions.update(other.to_functions)
        self.to_methods.update(other.to_methods)


def _receiver(node: ast.expr) -> str | None:
    """The name a method's object is reached from (``helper`` for
    ``helper.api.ids``, ``helper().ids``, ``helper['x'].ids``), or None for an
    object written in place (``', '.join``)."""
    while True:
        match node:
            case ast.Attribute(value=inner) | ast.Subscript(value=inner) | ast.Call(func=inner):
                node = inner
            case ast.Name(id=name):
                return name
            case _:
                return None


def _extract_names_from_expr(expr: str) -> _TemplateNames:
    """Free identifiers a Python expression reads, and those it calls.

    A name is called where it is the function of a call (``now()``) and read
    anywhere else, ``sorted(rows, key=len)``'s ``len`` included; one used both
    ways is in both sets. Comprehension targets are local bindings, not
    context references.

    Text that is no expression the engine evaluates (`parse_expression`: a
    syntax error, ``a; b``, an assignment, a lambda) holds no name at all. It
    fails wherever it renders, which is its one finding (HTTPCHAIN037/038,
    `extract_invalid_expressions`); read as a regex's identifiers, it had
    ``True``, keywords and attribute names reported undefined besides.

    A read may be handed to a function that takes a function: the ``key=`` of
    a built-in or a method (``sorted``, ``min``, ``max``, ``list.sort``), any
    argument of a user's function (``sign(now)``, ``sign(clock=now)``), or any
    argument of a method of an object reached from a name, which may be the
    user's (``helper.ids(uuid4)``). Any other argument of a built-in, or of a
    method of an object written in place (``str(now)``, ``dict(at=now)``,
    ``', '.join(now)``), is loose: none of those takes a function, so each
    would work on its repr.
    """
    names = _TemplateNames.empty()
    try:
        tree = parse_expression(expr)
    except TemplatesError:
        return names

    bound: set[str] = set()
    callees: set[int] = set()
    # What each argument that may be a function is handed to, by node.
    keys: set[int] = set()
    to_functions: set[int] = set()
    to_methods: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.comprehension):
            bound |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.Call):
            key = [keyword.value for keyword in node.keywords if keyword.arg == "key"]
            others = [*node.args, *(keyword.value for keyword in node.keywords if keyword.arg != "key")]
            match node.func:
                case ast.Name(id=name) if name not in TEMPLATE_BUILTINS:
                    to_functions.update(id(argument) for argument in (*key, *others))
                case ast.Name():
                    keys.update(id(argument) for argument in key)
                case ast.Attribute(value=value):
                    keys.update(id(argument) for argument in key)
                    if (receiver := _receiver(value)) is not None:
                        to_methods.update(dict.fromkeys(map(id, others), receiver))
            if isinstance(node.func, ast.Name):
                callees.add(id(node.func))

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in bound:
            if id(node) in callees:
                names.called.add(node.id)
                continue
            names.read.add(node.id)
            if id(node) in keys:
                names.keys.add(node.id)
            elif id(node) in to_functions:
                names.to_functions.add(node.id)
            elif (receiver := to_methods.get(id(node))) is not None and receiver not in bound:
                names.to_methods.add((receiver, node.id))
            else:
                names.loose.add(node.id)
    return names


def _template_expressions(obj: Any) -> Iterator[str]:
    """The text of every ``{{ expr }}`` in a structure, stripped as the engine
    strips it, in document order.

    Iterative on purpose: a recursive walk spent two stack frames per level of
    nesting, so a value a few hundred levels deep, which loads fine, crashed
    validation with a RecursionError.
    """
    pending = [obj]
    while pending:
        match pending.pop():
            case str() as text:
                for match in find_templates(text):
                    yield match.group("expr").strip()
            case dict() as mapping:
                pending.extend(reversed(mapping.values()))
            case list() as items:
                pending.extend(reversed(items))


def _template_names(obj: Any) -> _TemplateNames:
    """`_extract_names_from_expr` over every ``{{ expr }}`` in a structure."""
    names = _TemplateNames.empty()
    for expr in _template_expressions(obj):
        names.update(_extract_names_from_expr(expr))
    return names


def extract_invalid_expressions(obj: Any) -> dict[str, str]:
    """The templates in ``obj`` that hold no expression the engine evaluates
    (`parse_expression`), each once, in document order, written as the
    runtime's message writes them (``{{ x = 1 }}``, `template_form`), with the
    reason it gives: a syntax error, ``a; b``, an assignment, a kind of
    expression the engine does not evaluate. One fails wherever it renders,
    and names nothing (`_extract_names_from_expr`)."""
    invalid: dict[str, str] = {}
    for expr in _template_expressions(obj):
        try:
            parse_expression(expr)
        except TemplatesError as e:
            invalid.setdefault(template_form(expr), str(e))
    return invalid


@dataclass(frozen=True)
class DefinedNames:
    """What a scenario defines anywhere, which a built-in's name in a template
    is looked up in (`extract_template_variables`,
    `extract_builtin_stand_ins`); from `defined_names`."""

    names: frozenset[str]
    """Every name: substitutions, parameters, saves and fixtures, scenario-level
    and per stage."""
    callables: frozenset[str]
    """The names that may hold a function, which a call reaches before a
    built-in: fixtures (a factory fixture returns one) and function
    substitutions."""


def _used_as_values(names: _TemplateNames, defined: DefinedNames) -> set[str]:
    """The read names used where no function may take one: loose, or handed
    to a method of an object reached from a name the scenario defines as data,
    or does not define at all. Only a fixture or function substitution
    (`DefinedNames.callables`) may be an object whose methods take functions:
    a save or a variable is JSON data, and none of its methods does."""
    return names.loose | {name for receiver, name in names.to_methods if receiver not in defined.callables}


def extract_template_variables(obj: Any, *, defined: DefinedNames) -> set[str]:
    """Variable names referenced by every ``{{ expr }}`` in a structure: the
    names a template reads, and those it calls that are no built-in's.

    A built-in's name (`TEMPLATE_BUILTINS`) read (``{{ timestamp }}``) counts
    where the scenario defines that name too: the runtime looks a read up among
    the user's names first, so in a scenario that saves ``timestamp`` it means
    the save, and is checked for scope and order, and consumes the save, like
    any other name. Where the save has not landed yet the read gets the
    built-in function instead, which is what gets reported.

    A call under a built-in's name (``timestamp()``) is never one of these: a
    saved value is no function to consume, and out of the scope of the user's
    function of that name the call runs the built-in. Where the scenario
    defines such a function, `extract_builtin_stand_ins` has the call.
    """
    names = _template_names(obj)
    read = {name for name in names.read if name not in TEMPLATE_BUILTINS or name in defined.names}
    return read | (names.called - TEMPLATE_BUILTINS)


def extract_builtin_stand_ins(obj: Any, *, defined: DefinedNames) -> set[str]:
    """The built-ins' names the templates in ``obj`` use only as functions,
    where the scenario defines that name too: out of the scope of the user's
    definition, the built-in stands in for it, and nothing fails.

    A name counts where it is called (``timestamp()``) and the scenario defines
    it as a fixture or function substitution (``defined.callables``), the names
    a call reaches before the built-in; ``get()`` and ``exists()``
    (`CONTEXT_HELPERS`) never, since a call always reaches those. It counts too
    where it is handed to a function that takes one (`_extract_names_from_expr`),
    which that read, a reference too (`extract_template_variables`), finds in
    scope: as a ``key=`` (``sorted(rows, key=len)``) where the scenario
    defines it at all, and to the user's function or object (``sign(now)``,
    ``helper.ids(uuid4)``) where, as for a call, the scenario's definition may
    hold a function. Out of scope the user's function gets the built-in
    function: a stand-in for a function, never for a save's or a variable's
    value, which is then missing, the reference checks' business, as is a
    name the templates also use as a value (``{{ now }}``, ``str(now)``). The
    validator reports a stand-in out of scope as such (HTTPCHAIN036), never as
    an undefined name.
    """
    names = _template_names(obj)
    to_users = names.to_functions | {name for receiver, name in names.to_methods if receiver in defined.callables}
    called = (names.called & defined.callables) - CONTEXT_HELPERS
    passed = (names.keys & defined.names) | (to_users & defined.callables)
    return ((called | passed) & TEMPLATE_BUILTINS) - _used_as_values(names, defined) - (to_users - defined.callables)


def extract_uncalled_builtins(obj: Any, *, defined: DefinedNames) -> set[str]:
    """The call-only built-ins (`CALL_ONLY_BUILTINS`: ``now``, ``sha256``,
    ``env``, ...) a template in ``obj`` uses as a value without calling them,
    which renders the function itself (``{{ now }}`` for ``{{ now() }}``); the
    runtime refuses it.

    Handed to a function that may take one (``sorted(rows, key=sha256)``, a
    user's ``sign(now)``, a method of a fixture's ``helper.ids(uuid4)``) a
    built-in is used as a function, and a name the scenario defines is the
    user's (and a reference, `extract_template_variables`), so neither is
    reported.
    """
    return (_used_as_values(_template_names(obj), defined) & CALL_ONLY_BUILTINS) - defined.names


def substitution_names(substitutions: Substitutions) -> set[str]:
    """Names introduced by ``vars``/``functions`` substitution entries."""
    names: set[str] = set()
    for sub in substitutions:
        match sub:
            case VarsSubstitution():
                names.update(sub.vars.keys())
            case FunctionsSubstitution():
                names.update(sub.functions.keys())
    return names


def saved_in_step(response_step: ResponseStep) -> set[str]:
    """Names one response step saves. A ``user_functions`` save returns
    arbitrary keys, so it contributes none; verify steps save nothing."""
    if not isinstance(response_step, SaveStep):
        return set()
    match response_step.save:
        case JMESPathSave(jmespath=jmespath):
            return set(jmespath.keys())
        case RegexSave(regex=regex):
            return set(regex.keys())
        case SubstitutionsSave(substitutions=substitutions):
            return substitution_names(substitutions)
        case _:
            return set()


def saved_in_response(stage: Stage) -> set[str]:
    """Names one stage's response steps save."""
    saved: set[str] = set()
    for response_step in stage.response:
        saved |= saved_in_step(response_step)
    return saved


def stats_name(stage: Stage) -> str | None:
    """The name a parallel stage saves its stats under (``parallel.stats_as``), if any."""
    return stage.parallel.stats_as if stage.parallel is not None else None


def saved_in_stage(stage: Stage) -> set[str]:
    """Names one stage saves: its response steps', and its stats' name
    (`stats_name`), which it saves with them. The stats exist only once every
    iteration has ended, so none of the stage's own templates can read them:
    `response_step_templates` goes by `saved_in_response`."""
    saved = saved_in_response(stage)
    if (name := stats_name(stage)) is not None:
        saved.add(name)
    return saved


def extract_saved_variables(scenario: Scenario) -> set[str]:
    """Names saved anywhere in the scenario."""
    saved_vars: set[str] = set()
    for stage in scenario.stages:
        saved_vars |= saved_in_stage(stage)
    return saved_vars


def parameter_names(params: Parameters | None) -> set[str]:
    """Names injected by parametrize/foreach entries. A template-string form
    defers its values to runtime and contributes no static names."""
    names: set[str] = set()
    for param in params or []:
        match param:
            case IndividualParameter(individual=individual):
                names.update(individual)
            case CombinationsParameter(combinations=combinations):
                if not isinstance(combinations, str):
                    for combo in combinations:
                        names.update(combo)
    return names


def foreach_parameter_names(parallel: ParallelConfig | None) -> set[str]:
    """Names injected per iteration by a ``parallel.foreach`` config."""
    match parallel:
        case ParallelForeachConfig(foreach=foreach):
            return parameter_names(foreach)
        case _:
            return set()


def _function_substitution_names(substitutions: Substitutions) -> set[str]:
    return {name for sub in substitutions if isinstance(sub, FunctionsSubstitution) for name in sub.functions}


def defined_names(scenario: Scenario) -> DefinedNames:
    """Every name the scenario introduces anywhere, and those that may hold a
    function. A built-in's name in a template is the user's where it is one of
    these (see `extract_template_variables` and `extract_builtin_stand_ins`)."""
    fixtures = {*scenario.fixtures, *(name for stage in scenario.stages for name in stage.fixtures)}
    functions = _function_substitution_names(scenario.substitutions)
    for stage in scenario.stages:
        functions |= _function_substitution_names(stage.substitutions)
        for step in stage.response:
            if isinstance(step, SaveStep) and isinstance(step.save, SubstitutionsSave):
                functions |= _function_substitution_names(step.save.substitutions)
    return DefinedNames(
        names=frozenset(extract_defined_variables(scenario) | extract_saved_variables(scenario) | fixtures),
        callables=frozenset(fixtures | functions),
    )


def extract_defined_variables(scenario: Scenario) -> set[str]:
    """Every name substitutions and parameters define, scenario-wide. The
    order-aware checks use `stage_scopes` instead."""
    defined_vars = substitution_names(scenario.substitutions)

    for stage in scenario.stages:
        defined_vars |= substitution_names(stage.substitutions)
        defined_vars |= parameter_names(stage.parametrize)
        defined_vars |= foreach_parameter_names(stage.parallel)

    return defined_vars


def raw_stages(test_data: dict[str, Any]) -> list[Any]:
    """Raw (pre-validation) stage bodies in declaration order, from either the
    list or the ``{name: stage}`` form."""
    raw = test_data.get("stages")
    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, list):
        return raw
    return []


def raw_list_entries(raw: Any) -> list[Any]:
    """Raw entries of a list-or-mapping field in resolution order, so entry K
    pairs with the validated model's K.

    Shared by every raw-JSON reader of these fields — re-deriving the shape with
    a bare ``isinstance(..., list)`` silently drops the whole mapping form. The
    ``isinstance`` guard is what makes this different from the model's own use:
    the validator passes unrecognized input through for pydantic to reject,
    while a raw reader has no validator behind it and must degrade to ``[]``.
    """
    entries = normalize_list_input(raw)
    return list(entries) if isinstance(entries, list) else []


def _raw_substitution_entry_names(entry: Any) -> set[str]:
    if not isinstance(entry, dict):
        return set()
    names: set[str] = set()
    for key in ("vars", "functions"):
        value = entry.get(key)
        if isinstance(value, dict):
            names.update(value.keys())
    return names


def _raw_substitution_entry_templates(entry: Any) -> Any:
    """What the runtime renders at seed time: ``vars`` values and ``functions``
    import names; ``functions`` kwargs are passed to ``wrap_function`` raw."""
    if not isinstance(entry, dict):
        return None
    rendered: list[Any] = [entry.get("vars")]
    functions = entry.get("functions")
    if isinstance(functions, dict):
        for func_def in functions.values():
            if isinstance(func_def, str):
                rendered.append(func_def)
            elif isinstance(func_def, dict):
                rendered.append(func_def.get("name"))
    return rendered


def substitution_step_templates(raw_substitutions: Any) -> Iterator[tuple[Any, frozenset[str]]]:
    """Walk raw substitution steps in resolution order, yielding
    ``(what the runtime renders of the step, names defined by PRIOR steps)``.

    Steps resolve strictly in order, so the prior-name set is both the scope
    addition (validation) and the shadow addition (dataflow) for that step. The
    rendered part is the ``vars`` values and ``functions`` import names, not a
    function's kwargs, which reach it raw: every reader of a step's templates
    (references, `extract_builtin_stand_ins`, `extract_uncalled_builtins`)
    reads the same text.
    """
    prior_names: frozenset[str] = frozenset()
    for entry in raw_list_entries(raw_substitutions):
        yield _raw_substitution_entry_templates(entry), prior_names
        prior_names |= frozenset(_raw_substitution_entry_names(entry))


def _raw_save_substitutions(step_raw: Any) -> Any:
    """The raw ``substitutions`` list of a substitutions-save step."""
    save = step_raw.get("save") if isinstance(step_raw, dict) else None
    return save.get("substitutions") if isinstance(save, dict) else None


def response_step_templates(stage: Stage, raw_response: Any) -> Iterator[tuple[Any, frozenset[str]]]:
    """Walk response steps in resolution order, yielding ``(what the runtime
    renders of the step, names saved by STRICTLY EARLIER steps of this stage)``.

    The response-phase sibling of `substitution_step_templates`: a save lands
    only once its own step has run, so a step reading a name a LATER step saves
    is a forward reference, not a hit. The raw entries carry the template text
    and the validated steps say what each saves, so the two are walked in
    lockstep (`raw_list_entries` keeps the name-keyed mapping form paired
    correctly).

    A substitutions-save step yields one tuple per ENTRY rather than one for the
    step, because `response_steps.process_save` renders those entries itself,
    strictly in order — so an entry does see its own step's prior entries, and
    nothing else in that step (not even ``description``) is rendered at all.
    """
    prior_saves: frozenset[str] = frozenset()
    # A save whose names cannot be enumerated (``user_functions`` returns
    # arbitrary keys) ends the order-aware claim: from there on a name this stage
    # saves anywhere may already exist, so the whole-stage set is restored rather
    # than report a forward reference the runtime would satisfy.
    opaque_save_seen = False
    all_stage_saves = frozenset(saved_in_response(stage))
    raw_steps = raw_list_entries(raw_response)
    for k, step in enumerate(stage.response):
        step_raw = raw_steps[k] if k < len(raw_steps) else None
        visible = prior_saves | all_stage_saves if opaque_save_seen else prior_saves
        if isinstance(step, SaveStep) and isinstance(step.save, SubstitutionsSave):
            for entry_templates, prior_entry_names in substitution_step_templates(_raw_save_substitutions(step_raw)):
                yield entry_templates, visible | prior_entry_names
        else:
            yield step_raw, visible
        prior_saves |= frozenset(saved_in_step(step))
        if isinstance(step, SaveStep) and not isinstance(step.save, JMESPathSave | RegexSave | SubstitutionsSave):
            opaque_save_seen = True


# --------------------------------------------------------------------------- #
# Static scope model: per-stage, per-phase name availability.
# --------------------------------------------------------------------------- #


class NameUnion(AbstractSet[str]):
    """The union of name sets, kept as its parts: a name is in it when it is
    in one of them, asked of each in turn, and ``|`` adds a part. Nothing is
    copied, where a frozenset union copies every name of every part: a
    scenario an import wrote from a long HAR file has thousands of stages and
    of scenario ``vars``, and a union of those per stage, per phase, made
    validating it quadratic. Any other set operation gives a frozenset."""

    __slots__ = ("_parts",)

    def __init__(self, *parts: AbstractSet[str]) -> None:
        self._parts = parts

    def __contains__(self, name: object) -> bool:
        return any(name in part for part in self._parts)

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys(name for part in self._parts for name in part))

    def __len__(self) -> int:
        return len(frozenset().union(*self._parts))

    def __or__(self, other: AbstractSet[str]) -> "NameUnion":
        return NameUnion(*self._parts, other)

    @classmethod
    def _from_iterable(cls, iterable: Iterable[str]) -> frozenset[str]:
        return frozenset(iterable)


class Phases(NamedTuple):
    """Every phase's names of one `StageScopes`, as its properties give them."""

    always_run: NameUnion
    pre_iteration: NameUnion
    request: NameUnion
    response: NameUnion


@dataclass(frozen=True, slots=True)
class StageScopes:
    """Statically-known names visible to one stage, per resolution phase.

    Ingredients are stored separately so consumers can tell *why* a name is
    visible; the phase properties union them in the order the runtime layers its
    contexts, and each names its runtime twin.

    ``saves`` is the exception: no phase unions it, because a stage's own saves
    become visible step by step, which `response_step_templates` tracks, and
    its ``parallel.stats_as`` only to the stages after it. It remains a
    reporting input (`cli show`, `dataflow.StageFlow`, the first-save index) —
    reading it as in-stage visibility is what produced the bug that split it out.
    """

    scenario_substitutions: frozenset[str]
    scenario_fixtures: frozenset[str]
    stage_fixtures: frozenset[str]
    parametrize_params: frozenset[str]
    stage_substitutions: frozenset[str]
    foreach_params: frozenset[str]
    saves: frozenset[str]
    earlier_saves: frozenset[str]
    skippable_saves: frozenset[str]
    """The ``earlier_saves`` that only stages with a ``skip_if`` save: none
    is there when those stages skipped."""

    @property
    def when_skipped(self) -> "StageScopes":
        """This scope as it is when every earlier stage with a ``skip_if``
        skipped: without `skippable_saves`. A name in a phase's scope here but
        not in the same phase of ``when_skipped`` is defined only when such a
        stage ran."""
        if not self.skippable_saves:
            return self
        return replace(self, earlier_saves=self.earlier_saves - self.skippable_saves, skippable_saves=frozenset())

    @property
    def always_run(self) -> frozenset[str]:
        """``always_run`` and the stage's own substitutions as they resolve.
        Twin: `stage_start_context`."""
        return self.scenario_substitutions | self.earlier_saves | self.scenario_fixtures | self.stage_fixtures | self.parametrize_params

    @property
    def pre_iteration(self) -> frozenset[str]:
        """``skip_if``, the ``parallel`` config and the ``retry`` config. Twin: `with_stage_substitutions`."""
        return self.always_run | self.stage_substitutions

    @property
    def request(self) -> frozenset[str]:
        """Request templates, per iteration. Twin: `iteration_context`."""
        return self.pre_iteration | self.foreach_params

    @property
    def response(self) -> frozenset[str]:
        """Response steps as they start, before any of this stage's own saves
        have landed: the request scope plus the ``response`` namespace. Steps
        resolve strictly in order, so each additionally sees PRIOR steps' saves
        (see `response_step_templates`, whose runtime twin is `with_saves`).
        Twin: `response_step_context`."""
        return self.request | frozenset({RESPONSE_META_NAME})

    def phases(self) -> Phases:
        """Every phase's names as the properties give them, as `NameUnion`s
        of the ingredients, not copies: for a check that asks for all of them
        of every stage (see `NameUnion` for why)."""
        always_run = NameUnion(self.scenario_substitutions, self.earlier_saves, self.scenario_fixtures, self.stage_fixtures, self.parametrize_params)
        pre_iteration = always_run | self.stage_substitutions
        request = pre_iteration | self.foreach_params
        return Phases(always_run, pre_iteration, request, request | frozenset({RESPONSE_META_NAME}))

    # Shadow sets: names layered ABOVE the global context in each phase, behind
    # which a same-named earlier save is unreadable.

    @property
    def always_run_shadows(self) -> frozenset[str]:
        """Also the base shadows of each substitution step, to which prior steps'
        names add cumulatively (see `substitution_step_templates`)."""
        return self.scenario_fixtures | self.stage_fixtures | self.parametrize_params

    @property
    def pre_iteration_shadows(self) -> frozenset[str]:
        return self.always_run_shadows | self.stage_substitutions

    @property
    def request_shadows(self) -> frozenset[str]:
        return self.pre_iteration_shadows | self.foreach_params


def stage_scopes(scenario: Scenario) -> list[StageScopes]:
    """Per-stage scopes in execution order. ``earlier_saves`` accumulates stage
    by stage, mirroring the runtime commit of saves after a stage passes, and
    ``skippable_saves`` holds those of them no stage without a ``skip_if``
    saves (``skip_if: false`` never skips)."""
    scenario_substitutions = frozenset(substitution_names(scenario.substitutions))
    scenario_fixtures = frozenset(scenario.fixtures)

    scopes: list[StageScopes] = []
    earlier_saves: frozenset[str] = frozenset()
    unskippable_saves: frozenset[str] = frozenset()
    for stage in scenario.stages:
        saves = frozenset(saved_in_stage(stage))
        scopes.append(
            StageScopes(
                scenario_substitutions=scenario_substitutions,
                scenario_fixtures=scenario_fixtures,
                stage_fixtures=frozenset(stage.fixtures),
                parametrize_params=frozenset(parameter_names(stage.parametrize)),
                stage_substitutions=frozenset(substitution_names(stage.substitutions)),
                foreach_params=frozenset(foreach_parameter_names(stage.parallel)),
                saves=saves,
                earlier_saves=earlier_saves,
                skippable_saves=earlier_saves - unskippable_saves,
            )
        )
        earlier_saves |= saves
        if stage.skip_if is False:
            unskippable_saves |= saves
    return scopes


# --------------------------------------------------------------------------- #
# Runtime context builders: the value-level twins of the phases above.
# --------------------------------------------------------------------------- #


def base_global_context(scenario_substitutions: Mapping[str, Any]) -> ChainMap[str, Any]:
    """The pristine global context; saves accumulate on top via `with_saves`."""
    return ChainMap(dict(scenario_substitutions))


def stage_start_context(global_context: ChainMap[str, Any], stage_fixtures: Mapping[str, Any]) -> ChainMap[str, Any]:
    """Fixtures (and parametrize parameters, which pytest injects through the
    same signature) over the global context."""
    return ChainMap(dict(stage_fixtures), global_context)


def with_stage_substitutions(stage_start: ChainMap[str, Any], stage_substitutions: Mapping[str, Any]) -> ChainMap[str, Any]:
    """The stage-local context: what ``skip_if``, the ``parallel`` config and
    the ``retry`` config see, and the base for every iteration (each attempt
    of one starting from it afresh)."""
    return stage_start.new_child(dict(stage_substitutions))


def iteration_context(local_context: ChainMap[str, Any], iteration_params: Mapping[str, Any]) -> ChainMap[str, Any]:
    """Iteration parameters over the stage-local context."""
    return local_context.new_child(dict(iteration_params))


def response_step_context(iteration_ctx: ChainMap[str, Any], response_meta: Any) -> ChainMap[str, Any]:
    """The ``response`` namespace over the iteration context, layered last so a
    user variable cannot shadow it inside response steps."""
    return iteration_ctx.new_child({RESPONSE_META_NAME: response_meta})


def with_saves(context: ChainMap[str, Any], saves: Mapping[str, Any]) -> ChainMap[str, Any]:
    """Layer saved values over a context; later saves shadow earlier ones. Used
    both within a response and to commit a passed stage's saves."""
    return context.new_child(dict(saves))
