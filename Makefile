.PHONY: install test lint up down seed-erp sync status

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check . && uv run mypy src

up:
	docker compose up -d

down:
	docker compose down

seed-erp:
	uv run python scripts/seed_odoo.py

sync:
	uv run o2c sync

status:
	uv run o2c status
