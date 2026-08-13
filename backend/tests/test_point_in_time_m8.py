import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_codex.backtest.point_in_time import (
    POINT_IN_TIME_DATA_VERSION,
    PointInTimeEodView,
)
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.point_in_time import (
    ZZ500_VACANCY_DATES,
    assess_point_in_time_coverage,
    expected_index_member_count,
)
from trading_codex.data.quality import write_report
from trading_codex.data.requirements_cli import (
    _member_daily_requests,
    _point_in_time_requests,
)
from trading_codex.domain.models import PortfolioPosition, SnapshotValidationError

AS_OF = datetime(2024, 1, 10, tzinfo=UTC)
START = date(2024, 1, 2)
END = date(2024, 1, 4)
INDEX_COUNTS = {"sh.000300": 2, "sh.000905": 2}
CODES = ("sh.600000", "sh.600001", "sh.600002", "sh.600003", "sh.600004")
MEMBERS = {
    START: {
        "sh.000300": CODES[:2],
        "sh.000905": CODES[2:4],
    },
    START + timedelta(days=1): {
        "sh.000300": (CODES[0], CODES[4]),
        "sh.000905": CODES[2:4],
    },
    END: {
        "sh.000300": (CODES[0], CODES[4]),
        "sh.000905": (CODES[1], CODES[3]),
    },
}


def _provenance(payload: str, available_at: datetime) -> dict[str, object]:
    return {
        "available_at": available_at,
        "source": "fixture",
        "source_received_at": AS_OF,
        "source_payload_sha256": payload * 64,
        "raw_artifact": f"fixture/{payload}.json",
    }


def _store(
    root: Path,
    *,
    omit_benchmark: date | None = None,
    omit_instrument: str | None = None,
    omit_universe: tuple[date, str] | None = None,
    omit_price: tuple[date, str, str] | None = None,
    omit_nonmember_price: tuple[date, str, str] | None = None,
    mismatch_signal_status: tuple[date, str] | None = None,
    instrument_out_dates: dict[str, date] | None = None,
) -> ParquetDataStore:
    store = ParquetDataStore(root)
    days = tuple(MEMBERS)
    out_dates = instrument_out_dates or {}
    store.merge(
        "trade_calendar",
        (
            {
                "calendar_date": day,
                "is_trading_day": True,
                **_provenance(
                    "a",
                    datetime.combine(day, time.min, tzinfo=UTC),
                ),
            }
            for day in days
        ),
    )
    store.merge(
        "index_memberships",
        (
            {
                "snapshot_date": day,
                "index_code": index_code,
                "member_code": code,
                "member_name": code,
                **_provenance(
                    "b",
                    datetime.combine(day, time(1), tzinfo=UTC),
                ),
            }
            for day, indexes in MEMBERS.items()
            for index_code, codes in indexes.items()
            for code in codes
        ),
    )
    store.merge(
        "instruments",
        (
            {
                "code": code,
                "name": code,
                "ipo_date": date(2000, 1, 1),
                "out_date": out_dates.get(code),
                "security_type": "1",
                "status": "1",
                **_provenance("c", datetime(2000, 1, 1, tzinfo=UTC)),
            }
            for code in CODES
            if code != omit_instrument
        ),
    )
    store.merge(
        "historical_universe",
        (
            {
                "snapshot_date": day,
                "code": code,
                "name": code,
                "trade_status": True,
                **_provenance(
                    "d",
                    datetime.combine(day, time(1), tzinfo=UTC),
                ),
            }
            for day in MEMBERS
            for code in CODES
            if out_dates.get(code) is None or day < out_dates[code]
            if omit_universe != (day, code)
        ),
    )
    store.merge(
        "daily_bars",
        (
            row
            for day_index, day in enumerate(days)
            for code in (*CODES, "sh.000906")
            for adjustment_flag in (("3",) if code == "sh.000906" else ("2", "3"))
            if code == "sh.000906" or out_dates.get(code) is None or day < out_dates[code]
            if not (code == "sh.000906" and day == omit_benchmark)
            if omit_price != (day, code, adjustment_flag)
            if omit_nonmember_price != (day, code, adjustment_flag)
            for row in (
                _daily_row(
                    day,
                    day_index,
                    code,
                    adjustment_flag,
                    trade_status=(
                        mismatch_signal_status != (day, code)
                        or adjustment_flag != "2"
                    ),
                ),
            )
        ),
    )
    return store


def _daily_row(
    day: date,
    day_index: int,
    code: str,
    adjustment_flag: str,
    *,
    trade_status: bool = True,
) -> dict[str, object]:
    base = Decimal("100") if code == "sh.000906" else Decimal("10")
    track = Decimal("0.5") if adjustment_flag == "3" and code != "sh.000906" else Decimal(0)
    close = base + Decimal(day_index) + track
    previous = close - Decimal("0.01")
    return {
        "trade_date": day,
        "code": code,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "previous_close": previous,
        "volume": 100_000,
        "amount": close * 100_000,
        "adjustment_flag": adjustment_flag,
        "turnover": Decimal("0.5"),
        "trade_status": trade_status,
        "pct_change": Decimal("0.1"),
        "is_st": False,
        **_provenance(
            "e" if code == "sh.000906" else "f",
            datetime.combine(day, time(7), tzinfo=UTC),
        ),
    }


def test_point_in_time_coverage_and_view_switch_daily_members(tmp_path: Path) -> None:
    store = _store(tmp_path / "normalized")
    report = assess_point_in_time_coverage(
        store,
        start_date=START,
        end_date=END,
        as_of=AS_OF,
        expected_index_counts=INDEX_COUNTS,
    )

    assert report.status == "passed"
    assert report.formal_m4_oos is False
    assert report.trading_days == 3
    assert report.unique_member_codes == 5
    assert report.raw_member_code_days == 12
    assert report.excluded_out_of_listing_member_days == 0
    assert report.expected_member_code_days == 12
    assert report.source_received_at_min == AS_OF
    assert report.source_received_at_max == AS_OF
    assert not any(report.issue_counts.values())

    view = PointInTimeEodView(
        tmp_path / "normalized",
        as_of=AS_OF,
        start_date=START,
        end_date=END,
        expected_index_counts=INDEX_COUNTS,
    )
    first = view.snapshot(
        decision_date=START,
        as_of=datetime(2024, 1, 2, 7, 5, tzinfo=UTC),
        cash=Decimal("1000000"),
        priced_observations=2,
    )
    second = view.snapshot(
        decision_date=START + timedelta(days=1),
        as_of=datetime(2024, 1, 3, 7, 5, tzinfo=UTC),
        cash=Decimal("1000000"),
        positions=(
            PortfolioPosition(
                code=CODES[1],
                quantity=100,
                sellable_quantity=100,
                average_cost=Decimal("10"),
            ),
        ),
        priced_observations=2,
    )

    assert first.data_version == POINT_IN_TIME_DATA_VERSION
    assert first.candidate_codes == tuple(sorted((*CODES[:2], *CODES[2:4])))
    assert CODES[1] not in second.candidate_codes
    assert any(position.code == CODES[1] for position in second.positions)
    assert view.benchmark_return(START) == Decimal("0.001")


def test_point_in_time_excludes_index_residual_on_instrument_out_date(
    tmp_path: Path,
) -> None:
    residual = CODES[1]
    assert residual in MEMBERS[END]["sh.000905"]
    _store(
        tmp_path / "normalized",
        instrument_out_dates={residual: END},
    )

    report = assess_point_in_time_coverage(
        ParquetDataStore(tmp_path / "normalized"),
        start_date=START,
        end_date=END,
        as_of=AS_OF,
        expected_index_counts=INDEX_COUNTS,
    )

    assert report.status == "passed"
    assert report.raw_member_code_days == 12
    assert report.excluded_out_of_listing_member_days == 1
    assert report.expected_member_code_days == 11
    assert report.excluded_out_of_listing_members == (f"{END.isoformat()}:{residual}",)
    assert not any(report.issue_counts.values())

    view = PointInTimeEodView(
        tmp_path / "normalized",
        as_of=AS_OF,
        start_date=START,
        end_date=END,
        expected_index_counts=INDEX_COUNTS,
    )
    assert residual not in view._candidates[END]


def test_point_in_time_view_requires_universe_state_for_exited_position(
    tmp_path: Path,
) -> None:
    exited = CODES[1]
    decision_date = START + timedelta(days=1)
    assert exited not in set().union(*MEMBERS[decision_date].values())
    _store(
        tmp_path / "normalized",
        omit_universe=(decision_date, exited),
    )
    view = PointInTimeEodView(
        tmp_path / "normalized",
        as_of=AS_OF,
        start_date=START,
        end_date=END,
        expected_index_counts=INDEX_COUNTS,
    )

    with pytest.raises(
        SnapshotValidationError,
        match="positions lack current universe state",
    ):
        view.snapshot(
            decision_date=decision_date,
            as_of=datetime(2024, 1, 3, 7, 5, tzinfo=UTC),
            cash=Decimal("1000000"),
            positions=(
                PortfolioPosition(
                    code=exited,
                    quantity=100,
                    sellable_quantity=100,
                    average_cost=Decimal("10"),
                ),
            ),
            priced_observations=2,
        )


@pytest.mark.parametrize(
    ("fixture", "issue"),
    [
        ({"omit_benchmark": END}, "missing_benchmark_dates"),
        ({"omit_instrument": CODES[1]}, "missing_instruments"),
        ({"omit_universe": (END, CODES[1])}, "missing_universe_members"),
        ({"omit_price": (END, CODES[1], "2")}, "missing_signal_prices"),
        ({"mismatch_signal_status": (END, CODES[1])}, "trade_status_mismatches"),
    ],
)
def test_point_in_time_coverage_fails_closed(
    tmp_path: Path,
    fixture: dict[str, object],
    issue: str,
) -> None:
    store = _store(tmp_path / "normalized", **fixture)  # type: ignore[arg-type]
    report = assess_point_in_time_coverage(
        store,
        start_date=START,
        end_date=END,
        as_of=AS_OF,
        expected_index_counts=INDEX_COUNTS,
    )

    assert report.status == "failed"
    assert report.issue_counts[issue] == 1
    with pytest.raises(SnapshotValidationError, match="point-in-time coverage failed"):
        PointInTimeEodView(
            tmp_path / "normalized",
            as_of=AS_OF,
            start_date=START,
            end_date=END,
            expected_index_counts=INDEX_COUNTS,
        )


def test_point_in_time_requirements_are_deterministic_and_two_stage(tmp_path: Path) -> None:
    _store(tmp_path / "data" / "normalized")

    first = _point_in_time_requests(
        data_root=tmp_path / "data",
        start_date=START,
        end_date=END,
        benchmark_code="sh.000906",
    )
    second = _point_in_time_requests(
        data_root=tmp_path / "data",
        start_date=START,
        end_date=END,
        benchmark_code="sh.000906",
    )
    members = _member_daily_requests(
        data_root=tmp_path / "data",
        start_date=START,
        end_date=END,
        expected_index_counts=INDEX_COUNTS,
    )

    assert first == second
    assert len(first) == 10
    assert first[-1]["query"]["code"] == "sh.000906"
    assert len(members) == len(CODES) * 2
    assert {request["query"]["adjustflag"] for request in members} == {"2", "3"}


def test_index_membership_contract_limits_vacancies_to_verified_dates() -> None:
    assert len(ZZ500_VACANCY_DATES) == 22
    assert min(ZZ500_VACANCY_DATES) == date(2019, 1, 7)
    assert max(ZZ500_VACANCY_DATES) == date(2021, 9, 30)
    assert expected_index_member_count(date(2019, 1, 7), "sh.000905") == 499
    assert expected_index_member_count(date(2021, 9, 30), "sh.000905") == 499
    assert expected_index_member_count(date(2019, 1, 4), "sh.000905") == 500
    assert expected_index_member_count(date(2021, 10, 8), "sh.000905") == 500
    assert expected_index_member_count(date(2019, 1, 7), "sh.000300") == 300
    assert (
        expected_index_member_count(
            date(2019, 1, 7),
            "sh.000905",
            base_counts=INDEX_COUNTS,
        )
        == 2
    )


def test_point_in_time_coverage_rejects_price_track_date_mismatch_off_membership_day(
    tmp_path: Path,
) -> None:
    store = _store(
        tmp_path / "normalized",
        omit_nonmember_price=(END, CODES[2], "2"),
    )

    report = assess_point_in_time_coverage(
        store,
        start_date=START,
        end_date=END,
        as_of=AS_OF,
        expected_index_counts=INDEX_COUNTS,
    )

    assert CODES[2] not in MEMBERS[END]["sh.000905"]
    assert report.status == "failed"
    assert report.issue_counts["price_track_date_mismatches"] == 1


def test_point_in_time_report_is_content_addressed_and_preserves_received_at(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "normalized")
    report = assess_point_in_time_coverage(
        store,
        start_date=START,
        end_date=END,
        as_of=AS_OF,
        generated_at=AS_OF,
        expected_index_counts=INDEX_COUNTS,
    )

    first = write_report(report, tmp_path / "artifacts")
    second = write_report(report, tmp_path / "artifacts")
    payload = json.loads(first.read_text(encoding="utf-8"))

    assert first == second
    assert first.name.startswith("point-in-time-coverage-")
    assert payload["source_received_at_min"] == AS_OF.isoformat()
    assert payload["source_received_at_max"] == AS_OF.isoformat()
    assert payload["formal_m4_oos"] is False
