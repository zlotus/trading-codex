import httpx2
import pytest

from trading_codex.main import app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> httpx2.AsyncClient:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest.mark.anyio
async def test_health(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Trading Codex API",
        "version": "0.1.0",
    }


@pytest.mark.anyio
async def test_system_status_exposes_unconfigured_boundaries(
    client: httpx2.AsyncClient,
) -> None:
    response = await client.get("/api/v1/system/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "scaffold"
    assert {component["key"] for component in payload["components"]} == {
        "historical_data",
        "realtime_quotes",
        "backtest",
        "ai",
    }
    assert all(component["state"] == "not_configured" for component in payload["components"])
