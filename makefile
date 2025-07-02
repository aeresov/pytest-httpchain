.PHONY: tidyup
tidyup:
	uv run ruff check --fix .
	uv run ruff format .
