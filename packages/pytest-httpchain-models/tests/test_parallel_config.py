"""Unit tests for ParallelConfig models."""

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    ParallelForeachConfig,
    ParallelRepeatConfig,
    Stage,
)


class TestParallelConfigBase:
    """Tests for ParallelConfigBase defaults."""

    def test_default_max_concurrency(self):
        """Test default max_concurrency is 10."""
        config = ParallelRepeatConfig(repeat=5)
        assert config.max_concurrency == 10

    def test_default_calls_per_sec_none(self):
        """Test default calls_per_sec is None."""
        config = ParallelRepeatConfig(repeat=5)
        assert config.calls_per_sec is None


class TestParallelRepeatConfig:
    """Tests for ParallelRepeatConfig model."""

    def test_repeat_simple(self):
        """Test simple repeat configuration."""
        config = ParallelRepeatConfig(repeat=100)
        assert config.repeat == 100

    def test_repeat_with_concurrency(self):
        """Test repeat with custom max_concurrency."""
        config = ParallelRepeatConfig(repeat=50, max_concurrency=5)
        assert config.repeat == 50
        assert config.max_concurrency == 5

    def test_repeat_with_rate_limit(self):
        """Test repeat with rate limiting."""
        config = ParallelRepeatConfig(repeat=100, calls_per_sec=10)
        assert config.calls_per_sec == 10

    def test_repeat_with_template(self):
        """Test repeat with template expression."""
        config = ParallelRepeatConfig(repeat="{{ repeat_count }}")
        assert config.repeat == "{{ repeat_count }}"

    def test_repeat_max_concurrency_template(self):
        """Test max_concurrency with template expression."""
        config = ParallelRepeatConfig(repeat=100, max_concurrency="{{ max_workers }}")
        assert config.max_concurrency == "{{ max_workers }}"

    def test_repeat_calls_per_sec_template(self):
        """Test calls_per_sec with template expression."""
        config = ParallelRepeatConfig(repeat=100, calls_per_sec="{{ rate_limit }}")
        assert config.calls_per_sec == "{{ rate_limit }}"

    def test_repeat_must_be_positive(self):
        """Test that repeat must be positive."""
        with pytest.raises(ValidationError):
            ParallelRepeatConfig(repeat=0)
        with pytest.raises(ValidationError):
            ParallelRepeatConfig(repeat=-1)

    def test_repeat_max_concurrency_must_be_positive(self):
        """Test that max_concurrency must be positive."""
        with pytest.raises(ValidationError):
            ParallelRepeatConfig(repeat=10, max_concurrency=0)

    def test_repeat_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ParallelRepeatConfig(repeat=10, extra="field")


class TestParallelForeachConfig:
    """Tests for ParallelForeachConfig model."""

    def test_foreach_with_individual_params(self):
        """Test foreach with individual parameters."""
        config = ParallelForeachConfig(foreach=[{"individual": {"id": [1, 2, 3, 4, 5]}}])
        assert len(config.foreach) == 1

    def test_foreach_with_combinations(self):
        """Test foreach with combination parameters."""
        config = ParallelForeachConfig(
            foreach=[
                {
                    "combinations": [
                        {"method": "GET", "path": "/a"},
                        {"method": "POST", "path": "/b"},
                    ]
                }
            ]
        )
        assert len(config.foreach) == 1

    def test_foreach_with_concurrency(self):
        """Test foreach with custom max_concurrency."""
        config = ParallelForeachConfig(
            foreach=[{"individual": {"x": [1, 2]}}],
            max_concurrency=20,
        )
        assert config.max_concurrency == 20

    def test_foreach_with_rate_limit(self):
        """Test foreach with rate limiting."""
        config = ParallelForeachConfig(
            foreach=[{"individual": {"x": [1, 2]}}],
            calls_per_sec=5,
        )
        assert config.calls_per_sec == 5

    def test_foreach_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ParallelForeachConfig(
                foreach=[{"individual": {"x": [1]}}],
                extra="field",
            )


class TestParallelConfigDiscriminator:
    """Tests for ParallelConfig discriminated union."""

    def test_discriminator_repeat(self):
        """Test discriminator identifies ParallelRepeatConfig."""
        stage = Stage(
            name="load-test",
            request={"url": "https://example.com"},
            parallel={"repeat": 100},
        )
        assert isinstance(stage.parallel, ParallelRepeatConfig)
        assert stage.parallel.repeat == 100

    def test_discriminator_foreach(self):
        """Test discriminator identifies ParallelForeachConfig."""
        stage = Stage(
            name="batch-test",
            request={"url": "https://example.com"},
            parallel={"foreach": [{"individual": {"id": [1, 2, 3]}}]},
        )
        assert isinstance(stage.parallel, ParallelForeachConfig)

    def test_discriminator_invalid_rejected(self):
        """Test that invalid parallel config type is rejected."""
        with pytest.raises(ValueError, match="Unable to determine parallel config type"):
            Stage(
                name="test",
                request={"url": "https://example.com"},
                parallel={"invalid": "value"},
            )


class TestParallelInStage:
    """Tests for parallel configuration in Stage model."""

    def test_stage_without_parallel(self):
        """Test Stage without parallel field."""
        stage = Stage(name="test", request={"url": "https://example.com"})
        assert stage.parallel is None

    def test_stage_repeat_with_full_config(self):
        """Test Stage with full repeat configuration."""
        stage = Stage(
            name="stress-test",
            request={"url": "https://example.com/api"},
            parallel={
                "repeat": 1000,
                "max_concurrency": 50,
                "calls_per_sec": 100,
            },
        )
        assert stage.parallel.repeat == 1000
        assert stage.parallel.max_concurrency == 50
        assert stage.parallel.calls_per_sec == 100

    def test_stage_foreach_with_template_params(self):
        """Test Stage with foreach using template parameters."""
        stage = Stage(
            name="batch-test",
            request={"url": "https://example.com/{{ item_id }}"},
            parallel={
                "foreach": [{"individual": {"item_id": "{{ item_ids }}"}}],
                "max_concurrency": 10,
            },
        )
        assert isinstance(stage.parallel, ParallelForeachConfig)
