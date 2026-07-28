.PHONY: tidyup
tidyup:
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: update
update:
	uv lock --upgrade
	uv sync
