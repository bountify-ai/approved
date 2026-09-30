SERVICE := service
UV := uv --directory $(SERVICE)

.PHONY: setup test lint format typecheck check eval demo demo-agent demo-down demo-smoke cli-help docs-check tryit tryit-down tryit-smoke tryit-deploy-branch

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

docs-check:
	python3 scripts/check_docs.py

eval:
	$(UV) run python -m approved evaluate --offline

# The operator CLI (Maritime). See README "Operator CLI"; nothing here runs a maritime call.
cli-help:
	$(UV) run approved --help
	$(UV) run approved provision --help

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

# Try it: the whole demo in ONE container behind one port (docs/tryit.md). `make tryit`
# builds and runs it at http://127.0.0.1:$(TRYIT_PORT)/; `make tryit-down` removes exactly the
# container and volume it created (by name). Nothing here deploys anything.
TRYIT_IMAGE ?= approved-tryit:local
TRYIT_NAME ?= approved-tryit
TRYIT_PORT ?= 18789

tryit:
	@test -n "$${TRYIT_GATEWAY_SECRET:-}" || { echo 'set TRYIT_GATEWAY_SECRET for the local private demo'; exit 2; }
	@test -n "$${WANDB_API_KEY:-}" || { echo 'set WANDB_API_KEY for the live reviewer'; exit 2; }
	@test -n "$${REVIEWER_MODEL:-}" || { echo 'set REVIEWER_MODEL for the live reviewer'; exit 2; }
	docker build -f images/tryit/Dockerfile -t $(TRYIT_IMAGE) .
	-docker rm -f $(TRYIT_NAME) >/dev/null 2>&1
	docker run -d --name $(TRYIT_NAME) -e PORT=18789 -e TRYIT_LIVE=1 \
	  -e TRYIT_GATEWAY_SECRET -e WANDB_API_KEY -e REVIEWER_MODEL \
	  -v $(TRYIT_NAME)-data:/data \
	  -p 127.0.0.1:$(TRYIT_PORT):18789 $(TRYIT_IMAGE) >/dev/null
	@echo "private demo backend: http://127.0.0.1:$(TRYIT_PORT)/   (send X-Approved-Gateway; make tryit-down removes it)"

tryit-down:
	-docker rm -f $(TRYIT_NAME)
	-docker volume rm $(TRYIT_NAME)-data

# Headless end-to-end check of the image through its public port (images/tryit/smoke.py).
tryit-smoke:
	bash images/tryit/smoke.sh

# Build the local branch whose ROOT Dockerfile is the try-it image, for Maritime's GitHub
# route (docs/tryit.md). Local only: it prints the push command and never pushes.
tryit-deploy-branch:
	bash scripts/tryit-deploy-branch.sh
