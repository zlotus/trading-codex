import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from trading_codex.baostock_download.downloader import SocketCounterGate, download
from trading_codex.baostock_download.errors import (
    ProviderBlacklisted,
    ProviderFailure,
    ProviderLockError,
)
from trading_codex.baostock_download.raw_files import QueryRawFileStore
from trading_codex.baostock_download.requests import DownloadRequest
from trading_codex.baostock_download.runtime import (
    DailyAttemptCounter,
    GlobalDownloadLock,
)
from trading_codex.data.models import ProviderBatch, RawIntegrityError
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.raw_processing import (
    ingest_raw_envelopes,
    inspect_raw_envelopes,
)
from trading_codex.data.requirements_cli import _base_daily_requests, _parser


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeResult:
    def __init__(
        self,
        *,
        fields: tuple[str, ...] = (),
        rows: tuple[tuple[str, ...], ...] = (),
        error_code: str = "0",
        error_msg: str = "success",
    ) -> None:
        self.fields = list(fields)
        self.rows = rows
        self.error_code = error_code
        self.error_msg = error_msg
        self.index = 0

    def next(self) -> bool:
        return self.index < len(self.rows)

    def get_row_data(self) -> list[str]:
        row = list(self.rows[self.index])
        self.index += 1
        return row


class FakeSocket:
    def __init__(
        self,
        *,
        codes: dict[str, str] | None = None,
        after_send: Callable[[], None] | None = None,
    ) -> None:
        self.messages: list[str] = []
        self.codes = codes or {}
        self.after_send = after_send

    def send_msg(self, message: str) -> str:
        self.messages.append(message)
        code = self.codes.get(message, "0")
        if self.after_send is not None:
            self.after_send()
        return f"HEAD{code}|result|"


class EofSocket:
    def __init__(self) -> None:
        self.timeout: float | None = None
        self.sent: list[bytes] = []

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def sendall(self, message: bytes) -> None:
        self.sent.append(message)

    def recv(self, _size: int) -> bytes:
        return b""


class FakeBaoStock:
    __version__ = "00.9.30"

    def __init__(self, socket_module: ModuleType, *, fail_query: int | None = None) -> None:
        self.socket_module = socket_module
        self.fail_query = fail_query
        self.queries: list[tuple[str, str]] = []
        self.local_closes = 0

    def login(self) -> FakeResult:
        self.socket_module.send_msg("login")
        return FakeResult()

    def logout(self) -> FakeResult:
        self.socket_module.send_msg("logout")
        return FakeResult()

    def query_trade_dates(self, *, start_date: str, end_date: str) -> FakeResult:
        self.queries.append((start_date, end_date))
        self.socket_module.send_msg("query")
        if self.fail_query == len(self.queries):
            return FakeResult(error_code="1", error_msg="planned failure")
        return FakeResult(
            fields=("calendar_date", "is_trading_day"),
            rows=((start_date, "1"),),
        )

    def close_local_socket(self) -> None:
        self.local_closes += 1


def _request(day: str) -> DownloadRequest:
    return DownloadRequest.from_dict(
        {
            "operation": "trade_calendar",
            "query": {"start_date": day, "end_date": day},
        }
    )


def _daily_request(*, adjustflag: str = "2") -> DownloadRequest:
    return DownloadRequest.from_dict(
        {
            "operation": "daily_bars",
            "query": {
                "code": "sh.600000",
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
                "frequency": "d",
                "adjustflag": adjustflag,
            },
        }
    )


def _daily_row(**overrides: str) -> dict[str, str]:
    row = {
        "date": "2024-01-02",
        "code": "sh.600000",
        "open": "10.0000",
        "high": "10.1000",
        "low": "9.9000",
        "close": "10.0000",
        "preclose": "10.0000",
        "volume": "100",
        "amount": "1000.0000",
        "adjustflag": "2",
        "turn": "0.100000",
        "tradestatus": "1",
        "pctChg": "0.000000",
        "isST": "0",
    }
    row.update(overrides)
    return row


def _persist_daily(
    tmp_path: Path,
    request: DownloadRequest,
    row: dict[str, str],
) -> None:
    QueryRawFileStore(tmp_path / "data").persist(
        request,
        ProviderBatch(
            source="baostock",
            operation=request.operation,
            query=request.raw_query,
            fields=request.expected_fields,
            rows=(row,),
            received_at=datetime(2024, 1, 3, tzinfo=UTC),
        ),
    )


def _protocol() -> ModuleType:
    module = ModuleType("fake_protocol")
    module.MESSAGE_HEADER_LENGTH = 4
    module.MESSAGE_SPLIT = "|"
    return module


def _socket_module(fake: FakeSocket) -> ModuleType:
    module = ModuleType("fake_socket")
    module.send_msg = fake.send_msg
    return module


def _eof_socket_module(fake: EofSocket) -> ModuleType:
    context = ModuleType("fake_context")
    context.default_socket = fake
    module = ModuleType("fake_socket")
    module.context = context
    module.send_msg = lambda _message: "unexpected fallback"
    return module


def _download(
    tmp_path: Path,
    requests: tuple[DownloadRequest, ...],
    *,
    provider: FakeBaoStock,
    socket_module: ModuleType,
    counter: DailyAttemptCounter | None = None,
) -> dict[str, object]:
    return download(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        requests=requests,
        counter=counter,
        provider_module=provider,
        socket_module=socket_module,
        protocol_constants=_protocol(),
    )


def test_download_is_serial_query_file_idempotent_and_raw_only(tmp_path: Path) -> None:
    requests = (_request("2024-01-02"), _request("2024-01-03"))
    fake_socket = FakeSocket()
    socket_module = _socket_module(fake_socket)
    provider = FakeBaoStock(socket_module)

    first = _download(
        tmp_path,
        requests,
        provider=provider,
        socket_module=socket_module,
    )
    assert first["status"] == "passed"
    assert first["downloaded"] == 2
    assert first["network_attempts"] == 4
    assert provider.queries == [("2024-01-02", "2024-01-02"), ("2024-01-03", "2024-01-03")]
    assert all(request.raw_path(tmp_path / "data").is_file() for request in requests)
    assert not (tmp_path / "data" / "normalized").exists()
    assert not (tmp_path / "data" / "manifests").exists()
    assert not (tmp_path / "data" / "reports").exists()

    second_socket = FakeSocket()
    second_module = _socket_module(second_socket)
    second_provider = FakeBaoStock(second_module)
    second = _download(
        tmp_path,
        requests,
        provider=second_provider,
        socket_module=second_module,
    )
    assert second["status"] == "complete"
    assert second["network_access"] is False
    assert second["skipped_existing"] == 2
    assert second_socket.messages == []


def test_socket_eof_fails_instead_of_spinning(tmp_path: Path) -> None:
    fake_socket = EofSocket()
    socket_module = _eof_socket_module(fake_socket)
    gate = SocketCounterGate(
        counter=DailyAttemptCounter(tmp_path / "state"),
        socket_module=socket_module,
        protocol_constants=_protocol(),
    )
    gate.bind_login()

    with gate.installed(), pytest.raises(ProviderFailure, match="closed the socket"):
        socket_module.send_msg("login")

    assert fake_socket.sent == [b"login\n"]
    assert fake_socket.timeout is None
    assert gate.network_attempts == 1


def test_download_import_path_excludes_legacy_and_processing_modules() -> None:
    script = """
import sys
import trading_codex.baostock_download.download_cli
assert 'trading_codex.baostock_download.provider' not in sys.modules
assert 'trading_codex.baostock_download.manifest' not in sys.modules
assert 'pyarrow' not in sys.modules
import trading_codex.baostock_download.downloader
assert 'trading_codex.baostock_download.manifest' not in sys.modules
assert 'pyarrow' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_existing_bad_file_is_skipped_then_offline_inspection_warns(tmp_path: Path) -> None:
    request = _request("2024-01-02")
    path = request.raw_path(tmp_path / "data")
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n", encoding="utf-8")
    fake_socket = FakeSocket()
    socket_module = _socket_module(fake_socket)

    report = _download(
        tmp_path,
        (request,),
        provider=FakeBaoStock(socket_module),
        socket_module=socket_module,
    )
    assert report["status"] == "complete"
    assert report["network_attempts"] == 0
    inspection = inspect_raw_envelopes(tmp_path / "data")
    assert inspection["status"] == "warnings"
    assert inspection["valid"] == 0


def test_offline_inspection_detects_envelope_metadata_tampering(tmp_path: Path) -> None:
    request = _request("2024-01-02")
    fake_socket = FakeSocket()
    socket_module = _socket_module(fake_socket)
    _download(
        tmp_path,
        (request,),
        provider=FakeBaoStock(socket_module),
        socket_module=socket_module,
    )
    path = request.raw_path(tmp_path / "data")
    payload = json.loads(path.read_bytes())
    payload["received_at"] = "2000-01-01T00:00:00Z"
    path.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    report = inspect_raw_envelopes(tmp_path / "data")
    assert report["status"] == "warnings"
    assert report["valid"] == 0
    ingestion = ingest_raw_envelopes(tmp_path / "data")
    assert ingestion["status"] == "warnings"
    assert ingestion["valid_raw_files"] == 0
    assert not (tmp_path / "data" / "normalized").exists()


def test_envelope_address_includes_the_operation_directory(tmp_path: Path) -> None:
    request = _request("2024-01-02")
    fake_socket = FakeSocket()
    socket_module = _socket_module(fake_socket)
    _download(
        tmp_path,
        (request,),
        provider=FakeBaoStock(socket_module),
        socket_module=socket_module,
    )
    original = request.raw_path(tmp_path / "data")
    moved = original.parents[1] / "daily_bars" / original.name
    moved.parent.mkdir(parents=True)
    original.rename(moved)

    report = inspect_raw_envelopes(tmp_path / "data")
    assert report["status"] == "warnings"
    assert report["valid"] == 0


def test_invalid_encoded_envelope_is_not_published(tmp_path: Path) -> None:
    request = _request("2024-01-02")
    batch = ProviderBatch(
        source="baostock",
        operation=request.operation,
        query=request.raw_query,
        fields=request.expected_fields,
        rows=({"calendar_date": "2024-01-02", "is_trading_day": 1},),  # type: ignore[dict-item]
        received_at=datetime(2024, 1, 2, tzinfo=UTC),
    )

    with pytest.raises(RawIntegrityError):
        QueryRawFileStore(tmp_path / "data").persist(request, batch)
    assert not request.raw_path(tmp_path / "data").exists()


def test_error_stops_and_rerun_downloads_only_missing_file(tmp_path: Path) -> None:
    requests = (_request("2024-01-02"), _request("2024-01-03"))
    first_socket = FakeSocket()
    first_module = _socket_module(first_socket)
    with pytest.raises(ProviderFailure, match="planned failure"):
        _download(
            tmp_path,
            requests,
            provider=FakeBaoStock(first_module, fail_query=2),
            socket_module=first_module,
        )
    assert requests[0].raw_path(tmp_path / "data").is_file()
    assert not requests[1].raw_path(tmp_path / "data").exists()

    second_socket = FakeSocket()
    second_module = _socket_module(second_socket)
    provider = FakeBaoStock(second_module)
    report = _download(
        tmp_path,
        requests,
        provider=provider,
        socket_module=second_module,
    )
    assert report["downloaded"] == 1
    assert provider.queries == [("2024-01-03", "2024-01-03")]


def test_daily_counter_pauses_without_retry_and_resumes_next_day(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2024, 1, 1, tzinfo=UTC))
    counter = DailyAttemptCounter(tmp_path / "state", stop_at=3, clock=clock)
    requests = (_request("2024-01-02"), _request("2024-01-03"))
    first_socket = FakeSocket()
    first_module = _socket_module(first_socket)
    first = _download(
        tmp_path,
        requests,
        provider=FakeBaoStock(first_module),
        socket_module=first_module,
        counter=counter,
    )
    assert first["status"] == "paused_daily_limit"
    assert first["downloaded"] == 1
    assert first["attempt_budget"]["stop_at"] == 3
    assert first["attempt_budget"]["official_limit"] == 50_000

    clock.value += timedelta(days=1)
    second_socket = FakeSocket()
    second_module = _socket_module(second_socket)
    second = _download(
        tmp_path,
        requests,
        provider=FakeBaoStock(second_module),
        socket_module=second_module,
        counter=counter,
    )
    assert second["status"] == "passed"
    assert second["downloaded"] == 1


def test_network_attempt_report_is_correct_across_shanghai_midnight(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2024, 1, 1, 15, 59, 59, tzinfo=UTC))

    def advance_clock() -> None:
        clock.value += timedelta(seconds=1)

    fake_socket = FakeSocket(after_send=advance_clock)
    socket_module = _socket_module(fake_socket)
    report = _download(
        tmp_path,
        (_request("2024-01-02"),),
        provider=FakeBaoStock(socket_module),
        socket_module=socket_module,
        counter=DailyAttemptCounter(tmp_path / "state", clock=clock),
    )

    assert report["status"] == "passed"
    assert report["network_attempts"] == 3
    assert report["network_access"] is True


def test_global_lock_rejects_a_second_downloader(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    with GlobalDownloadLock(state_root):
        with pytest.raises(ProviderLockError):
            with GlobalDownloadLock(state_root):
                pass


def test_blacklist_response_writes_marker_and_blocks_later_network(tmp_path: Path) -> None:
    request = _request("2024-01-02")
    first_socket = FakeSocket(codes={"login": "10001011"})
    first_module = _socket_module(first_socket)
    with pytest.raises(ProviderBlacklisted):
        _download(
            tmp_path,
            (request,),
            provider=FakeBaoStock(first_module),
            socket_module=first_module,
        )
    assert (tmp_path / "state" / "provider-blacklisted.json").is_file()

    second_socket = FakeSocket()
    second_module = _socket_module(second_socket)
    with pytest.raises(ProviderBlacklisted, match="marker"):
        _download(
            tmp_path,
            (request,),
            provider=FakeBaoStock(second_module),
            socket_module=second_module,
        )
    assert second_socket.messages == []


def test_offline_envelope_check_and_ingest_are_independently_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request("2024-01-02")
    fake_socket = FakeSocket()
    socket_module = _socket_module(fake_socket)
    _download(
        tmp_path,
        (request,),
        provider=FakeBaoStock(socket_module),
        socket_module=socket_module,
    )
    inspection = inspect_raw_envelopes(tmp_path / "data")
    assert inspection["status"] == "passed"
    first = ingest_raw_envelopes(tmp_path / "data")
    monkeypatch.setattr(
        "trading_codex.data.raw_processing.normalize_batch",
        lambda *_args: pytest.fail("published segment must skip normalization"),
    )
    second = ingest_raw_envelopes(tmp_path / "data")
    assert first["status"] == "passed"
    assert first["datasets"][0]["published_segments"] == 1
    assert second["datasets"][0]["published_segments"] == 0
    assert second["datasets"][0]["skipped"] == 1
    assert ParquetDataStore(tmp_path / "data" / "normalized").read(
        "trade_calendar"
    ).num_rows == 1


def test_ingest_revalidates_an_existing_published_segment(tmp_path: Path) -> None:
    request = _request("2024-01-02")
    fake_socket = FakeSocket()
    socket_module = _socket_module(fake_socket)
    _download(
        tmp_path,
        (request,),
        provider=FakeBaoStock(socket_module),
        socket_module=socket_module,
    )
    first = ingest_raw_envelopes(tmp_path / "data")
    segment = next(
        (tmp_path / "data" / "normalized" / ".segments" / "trade_calendar").iterdir()
    )
    segment.write_bytes(b"not parquet")

    second = ingest_raw_envelopes(tmp_path / "data")

    assert first["status"] == "passed"
    assert second["status"] == "warnings"
    assert "cannot read normalized trade_calendar" in second["warnings"][0]["reason"]


def test_ingest_normalizes_suspended_blank_volume_and_exact_query_price_track(
    tmp_path: Path,
) -> None:
    request = _daily_request(adjustflag="2")
    _persist_daily(
        tmp_path,
        request,
        _daily_row(
            volume="",
            amount="",
            adjustflag="3",
            turn="",
            tradestatus="0",
            pctChg="",
        ),
    )

    report = ingest_raw_envelopes(tmp_path / "data")
    rows = ParquetDataStore(tmp_path / "data" / "normalized").read(
        "daily_bars"
    ).to_pylist()

    assert report["status"] == "passed"
    assert report["warnings"] == []
    assert rows[0]["volume"] == 0
    assert rows[0]["amount"] is None
    assert rows[0]["turnover"] is None
    assert rows[0]["pct_change"] is None
    assert rows[0]["trade_status"] is False
    assert rows[0]["adjustment_flag"] == "2"


def test_ingest_rejects_blank_volume_for_a_trading_daily_bar(tmp_path: Path) -> None:
    request = _daily_request()
    _persist_daily(tmp_path, request, _daily_row(volume=""))

    report = ingest_raw_envelopes(tmp_path / "data")

    assert report["status"] == "warnings"
    assert "blank required provider field: volume" in report["warnings"][0]["reason"]
    assert not (
        tmp_path / "data" / "normalized" / ".segments" / "daily_bars"
    ).exists()


def test_ingest_rejects_unexpected_provider_adjustment_flag_mismatch(
    tmp_path: Path,
) -> None:
    request = _daily_request(adjustflag="3")
    _persist_daily(tmp_path, request, _daily_row(adjustflag="2"))

    report = ingest_raw_envelopes(tmp_path / "data")

    assert report["status"] == "warnings"
    assert "differs from exact query" in report["warnings"][0]["reason"]


def test_ingest_deduplicates_identical_cross_payload_business_key_overlap(
    tmp_path: Path,
) -> None:
    requests = (
        DownloadRequest.from_dict(
            {
                "operation": "trade_calendar",
                "query": {"start_date": "2024-01-01", "end_date": "2024-01-02"},
            }
        ),
        DownloadRequest.from_dict(
            {
                "operation": "trade_calendar",
                "query": {"start_date": "2024-01-02", "end_date": "2024-01-03"},
            }
        ),
    )
    store = QueryRawFileStore(tmp_path / "data")
    for request in requests:
        store.persist(
            request,
            ProviderBatch(
                source="baostock",
                operation=request.operation,
                query=request.raw_query,
                fields=request.expected_fields,
                rows=({"calendar_date": "2024-01-02", "is_trading_day": "1"},),
                received_at=datetime(2024, 1, 3, tzinfo=UTC),
            ),
        )

    report = ingest_raw_envelopes(tmp_path / "data")

    assert report["status"] == "passed"
    assert report["datasets"][0]["published_segments"] == 1
    assert report["datasets"][0]["skipped"] == 1
    assert report["datasets"][0]["deduplicated_rows"] == 1
    assert report["datasets"][0]["conflicting_rows"] == 0
    assert report["warnings"] == []
    assert ParquetDataStore(tmp_path / "data" / "normalized").read(
        "trade_calendar"
    ).num_rows == 1


def test_ingest_keeps_non_overlapping_rows_from_overlapping_payloads(
    tmp_path: Path,
) -> None:
    requests = (
        DownloadRequest.from_dict(
            {
                "operation": "trade_calendar",
                "query": {"start_date": "2024-01-01", "end_date": "2024-01-02"},
            }
        ),
        DownloadRequest.from_dict(
            {
                "operation": "trade_calendar",
                "query": {"start_date": "2024-01-02", "end_date": "2024-01-03"},
            }
        ),
    )
    rows_by_request = (
        (
            {"calendar_date": "2024-01-01", "is_trading_day": "1"},
            {"calendar_date": "2024-01-02", "is_trading_day": "1"},
        ),
        (
            {"calendar_date": "2024-01-02", "is_trading_day": "1"},
            {"calendar_date": "2024-01-03", "is_trading_day": "1"},
        ),
    )
    store = QueryRawFileStore(tmp_path / "data")
    for request, rows in zip(requests, rows_by_request, strict=True):
        store.persist(
            request,
            ProviderBatch(
                source="baostock",
                operation=request.operation,
                query=request.raw_query,
                fields=request.expected_fields,
                rows=rows,
                received_at=datetime(2024, 1, 4, tzinfo=UTC),
            ),
        )

    report = ingest_raw_envelopes(tmp_path / "data")
    normalized = ParquetDataStore(tmp_path / "data" / "normalized").read(
        "trade_calendar"
    )

    assert report["status"] == "passed"
    assert report["datasets"][0]["published_segments"] == 2
    assert report["datasets"][0]["rows"] == 3
    assert report["datasets"][0]["deduplicated_rows"] == 1
    assert report["datasets"][0]["conflicting_rows"] == 0
    assert report["warnings"] == []
    assert normalized["calendar_date"].to_pylist() == [
        date(2024, 1, 1),
        date(2024, 1, 2),
        date(2024, 1, 3),
    ]


def test_ingest_rejects_conflicting_cross_payload_business_key_overlap(
    tmp_path: Path,
) -> None:
    requests = (
        DownloadRequest.from_dict(
            {
                "operation": "trade_calendar",
                "query": {"start_date": "2024-01-01", "end_date": "2024-01-02"},
            }
        ),
        DownloadRequest.from_dict(
            {
                "operation": "trade_calendar",
                "query": {"start_date": "2024-01-02", "end_date": "2024-01-03"},
            }
        ),
    )
    rows_by_request = (
        (
            {"calendar_date": "2024-01-01", "is_trading_day": "1"},
            {"calendar_date": "2024-01-02", "is_trading_day": "1"},
        ),
        (
            {"calendar_date": "2024-01-02", "is_trading_day": "0"},
            {"calendar_date": "2024-01-03", "is_trading_day": "1"},
        ),
    )
    store = QueryRawFileStore(tmp_path / "data")
    for request, rows in zip(requests, rows_by_request, strict=True):
        store.persist(
            request,
            ProviderBatch(
                source="baostock",
                operation=request.operation,
                query=request.raw_query,
                fields=request.expected_fields,
                rows=rows,
                received_at=datetime(2024, 1, 4, tzinfo=UTC),
            ),
        )

    report = ingest_raw_envelopes(tmp_path / "data")
    normalized = ParquetDataStore(tmp_path / "data" / "normalized").read(
        "trade_calendar"
    )

    assert report["status"] == "warnings"
    assert report["datasets"][0]["published_segments"] == 1
    assert report["datasets"][0]["conflicting_rows"] == 1
    assert "business keys conflict" in report["warnings"][0]["reason"]
    assert normalized["calendar_date"].to_pylist() == [
        date(2024, 1, 1),
        date(2024, 1, 2),
    ]


def test_base_daily_requirements_use_index_union_and_both_price_tracks(
    tmp_path: Path,
) -> None:
    store = ParquetDataStore(tmp_path / "data" / "normalized")
    received = datetime(2024, 6, 8, tzinfo=UTC)
    common = {
        "snapshot_date": date(2024, 6, 7),
        "member_name": "sample",
        "available_at": received,
        "source": "test",
        "source_received_at": received,
        "source_payload_sha256": "a" * 64,
        "raw_artifact": "sample.json",
    }
    store.merge(
        "index_memberships",
        [
            {
                **common,
                "index_code": "sh.000300",
                "member_code": "sh.600000",
            },
            {
                **common,
                "index_code": "sh.000905",
                "member_code": "sz.000001",
            },
        ],
    )
    requests = _base_daily_requests(
        data_root=tmp_path / "data",
        snapshot_date=None,
        start_date=date(2011, 1, 1),
        end_date=date(2026, 8, 10),
    )
    daily = [request for request in requests if request["operation"] == "daily_bars"]
    assert len(daily) == 4
    assert {request["query"]["adjustflag"] for request in daily} == {"2", "3"}
    assert requests[0]["operation"] == "instruments"
    assert requests[0]["query"] == {"code": ""}
    assert requests[1]["operation"] == "trade_calendar"
    assert requests[1]["query"]["end_date"] == "2026-08-10"


def test_base_daily_cli_defaults_freeze_the_completed_m8_baseline() -> None:
    args = _parser().parse_args(
        ["base-daily", "--data-root", "/tmp/trading-codex-test-data"]
    )

    assert args.start_date == date(2011, 1, 1)
    assert args.end_date == date(2026, 8, 10)
