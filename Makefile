.PHONY: install demo test lint clean

VENV := .venv
PY   := $(VENV)/bin/python

install:
	uv venv --python 3.13 $(VENV)
	uv pip install --python $(PY) -e ".[dev]"

demo:
	$(PY) -m chukta --offline

demo-llm:
	$(PY) -m chukta

test:
	$(PY) -m pytest tests/ -q

lint:
	$(PY) -m ruff check chukta tests

clean:
	rm -rf $(VENV) .pytest_cache **/__pycache__
