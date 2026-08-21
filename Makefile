.PHONY: install demo report bench attack test lint clean

VENV := .venv
PY   := $(VENV)/bin/python

install:
	uv venv --python 3.13 $(VENV)
	uv pip install --python $(PY) -e ".[dev]"

demo:
	$(PY) -m quittance --offline --payments 2000 --days 90

report:
	$(PY) -m quittance --offline --payments 2000 --days 90 --report --open

demo-llm:
	$(PY) -m quittance --payments 2000 --days 90

bench:
	$(PY) -m quittance.bench --payments 2000 --days 90

attack:
	$(PY) -m quittance.adversarial

test:
	$(PY) -m pytest tests/ -q

lint:
	$(PY) -m ruff check quittance tests

clean:
	rm -rf $(VENV) .pytest_cache **/__pycache__
