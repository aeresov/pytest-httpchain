.PHONY: tidyup
tidyup:
	uv run ruff check --fix --unsafe-fixes .
	uv run ruff format .
