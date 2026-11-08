.PHONY: install lint test demo

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
