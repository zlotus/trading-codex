# Trading Codex

Trading Codex is a personal, AI-assisted A-share decision system. It combines
point-in-time historical research, opening-session market snapshots, auditable
strategy allocation, deterministic risk controls, and manually reconciled
positions.

The repository currently contains the initial application scaffold. Market
data, trading strategies, the backtest adapter, persistence, and AI providers
are intentionally not connected yet.

## Repository Layout

```text
backend/            FastAPI application and Python domain modules
web/                React decision-workspace shell
data/               Local market-data layout; payloads are ignored by Git
docs/               Product context, implementation plan, progress, and ADRs
artifacts/          Generated backtest and experiment output
```

## Local Development

Prerequisites: Python 3.12, `uv`, Node.js, and `pnpm`.

```bash
uv sync
pnpm --dir web install
```

Run the API and web application in separate terminals:

```bash
make dev-api
make dev-web
```

- Web: <http://127.0.0.1:5173>
- API documentation: <http://127.0.0.1:8000/docs>

## Checks

```bash
make test
make lint
make build-web
```

Read [`docs/implementation-plan.md`](docs/implementation-plan.md) before
starting feature work.
