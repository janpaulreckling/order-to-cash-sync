.PHONY: install test lint up down sync status

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

sync:
	uv run o2c sync

status:
	uv run o2c status
