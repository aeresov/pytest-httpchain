"""Tests for the validate_scenario tool using pytest-datadir."""

from pytest_httpchain_mcp.server import validate_scenario


def test_validate_valid_scenario(datadir):
    """Test validation of a valid scenario."""
    result = validate_scenario(datadir / "valid_scenario.json")

    assert result.valid is True
    assert len(result.errors) == 0
    assert result.scenario_info is not None
    assert result.scenario_info.num_stages == 2
    assert result.scenario_info.stage_names == ["get_user", "update_user"]
    assert "user_id" in result.scenario_info.vars_referenced
    assert "user_name" in result.scenario_info.vars_saved


def test_validate_invalid_json(datadir):
    """Test validation of invalid JSON."""
    result = validate_scenario(datadir / "invalid_json.json")

    assert result.valid is False
    # Error can come from jsonref loader or direct JSON parsing
    assert any("Invalid JSON syntax" in error or "Failed to load JSON" in error for error in result.errors)


def test_validate_missing_file(datadir):
    """Test validation of non-existent file."""
    result = validate_scenario(datadir / "non_existent.json")

    assert result.valid is False
    assert any("File not found" in error for error in result.errors)


def test_validate_duplicate_stage_names(datadir):
    """Test detection of duplicate stage names."""
    result = validate_scenario(datadir / "duplicate_stage_names.json")

    assert result.valid is False
    assert any("Duplicate stage names" in error for error in result.errors)


def test_validate_undefined_variables(datadir):
    """Test detection of undefined variables."""
    result = validate_scenario(datadir / "undefined_variables.json")

    assert result.valid is True  # This is a warning, not an error
    assert any("undefined" in warning.lower() for warning in result.warnings)
    assert result.scenario_info is not None
    assert "undefined_var" in result.scenario_info.vars_referenced


def test_validate_no_response_validation(datadir):
    """Test warning for stages with no response validation."""
    result = validate_scenario(datadir / "no_response_validation.json")

    assert result.valid is True
    assert any("no response validation" in warning for warning in result.warnings)


def test_validate_fixture_var_conflict(datadir):
    """Test detection of fixture/variable name conflicts."""
    result = validate_scenario(datadir / "fixture_var_conflict.json")

    assert result.valid is False
    assert any("Conflicting fixtures and vars" in error for error in result.errors)


def test_validate_wrong_extension(datadir):
    """Test warning for wrong file extension."""
    result = validate_scenario(datadir / "scenario_wrong_extension.txt")

    assert result.valid is True
    assert any(".txt" in warning and ".json" in warning for warning in result.warnings)


def test_validate_directory_path(tmp_path):
    """Test validation of a directory path (not a file)."""
    result = validate_scenario(tmp_path)

    assert result.valid is False
    assert any("not a file" in error for error in result.errors)


def test_validate_empty_stages(datadir):
    """Test validation of scenario with empty stages array."""
    result = validate_scenario(datadir / "empty_stages.json")

    assert result.valid is True
    assert result.scenario_info is not None
    assert result.scenario_info.num_stages == 0
    assert result.scenario_info.stage_names == []


def test_validate_schema_error(datadir):
    """Test validation with schema errors (missing required fields)."""
    result = validate_scenario(datadir / "schema_error.json")

    assert result.valid is False
    assert any("Schema validation failed" in error for error in result.errors)


def test_validate_with_fixtures(datadir):
    """Test that fixtures are properly extracted."""
    result = validate_scenario(datadir / "with_fixtures.json")

    assert result.valid is True
    assert result.scenario_info is not None
    assert "auth_token" in result.scenario_info.fixtures


def test_validate_with_substitutions(datadir):
    """Test that substitution variables are properly detected."""
    result = validate_scenario(datadir / "with_substitutions.json")

    assert result.valid is True
    assert result.scenario_info is not None
    assert "base_url" in result.scenario_info.vars_defined
