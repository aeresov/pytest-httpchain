from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pytest_httpchain.constants import ConfigOptions
from pytest_httpchain.plugin import pytest_collect_file, pytest_configure


def make_config(suffix="http", ref_depth=3, max_comp=50000, max_parallel=10000):
    # M17: numeric options are registered type="int", so getini returns ints and
    # range-check failures raise pytest.UsageError (not bare ValueError).
    # getini(name) returns None for anything not explicitly modeled — matching
    # the real registration, where default=None is the "unset" sentinel (the
    # legacy alias names therefore read as unset here).
    config = MagicMock()
    values = {
        str(ConfigOptions.SUFFIX): suffix,
        str(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH): ref_depth,
        str(ConfigOptions.MAX_COMPREHENSION_LENGTH): max_comp,
        str(ConfigOptions.MAX_PARALLEL_ITERATIONS): max_parallel,
        "addopts": [],
    }
    config.getini.side_effect = lambda name: values.get(str(name))
    config.invocation_params.args = []
    return config


class TestPytestConfigure:
    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({}, id="defaults"),
            pytest.param({"suffix": "mytest123"}, id="suffix-alphanumeric"),
            pytest.param({"suffix": "my_test"}, id="suffix-underscore"),
            pytest.param({"suffix": "my-test"}, id="suffix-hyphen"),
            pytest.param({"ref_depth": 0}, id="ref-depth-zero"),
            pytest.param({"ref_depth": 10}, id="ref-depth-positive"),
            pytest.param({"max_comp": 1}, id="max-comp-minimum"),
            pytest.param({"max_comp": 1000000}, id="max-comp-maximum"),
            pytest.param({"max_parallel": 1}, id="max-parallel-minimum"),
            pytest.param({"max_parallel": 1000000}, id="max-parallel-maximum"),
        ],
    )
    def test_valid_config(self, kwargs):
        # Should not raise.
        pytest_configure(make_config(**kwargs))

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            pytest.param({"suffix": "test.http"}, "suffix must contain only alphanumeric", id="suffix-special-chars"),
            pytest.param({"suffix": "test http"}, "suffix must contain only alphanumeric", id="suffix-spaces"),
            pytest.param({"suffix": "a" * 33}, "suffix must contain only alphanumeric", id="suffix-too-long"),
            pytest.param({"suffix": ""}, "suffix must contain only alphanumeric", id="suffix-empty"),
            pytest.param({"ref_depth": -1}, "must be non-negative", id="ref-depth-negative"),
            pytest.param({"max_comp": 0}, "must be a positive integer", id="max-comp-zero"),
            pytest.param({"max_comp": -1}, "must be a positive integer", id="max-comp-negative"),
            pytest.param({"max_comp": 1000001}, "must not exceed 1,000,000", id="max-comp-too-large"),
            pytest.param({"max_parallel": 0}, "must be a positive integer", id="max-parallel-zero"),
            pytest.param({"max_parallel": -1}, "must be a positive integer", id="max-parallel-negative"),
            pytest.param({"max_parallel": 1000001}, "must not exceed 1,000,000", id="max-parallel-too-large"),
        ],
    )
    def test_invalid_config(self, kwargs, match):
        with pytest.raises(pytest.UsageError, match=match):
            pytest_configure(make_config(**kwargs))

    @pytest.mark.parametrize(
        "bad_option",
        [
            ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH,
            ConfigOptions.MAX_COMPREHENSION_LENGTH,
            ConfigOptions.MAX_PARALLEL_ITERATIONS,
        ],
    )
    def test_non_integer_ini_value_raises_usage_error(self, bad_option):
        """M9: pytest's type="int" handling does a bare int(value) that raises
        ValueError for a non-integer ini value, which pytest renders as an
        INTERNALERROR traceback. The plugin must turn it into a clean UsageError."""
        config = MagicMock()
        defaults = {
            ConfigOptions.SUFFIX: "http",
            ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH: 3,
            ConfigOptions.MAX_COMPREHENSION_LENGTH: 50000,
            ConfigOptions.MAX_PARALLEL_ITERATIONS: 10000,
        }

        def getini(name):
            if name == bad_option:
                raise ValueError("invalid literal for int() with base 10: 'notanumber'")
            if str(name) == "addopts":
                return []
            # legacy aliases (and anything unmodeled) read as unset
            return defaults.get(name)

        config.getini.side_effect = getini
        config.invocation_params.args = []
        with pytest.raises(pytest.UsageError, match="must be an integer"):
            pytest_configure(config)


class TestPytestCollectFile:
    def make_parent(self, suffix="http"):
        parent = MagicMock()
        parent.config.getini.return_value = suffix
        return parent

    def test_matches_standard_pattern(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_example.http.json")

        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            result = pytest_collect_file(file_path, parent)

            assert result == "mock_module"
            MockJsonModule.from_parent.assert_called_once()
            call_kwargs = MockJsonModule.from_parent.call_args[1]
            assert call_kwargs["name"] == "example"

    def test_matches_underscore_in_name(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_my_api_test.http.json")

        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            result = pytest_collect_file(file_path, parent)

            assert result == "mock_module"
            call_kwargs = MockJsonModule.from_parent.call_args[1]
            assert call_kwargs["name"] == "my_api_test"

    def test_matches_custom_suffix(self):
        parent = self.make_parent(suffix="api")
        file_path = Path("/some/path/test_endpoint.api.json")

        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            result = pytest_collect_file(file_path, parent)

            assert result == "mock_module"

    def test_does_not_match_wrong_suffix(self):
        parent = self.make_parent(suffix="http")
        file_path = Path("/some/path/test_example.api.json")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_missing_test_prefix(self):
        parent = self.make_parent()
        file_path = Path("/some/path/example.http.json")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_missing_json_extension(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_example.http.yaml")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_regular_json(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_example.json")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_python_file(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_example.py")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_partial_pattern(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_.http.json")  # empty name

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_suffix_with_hyphen(self):
        parent = self.make_parent(suffix="my-test")
        file_path = Path("/some/path/test_example.my-test.json")

        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            result = pytest_collect_file(file_path, parent)

            assert result == "mock_module"

    def test_suffix_special_chars_escaped(self):
        # A suffix containing a regex metacharacter ('.') must be matched
        # literally. pytest_collect_file re.escape()s the suffix, so the '.' only
        # matches a literal dot — not any character.
        parent = self.make_parent(suffix="v1.2")

        # Literal match: the dot in the suffix lines up with the dot in the name.
        literal = Path("/some/path/test_example.v1.2.json")
        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            assert pytest_collect_file(literal, parent) == "mock_module"
            assert MockJsonModule.from_parent.call_args[1]["name"] == "example"

        # Without escaping, '.' would match any char, so 'v1X2' would match too.
        # With escaping it must NOT, proving the metacharacter is treated literally.
        injected = Path("/some/path/test_example.v1X2.json")
        assert pytest_collect_file(injected, parent) is None
