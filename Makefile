.PHONY: tidyup
tidyup:
	uv run ruff check --fix --unsafe-fixes .
	uv run ruff format .

.PHONY: sync
sync:
	uv sync --dev --all-extras --all-packages
