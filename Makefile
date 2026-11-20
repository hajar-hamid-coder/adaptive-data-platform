.PHONY: install lint test demo bench-optim

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
