.PHONY: install lint test demo bench bench-optim

install:
	uv sync
	uv run pre-commit install

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy src

test:
	uv run pytest

demo:
	@echo "demo: not yet implemented (P1+)"

# P6-05 / H-11 : benchmark lent (1 M lignes, plusieurs minutes), hors CI et hors `pytest`
bench-optim:
	uv run python scripts/bench_optim.py

# P6-09 : benchmarks complets (~6 min, hors CI et hors `pytest`) -> docs/perf/report.md + results.json
# (`make bench ARGS=--full` : 1 M lignes). Un banc seul : uv run python scripts/bench.py --only <nom>
bench:
	uv run python scripts/bench.py $(ARGS)
