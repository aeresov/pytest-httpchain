"""Unit tests for validation/discovery.py: the files a directory given to
`validate` stands for (`find_scenario_files`), and the suffix they are named by
when `--suffix` is not given (`configured_suffix`). That both agree with a real
pytest run is pinned in tests/integration/test_validate_directories.py."""

import errno
import itertools
import os
import re
import sys
from pathlib import Path

import pytest

from pytest_httpchain.validation import DiscoveryError, configured_suffix, find_scenario_files

symlinks = pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink takes a privilege a Windows runner may not have")


def _touch(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")


def _found(directory: Path, suffix: str = "http") -> list[str]:
    return [path.relative_to(directory).as_posix() for path in find_scenario_files(directory, suffix)]


def _re(path: Path) -> str:
    return re.escape(str(path))


# --- find_scenario_files ---


@pytest.mark.parametrize(
    ("name", "found"),
    [
        ("test_login.http.json", True),
        ("test_login.http.jsonc", True),
        # The name between `test_` and the suffix may hold dots.
        ("test_login.v2.http.json", True),
        ("login.http.json", False),
        ("test_login.json", False),
        ("test_login.api.json", False),
        ("test_login.http.json.bak", False),
        # Case counts, in the name as in pytest's collection, on Windows too.
        ("test_login.http.JSON", False),
        ("Test_login.http.json", False),
        ("test_.http.json", False),
    ],
)
def test_finds_the_names_pytest_collects(tmp_path, name, found):
    _touch(tmp_path, name)
    assert _found(tmp_path) == ([name] if found else [])


def test_suffix_names_the_files(tmp_path):
    _touch(tmp_path, "test_a.api.json", "test_b.http.json", "test_c.api.jsonc")
    assert _found(tmp_path, "api") == ["test_a.api.json", "test_c.api.jsonc"]


def test_order_is_the_one_pytest_collects_in(tmp_path):
    """Depth first, each directory's entries by name, files and directories
    interleaved: not the order the filesystem lists them in."""
    _touch(tmp_path, "z/test_a.http.json", "test_m.http.json", "b/test_z.http.json", "b/c/test_b.http.json", "a/test_y.http.json", "b/test_a.http.json")
    assert _found(tmp_path) == [
        "a/test_y.http.json",
        "b/c/test_b.http.json",
        "b/test_a.http.json",
        "b/test_z.http.json",
        "test_m.http.json",
        "z/test_a.http.json",
    ]


@pytest.mark.parametrize("skipped", ["pkg.egg", ".git", ".venv", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}", "__pycache__"])
def test_skips_the_directories_pytest_skips(tmp_path, skipped):
    """pytest's default `norecursedirs`, and `__pycache__`, at any depth."""
    _touch(tmp_path, f"{skipped}/test_a.http.json", f"suite/{skipped}/test_b.http.json", "suite/test_c.http.json")
    assert _found(tmp_path) == ["suite/test_c.http.json"]


@pytest.mark.parametrize(
    "marker",
    [
        "pyvenv.cfg",
        # A conda environment, which may have no pyvenv.cfg (`conda create -p ./env`).
        "conda-meta/history",
    ],
)
def test_skips_a_virtual_environment_whatever_its_name(tmp_path, marker):
    """pytest knows one by either file (unless --collect-in-virtualenv), and
    only by the file: a conda-meta directory is not enough."""
    _touch(tmp_path, f"env/{marker}", "env/lib/test_a.http.json", "envy/test_b.http.json", "other/conda-meta/test_c.http.json")
    assert _found(tmp_path) == ["envy/test_b.http.json", "other/conda-meta/test_c.http.json"]


@pytest.mark.parametrize("given", ["build", ".hidden", "env", ".ci/suite"])
def test_searches_a_directory_given_whatever_its_name(tmp_path, given):
    """As pytest collects a directory named on its command line: the
    skipping is for what the search meets, not for where it starts."""
    _touch(tmp_path, f"{given}/test_a.http.json", "env/pyvenv.cfg")
    assert _found(tmp_path / given) == ["test_a.http.json"]


@symlinks
def test_does_not_follow_a_directory_symlink(tmp_path):
    """A link back up the tree would loop the search; a link across it would
    find a file twice."""
    _touch(tmp_path, "real/test_a.http.json")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    (tmp_path / "real" / "loop").symlink_to(tmp_path, target_is_directory=True)
    assert _found(tmp_path) == ["real/test_a.http.json"]


@symlinks
def test_follows_a_file_symlink_and_a_directory_given_as_one(tmp_path):
    """pytest collects both; only the links the search meets on its way down
    are directories it does not enter."""
    _touch(tmp_path, "shared/test_a.http.json")
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite" / "test_b.http.json").symlink_to(tmp_path / "shared" / "test_a.http.json")
    (tmp_path / "given").symlink_to(tmp_path / "suite", target_is_directory=True)
    assert _found(tmp_path / "given") == ["test_b.http.json"]


@symlinks
def test_passes_over_a_broken_symlink(tmp_path):
    """It is no file, and pytest passes it over too."""
    (tmp_path / "test_a.http.json").symlink_to(tmp_path / "missing.json")
    assert _found(tmp_path) == []


@symlinks
def test_passes_over_a_symlink_to_itself(tmp_path):
    """Looking at it fails (ELOOP), and pytest passes it over, named like a
    scenario or not; the rest of the tree is still searched."""
    _touch(tmp_path, "test_b.http.json")
    (tmp_path / "loop").symlink_to("loop")
    (tmp_path / "test_a.http.json").symlink_to("test_a.http.json")
    assert _found(tmp_path) == ["test_b.http.json"]


class _Unreadable:
    """A directory entry named like a scenario that cannot be looked at."""

    def __init__(self, entry: os.DirEntry[str], error: OSError) -> None:
        self.name, self.path, self._entry, self._error = entry.name, entry.path, entry, error

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def is_junction(self) -> bool:
        return self._entry.is_junction()

    def is_file(self) -> bool:
        raise self._error


@pytest.mark.parametrize(
    ("error", "passed_over"),
    [
        # The errors pytest's collection passes over an entry for: gone since
        # the listing, under something that is no directory, a stale
        # descriptor, a link loop.
        pytest.param(FileNotFoundError(errno.ENOENT, "No such file or directory"), True, id="ENOENT"),
        pytest.param(NotADirectoryError(errno.ENOTDIR, "Not a directory"), True, id="ENOTDIR"),
        pytest.param(OSError(errno.EBADF, "Bad file descriptor"), True, id="EBADF"),
        pytest.param(OSError(errno.ELOOP, "Too many levels of symbolic links"), True, id="ELOOP"),
        # Any other stops pytest, and the search: the file would go unchecked.
        pytest.param(PermissionError(errno.EACCES, "Permission denied"), False, id="EACCES"),
    ],
)
def test_an_entry_that_cannot_be_looked_at(tmp_path, monkeypatch, error, passed_over):
    """Passed over when pytest passes it over, else an error. (Simulated: most
    of these cannot be made to happen on demand.)"""
    _touch(tmp_path, "test_a.http.json", "test_b.http.json")
    real_scandir = os.scandir

    class Listing:
        def __init__(self, path):
            self._entries = real_scandir(path)

        def __enter__(self):
            return (_Unreadable(entry, error) if entry.name == "test_a.http.json" else entry for entry in self._entries.__enter__())

        def __exit__(self, *exc):
            return self._entries.__exit__(*exc)

    monkeypatch.setattr(os, "scandir", Listing)
    if passed_over:
        assert _found(tmp_path) == ["test_b.http.json"]
    else:
        with pytest.raises(DiscoveryError, match=rf"^cannot look at {_re(tmp_path / 'test_a.http.json')}: Permission denied$"):
            _found(tmp_path)


@pytest.mark.parametrize(
    ("error", "passed_over"),
    [
        # Gone since its parent was listed: pytest finds nothing in it.
        pytest.param(FileNotFoundError(errno.ENOENT, "No such file or directory"), True, id="gone"),
        # Not passed over: the files in it would go unchecked, and the gate pass.
        pytest.param(PermissionError(errno.EACCES, "Permission denied"), False, id="EACCES"),
    ],
)
def test_a_directory_that_cannot_be_listed(tmp_path, monkeypatch, error, passed_over):
    """(Simulated: a test run as root can list any directory.)"""
    _touch(tmp_path, "open/test_a.http.json", "locked/test_b.http.json")
    real_scandir = os.scandir

    def scandir(path):
        if Path(path).name == "locked":
            raise error
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    if passed_over:
        assert _found(tmp_path) == ["open/test_a.http.json"]
    else:
        with pytest.raises(DiscoveryError, match=rf"^cannot search directory {_re(tmp_path / 'locked')}: Permission denied$"):
            _found(tmp_path)


# --- configured_suffix ---

# Each file pytest takes its configuration from, in the order it looks for
# them in a directory, setting the suffix given.
CONFIG_FILES = {
    "pytest.toml": '[pytest]\nhttpchain_suffix = "{}"\n',
    ".pytest.toml": '[pytest]\nhttpchain_suffix = "{}"\n',
    "pytest.ini": "[pytest]\nhttpchain_suffix = {}\n",
    ".pytest.ini": "[pytest]\nhttpchain_suffix = {}\n",
    "pyproject.toml": '[tool.pytest]\nhttpchain_suffix = "{}"\n',
    "tox.ini": "[pytest]\nhttpchain_suffix = {}\n",
    "setup.cfg": "[tool:pytest]\nhttpchain_suffix = {}\n",
}


def _configure(directory: Path, filename: str, suffix: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(CONFIG_FILES[filename].format(suffix))


@pytest.fixture
def clean_ancestors(tmp_path):
    """For a test that searches past tmp_path: skip it when a directory above
    holds what the search would stop at."""
    for directory in tmp_path.parents:
        if any((directory / name).is_file() for name in (*CONFIG_FILES, "setup.py")):
            pytest.skip(f"{directory} holds a configuration file or a setup.py")


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        *((name, content.format("api")) for name, content in CONFIG_FILES.items()),
        ("pyproject.toml", '[tool.pytest.ini_options]\nhttpchain_suffix = "api"\n'),
    ],
    ids=[*CONFIG_FILES, "pyproject.toml-ini_options"],
)
def test_reads_the_suffix_from_each_file_pytest_reads(tmp_path, filename, content):
    (tmp_path / filename).write_text(content)
    (tmp_path / "tests").mkdir()
    assert configured_suffix([tmp_path / "tests"]) == "api"


@pytest.mark.parametrize(("first", "second"), list(itertools.pairwise(CONFIG_FILES)))
def test_in_one_directory_the_file_pytest_looks_for_first_wins(tmp_path, first, second):
    _configure(tmp_path, first, "first")
    _configure(tmp_path, second, "second")
    assert configured_suffix([tmp_path]) == "first"


def test_the_nearest_directory_wins(tmp_path):
    _configure(tmp_path, "pytest.toml", "far")
    _configure(tmp_path / "suite", "setup.cfg", "near")
    assert configured_suffix([tmp_path / "suite"]) == "near"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("pyproject.toml", '[project]\nname = "api"\n'),
        # An empty table sets nothing, in either form.
        ("pyproject.toml", "[tool.pytest]\n"),
        ("tox.ini", "[tox]\nenvlist = py313\n"),
        ("setup.cfg", "[metadata]\nname = api\n"),
    ],
    ids=["pyproject.toml", "pyproject.toml-empty-table", "tox.ini", "setup.cfg"],
)
def test_a_shared_file_without_pytest_configuration_is_passed_over(tmp_path, filename, content):
    _configure(tmp_path, "pytest.ini", "outer")
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite" / filename).write_text(content)
    assert configured_suffix([tmp_path / "suite"]) == "outer"


@pytest.mark.parametrize("filename", ["pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml"])
def test_a_pytest_file_is_the_configuration_even_empty(tmp_path, filename):
    """It exists only for pytest, so it needs no section: an outer file setting
    the suffix is never read."""
    _configure(tmp_path, "pyproject.toml", "outer")
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite" / filename).write_text("")
    assert configured_suffix([tmp_path / "suite"]) == "http"


def test_a_configuration_without_the_suffix_sets_the_default(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    assert configured_suffix([tmp_path]) == "http"


def test_the_string_based_table_turns_a_number_into_text(tmp_path):
    """As pytest reads [tool.pytest.ini_options]: every value but a list is text."""
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\nhttpchain_suffix = 2\n")
    assert configured_suffix([tmp_path]) == "2"


def test_searches_from_the_common_ancestor_of_the_paths(tmp_path):
    """`pytest a/test_x.http.json b` reads the configuration above both, not
    a's own; a file stands for its directory."""
    _configure(tmp_path, "pytest.ini", "common")
    _configure(tmp_path / "a", "pytest.ini", "own")
    _touch(tmp_path, "a/test_x.http.json", "b/test_y.http.json")
    assert configured_suffix([tmp_path / "a" / "test_x.http.json", tmp_path / "b"]) == "common"
    assert configured_suffix([tmp_path / "a" / "test_x.http.json"]) == "own"


def test_leaves_out_paths_that_do_not_exist(tmp_path, monkeypatch):
    """Relative paths are the current directory's, which is where the search
    starts when no path exists."""
    _configure(tmp_path, "pytest.ini", "here")
    _configure(tmp_path / "a", "pytest.ini", "own")
    monkeypatch.chdir(tmp_path)
    assert configured_suffix([Path("missing")]) == "here"
    assert configured_suffix([Path("a"), Path("missing")]) == "own"


@pytest.mark.parametrize(
    ("marker", "suffix"),
    [
        pytest.param(None, "own", id="each-path"),
        # A setup.py makes its directory pytest's rootdir, without a file.
        pytest.param("setup.py", "http", id="setup.py"),
        # A pyproject.toml met on the way is the configuration file then.
        pytest.param("pyproject.toml", "http", id="bare-pyproject.toml"),
    ],
)
def test_without_a_configuration_above_the_paths_searches_from_each(tmp_path, clean_ancestors, marker, suffix):
    _configure(tmp_path / "a", "pytest.ini", "own")
    (tmp_path / "b").mkdir()
    if marker is not None:
        (tmp_path / marker).write_text("")
    assert configured_suffix([tmp_path / "a", tmp_path / "b"]) == suffix


def test_files_past_the_configuration_are_not_read(tmp_path):
    (tmp_path / "pyproject.toml").write_text("this is not [ valid toml")
    _configure(tmp_path / "suite", "pytest.ini", "near")
    assert configured_suffix([tmp_path / "suite"]) == "near"


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        pytest.param("pyproject.toml", b"[tool.pytest\n", "cannot read pytest configuration from {file}: ", id="toml-syntax"),
        pytest.param("pytest.toml", b'[pytest]\nhttpchain_suffix = "caf\xe9"\n', "cannot read pytest configuration from {file}: ", id="toml-not-utf8"),
        pytest.param("tox.ini", b"  a continuation line first\n", "cannot read pytest configuration from {file}: ", id="ini-syntax"),
        pytest.param("pytest.ini", b"[pytest]\nhttpchain_suffix = caf\xe9\n", "cannot read pytest configuration from {file}: ", id="ini-not-utf8"),
        # A [tool.pytest] table keeps TOML's types, and pytest refuses a
        # number for a string option there.
        pytest.param("pyproject.toml", b"[tool.pytest]\nhttpchain_suffix = 1\n", "{file}: httpchain_suffix must be a string, not int: 1", id="native-number"),
        pytest.param(
            "pyproject.toml", b'[tool.pytest.ini_options]\nhttpchain_suffix = ["a"]\n', "{file}: httpchain_suffix must be a string, not list: ['a']", id="ini-options-list"
        ),
        pytest.param(
            "pytest.ini",
            b"[pytest]\nhttpchain_suffix = a.b\n",
            "{file}: httpchain_suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars",
            id="not-a-suffix",
        ),
        pytest.param(
            "pyproject.toml",
            b'[tool.pytest]\nx = 1\n[tool.pytest.ini_options]\ny = "2"\n',
            "{file}: cannot use both [tool.pytest] and [tool.pytest.ini_options]",
            id="both-tables",
        ),
        pytest.param(
            "setup.cfg",
            b"[pytest]\nhttpchain_suffix = api\n",
            "{file}: [pytest] section in setup.cfg files is no longer supported, change to [tool:pytest] instead.",
            id="setup.cfg-pytest-section",
        ),
    ],
)
def test_configuration_pytest_stops_on_is_an_error(tmp_path, filename, content, message):
    """pytest refuses to run on each of these, so no suffix can be taken from it."""
    file = tmp_path / filename
    file.write_bytes(content)
    with pytest.raises(DiscoveryError) as raised:
        configured_suffix([tmp_path])
    expected = message.format(file=file)
    assert str(raised.value).startswith(expected) if expected.endswith(": ") else str(raised.value) == expected
