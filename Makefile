.PHONY: install dev-api dev-web test lint build-web

install:
	uv sync
	pnpm --dir web install

dev-api:
	uv run uvicorn trading_codex.main:app --app-dir backend/src --reload --host 127.0.0.1 --port 8000

dev-web:
	pnpm --dir web dev --host 127.0.0.1

test:
	uv run pytest

lint:
	uv run ruff check .

build-web:
	pnpm --dir web build
