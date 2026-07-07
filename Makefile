VENV := .venv
PY := $(VENV)/bin/python

$(VENV)/bin/pip:
	python3 -m venv $(VENV)

.PHONY: deps
deps: $(VENV)/bin/pip
	$(PY) -m pip install -q -r requirements.txt

.PHONY: run
run: deps
	$(PY) -m app.serve

.PHONY: backup
backup: deps
	$(PY) -m app.backup

.PHONY: test
test: deps
	$(PY) -m pytest -q
