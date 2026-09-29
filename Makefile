SERVICE := service
UV := uv --directory $(SERVICE)

.PHONY: setup test lint format typecheck check eval demo

setup:
	$(UV) sync

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:
	$(UV) run pyright

check: lint typecheck test

eval:
	$(UV) run python -m approved evaluate --offline

demo:
	@echo "demo: not wired yet (lands in a later unit)"
