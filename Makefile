.PHONY: tidyup
tidyup:
	uv run ruff check --fix .
	uv run ruff format .

# CI's Lint job, gate for gate and in its order, stopping at the first failure:
# run it before pushing. `uv lock --check` stands in for the job's
# `uv sync --locked`, which fails on a stale uv.lock where a local `uv run`
# re-locks silently.
.PHONY: lint
lint:
	uv lock --check
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check
	uv run lint-imports
	uv run python scripts/generate_schema.py --check

.PHONY: update
update:
	uv lock --upgrade
	uv sync
