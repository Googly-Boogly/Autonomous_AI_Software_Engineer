PY ?= .venv/bin/python

.PHONY: install test lint typecheck check examples demo clean-runs

install:            ## Create venv deps (editable install + dev + anthropic extras)
	$(PY) -m pip install --no-binary claude-agent-sdk -e '.[dev,claude-code,anthropic]'

test:               ## Run the full test suite
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy

check: lint typecheck test

examples:           ## Materialize fixture repos as standalone git repos under ./examples
	$(PY) -m engineer.examples

demo: examples      ## Offline demo of the full flow using the scripted provider (no API key)
	$(PY) -m engineer.run --repo examples/sample-fastapi \
	  --task "Add a GET /health endpoint that returns {'status':'ok'} and add tests." \
	  --provider scripted --script tests/fixtures/scripts/sample-fastapi.json
	$(PY) -m engineer.run --repo examples/calculator \
	  --task "Fix subtraction and add regression test." \
	  --provider scripted --script tests/fixtures/scripts/calculator.json
	$(PY) -m engineer.run --repo examples/inventory \
	  --task "Add validation preventing negative quantities." \
	  --provider scripted --script tests/fixtures/scripts/inventory.json

clean-runs:         ## Remove run data, then unregister the deleted worktrees (branches are kept)
	rm -rf data/
	for d in examples/*/; do if [ -d "$$d/.git" ]; then git -C "$$d" worktree prune; fi; done
