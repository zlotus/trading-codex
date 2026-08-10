import json
import multiprocessing
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trading_codex.baostock_download.cli import _parser, _run
from trading_codex.baostock_download.constants import (
    BLACKLIST_RULES_SHA256,
    provider_rule_snapshot,
)
from trading_codex.baostock_download.errors import (
    BudgetExceeded,
    ManifestError,
    OfflineSyncError,
    ProviderLockError,
    StateError,
    StoragePreflightError,
)
from trading_codex.baostock_download.manifest import (
    RequestLimits,
    create_manifest,
    freeze_manifest,
    load_manifest,
    write_draft,
)
from trading_codex.baostock_download.state import (
    GlobalProviderLock,
    StateStore,
    _check_global_capacity,
    _check_provider_capacity,
)
from trading_codex.baostock_download.storage import (
    StorageGuard,
    StorageThresholds,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _spec(*, limits: dict[str, int | float] | None = None) -> dict[str, object]:
    return {
        "created_by": "test-operator",
        "boundaries": {
            "warmup": None,
            "train": "2024-01-01/2024-01-31",
            "validation": None,
            "test": "2024-02-01/2024-02-29",
        },
        "limits": limits or {},
        "estimated_peak_bytes": 1024,
        "items": [
            {
                "key": "calendar",
                "operation": "trade_calendar",
                "query": {
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-03",
                },
                "max_pages": 1,
                "max_attempts": 1,
            },
            {
                "key": "universe",
                "operation": "historical_universe",
                "query": {"day": "2024-01-02"},
                "dependencies": ["calendar"],
            },
        ],
    }


def _test_storage(data_root: Path) -> StorageGuard:
    return StorageGuard(
        data_root=data_root,
        thresholds=StorageThresholds(
            warn_free_bytes=0,
            fail_free_bytes=0,
            peak_reserve_bytes=0,
            max_used_percent=100,
        ),
    )


def test_manifest_hash_is_deterministic_and_freeze_is_immutable(tmp_path: Path) -> None:
    first = create_manifest(
        _spec(), created_at=datetime(2024, 1, 1, tzinfo=UTC)
    )
    second = create_manifest(
        _spec(), created_at=datetime(2025, 1, 1, tzinfo=UTC)
    )

    assert first.manifest_sha256 == second.manifest_sha256
    assert first.manifest_id == second.manifest_id
    assert first.items[1].dependencies == (first.items[0].item_id,)
    assert first.items[0].raw_query["_provider_client_version"] == "00.9.30"
    assert first.provider_rules_sha256 == BLACKLIST_RULES_SHA256

    data_root = tmp_path / "data"
    _test_storage(data_root).preflight(initialize=True)
    draft_path = write_draft(data_root, first)
    frozen, frozen_path = freeze_manifest(data_root, draft_path)

    assert frozen.status == "frozen"
    assert frozen.manifest_sha256 == first.manifest_sha256
    assert load_manifest(frozen_path) == frozen

    payload = frozen_path.read_text(encoding="utf-8").replace(
        '"calendar_day_attempts":2000', '"calendar_day_attempts":2001'
    )
    frozen_path.write_text(payload, encoding="utf-8")
    with pytest.raises(ManifestError, match="SHA-256"):
        load_manifest(frozen_path)


def test_manifest_file_requires_canonical_json_without_duplicate_keys(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    _test_storage(data_root).preflight(initialize=True)
    manifest = create_manifest(
        _spec(), created_at=datetime(2024, 1, 1, tzinfo=UTC)
    )
    draft = write_draft(data_root, manifest)
    _, frozen_path = freeze_manifest(data_root, draft)
    payload = json.loads(frozen_path.read_bytes())
    frozen_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(ManifestError, match="canonical JSON"):
        load_manifest(frozen_path)

    frozen_path.write_text(
        '{"schema_version":1,"schema_version":1}\n',
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="not valid JSON"):
        load_manifest(frozen_path)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"minimum_interval_seconds": 2.99}, "cannot be lower"),
        ({"minimum_interval_seconds": float("nan")}, "must be finite"),
        ({"calendar_day_attempts": 45_001}, "hard limit"),
        ({"rolling_24h_attempts": 0}, "positive"),
    ],
)
def test_manifest_limits_cannot_raise_safety_boundaries(
    change: dict[str, int | float], match: str
) -> None:
    with pytest.raises(ManifestError, match=match):
        create_manifest(
            _spec(limits=change),
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"calendar_day_attempts": True}, "must be an integer"),
        ({"rolling_24h_attempts": 1.5}, "must be an integer"),
        ({"session_attempts": "10"}, "must be an integer"),
        ({"minimum_interval_seconds": False}, "must be numeric"),
    ],
)
def test_manifest_limits_reject_coercible_non_numeric_contracts(
    change: dict[str, object], match: str
) -> None:
    with pytest.raises(ManifestError, match=match):
        create_manifest(
            _spec(limits=change),  # type: ignore[arg-type]
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )


def test_manifest_rejects_schema_drift_and_unbounded_chunks() -> None:
    spec = _spec()
    item = spec["items"][0]  # type: ignore[index]
    item["expected_fields"] = ["unexpected"]  # type: ignore[index]
    with pytest.raises(ManifestError, match="fixed endpoint contract"):
        create_manifest(spec, created_at=datetime(2024, 1, 1, tzinfo=UTC))

    five_minute = {
        "created_by": "test",
        "items": [
            {
                "operation": "five_minute_bars",
                "query": {
                    "code": "sh.600000",
                    "start_date": "2024-01-01",
                    "end_date": "2024-02-02",
                    "frequency": "5",
                    "adjustflag": "3",
                },
            }
        ],
    }
    with pytest.raises(ManifestError, match="20-trading-day"):
        create_manifest(
            five_minute, created_at=datetime(2024, 1, 1, tzinfo=UTC)
        )


def test_daily_bars_accept_a_fifteen_year_range_with_bounded_pages() -> None:
    manifest = create_manifest(
        {
            "created_by": "test",
            "items": [
                {
                    "operation": "daily_bars",
                    "query": {
                        "code": "sh.600000",
                        "start_date": "2011-01-01",
                        "end_date": "2025-12-31",
                        "frequency": "d",
                        "adjustflag": "2",
                    },
                    "max_pages": 3,
                    "max_attempts": 3,
                }
            ],
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert manifest.items[0].max_pages == 3
    assert manifest.items[0].query["start_date"] == "2011-01-01"
    assert manifest.items[0].query["end_date"] == "2025-12-31"


def test_storage_preflight_initializes_any_requested_directory(tmp_path: Path) -> None:
    data_root = tmp_path / "arbitrary" / "nested" / "data"
    guard = _test_storage(data_root)

    report = guard.preflight(initialize=True)
    assert report.data_root == str(data_root.resolve())
    assert (data_root / "raw").is_dir()
    assert not (data_root / "tmp" / ".storage-probe").exists()
    assert "mount" not in report.as_dict()

    # Files left by the superseded mount-identity contract are ignored.
    legacy_identity = data_root / "state" / "storage-identity.json"
    legacy_identity.write_text("{}", encoding="utf-8")
    guard.preflight()


def test_cli_has_one_storage_location_parameter(tmp_path: Path) -> None:
    parser = _parser()
    data_root = tmp_path / "chosen" / "directory"

    args = parser.parse_args(
        ["--data-root", str(data_root), "doctor", "--initialize", "--json"]
    )

    assert args.data_root == data_root
    assert not hasattr(args, "expected_mount")
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--data-root",
                str(data_root),
                "--expected-mount",
                str(tmp_path),
                "doctor",
            ]
        )


def test_doctor_reports_the_frozen_provider_rule_snapshot(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state = StateStore(tmp_path / "global")
    report = _run(
        SimpleNamespace(command="doctor", initialize=True),
        data_root=data_root,
        state=state,
        storage=_test_storage(data_root),
    )

    assert report["network_access"] is False
    assert report["provider_rules"] == provider_rule_snapshot()
    assert report["global_state"]["provider_rules_sha256"] == BLACKLIST_RULES_SHA256


def test_cli_verify_failure_is_a_nonzero_quality_gate(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state = StateStore(tmp_path / "global")
    storage = _test_storage(data_root)
    _run(
        SimpleNamespace(command="doctor", initialize=True),
        data_root=data_root,
        state=state,
        storage=storage,
    )
    draft = write_draft(
        data_root,
        create_manifest(_spec(), created_at=datetime(2024, 1, 1, tzinfo=UTC)),
    )
    _, frozen = freeze_manifest(data_root, draft)

    with pytest.raises(OfflineSyncError, match="manifest verification failed"):
        _run(
            SimpleNamespace(
                command="verify",
                manifest=frozen,
                as_of=datetime(2030, 1, 1, tzinfo=UTC),
            ),
            data_root=data_root,
            state=state,
            storage=storage,
        )


def test_storage_preflight_fails_closed_on_space_boundary(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    initial = _test_storage(data_root)
    initial.preflight(initialize=True)
    impossible = StorageGuard(
        data_root=data_root,
        thresholds=StorageThresholds(
            warn_free_bytes=0,
            fail_free_bytes=10**30,
            peak_reserve_bytes=0,
            max_used_percent=100,
        ),
    )
    with pytest.raises(StoragePreflightError, match="free space"):
        impossible.preflight()


def test_state_budget_is_append_only_and_rolling_window_survives_midnight(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2024, 1, 1, 15, 59, 59, tzinfo=UTC))
    state = StateStore(tmp_path / "global", clock=clock, sleep=clock.advance)
    state.initialize()
    limits = RequestLimits(
        calendar_day_attempts=2,
        rolling_24h_attempts=2,
        session_attempts=5,
        minimum_interval_seconds=3,
    )
    session = state.start_session(
        manifest_sha256="a" * 64,
        data_root=tmp_path / "one",
    )

    first = state.reserve_attempt(
        session_id=session,
        manifest_sha256="a" * 64,
        item_id=None,
        kind="login",
        limits=limits,
    )
    state.record_attempt_result(first, status="succeeded", provider_code="0")
    assert state.cooldown_remaining(minimum_interval_seconds=3) == 3
    clock.advance(3)
    second = state.reserve_attempt(
        session_id=session,
        manifest_sha256="a" * 64,
        item_id="item-1",
        kind="query",
        limits=limits,
        item_attempt_limit=1,
    )
    state.record_attempt_result(second, status="transport_error")

    clock.advance(60)
    with pytest.raises(BudgetExceeded, match="rolling-24-hour"):
        state.reserve_attempt(
            session_id=session,
            manifest_sha256="a" * 64,
            item_id=None,
            kind="logout",
            limits=limits,
        )

    clock.advance(24 * 60 * 60)
    third = state.reserve_attempt(
        session_id=session,
        manifest_sha256="a" * 64,
        item_id=None,
        kind="logout",
        limits=limits,
    )
    state.record_attempt_result(third, status="succeeded", provider_code="0")
    state.append_session_event(session, "completed")

    connection = state._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE attempts SET kind = 'changed'")
    finally:
        connection.close()


def test_attempt_reservation_rechecks_cooldown_after_an_early_wakeup(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2024, 1, 1, tzinfo=UTC))
    state = StateStore(tmp_path / "global", clock=clock, sleep=lambda _: None)
    state.initialize()
    limits = RequestLimits()
    session = state.start_session(
        manifest_sha256="e" * 64,
        data_root=tmp_path / "data",
    )
    first = state.reserve_attempt(
        session_id=session,
        manifest_sha256="e" * 64,
        item_id=None,
        kind="login",
        limits=limits,
    )
    state.record_attempt_result(first, status="succeeded", provider_code="0")

    state.wait_for_cooldown(limits)
    with pytest.raises(StateError, match="minimum request interval"):
        state.reserve_attempt(
            session_id=session,
            manifest_sha256="e" * 64,
            item_id="item-1",
            kind="query",
            limits=limits,
            item_attempt_limit=1,
        )


def test_state_blacklist_requires_append_only_administrator_recovery(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2024, 1, 1, tzinfo=UTC))
    state = StateStore(tmp_path / "global", clock=clock)
    state.initialize()
    session = state.start_session(
        manifest_sha256="b" * 64,
        data_root=tmp_path / "data",
    )
    attempt = state.reserve_attempt(
        session_id=session,
        manifest_sha256="b" * 64,
        item_id=None,
        kind="login",
        limits=RequestLimits(),
    )
    state.record_attempt_result(
        attempt,
        status="provider_blacklisted",
        provider_code="10001011",
    )
    incident = state.record_blacklist(
        session_id=session,
        attempt_id=attempt,
        detail={"provider_code": "10001011"},
    )
    state.append_session_event(session, "blacklisted")

    clock.advance(48 * 60 * 60)
    with pytest.raises(StateError, match="provider_blacklisted"):
        state.assert_fetch_ready()
    with pytest.raises(StateError, match="requires"):
        state.recover_blacklist(
            incident_id=incident,
            operator="",
            administrator_confirmation="confirmed",
            reason="resolved",
        )

    state.recover_blacklist(
        incident_id=incident,
        operator="operator",
        administrator_confirmation="BaoStock administrator confirmed removal",
        reason="provider administrator removed public IP from blacklist",
    )
    state.assert_fetch_ready()
    assert state.budget_snapshot(RequestLimits()).calendar_day_attempts == 0


def test_provider_and_project_hard_limit_boundaries_are_independent() -> None:
    limits = RequestLimits(
        calendar_day_attempts=45_000,
        rolling_24h_attempts=45_000,
        session_attempts=100,
        minimum_interval_seconds=3,
    )
    _check_global_capacity((44_999, 0), 1, limits)
    with pytest.raises(BudgetExceeded, match="project 45,000"):
        _check_global_capacity((45_000, 0), 1, limits)

    _check_provider_capacity(49_999, 1)
    with pytest.raises(BudgetExceeded, match="50,000/IP"):
        _check_provider_capacity(50_000, 1)


def test_unclosed_session_and_clock_rollback_require_manual_recovery(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2024, 1, 2, tzinfo=UTC))
    state = StateStore(tmp_path / "global", clock=clock)
    state.initialize()
    session = state.start_session(
        manifest_sha256="c" * 64,
        data_root=tmp_path / "data-one",
    )
    with pytest.raises(StateError, match="unclosed fetch session"):
        state.assert_fetch_ready()
    state.abandon_session(
        session_id=session,
        operator="operator",
        reason="reviewed simulated process crash",
    )
    state.assert_fetch_ready()

    next_session = state.start_session(
        manifest_sha256="d" * 64,
        data_root=tmp_path / "data-two",
    )
    attempt = state.reserve_attempt(
        session_id=next_session,
        manifest_sha256="d" * 64,
        item_id=None,
        kind="login",
        limits=RequestLimits(),
    )
    state.record_attempt_result(attempt, status="succeeded", provider_code="0")
    state.append_session_event(next_session, "completed")
    clock.value -= timedelta(minutes=1)
    with pytest.raises(StateError, match="clock moved backwards"):
        state.assert_fetch_ready()


def _hold_lock(state_root: str, ready: Any, release: Any) -> None:
    with GlobalProviderLock(Path(state_root)):
        ready.set()
        release.wait(10)


def test_global_provider_lock_blocks_a_second_process(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "global")
    state.initialize()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_lock,
        args=(str(state.root), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(ProviderLockError):
            with GlobalProviderLock(state.root):
                pass
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0
