import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import trading_codex.baostock_download.offline as offline_module
from trading_codex.baostock_download import online as online_module
from trading_codex.baostock_download.errors import (
    BudgetExceeded,
    OfflineSyncError,
    ProviderBlacklisted,
    SchemaDriftError,
    StateError,
)
from trading_codex.baostock_download.manifest import create_manifest
from trading_codex.baostock_download.offline import (
    data_root_lock,
    sync_manifest,
    verify_manifest,
)
from trading_codex.baostock_download.online import fetch_manifest
from trading_codex.baostock_download.state import StateStore
from trading_codex.baostock_download.storage import StorageGuard, StorageThresholds
from trading_codex.data.cli import main as legacy_data_main
from trading_codex.data.models import DataValidationError, ProviderBatch
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.raw_store import ImmutableRawStore
from trading_codex.data.sync import IngestionPipeline


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeResult:
    def __init__(
        self,
        *,
        fields: tuple[str, ...] = (),
        pages: tuple[tuple[tuple[str, ...], ...], ...] = ((),),
        socket_module: ModuleType | None = None,
        error_code: str = "0",
        error_msg: str = "success",
    ) -> None:
        self.fields = list(fields)
        self.pages = pages
        self.socket_module = socket_module
        self.error_code = error_code
        self.error_msg = error_msg
        self.page = 0
        self.row = 0

    def next(self) -> bool:
        if self.row < len(self.pages[self.page]):
            return True
        if self.page + 1 >= len(self.pages):
            return False
        assert self.socket_module is not None
        response = self.socket_module.send_msg("page")
        self.error_code, self.error_msg = _decode(response)
        if self.error_code != "0":
            return False
        self.page += 1
        self.row = 0
        return bool(self.pages[self.page])

    def get_row_data(self) -> list[str]:
        row = list(self.pages[self.page][self.row])
        self.row += 1
        return row


class FakeSocket:
    def __init__(self, codes: dict[str, str] | None = None) -> None:
        self.codes = codes or {}
        self.messages: list[str] = []

    def send_msg(self, message: str) -> str:
        self.messages.append(message)
        code = self.codes.get(message, "0")
        text = "blacklisted" if code == "10001011" else "success"
        return f"HEAD{code}|{text}|"


class FakeBaoStockModule:
    __version__ = "00.9.30"

    def __init__(
        self,
        socket_module: ModuleType,
        *,
        fields: tuple[str, ...] = ("calendar_date", "is_trading_day"),
        pages: tuple[tuple[tuple[str, ...], ...], ...] | None = None,
    ) -> None:
        self.socket_module = socket_module
        self.fields = fields
        self.pages = pages or ((("2024-01-02", "1"),),)
        self.local_closes = 0

    def login(self) -> FakeResult:
        code, message = _decode(self.socket_module.send_msg("login"))
        return FakeResult(error_code=code, error_msg=message)

    def logout(self) -> FakeResult:
        code, message = _decode(self.socket_module.send_msg("logout"))
        return FakeResult(error_code=code, error_msg=message)

    def query_trade_dates(self, *, start_date: str, end_date: str) -> FakeResult:
        assert start_date <= end_date
        code, message = _decode(self.socket_module.send_msg("query"))
        return FakeResult(
            fields=self.fields,
            pages=self.pages,
            socket_module=self.socket_module,
            error_code=code,
            error_msg=message,
        )

    def close_local_socket(self) -> None:
        self.local_closes += 1


def _decode(response: str) -> tuple[str, str]:
    code, message, _ = response[4:].split("|", maxsplit=2)
    return code, message


def _protocol() -> ModuleType:
    protocol = ModuleType("fake_protocol")
    protocol.MESSAGE_HEADER_LENGTH = 4
    protocol.MESSAGE_SPLIT = "|"
    return protocol


def _socket(fake: FakeSocket) -> ModuleType:
    module = ModuleType("fake_socket")
    module.send_msg = fake.send_msg
    return module


def _manifest(*, max_pages: int = 1):
    return create_manifest(
        {
            "created_by": "test-operator",
            "limits": {
                "calendar_day_attempts": 100,
                "rolling_24h_attempts": 100,
                "session_attempts": 10,
                "minimum_interval_seconds": 3,
            },
            "items": [
                {
                    "operation": "trade_calendar",
                    "query": {
                        "start_date": "2024-01-01",
                        "end_date": "2024-01-03",
                    },
                    "max_pages": max_pages,
                    "max_attempts": max_pages,
                }
            ],
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    ).frozen()


def _environment(tmp_path: Path):
    data_root = tmp_path / "data"
    storage = StorageGuard(
        data_root=data_root,
        thresholds=StorageThresholds(
            warn_free_bytes=0,
            fail_free_bytes=0,
            peak_reserve_bytes=0,
            max_used_percent=100,
        ),
    )
    storage.preflight(initialize=True)
    clock = MutableClock(datetime(2024, 1, 1, tzinfo=UTC))
    state = StateStore(tmp_path / "global", clock=clock, sleep=clock.sleep)
    state.initialize()
    return data_root, storage, state, clock


def test_fetch_counts_login_query_logout_and_cache_hit_is_zero_network(
    tmp_path: Path,
) -> None:
    data_root, storage, state, _ = _environment(tmp_path)
    manifest = _manifest()
    fake_socket = FakeSocket()
    socket_module = _socket(fake_socket)
    provider = FakeBaoStockModule(socket_module)

    report = fetch_manifest(
        data_root=data_root,
        manifest=manifest,
        confirmed_sha256=manifest.manifest_sha256,
        state=state,
        storage=storage,
        provider_module=provider,
        socket_module=socket_module,
        protocol_constants=_protocol(),
    )

    assert report["status"] == "passed"
    assert fake_socket.messages == ["login", "query", "logout"]
    connection = state._connect()
    try:
        attempts = connection.execute(
            "SELECT kind FROM attempts ORDER BY sequence"
        ).fetchall()
        results = connection.execute(
            "SELECT status, provider_code FROM attempt_results ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    assert [row["kind"] for row in attempts] == ["login", "query", "logout"]
    assert [(row["status"], row["provider_code"]) for row in results] == [
        ("succeeded", "0"),
        ("succeeded", "0"),
        ("succeeded", "0"),
    ]

    second_socket = FakeSocket()
    second_module = _socket(second_socket)
    cached = fetch_manifest(
        data_root=data_root,
        manifest=manifest,
        confirmed_sha256=manifest.manifest_sha256,
        state=state,
        storage=storage,
        provider_module=FakeBaoStockModule(second_module),
        socket_module=second_module,
        protocol_constants=_protocol(),
    )
    assert cached["network_attempts"] == 0
    assert second_socket.messages == []


def test_fetch_cannot_overlap_an_offline_data_root_operation(tmp_path: Path) -> None:
    data_root, storage, state, _ = _environment(tmp_path)
    manifest = _manifest()
    fake = FakeSocket()
    socket_module = _socket(fake)

    with data_root_lock(data_root):
        with pytest.raises(OfflineSyncError, match="data-root lock"):
            fetch_manifest(
                data_root=data_root,
                manifest=manifest,
                confirmed_sha256=manifest.manifest_sha256,
                state=state,
                storage=storage,
                provider_module=FakeBaoStockModule(socket_module),
                socket_module=socket_module,
                protocol_constants=_protocol(),
            )

    assert fake.messages == []


def test_fetch_sync_verify_runs_end_to_end_without_provider_in_offline_steps(
    tmp_path: Path,
) -> None:
    data_root, storage, state, _ = _environment(tmp_path)
    manifest = _manifest()
    fake = FakeSocket()
    socket_module = _socket(fake)
    fetch_manifest(
        data_root=data_root,
        manifest=manifest,
        confirmed_sha256=manifest.manifest_sha256,
        state=state,
        storage=storage,
        provider_module=FakeBaoStockModule(socket_module),
        socket_module=socket_module,
        protocol_constants=_protocol(),
    )

    sys.modules.pop("baostock", None)
    sync = sync_manifest(data_root=data_root, manifest=manifest, state=state)
    verification = verify_manifest(
        data_root=data_root,
        manifest=manifest,
        state=state,
        as_of=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert sync["status"] == "passed"
    assert verification["status"] == "passed"
    assert verification["issues"] == []
    assert "baostock" not in sys.modules
    assert list(
        (data_root / "normalized" / ".segments" / "trade_calendar").glob(
            "*.parquet"
        )
    )
    assert list((data_root / "manifests" / "completed").glob("*.receipt.json"))


def test_daily_bar_sync_quantizes_real_adjusted_prices_and_keeps_blank_turnover(
    tmp_path: Path,
) -> None:
    data_root, _, state, _ = _environment(tmp_path)
    manifest = create_manifest(
        {
            "created_by": "test-operator",
            "items": [
                {
                    "operation": "daily_bars",
                    "query": {
                        "code": "sh.600000",
                        "start_date": "2024-01-02",
                        "end_date": "2024-01-03",
                        "frequency": "d",
                        "adjustflag": "2",
                    },
                }
            ],
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    ).frozen()
    item = manifest.items[0]
    ImmutableRawStore(data_root / "raw").persist(
        ProviderBatch(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
            fields=item.expected_fields,
            rows=(
                {
                    "date": "2024-01-02",
                    "code": "sh.600000",
                    "open": "3.6152757000",
                    "high": "3.6909575000",
                    "low": "3.5803445000",
                    "close": "3.6705818500",
                    "preclose": "3.6065431500",
                    "volume": "104301454",
                    "amount": "1307174907.0000",
                    "adjustflag": "2",
                    "turn": "0.908624",
                    "tradestatus": "1",
                    "pctChg": "1.775600",
                    "isST": "0",
                },
                {
                    "date": "2024-01-03",
                    "code": "sh.600000",
                    "open": "3.6705818500",
                    "high": "3.6705818500",
                    "low": "3.6705818500",
                    "close": "3.6705818500",
                    "preclose": "3.6705818500",
                    "volume": "0",
                    "amount": "0.0000",
                    "adjustflag": "2",
                    "turn": "",
                    "tradestatus": "0",
                    "pctChg": "0.000000",
                    "isST": "0",
                },
            ),
            received_at=datetime(2024, 1, 4, tzinfo=UTC),
        )
    )

    sync = sync_manifest(data_root=data_root, manifest=manifest, state=state)
    verification = verify_manifest(
        data_root=data_root,
        manifest=manifest,
        state=state,
        as_of=datetime(2030, 1, 1, tzinfo=UTC),
    )
    rows = ParquetDataStore(data_root / "normalized").read("daily_bars").to_pylist()

    assert sync["status"] == "passed"
    assert verification["status"] == "passed"
    assert rows[0]["open"] == Decimal("3.615276")
    assert rows[0]["high"] == Decimal("3.690958")
    assert rows[0]["low"] == Decimal("3.580344")
    assert rows[0]["close"] == Decimal("3.670582")
    assert rows[1]["turnover"] is None


def test_schema_drift_is_quarantined_and_stops_without_logout(tmp_path: Path) -> None:
    data_root, storage, state, _ = _environment(tmp_path)
    manifest = _manifest()
    fake = FakeSocket()
    socket_module = _socket(fake)
    provider = FakeBaoStockModule(
        socket_module,
        fields=("unexpected",),
        pages=((('value',),),),
    )

    with pytest.raises(SchemaDriftError):
        fetch_manifest(
            data_root=data_root,
            manifest=manifest,
            confirmed_sha256=manifest.manifest_sha256,
            state=state,
            storage=storage,
            provider_module=provider,
            socket_module=socket_module,
            protocol_constants=_protocol(),
        )

    assert fake.messages == ["login", "query"]
    assert provider.local_closes == 1
    assert list((data_root / "quarantine").rglob("*.json"))
    assert not list((data_root / "raw" / "baostock").rglob("*.json"))


def test_blacklist_response_is_persistent_and_never_sends_logout(tmp_path: Path) -> None:
    data_root, storage, state, clock = _environment(tmp_path)
    manifest = _manifest()
    fake = FakeSocket({"query": "10001011"})
    socket_module = _socket(fake)
    provider = FakeBaoStockModule(socket_module)

    with pytest.raises(ProviderBlacklisted):
        fetch_manifest(
            data_root=data_root,
            manifest=manifest,
            confirmed_sha256=manifest.manifest_sha256,
            state=state,
            storage=storage,
            provider_module=provider,
            socket_module=socket_module,
            protocol_constants=_protocol(),
        )

    assert fake.messages == ["login", "query"]
    assert provider.local_closes == 1
    clock.sleep(48 * 60 * 60)
    with pytest.raises(StateError, match="provider_blacklisted"):
        state.assert_fetch_ready()
    mirror = data_root / "state" / "request-audit.sqlite"
    assert mirror.is_file()


@pytest.mark.parametrize(
    ("phase", "messages"),
    [
        ("login", ["login"]),
        ("logout", ["login", "query", "logout"]),
    ],
)
def test_login_and_logout_blacklists_preserve_the_hard_stop_error(
    tmp_path: Path,
    phase: str,
    messages: list[str],
) -> None:
    data_root, storage, state, _ = _environment(tmp_path)
    manifest = _manifest()
    fake = FakeSocket({phase: "10001011"})
    socket_module = _socket(fake)
    provider = FakeBaoStockModule(socket_module)

    with pytest.raises(ProviderBlacklisted):
        fetch_manifest(
            data_root=data_root,
            manifest=manifest,
            confirmed_sha256=manifest.manifest_sha256,
            state=state,
            storage=storage,
            provider_module=provider,
            socket_module=socket_module,
            protocol_constants=_protocol(),
        )

    assert fake.messages == messages
    assert provider.local_closes == 1
    with pytest.raises(StateError, match="provider_blacklisted"):
        state.assert_fetch_ready()
    connection = state._connect()
    try:
        terminal = connection.execute(
            "SELECT event FROM session_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()["event"]
    finally:
        connection.close()
    assert terminal == "blacklisted"


def test_high_level_blacklist_detection_still_persists_the_hard_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, storage, state, _ = _environment(tmp_path)
    manifest = _manifest()
    fake = FakeSocket({"query": "10001011"})
    socket_module = _socket(fake)
    provider = FakeBaoStockModule(socket_module)
    original = online_module.SocketAttemptGate._response_code

    def miss_blacklist_code(self: object, response: object) -> str | None:
        if isinstance(response, str) and "10001011" in response:
            return None
        return original(self, response)  # type: ignore[arg-type]

    monkeypatch.setattr(
        online_module.SocketAttemptGate,
        "_response_code",
        miss_blacklist_code,
    )

    with pytest.raises(ProviderBlacklisted):
        fetch_manifest(
            data_root=data_root,
            manifest=manifest,
            confirmed_sha256=manifest.manifest_sha256,
            state=state,
            storage=storage,
            provider_module=provider,
            socket_module=socket_module,
            protocol_constants=_protocol(),
        )

    assert fake.messages == ["login", "query"]
    with pytest.raises(StateError, match="provider_blacklisted"):
        state.assert_fetch_ready()


def test_page_limit_blocks_extra_send_and_failed_attempts_are_not_retried(
    tmp_path: Path,
) -> None:
    data_root, storage, state, _ = _environment(tmp_path)
    manifest = _manifest(max_pages=1)
    fake = FakeSocket()
    socket_module = _socket(fake)
    provider = FakeBaoStockModule(
        socket_module,
        pages=((("2024-01-02", "1"),), (("2024-01-03", "1"),)),
    )

    with pytest.raises(BudgetExceeded, match="page limit"):
        fetch_manifest(
            data_root=data_root,
            manifest=manifest,
            confirmed_sha256=manifest.manifest_sha256,
            state=state,
            storage=storage,
            provider_module=provider,
            socket_module=socket_module,
            protocol_constants=_protocol(),
        )

    assert fake.messages == ["login", "query"]
    with pytest.raises(Exception, match="reviewed retry manifest"):
        fetch_manifest(
            data_root=data_root,
            manifest=manifest,
            confirmed_sha256=manifest.manifest_sha256,
            state=state,
            storage=storage,
            provider_module=provider,
            socket_module=socket_module,
            protocol_constants=_protocol(),
        )


def test_offline_sync_quarantines_conflicting_business_values(tmp_path: Path) -> None:
    data_root, _, state, _ = _environment(tmp_path)
    first = _manifest()
    raw = ImmutableRawStore(data_root / "raw")
    item = first.items[0]
    original = ProviderBatch(
        source="baostock",
        operation=item.operation,
        query=item.raw_query,
        fields=item.expected_fields,
        rows=(
            {"calendar_date": "2024-01-02", "is_trading_day": "1"},
        ),
        received_at=datetime(2024, 1, 3, tzinfo=UTC),
    )
    raw.persist(original)
    sync_manifest(data_root=data_root, manifest=first, state=state)
    published_path = next(
        (data_root / "normalized" / ".segments" / "trade_calendar").glob(
            "*.parquet"
        )
    )
    published = published_path.read_bytes()

    second = create_manifest(
        {
            "created_by": "test",
            "items": [
                {
                    "operation": "trade_calendar",
                    "query": {
                        "start_date": "2024-01-02",
                        "end_date": "2024-01-04",
                    },
                }
            ],
        },
        created_at=datetime(2024, 1, 2, tzinfo=UTC),
    ).frozen()
    second_item = second.items[0]
    conflicting = ProviderBatch(
        source="baostock",
        operation=second_item.operation,
        query=second_item.raw_query,
        fields=second_item.expected_fields,
        rows=(
            {"calendar_date": "2024-01-02", "is_trading_day": "0"},
        ),
        received_at=datetime(2024, 1, 4, tzinfo=UTC),
    )
    raw.persist(conflicting)

    with pytest.raises(OfflineSyncError, match="key conflict"):
        sync_manifest(data_root=data_root, manifest=second, state=state)

    assert published_path.read_bytes() == published
    assert list((data_root / "quarantine").rglob("*.json"))


def test_interrupted_segment_publish_preserves_existing_normalized_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, _, state, _ = _environment(tmp_path)
    normalized = ParquetDataStore(data_root / "normalized")
    legacy_raw = ImmutableRawStore(data_root / "legacy-raw")
    IngestionPipeline(legacy_raw, normalized).ingest(
        ProviderBatch(
            source="baostock",
            operation="trade_calendar",
            query={"start_date": "2024-01-01", "end_date": "2024-01-01"},
            fields=("calendar_date", "is_trading_day"),
            rows=({"calendar_date": "2024-01-01", "is_trading_day": "0"},),
            received_at=datetime(2024, 1, 2, tzinfo=UTC),
        )
    )
    baseline_path = normalized.path_for("trade_calendar")
    baseline = baseline_path.read_bytes()
    manifest = _manifest()
    item = manifest.items[0]
    ImmutableRawStore(data_root / "raw").persist(
        ProviderBatch(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
            fields=item.expected_fields,
            rows=({"calendar_date": "2024-01-02", "is_trading_day": "1"},),
            received_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
    )

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"simulated interruption before {destination}")

    monkeypatch.setattr(offline_module.os, "replace", fail_replace)
    with pytest.raises(OfflineSyncError, match="atomic segment publish"):
        sync_manifest(data_root=data_root, manifest=manifest, state=state)

    assert baseline_path.read_bytes() == baseline
    assert normalized.segment_paths("trade_calendar") == []


def test_parquet_read_rejects_physical_schema_drift(tmp_path: Path) -> None:
    data_root, _, state, _ = _environment(tmp_path)
    manifest = _manifest()
    item = manifest.items[0]
    ImmutableRawStore(data_root / "raw").persist(
        ProviderBatch(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
            fields=item.expected_fields,
            rows=({"calendar_date": "2024-01-02", "is_trading_day": "1"},),
            received_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
    )
    sync_manifest(data_root=data_root, manifest=manifest, state=state)
    store = ParquetDataStore(data_root / "normalized")
    segment = store.segment_paths("trade_calendar")[0]
    table = pq.read_table(segment)
    pq.write_table(table.append_column("unexpected", pa.array(["drift"])), segment)

    with pytest.raises(DataValidationError, match="schema mismatch"):
        store.read("trade_calendar")


def test_parquet_read_rejects_duplicate_keys_across_segments(tmp_path: Path) -> None:
    data_root, _, state, _ = _environment(tmp_path)
    manifest = _manifest()
    item = manifest.items[0]
    ImmutableRawStore(data_root / "raw").persist(
        ProviderBatch(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
            fields=item.expected_fields,
            rows=({"calendar_date": "2024-01-02", "is_trading_day": "1"},),
            received_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
    )
    sync_manifest(data_root=data_root, manifest=manifest, state=state)
    store = ParquetDataStore(data_root / "normalized")
    segment = store.segment_paths("trade_calendar")[0]
    duplicate = store.segment_path("trade_calendar", "f" * 64)
    duplicate.write_bytes(segment.read_bytes())

    with pytest.raises(DataValidationError, match="duplicate business keys"):
        store.read("trade_calendar")


def test_verify_detects_missing_rows_from_an_expected_raw_payload(tmp_path: Path) -> None:
    data_root, _, state, _ = _environment(tmp_path)
    manifest = _manifest()
    item = manifest.items[0]
    ImmutableRawStore(data_root / "raw").persist(
        ProviderBatch(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
            fields=item.expected_fields,
            rows=(
                {"calendar_date": "2024-01-01", "is_trading_day": "0"},
                {"calendar_date": "2024-01-02", "is_trading_day": "1"},
            ),
            received_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
    )
    sync_manifest(data_root=data_root, manifest=manifest, state=state)
    store = ParquetDataStore(data_root / "normalized")
    segment = store.segment_paths("trade_calendar")[0]
    table = pq.read_table(segment)
    pq.write_table(table.slice(0, 1), segment)

    report = verify_manifest(
        data_root=data_root,
        manifest=manifest,
        state=state,
        as_of=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert report["status"] == "failed"
    assert "missing_normalized_rows:trade_calendar:1" in report["issues"]


def test_completion_receipt_and_its_report_are_immutable(tmp_path: Path) -> None:
    data_root, _, state, _ = _environment(tmp_path)
    manifest = _manifest()
    item = manifest.items[0]
    ImmutableRawStore(data_root / "raw").persist(
        ProviderBatch(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
            fields=item.expected_fields,
            rows=({"calendar_date": "2024-01-02", "is_trading_day": "1"},),
            received_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
    )
    sync_manifest(data_root=data_root, manifest=manifest, state=state)
    first = verify_manifest(
        data_root=data_root,
        manifest=manifest,
        state=state,
        as_of=datetime(2030, 1, 1, tzinfo=UTC),
    )
    receipt_path = Path(first["completion_receipt"])
    receipt = receipt_path.read_bytes()
    recorded = json.loads(receipt)
    report_path = data_root / recorded["report"]
    report = report_path.read_bytes()

    with pytest.raises(OfflineSyncError, match="completion receipt"):
        verify_manifest(
            data_root=data_root,
            manifest=manifest,
            state=state,
            as_of=datetime(2031, 1, 1, tzinfo=UTC),
        )

    assert receipt_path.read_bytes() == receipt
    assert report_path.read_bytes() == report


def test_legacy_fetch_missing_flag_is_permanently_disabled(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        legacy_data_main(
            [
                "--data-root",
                str(tmp_path),
                "sync",
                "--start-date",
                "2024-01-01",
                "--end-date",
                "2024-01-02",
                "--codes",
                "sh.600000",
                "--fetch-missing",
            ]
        )
    assert exit_info.value.code == 2
