.PHONY: tidyup
tidyup:
	uv run ruff check --fix --unsafe-fixes .
	uv run ruff format .

.PHONY: sync
sync:
	uv sync --all-extras --all-packages
