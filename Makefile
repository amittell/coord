VENV := .venv
ACTIVATE := . $(VENV)/bin/activate

.PHONY: install run init doctor mcp lint test smoke check docker-build

install:
	python -m venv $(VENV)
	$(ACTIVATE) && pip install -e ".[dev]"

run:
	$(ACTIVATE) && coord start

init:
	$(ACTIVATE) && coord init --tool claude --mode local --yes

doctor:
	$(ACTIVATE) && coord doctor

mcp:
	$(ACTIVATE) && coord-mcp

lint:
	$(ACTIVATE) && ruff check .

test:
	$(ACTIVATE) && pytest -q

smoke:
	$(ACTIVATE) && ./scripts/smoke-test.sh

check: lint test

docker-build:
	docker build -t multi-agent-coordination:local .
