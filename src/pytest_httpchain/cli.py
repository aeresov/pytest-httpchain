"""The ``pytest-httpchain`` command line: ``validate``, ``schema``, ``resolve``,
``show`` and ``graph`` over scenario files, outside a pytest run, and
``import``, which writes one from recorded traffic."""

import importlib.metadata
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn, override

import typer
import typer.core
from pydantic import ValidationError

from pytest_httpchain.constants import check_suffix
from pytest_httpchain.dataflow import DataFlow, analyze_dataflow
from pytest_httpchain.importers import ImportResult, ImportSourceError, build_scenario, parse_curl, parse_curl_words, read_har, scenario_text, validate_text
from pytest_httpchain.jsonref import ReferenceResolverError
from pytest_httpchain.models import Scenario
from pytest_httpchain.schema import build_schema
from pytest_httpchain.validation import Diagnostic, DiagnosticCode, DiscoveryError, ValidateResult, configured_depth, load_scenario, load_scenario_json, validate_paths


def _hide_flag_value_types(command: typer.core.TyperCommand | typer.core.TyperGroup, ctx: typer.Context) -> None:
    """Have the command's help list its flags with no value type beside them.

    typer 0.29.0 and 0.29.1 (the newest when this was written) list a value
    type beside every option, a flag included: ``--force <bool>``, even
    ``--help <bool>``. A flag takes no value (``--force true`` reads ``true``
    as the next argument), and typer up to 0.28 listed none. What 0.29 lists
    is an option's metavar when it has one, so each flag without one is given
    an empty one; typer up to 0.28 shows a flag's metavar nowhere. The flags
    are the command's own and the ones typer adds (--help, the completion
    options), which no declaration here reaches. Only help reads a flag's
    metavar (an error names an option by its names), so a command whose help
    was drawn parses as it did before.

    Delete this, `_Command.get_help`, and `_Group` with `_Typer.__init__`,
    which makes it every app's group, once no typer the ``typer>=``
    requirement admits lists a type for a flag: the CLI's tests of every
    command's help (``test_help_lists_a_value_type_for_every_option_but_a_flag``,
    ``test_help_without_a_command_lists_no_value_type_for_a_flag``) go red on
    one that does. `_Command` and `_Typer` stay while `_Command.parse_args`
    does.
    """
    for param in command.get_params(ctx):
        if getattr(param, "is_flag", False) and param.metavar is None:
            param.metavar = ""


# The classes every command and group of this CLI is made with, for the help
# they draw and the words a command refuses a value in. typer.Context is the
# context typer makes; the base names the click Context typer vendors
# privately.
class _Command(typer.core.TyperCommand):
    """A command with two workarounds of typer 0.29's, each with its own
    removal note: `get_help` lists no value type beside a flag
    (`_hide_flag_value_types`), and `parse_args` drops the "Value error, "
    pydantic puts before a refused value. Delete this class and `_Typer`,
    making the apps plain ``typer.Typer`` again (`_OwnOptionsFirst` then
    derives from typer.core.TyperCommand), only once both are gone."""

    @override
    def get_help(self, ctx: typer.Context) -> str:  # ty: ignore[incompatible-method-override]
        _hide_flag_value_types(self, ctx)
        return super().get_help(ctx)

    @override
    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:  # ty: ignore[incompatible-method-override]
        """A value an option refuses is reported in the same words on every
        typer. typer 0.29 converts values through pydantic, which puts
        "Value error, " before what a check that raised ValueError said:
        ``--format xml`` was "Value error, 'xml' is not one of 'text',
        'json'.", where typer up to 0.28 says the check's words alone, as
        click does. The CLI's options that take a value are all on its
        commands.

        Delete this once no typer the ``typer>=`` requirement admits puts
        "Value error, " there, whatever it lists beside a flag:
        ``test_choice_options_refuse_any_other_value`` goes red on one that
        does."""
        try:
            return super().parse_args(ctx, args)
        except typer.BadParameter as e:
            e.message = e.message.removeprefix("Value error, ")
            raise


class _Group(typer.core.TyperGroup):
    """A group whose help lists no value type beside a flag: it goes with
    `_hide_flag_value_types` (see there)."""

    @override
    def get_help(self, ctx: typer.Context) -> str:  # ty: ignore[incompatible-method-override]
        _hide_flag_value_types(self, ctx)
        return super().get_help(ctx)


class _Typer(typer.Typer):
    """An app whose group and commands are made with the classes above unless
    a ``cls=`` says otherwise: typer has no setting for an app's default
    classes, and a command made with typer's own would list `<bool>` again on
    typer 0.29, and refuse a value in pydantic's words. Its `__init__` goes
    with `_Group`, and the class itself with `_Command` (see there)."""

    def __init__(self, *, cls: type[typer.core.TyperGroup] | None = _Group, **kwargs) -> None:
        super().__init__(cls=cls, **kwargs)

    @override
    def command(self, name: str | None = None, *, cls: type[typer.core.TyperCommand] | None = _Command, **kwargs):
        return super().command(name, cls=cls, **kwargs)


app = _Typer(no_args_is_help=True)


def _fail(message: str) -> NoReturn:
    """End the command with exit code 1, ``message`` its ``error:`` on stderr."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


class OutputFormat(StrEnum):
    text = "text"
    json = "json"


class GraphDirection(StrEnum):
    TD = "TD"
    LR = "LR"


def _depth_option(value: str) -> int:
    """The depth as Python's ``int()`` reads it, as pytest reads the
    ``httpchain_ref_parent_traversal_depth`` ini option and typer did up to
    0.28, with the CLI's own error. typer 0.29 converts an ``int`` through
    pydantic, which takes ``3.0`` and refuses ``３``; a parser is given the
    word as typed on every typer (never the default, None, which stands for
    the depth pytest's configuration sets), and the error it raises, not
    being a ValueError, reaches click unchanged.

    A depth below 0 is refused, as the ini option's is ("must be
    non-negative"): it counts parent directories, and with one the resolver
    refused every file reference, even one beside the scenario, as
    "exceeds maximum parent traversal depth of -1"."""
    try:
        depth = int(value)
    except ValueError:
        raise typer.BadParameter(f"{value!r} is not a valid integer.") from None
    if depth < 0:
        raise typer.BadParameter(f"{value!r} must be non-negative.")
    return depth


# What --help names the type after up to typer 0.28 (0.29 names the
# annotation): <int> on 0.27 and 0.28 as before, INT where 0.26 wrote INTEGER.
_depth_option.__name__ = "int"

# None, the default, is the depth pytest's configuration sets for the paths
# read (`configured_depth`): `validate_paths` reads it for validate's (a file
# of another project than the run's, for that file alone), `_configured_depth`
# for the scenario resolve, show and graph read.
RefParentTraversalDepth = Annotated[
    int | None,
    typer.Option(
        help="Maximum $ref parent directory traversal depth (default: the httpchain_ref_parent_traversal_depth set in the configuration file pytest would read, else 3).",
        parser=_depth_option,
    ),
]
RootPath = Annotated[Path | None, typer.Option("--root-path", help="Directory that constrains $ref resolution (default: the rootdir pytest would use).")]
OutputFormatOption = Annotated[OutputFormat, typer.Option("--format", help="Output format: human-readable text or machine-readable JSON.")]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(importlib.metadata.version("pytest-httpchain"))
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the pytest-httpchain version and exit.", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """pytest-httpchain command-line tools."""


def _suffix_option(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return check_suffix(value)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None


# The results that stand for no file checked, each the one diagnostic of a
# path given that names nothing to check, and how the summary counts them
# apart (singular, plural).
_NOTHING_CHECKED = {
    DiagnosticCode.FILE_NOT_FOUND: ("path not found", "paths not found"),
    DiagnosticCode.NOT_A_FILE: ("path that is not a file", "paths that are not files"),
    DiagnosticCode.NO_SCENARIO_FILES: ("directory without scenario files", "directories without scenario files"),
}


def _summary(results: list[tuple[Path, ValidateResult]]) -> str | None:
    """The closing line of a text report, when it checked more than one file:
    each file counted once, under errors if it has any, else under warnings if
    it has any. A path that names nothing to check (`_NOTHING_CHECKED`) is no
    file checked, and is counted apart."""
    files: list[ValidateResult] = []
    apart: Counter[DiagnosticCode] = Counter()
    for _, result in results:
        code = next((d.code for d in result.diagnostics if d.code in _NOTHING_CHECKED), None)
        if code is None:
            files.append(result)
        else:
            apart[code] += 1
    if len(files) < 2:
        return None
    with_errors = sum(not result.valid for result in files)
    with_warnings = sum(result.valid and bool(result.warnings) for result in files)
    line = f"{len(files)} files checked, {with_errors} with errors, {with_warnings} with warnings"
    for code, (one, many) in _NOTHING_CHECKED.items():
        if count := apart[code]:
            line += f", {count} {one if count == 1 else many}"
    return line


def _diagnostic_line(diagnostic: Diagnostic) -> str:
    """A diagnostic as `validate` and an import report it, on a line of its
    own: ``severity [code]: message (at location)``."""
    # Some diagnostics already name the location inside the message;
    # repeating it makes the suffix look untrustworthy everywhere else. The
    # field itself stays — it is the machine-readable form of what the
    # message spells out in prose, so a JSON consumer can route on it
    # without parsing English.
    at = f" (at {diagnostic.location})" if diagnostic.location and diagnostic.location not in diagnostic.message else ""
    return f"{diagnostic.severity} [{diagnostic.code}]: {diagnostic.message}{at}"


@app.command()
def validate(
    paths: Annotated[list[Path], typer.Argument(help="Scenario files to validate, or directories to search for them.")],
    ref_parent_traversal_depth: RefParentTraversalDepth = None,
    root_path: RootPath = None,
    output_format: OutputFormatOption = OutputFormat.text,
    deep: Annotated[bool, typer.Option("--deep", help="Run deep checks: resolve user-function imports/signatures and referenced files. Imports user modules.")] = False,
    syspath: Annotated[list[Path] | None, typer.Option("--syspath", help="Extra directories to add to sys.path for --deep import resolution (repeatable).")] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Treat warnings as failures for the exit code.")] = False,
    suffix: Annotated[
        str | None,
        typer.Option(
            "--suffix",
            help="Find scenario files in directories as test_<name>.<SUFFIX>.json and .jsonc "
            "(default: the httpchain_suffix pytest's configuration sets for these paths, else http).",
            callback=_suffix_option,
        ),
    ] = None,
) -> None:
    """Validate pytest-httpchain scenario files, given one by one or as the
    directories holding them.

    A directory is searched as pytest collects it. Reports errors and warnings
    (each with a stable HTTPCHAINxxx diagnostic code) per file and exits
    non-zero if any file is invalid (or, with --strict, has any warnings), or a
    directory holds no scenario file.
    """
    try:
        results = validate_paths(paths, suffix=suffix, ref_parent_traversal_depth=ref_parent_traversal_depth, root_path=root_path, deep=deep, syspaths=list(syspath or []))
    except DiscoveryError as e:
        _fail(str(e))

    def passed(result: ValidateResult) -> bool:
        return result.valid and not (strict and result.warnings)

    all_passed = all(passed(result) for _, result in results)

    if output_format is OutputFormat.json:
        # Top-level `valid` is the gate result and matches the exit code; each
        # file's own `valid` is pure validity.
        payload = {
            "valid": all_passed,
            "strict": strict,
            "files": [{"path": str(path), "result": result.model_dump()} for path, result in results],
        }
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        for path, result in results:
            if not result.valid:
                status = "INVALID"
            elif result.warnings:
                status = "FAILED (warnings)" if strict else "OK with warnings"
            else:
                status = "OK"
            typer.echo(f"{path}: {status}")
            for diagnostic in result.diagnostics:
                typer.echo(f"  {_diagnostic_line(diagnostic)}")
        if summary := _summary(results):
            typer.echo(summary)

    raise typer.Exit(0 if all_passed else 1)


@app.command()
def schema() -> None:
    """Emit the JSON Schema for scenario files (editor autocomplete/validation).

    Writes to stdout; redirect to a file (``pytest-httpchain schema > scenario.schema.json``).
    """
    typer.echo(json.dumps(build_schema(), indent=2, default=str))


def _configured_depth(depth: int | None, path: Path) -> int:
    """``--ref-parent-traversal-depth`` when given, else the depth pytest's
    configuration sets for a run on ``path`` (`configured_depth`): a value
    there pytest would refuse exits 1, as `validate` exits on it."""
    if depth is not None:
        return depth
    try:
        return configured_depth([path])
    except DiscoveryError as e:
        _fail(str(e))


# What loading a scenario raises for a file that cannot be loaded: the
# resolver's errors (a file it cannot read or parse is its `FileLoadError`),
# and an OSError finding the default root (`resolve_root_path`).
_LOAD_ERRORS = (ReferenceResolverError, OSError)


@app.command()
def resolve(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to resolve.")],
    ref_parent_traversal_depth: RefParentTraversalDepth = None,
    root_path: RootPath = None,
) -> None:
    """Resolve $ref/$include/$merge and print the merged scenario JSON to stdout.

    The output is strict JSON: the comments and trailing commas a scenario or
    an included file may hold are not in it.
    """
    depth = _configured_depth(ref_parent_traversal_depth, scenario)
    try:
        # The loader load_scenario uses, so the printed document is what
        # collection sees.
        data = load_scenario_json(scenario, root_path=root_path, ref_parent_traversal_depth=depth)
    except _LOAD_ERRORS as e:
        _fail(str(e))

    typer.echo(json.dumps(data, indent=2, default=str))


def _load_for_inspection(path: Path, depth: int | None, root_path: Path | None = None) -> tuple[Scenario, dict]:
    """Load + validate a scenario for show/graph, mapping failures to Exit(1);
    ``depth`` None is the configured depth (`_configured_depth`)."""
    depth = _configured_depth(depth, path)
    try:
        return load_scenario(path, root_path=root_path, ref_parent_traversal_depth=depth)
    except _LOAD_ERRORS as e:
        _fail(f"cannot load {path}: {e}")
    except ValidationError:
        _fail(f"{path} is not a valid scenario — run `pytest-httpchain validate {path}` for details")


def _render_show_text(path: Path, scenario: Scenario, flow: DataFlow) -> list[str]:
    # Listed nearest first: a name has several producers when the nearer ones
    # may skip (analyze_dataflow), each read only when the nearer skipped.
    producers_of: dict[tuple[int, str], list[int]] = {}
    for edge in flow.edges:
        for var_name in edge.vars:
            producers_of.setdefault((edge.consumer, var_name), []).append(edge.producer)

    all_fixtures = sorted({*flow.scenario_fixtures, *(f for s in flow.stages for f in s.fixtures)})
    lines: list[str] = [scenario.description or path.name]
    summary = f"{len(flow.stages)} stage(s)"
    if all_fixtures:
        summary += f" · fixtures: {', '.join(all_fixtures)}"
    if flow.scenario_vars:
        summary += f" · vars: {', '.join(flow.scenario_vars)}"
    lines.append(summary)
    lines.append("")

    for s in flow.stages:
        name = s.name or f"(stage {s.index + 1})"
        # A connection stage shows the actions it sets, in the order it takes them.
        target = f"CONNECTION {s.connection} {'·'.join(s.actions)}" if s.kind == "connection" else f"{s.method} {s.url}"
        lines.append(f"{s.index + 1} · {name}    {target}")
        if s.skip_if is not False:
            lines.append(f"    skip_if:  {s.skip_if.strip() if isinstance(s.skip_if, str) else 'true'}")
        if s.saves:
            lines.append(f"    saves:    {', '.join(s.saves)}")
        if s.consumes:
            parts: list[str] = []
            for var_name in s.consumes:
                # analyze_dataflow lists a consume only when an earlier stage
                # saved the name, so every one has a producing edge.
                sources = [f"#{p + 1} {flow.stages[p].name or f'stage {p + 1}'}" for p in sorted(producers_of[(s.index, var_name)], reverse=True)]
                parts.append(f"{var_name} (from {', else '.join(sources)})")
            lines.append(f"    consumes: {', '.join(parts)}")
        if s.marks:
            lines.append(f"    marks:    {', '.join(s.marks)}")
    return lines


@app.command()
def show(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to summarize.")],
    output_format: OutputFormatOption = OutputFormat.text,
    ref_parent_traversal_depth: RefParentTraversalDepth = None,
    root_path: RootPath = None,
) -> None:
    """Summarize a scenario's stages and variable data-flow."""
    sc, test_data = _load_for_inspection(scenario, ref_parent_traversal_depth, root_path)
    flow = analyze_dataflow(sc, test_data)

    if output_format is OutputFormat.json:
        payload = flow.model_dump()
        payload["description"] = sc.description or None
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        for line in _render_show_text(scenario, sc, flow):
            typer.echo(line)


def _mermaid_label(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def _to_mermaid(flow: DataFlow, direction: str = "TD") -> str:
    lines = [f"flowchart {direction}"]
    if not flow.stages:
        lines.append("    %% (no stages)")
        return "\n".join(lines)
    for s in flow.stages:
        label = f"{s.index + 1} · {s.name}" if s.name else f"{s.index + 1}"
        lines.append(f'    S{s.index}["{_mermaid_label(label)}"]')
    for edge in flow.edges:
        # Dotted from a stage that may skip (skip_if, or a skip or xfail mark):
        # the values may come from an earlier producer instead (its own edge), or never.
        arrow = "-.->" if flow.stages[edge.producer].may_skip else "-->"
        lines.append(f"    S{edge.producer} {arrow}|{', '.join(edge.vars)}| S{edge.consumer}")
    for connection_edge in flow.connection_edges:
        # Dashed: no value flows, the later stage talks over the session the
        # earlier one may have opened.
        lines.append(f'    S{connection_edge.opener} -.->|"{_mermaid_label(f"connection: {connection_edge.connection}")}"| S{connection_edge.user}')
    return "\n".join(lines)


@app.command()
def graph(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to graph.")],
    direction: Annotated[GraphDirection, typer.Option("--direction", help="Flowchart orientation.")] = GraphDirection.TD,
    ref_parent_traversal_depth: RefParentTraversalDepth = None,
    root_path: RootPath = None,
) -> None:
    """Emit a Mermaid flowchart of the stage data-flow."""
    sc, test_data = _load_for_inspection(scenario, ref_parent_traversal_depth, root_path)
    flow = analyze_dataflow(sc, test_data)
    typer.echo(_to_mermaid(flow, direction.value))


import_app = _Typer(no_args_is_help=True, help="Write a starter scenario from recorded traffic: a HAR file or curl commands.")
app.add_typer(import_app, name="import")


class _OwnOptionsFirst(_Command):
    """A command whose own options come first: from the first word that is
    none of them on, every word is its argument's, as after a ``--``. For
    ``import curl``, whose argument is a curl command: told to leave options
    it does not know alone, click would still read an ``-o`` after one
    (``-sSL -o page.html https://...``) as the import's own, and write the
    scenario where curl was to write its answer."""

    @override
    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        own = {name: param for param in self.get_params(ctx) if param.param_type_name == "option" for name in (*param.opts, *param.secondary_opts)}
        index = 0
        while index < len(args) and args[index] != "--":
            word = args[index]
            if word.startswith("--"):
                name, equals, _ = word.partition("=")
                option, attached = own.get(name), bool(equals)
            elif word.startswith("-") and len(word) > 1:
                option, attached = own.get(word[:2]), len(word) > 2
            else:
                option, attached = None, False
            if option is None or (getattr(option, "is_flag", False) and attached):
                return super().parse_args(ctx, [*args[:index], "--", *args[index:]])
            if not getattr(option, "is_flag", False) and not attached:
                if index + 1 == len(args):
                    # Its value missing: click says so.
                    break
                index += 1
            index += 1
        return super().parse_args(ctx, args)


def _output_option(value: Path | None) -> Path | None:
    """An empty --output is a usage error on every typer: 0.29 refuses it as
    the directory ``.`` it reads it as, and up to 0.28 it reached the import
    as that directory, to fail there. (A ``.`` typed never gets here: typer
    refuses a directory.)"""
    if value == Path():
        raise typer.BadParameter("an empty path names no file")
    return value


OutputPathOption = Annotated[Path | None, typer.Option("--output", "-o", help="Write the scenario to this file instead of stdout.", dir_okay=False, callback=_output_option)]
ForceOption = Annotated[bool, typer.Option("--force", help="Overwrite the --output file if it exists.")]


def _patterns(values: list[str] | None, option: str) -> list[re.Pattern[str]]:
    patterns = []
    for value in values or []:
        try:
            patterns.append(re.compile(value))
        except re.error as e:
            raise typer.BadParameter(f"{value!r} is not a regular expression: {e}", param_hint=option) from None
    return patterns


def _refuse_existing(output: Path | None, force: bool) -> None:
    """Checked before any work: an import never replaces a file unasked."""
    if output is not None and output.exists() and not force:
        _fail(f"{output} exists; pass --force to overwrite it")


def _read_text(source: str, read: Callable[[], str]) -> str:
    try:
        return read()
    except (OSError, UnicodeDecodeError) as e:
        _fail(f"cannot read {source}: {e}")


def _read_stdin() -> str:
    """The text standard input holds, less a byte-order mark: one is
    allowed there, as in a file."""
    return sys.stdin.read().removeprefix("\ufeff")


def _emit(result: ImportResult, warnings: list[str], output: Path | None) -> None:
    """Validate the scenario an import built, report on it on stderr, and
    write it: to ``output``, else to stdout.

    It is validated as the file written, as `validate` would read it
    (`validate_text`): one that would not pass (a URL the model refuses) is
    not written, and the command fails naming why. The notes list the
    placeholders standing for the secrets left out, which the scenario reads
    from the environment.
    """
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    for note in result.notes:
        typer.echo(f"note: {note}", err=True)
    try:
        text = scenario_text(result.scenario)
    except ImportSourceError as e:
        _fail(str(e))
    checked = validate_text(text)
    for diagnostic in checked.diagnostics:
        typer.echo(_diagnostic_line(diagnostic), err=True)
    if not checked.valid:
        _fail("the imported scenario would not pass validate, so it was not written")
    if result.placeholders:
        width = max(len(entry.env) for entry in result.placeholders)
        typer.echo("note: secrets were left out of the scenario, which reads them from these environment variables:", err=True)
        for entry in result.placeholders:
            stages = ", ".join(entry.stages)
            typer.echo(f"  {entry.env:<{width}}  {entry.what} ({'stage' if len(entry.stages) == 1 else 'stages'} {stages})", err=True)
    if result.files:
        typer.echo(f"note: the scenario reads these files, a relative path from the scenario's own directory: {', '.join(result.files)}", err=True)
    if output is None:
        typer.echo(text, nl=False)
        return
    try:
        output.write_text(text, encoding="utf-8")
    except OSError as e:
        _fail(f"cannot write {output}: {e}")


@import_app.command("har")
def import_har(
    har: Annotated[Path, typer.Argument(help="HAR file to import (- for stdin), as a browser's developer tools or the plugin's --httpchain-output-dir export it.")],
    output: OutputPathOption = None,
    force: ForceOption = False,
    keep_all: Annotated[bool, typer.Option("--all", help="Keep the static assets (images, stylesheets, fonts, scripts) a page loaded too.")] = False,
    include: Annotated[list[str] | None, typer.Option("--include", help="Import only the entries whose URL this regex matches (repeatable: any of them).")] = None,
    exclude: Annotated[list[str] | None, typer.Option("--exclude", help="Leave out the entries whose URL this regex matches (repeatable).")] = None,
) -> None:
    """Write a starter scenario from a HAR file: a stage per entry, in order,
    verifying the status each response had.

    Secrets (Authorization, cookies, and the headers and query parameters
    reports redact) become placeholders the scenario reads from environment
    variables, listed on stderr.
    """
    included, excluded = _patterns(include, "--include"), _patterns(exclude, "--exclude")
    _refuse_existing(output, force)
    source = "stdin" if str(har) == "-" else str(har)
    # A byte-order mark is allowed, from stdin as from a file.
    text = _read_text(source, lambda: _read_stdin() if str(har) == "-" else har.read_bytes().decode("utf-8-sig"))
    try:
        requests, notes = read_har(text, source=source, keep_all=keep_all, include=included, exclude=excluded)
    except ImportSourceError as e:
        _fail(str(e))
    result = build_scenario(requests, description=f"Imported from {'a HAR file' if source == 'stdin' else har.name}")
    result.notes[:0] = notes
    _emit(result, [], output)


@import_app.command("curl", cls=_OwnOptionsFirst)
def import_curl(
    command: Annotated[
        list[str],
        typer.Argument(
            help="The curl command: one argument holding it as pasted (several commands, one per line, make a stage each), "
            "its words as separate arguments, or - to read it from stdin.",
            show_default=False,
        ),
    ],
    output: OutputPathOption = None,
    force: ForceOption = False,
) -> None:
    """Write a starter scenario from a curl command: a stage per request it
    sends, verifying a 2xx status.

    Give this command's own options first: everything from the curl command
    on is the command's. Secrets (Authorization, -u passwords, cookies, and
    the headers and query parameters reports redact) become placeholders the
    scenario reads from environment variables, listed on stderr. An option the
    import does not map is named in a warning.
    """
    if command[0] == "-" and len(command) > 1:
        # Everything after the command's first word is the command's.
        raise typer.BadParameter(f"'-' reads the command from stdin, so nothing may follow it; give {' '.join(command[1:])!r} before it")
    if len(command) > 1 and not command[0].startswith("-") and len(command[0].split()) > 1:
        # A whole command in one argument, then more: its words would be
        # taken for one (a URL "curl https://..."), and the rest is most
        # likely this command's options, given after it. (An option's word
        # may hold a space: -H"X-A: 1".)
        raise typer.BadParameter(f"the first argument holds a whole command, so nothing may follow it; give {' '.join(command[1:])!r} before it, or the command as its words")
    _refuse_existing(output, force)
    try:
        # curl reads a -d @file from the directory it runs in.
        if len(command) == 1:
            text = _read_text("stdin", _read_stdin) if command[0] == "-" else command[0]
            requests, warnings = parse_curl(text, data_dir=Path.cwd())
        else:
            requests, warnings = parse_curl_words(command, data_dir=Path.cwd())
    except ImportSourceError as e:
        _fail(str(e))
    count = len(requests)
    _emit(build_scenario(requests, description="Imported from a curl command" if count == 1 else f"Imported from {count} curl requests"), warnings, output)


if __name__ == "__main__":
    app()
