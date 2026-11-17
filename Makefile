.PHONY: install test lint up down seed-erp seed-crm finance sync status analytics

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check . && uv run mypy src scripts analytics

up:
	docker compose up -d

down:
	docker compose down

seed-erp:
	uv run python scripts/seed_odoo.py

seed-crm:
	uv run python scripts/seed_salesforce.py

finance:
	uv run python scripts/simulate_finance.py

sync:
	uv run o2c sync

status:
	uv run o2c status

analytics:
	uv run python analytics/build.py
