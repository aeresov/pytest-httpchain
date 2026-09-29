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
from typing import Annotated

import typer
import typer.core
from pydantic import ValidationError

from pytest_httpchain.constants import check_suffix
from pytest_httpchain.dataflow import DataFlow, analyze_dataflow
from pytest_httpchain.importers import ImportResult, ImportSourceError, build_scenario, parse_curl, parse_curl_words, read_har, scenario_text, validate_text
from pytest_httpchain.jsonref import ReferenceResolverError
from pytest_httpchain.models import Scenario
from pytest_httpchain.schema import build_schema
from pytest_httpchain.validation import DiagnosticCode, DiscoveryError, ValidateResult, load_scenario, load_scenario_json, validate_paths

app = typer.Typer(no_args_is_help=True)


class OutputFormat(StrEnum):
    text = "text"
    json = "json"


class GraphDirection(StrEnum):
    TD = "TD"
    LR = "LR"


RefParentTraversalDepth = Annotated[int, typer.Option(help="Maximum $ref parent directory traversal depth.")]
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


@app.command()
def validate(
    paths: Annotated[list[Path], typer.Argument(help="Scenario files to validate, or directories to search for them.")],
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
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
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e

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
                # Some diagnostics already name the location inside the
                # message; repeating it makes the suffix look untrustworthy
                # everywhere else. The field itself stays — it is the
                # machine-readable form of what the message spells out in prose,
                # so a JSON consumer can route on it without parsing English.
                at = f" (at {diagnostic.location})" if diagnostic.location and diagnostic.location not in diagnostic.message else ""
                typer.echo(f"  {diagnostic.severity} [{diagnostic.code}]: {diagnostic.message}{at}")
        if summary := _summary(results):
            typer.echo(summary)

    raise typer.Exit(0 if all_passed else 1)


@app.command()
def schema() -> None:
    """Emit the JSON Schema for scenario files (editor autocomplete/validation).

    Writes to stdout; redirect to a file (``pytest-httpchain schema > scenario.schema.json``).
    """
    typer.echo(json.dumps(build_schema(), indent=2, default=str))


@app.command()
def resolve(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to resolve.")],
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
    root_path: RootPath = None,
) -> None:
    """Resolve $ref/$include/$merge and print the merged scenario JSON to stdout.

    The output is strict JSON: the comments and trailing commas a scenario or
    an included file may hold are not in it.
    """
    try:
        # The loader load_scenario uses, so the printed document is what
        # collection sees.
        data = load_scenario_json(scenario, root_path=root_path, ref_parent_traversal_depth=ref_parent_traversal_depth)
    except (ReferenceResolverError, json.JSONDecodeError, OSError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e

    typer.echo(json.dumps(data, indent=2, default=str))


def _load_for_inspection(path: Path, depth: int, root_path: Path | None = None) -> tuple[Scenario, dict]:
    """Load + validate a scenario for show/graph, mapping failures to Exit(1)."""
    try:
        return load_scenario(path, root_path=root_path, ref_parent_traversal_depth=depth)
    except (ReferenceResolverError, json.JSONDecodeError, OSError) as e:
        typer.echo(f"error: cannot load {path}: {e}", err=True)
        raise typer.Exit(1) from e
    except ValidationError:
        typer.echo(f"error: {path} is not a valid scenario — run `pytest-httpchain validate {path}` for details", err=True)
        raise typer.Exit(1) from None


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
        lines.append(f"{s.index + 1} · {name}    {s.method} {s.url}")
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
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
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
    return "\n".join(lines)


@app.command()
def graph(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to graph.")],
    direction: Annotated[GraphDirection, typer.Option("--direction", help="Flowchart orientation.")] = GraphDirection.TD,
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
    root_path: RootPath = None,
) -> None:
    """Emit a Mermaid flowchart of the stage data-flow."""
    sc, test_data = _load_for_inspection(scenario, ref_parent_traversal_depth, root_path)
    flow = analyze_dataflow(sc, test_data)
    typer.echo(_to_mermaid(flow, direction.value))


import_app = typer.Typer(no_args_is_help=True, help="Write a starter scenario from recorded traffic: a HAR file or curl commands.")
app.add_typer(import_app, name="import")


class _OwnOptionsFirst(typer.core.TyperCommand):
    """A command whose own options come first: from the first word that is
    none of them on, every word is its argument's, as after a ``--``. For
    ``import curl``, whose argument is a curl command: told to leave options
    it does not know alone, click would still read an ``-o`` after one
    (``-sSL -o page.html https://...``) as the import's own, and write the
    scenario where curl was to write its answer."""

    # typer.Context is the context typer makes; the base names the click
    # Context typer vendors privately.
    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:  # ty: ignore[invalid-method-override]
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


OutputPathOption = Annotated[Path | None, typer.Option("--output", "-o", help="Write the scenario to this file instead of stdout.", dir_okay=False)]
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
        typer.echo(f"error: {output} exists; pass --force to overwrite it", err=True)
        raise typer.Exit(1)


def _read_text(source: str, read: Callable[[], str]) -> str:
    try:
        return read()
    except (OSError, UnicodeDecodeError) as e:
        typer.echo(f"error: cannot read {source}: {e}", err=True)
        raise typer.Exit(1) from None


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
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    checked = validate_text(text)
    for diagnostic in checked.diagnostics:
        at = f" (at {diagnostic.location})" if diagnostic.location and diagnostic.location not in diagnostic.message else ""
        typer.echo(f"{diagnostic.severity} [{diagnostic.code}]: {diagnostic.message}{at}", err=True)
    if not checked.valid:
        typer.echo("error: the imported scenario would not pass validate, so it was not written", err=True)
        raise typer.Exit(1)
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
        typer.echo(f"error: cannot write {output}: {e}", err=True)
        raise typer.Exit(1) from None


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
    text = _read_text(source, lambda: sys.stdin.read().removeprefix("\ufeff") if str(har) == "-" else har.read_bytes().decode("utf-8-sig"))
    try:
        requests, notes = read_har(text, source=source, keep_all=keep_all, include=included, exclude=excluded)
    except ImportSourceError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
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
            text = _read_text("stdin", lambda: sys.stdin.read().removeprefix("\ufeff")) if command[0] == "-" else command[0]
            requests, warnings = parse_curl(text, data_dir=Path.cwd())
        else:
            requests, warnings = parse_curl_words(command, data_dir=Path.cwd())
    except ImportSourceError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    count = len(requests)
    _emit(build_scenario(requests, description="Imported from a curl command" if count == 1 else f"Imported from {count} curl requests"), warnings, output)


if __name__ == "__main__":
    app()
