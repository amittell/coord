VENV := .venv
ACTIVATE := . $(VENV)/bin/activate

.PHONY: install run init doctor mcp lint typecheck test test-fast smoke check verify docker-build docker-smoke clean

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

# --- linting and testing (what CI runs that is cheap locally) ---

lint:
	$(ACTIVATE) && ruff check .

typecheck:
	$(ACTIVATE) && mypy coordination

test:
	$(ACTIVATE) && pytest -q

# Fast loop: skip the real-process integration tests. Use during active
# edits, then run `make test` before pushing.
test-fast:
	$(ACTIVATE) && pytest -q -m "not integration"

# Mirror of CI's cheap jobs: ruff + mypy + full pytest. ~30 seconds.
check: lint typecheck test

# --- container ---

docker-build:
	docker build -t coord:local .

# Build the image, start it on a high port, probe /readyz, stop. The
# same signal CI's docker-build-smoke gives you, plus a live /readyz
# probe, in about 45 seconds.
docker-smoke: docker-build
	@set -e; \
	port=18099; \
	docker rm -f coord-make-smoke >/dev/null 2>&1 || true; \
	docker run --rm -d --name coord-make-smoke \
	  -e COORD_AUTH_TOKEN=make-smoke \
	  -p $$port:8080 coord:local >/dev/null; \
	trap 'docker stop coord-make-smoke >/dev/null 2>&1 || true' EXIT; \
	for i in 1 2 3 4 5 6 7 8 9 10; do \
	  if curl -sf http://127.0.0.1:$$port/readyz >/dev/null; then break; fi; \
	  sleep 1; \
	done; \
	curl -sf http://127.0.0.1:$$port/readyz | head -c 200 && echo; \
	echo "docker-smoke: OK"

# Full local CI equivalent: lint + typecheck + test + docker smoke.
# Run this before pushing to catch anything CI will reject. ~2 minutes.
verify: check docker-smoke

smoke:
	$(ACTIVATE) && ./scripts/smoke-test.sh

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
