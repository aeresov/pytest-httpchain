import re
from pathlib import Path

import pytest
from _pytest import config, nodes, python, reports, runner
from _pytest.config import argparsing

from pytest_httpchain.core.collector import JsonModuleCollector

SUFFIX: str = "suffix"
REF_PARENT_TRAVERSAL_DEPTH: str = "ref_parent_traversal_depth"


class JsonModule(python.Module):
    """JSON test module that uses the collector for test collection."""

    def collect(self):
        """Delegate collection to JsonModuleCollector."""
        collector = JsonModuleCollector(self)
        return collector.collect()


def pytest_addoption(parser: argparsing.Parser) -> None:
    """Add command-line options for the plugin."""
    parser.addini(
        name=SUFFIX,
        help="File suffix for HTTP test files.",
        type="string",
        default="http",
    )
    parser.addini(
        name=REF_PARENT_TRAVERSAL_DEPTH,
        help="Maximum number of parent directory traversals allowed in $ref paths.",
        type="string",
        default="3",
    )


def pytest_configure(config: config.Config) -> None:
    """Validate configuration settings."""
    suffix: str = config.getini(SUFFIX)
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    try:
        ref_parent_traversal_depth = int(config.getini(REF_PARENT_TRAVERSAL_DEPTH))
        if ref_parent_traversal_depth < 0:
            raise ValueError("Maximum number of parent directory traversals must be non-negative")
    except ValueError as e:
        raise ValueError("Maximum number of parent directory traversals must be a non-negative integer") from e


def pytest_collect_file(file_path: Path, parent: nodes.Collector) -> nodes.Collector | None:
    """Collect JSON test files matching the configured pattern."""
    pattern, group_name = _get_test_name_pattern(parent.config)
    match: re.Match[str] | None = pattern.match(file_path.name)
    if match:
        return JsonModule.from_parent(parent, path=file_path, name=match.group(group_name))
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: nodes.Item, call: runner.CallInfo):
    """Add custom sections to test reports."""
    outcome = yield
    report: reports.TestReport = outcome.get_result()
    if call.when == "call":
        report.sections.append(("call_title", "call_value"))


def _get_test_name_pattern(config: config.Config) -> tuple[re.Pattern[str], str]:
    """Get the regex pattern for matching test file names."""
    suffix: str = config.getini(SUFFIX)
    group_name: str = "name"
    return re.compile(rf"^test_(?P<{group_name}>.+)\.{re.escape(suffix)}\.json$"), group_name
