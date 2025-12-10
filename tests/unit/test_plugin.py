from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pytest_httpchain.constants import ConfigOptions
from pytest_httpchain.plugin import pytest_collect_file, pytest_configure


class TestPytestConfigure:
    def make_config(self, suffix="http", ref_depth="3", max_comp="50000"):
        config = MagicMock()
        config.getini.side_effect = lambda name: {
            ConfigOptions.SUFFIX: suffix,
            ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH: ref_depth,
            ConfigOptions.MAX_COMPREHENSION_LENGTH: max_comp,
        }[name]
        return config

    def test_valid_config_defaults(self):
        config = self.make_config()

        # Should not raise
        pytest_configure(config)

    def test_valid_suffix_alphanumeric(self):
        config = self.make_config(suffix="mytest123")

        # Should not raise
        pytest_configure(config)

    def test_valid_suffix_with_underscore(self):
        config = self.make_config(suffix="my_test")

        # Should not raise
        pytest_configure(config)

    def test_valid_suffix_with_hyphen(self):
        config = self.make_config(suffix="my-test")

        # Should not raise
        pytest_configure(config)

    def test_invalid_suffix_special_chars(self):
        config = self.make_config(suffix="test.http")

        with pytest.raises(ValueError, match="suffix must contain only alphanumeric"):
            pytest_configure(config)

    def test_invalid_suffix_spaces(self):
        config = self.make_config(suffix="test http")

        with pytest.raises(ValueError, match="suffix must contain only alphanumeric"):
            pytest_configure(config)

    def test_invalid_suffix_too_long(self):
        config = self.make_config(suffix="a" * 33)

        with pytest.raises(ValueError, match="suffix must contain only alphanumeric"):
            pytest_configure(config)

    def test_invalid_suffix_empty(self):
        config = self.make_config(suffix="")

        with pytest.raises(ValueError, match="suffix must contain only alphanumeric"):
            pytest_configure(config)

    def test_valid_ref_depth_zero(self):
        config = self.make_config(ref_depth="0")

        # Should not raise
        pytest_configure(config)

    def test_valid_ref_depth_positive(self):
        config = self.make_config(ref_depth="10")

        # Should not raise
        pytest_configure(config)

    def test_invalid_ref_depth_negative(self):
        config = self.make_config(ref_depth="-1")

        with pytest.raises(ValueError, match="must be non-negative"):
            pytest_configure(config)

    def test_valid_max_comprehension_minimum(self):
        config = self.make_config(max_comp="1")

        # Should not raise
        pytest_configure(config)

    def test_valid_max_comprehension_maximum(self):
        config = self.make_config(max_comp="1000000")

        # Should not raise
        pytest_configure(config)

    def test_invalid_max_comprehension_zero(self):
        config = self.make_config(max_comp="0")

        with pytest.raises(ValueError, match="must be a positive integer"):
            pytest_configure(config)

    def test_invalid_max_comprehension_negative(self):
        config = self.make_config(max_comp="-1")

        with pytest.raises(ValueError, match="must be a positive integer"):
            pytest_configure(config)

    def test_invalid_max_comprehension_too_large(self):
        config = self.make_config(max_comp="1000001")

        with pytest.raises(ValueError, match="must not exceed 1,000,000"):
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
        # Test that regex special characters in suffix are properly escaped
        parent = self.make_parent(suffix="test")
        file_path = Path("/some/path/test_example.test.json")

        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            result = pytest_collect_file(file_path, parent)

            assert result == "mock_module"
