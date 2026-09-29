SERVICE := service
UV := uv --directory $(SERVICE)

.PHONY: setup test lint format typecheck check eval demo demo-agent demo-down demo-smoke

setup:
	$(UV) sync

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check . ../demo/agent
	$(UV) run ruff format --check . ../demo/agent

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:
	$(UV) run pyright

check: lint typecheck test

eval:
	$(UV) run python -m approved evaluate --offline

# The local demo (Docker): gate + AI judge + fake Telegram. See demo/README.md.
demo:
	bash demo/run.sh up
	bash demo/run.sh agent

demo-agent:
	bash demo/run.sh agent

demo-down:
	bash demo/run.sh down

# Headless end-to-end check of the demo (what CI runs).
demo-smoke:
	bash demo/smoke.sh
