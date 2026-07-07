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

.PHONY: docker-build
docker-build:
	docker compose build

.PHONY: docker-up
docker-up:
	docker compose up -d

.PHONY: docker-down
docker-down:
	docker compose down

.PHONY: docker-logs
docker-logs:
	docker compose logs -f app

.PHONY: docker-backup
docker-backup:
	docker compose exec app python -m app.backup

.PHONY: backup
backup: deps
	$(PY) -m app.backup

.PHONY: test
test: deps
	$(PY) -m pytest -q
