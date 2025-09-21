.PHONY: tidyup
tidyup:
	uv run ruff check --fix --unsafe-fixes .
	uv run ruff format .

.PHONY: update
update:
	uv lock --upgrade
	uv sync --all-extras --all-packages
