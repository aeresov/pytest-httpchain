import logging
from collections import ChainMap
from typing import Any

from pytest_httpchain_models.entities import Scenario, Stage

from .fixture_manager import FixtureManager
from .utils import process_substitutions

logger = logging.getLogger(__name__)


class ContextManager:
    """Manages all context-related operations for test execution.

    This class centralizes:
    - Global data context shared across stages
    - Scenario variable caching (evaluated once per test class)
    - Context preparation for each stage
    """

    def __init__(self, seed_context: dict[str, Any] | None = None):
        self.global_context: dict[str, Any] = seed_context or {}
        self.fixture_manager = FixtureManager()

    def prepare_stage_context(
        self,
        scenario: Scenario,
        stage: Stage,
        fixture_kwargs: dict[str, Any],
    ) -> ChainMap[str, Any]:
        stage_fixtures = self.fixture_manager.process_fixtures(fixture_kwargs)
        base_context = ChainMap(stage_fixtures, self.global_context)
        local_context = process_substitutions(stage.substitutions, base_context)
        return ChainMap(local_context, stage_fixtures, self.global_context)

    def update_global_context(self, updates: dict[str, Any]):
        self.global_context.update(updates)

    def cleanup(self):
        self.fixture_manager.cleanup()
