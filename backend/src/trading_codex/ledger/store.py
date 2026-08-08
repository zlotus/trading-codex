from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading_codex.domain.contracts import DecisionRun
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import DecisionSnapshot
from trading_codex.ledger.models import (
    CashMovementKind,
    CashMovementRecord,
    FillRecord,
    JobRunView,
    JobStatus,
    JobType,
    LedgerConflictError,
    LedgerDashboard,
    LedgerInvariantError,
    LedgerNotFoundError,
    PortfolioTrack,
    PositionView,
    PricePoint,
    ReconciliationRow,
    ReconciliationView,
    SignalDetail,
    SignalStatus,
    SignalTrace,
    SignalView,
    TrackView,
    as_utc,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
LEDGER_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


class SQLiteLedger:
    """Append-only SQLite event store with deterministic portfolio projections."""

    _APPEND_ONLY_TABLES = (
        "decision_runs",
        "decision_prices",
        "signals",
        "order_intents",
        "fills",
        "cash_movements",
        "signal_dispositions",
        "job_runs",
        "job_attempt_events",
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def record_decision(
        self,
        snapshot: DecisionSnapshot,
        run: DecisionRun,
        *,
        portfolio_track: PortfolioTrack = PortfolioTrack.BASE,
        recorded_at: datetime | None = None,
    ) -> tuple[str, ...]:
        if portfolio_track is PortfolioTrack.ACTUAL:
            raise LedgerInvariantError("actual track cannot originate an automated decision")
        if run.snapshot_id != snapshot.snapshot_id:
            raise LedgerInvariantError("decision run belongs to a different snapshot")
        stage_snapshot_ids = (
            run.features.snapshot_id,
            run.proposal.snapshot_id,
            run.allocated.snapshot_id,
            run.risk.snapshot_id,
            run.risk.requested.snapshot_id,
            run.execution.snapshot_id,
        )
        if any(snapshot_id != snapshot.snapshot_id for snapshot_id in stage_snapshot_ids):
            raise LedgerInvariantError("decision stage belongs to a different snapshot")
        if run.features.as_of != snapshot.as_of:
            raise LedgerInvariantError("feature set uses a different as_of")
        if run.risk.requested != run.allocated:
            raise LedgerInvariantError("risk decision does not reference allocated target")
        expected_decision_id = canonical_sha256(
            {
                "snapshot_id": snapshot.snapshot_id,
                "configuration_id": run.configuration_id,
                "features": run.features,
                "proposal": run.proposal,
                "allocated": run.allocated,
                "risk": run.risk,
                "execution": run.execution,
            }
        )
        if run.decision_id != expected_decision_id:
            raise LedgerInvariantError("decision id does not match its content")
        order_codes = tuple(order.code for order in run.execution.orders)
        if len(order_codes) != len(set(order_codes)):
            raise LedgerInvariantError("decision contains duplicate order codes")
        recorded = as_utc(recorded_at or datetime.now(UTC), field="recorded_at")
        if recorded < snapshot.as_of:
            raise LedgerInvariantError("recorded_at cannot precede decision as_of")
        decision_payload = _json_text(run)
        snapshot_payload = _json_text(snapshot)
        source_payloads = json.dumps(list(snapshot.source_payloads), separators=(",", ":"))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT portfolio_track, decision_payload_json, snapshot_payload_json "
                "FROM decision_runs WHERE decision_id = ?",
                (run.decision_id,),
            ).fetchone()
            if existing is not None:
                expected = (portfolio_track.value, decision_payload, snapshot_payload)
                actual = tuple(existing)
                if actual != expected:
                    raise LedgerConflictError("decision id already has different content")
            else:
                connection.execute(
                    """
                    INSERT INTO decision_runs (
                        decision_id, snapshot_id, configuration_id, pipeline_version,
                        portfolio_track, as_of, decision_date, expires_at,
                        source_payloads_json, decision_payload_json, snapshot_payload_json,
                        recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.decision_id,
                        run.snapshot_id,
                        run.configuration_id,
                        run.pipeline_version,
                        portfolio_track.value,
                        _datetime_text(snapshot.as_of),
                        snapshot.decision_date.isoformat(),
                        _datetime_text(snapshot.execution_deadline),
                        source_payloads,
                        decision_payload,
                        snapshot_payload,
                        _datetime_text(recorded),
                    ),
                )

            for bar in snapshot.bars:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO decision_prices (
                        decision_id, code, trade_date, signal_close, execution_close
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run.decision_id,
                        bar.code,
                        bar.trade_date.isoformat(),
                        _decimal_text(bar.signal_close) if bar.signal_close is not None else None,
                        _decimal_text(bar.execution_close)
                        if bar.execution_close is not None
                        else None,
                    ),
                )

            signal_ids: list[str] = []
            for order in run.execution.orders:
                signal_id = canonical_sha256(
                    {
                        "decision_id": run.decision_id,
                        "portfolio_track": portfolio_track,
                        "code": order.code,
                        "side": order.side,
                        "quantity": order.quantity,
                        "expires_at": order.expires_at,
                    }
                )
                order_intent_id = canonical_sha256(
                    {"signal_id": signal_id, "execution_version": run.execution.version}
                )
                signal_ids.append(signal_id)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO signals (
                        signal_id, decision_id, portfolio_track, code, side,
                        suggested_quantity, reference_price, target_weight,
                        expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id,
                        run.decision_id,
                        portfolio_track.value,
                        order.code,
                        order.side.value,
                        order.quantity,
                        _decimal_text(order.reference_price),
                        _decimal_text(order.target_weight),
                        _datetime_text(order.expires_at),
                        _datetime_text(snapshot.as_of),
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO order_intents (
                        order_intent_id, signal_id, decision_id, portfolio_track,
                        code, side, quantity, reference_price, estimated_fees,
                        expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_intent_id,
                        signal_id,
                        run.decision_id,
                        portfolio_track.value,
                        order.code,
                        order.side.value,
                        order.quantity,
                        _decimal_text(order.reference_price),
                        _decimal_text(order.estimated_fees),
                        _datetime_text(order.expires_at),
                        _datetime_text(snapshot.as_of),
                    ),
                )
            return tuple(signal_ids)

    def record_cash_movement(
        self,
        *,
        portfolio_track: PortfolioTrack,
        kind: CashMovementKind,
        amount: Decimal,
        occurred_at: datetime,
        idempotency_key: str,
        note: str | None = None,
    ) -> CashMovementRecord:
        if kind not in (CashMovementKind.DEPOSIT, CashMovementKind.WITHDRAWAL):
            raise LedgerInvariantError("only deposits and withdrawals can be recorded directly")
        _require_positive_money(amount, field="amount")
        occurred = as_utc(occurred_at, field="occurred_at")
        key = _require_idempotency_key(idempotency_key)
        signed_amount = amount if kind is CashMovementKind.DEPOSIT else -amount
        movement_id = canonical_sha256(
            {"event": "cash_movement", "track": portfolio_track, "key": key}
        )
        record = CashMovementRecord(
            movement_id=movement_id,
            portfolio_track=portfolio_track,
            kind=kind,
            amount=signed_amount,
            occurred_at=occurred,
            fill_id=None,
            note=_clean_note(note),
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM cash_movements WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                existing_record = self._cash_record(existing)
                if existing_record != record:
                    raise LedgerConflictError(
                        "cash movement idempotency key has different content"
                    )
                return existing_record
            connection.execute(
                """
                INSERT INTO cash_movements (
                    movement_id, portfolio_track, kind, amount, occurred_at,
                    fill_id, idempotency_key, note
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    record.movement_id,
                    record.portfolio_track.value,
                    record.kind.value,
                    _decimal_text(record.amount),
                    _datetime_text(record.occurred_at),
                    key,
                    record.note,
                ),
            )
            self._project_track(connection, portfolio_track)
            return record

    def record_fill(
        self,
        *,
        source_order_intent_id: str,
        portfolio_track: PortfolioTrack,
        quantity: int,
        price: Decimal,
        fees: Decimal,
        occurred_at: datetime,
        idempotency_key: str,
        note: str | None = None,
    ) -> FillRecord:
        if quantity <= 0:
            raise LedgerInvariantError("fill quantity must be positive")
        _require_positive_money(price, field="price")
        _require_non_negative_money(fees, field="fees")
        occurred = as_utc(occurred_at, field="occurred_at")
        key = _require_idempotency_key(idempotency_key)
        cleaned_note = _clean_note(note)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                "SELECT * FROM order_intents WHERE order_intent_id = ?",
                (source_order_intent_id,),
            ).fetchone()
            if intent is None:
                raise LedgerNotFoundError("source order intent does not exist")
            source_track = PortfolioTrack(intent["portfolio_track"])
            if portfolio_track is not PortfolioTrack.ACTUAL and portfolio_track is not source_track:
                raise LedgerInvariantError("simulated fill must stay on its source track")
            if occurred < _parse_datetime(intent["created_at"]):
                raise LedgerInvariantError("fill cannot precede its order intent")
            if occurred > _parse_datetime(intent["expires_at"]):
                raise LedgerInvariantError("fill cannot occur after the order intent expires")

            fill_id = canonical_sha256(
                {"event": "fill", "track": portfolio_track, "key": key}
            )
            record = FillRecord(
                fill_id=fill_id,
                source_order_intent_id=source_order_intent_id,
                portfolio_track=portfolio_track,
                code=intent["code"],
                side=intent["side"],
                quantity=quantity,
                price=price,
                fees=fees,
                occurred_at=occurred,
                note=cleaned_note,
            )
            existing = connection.execute(
                "SELECT * FROM fills WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                existing_record = self._fill_record(existing)
                if existing_record != record:
                    raise LedgerConflictError("fill idempotency key has different content")
                return existing_record

            disposition = connection.execute(
                """
                SELECT 1 FROM signal_dispositions
                WHERE signal_id = ? AND portfolio_track = ?
                """,
                (intent["signal_id"], portfolio_track.value),
            ).fetchone()
            if disposition is not None:
                raise LedgerInvariantError("cannot fill a skipped signal")
            filled = connection.execute(
                """
                SELECT COALESCE(SUM(quantity), 0) FROM fills
                WHERE source_order_intent_id = ? AND portfolio_track = ?
                """,
                (source_order_intent_id, portfolio_track.value),
            ).fetchone()[0]
            if int(filled) + quantity > int(intent["quantity"]):
                raise LedgerInvariantError("cumulative fills exceed the order intent")

            connection.execute(
                """
                INSERT INTO fills (
                    fill_id, source_order_intent_id, portfolio_track, code, side,
                    quantity, price, fees, occurred_at, idempotency_key, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.fill_id,
                    record.source_order_intent_id,
                    record.portfolio_track.value,
                    record.code,
                    record.side,
                    record.quantity,
                    _decimal_text(record.price),
                    _decimal_text(record.fees),
                    _datetime_text(record.occurred_at),
                    key,
                    record.note,
                ),
            )
            notional = _multiply(price, Decimal(quantity))
            trade_amount = -notional if record.side == "buy" else notional
            self._insert_derived_cash_movement(
                connection,
                fill=record,
                kind=CashMovementKind.TRADE,
                amount=trade_amount,
            )
            if fees:
                self._insert_derived_cash_movement(
                    connection,
                    fill=record,
                    kind=CashMovementKind.FEE,
                    amount=-fees,
                )
            self._project_track(connection, portfolio_track)
            return record

    def skip_signal(
        self,
        signal_id: str,
        *,
        portfolio_track: PortfolioTrack,
        reason: str,
        occurred_at: datetime,
        idempotency_key: str,
    ) -> SignalView:
        occurred = as_utc(occurred_at, field="occurred_at")
        key = _require_idempotency_key(idempotency_key)
        cleaned_reason = reason.strip()
        if not cleaned_reason or len(cleaned_reason) > 500:
            raise LedgerInvariantError("skip reason must contain 1 to 500 characters")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            signal = self._signal_row(connection, signal_id)
            if _parse_datetime(signal["created_at"]) > occurred:
                raise LedgerInvariantError("signal cannot be skipped before it is created")
            intent = connection.execute(
                "SELECT * FROM order_intents WHERE signal_id = ?", (signal_id,)
            ).fetchone()
            assert intent is not None
            filled = connection.execute(
                """
                SELECT COALESCE(SUM(quantity), 0) FROM fills
                WHERE source_order_intent_id = ? AND portfolio_track = ?
                """,
                (intent["order_intent_id"], portfolio_track.value),
            ).fetchone()[0]
            if int(filled) >= int(intent["quantity"]):
                raise LedgerInvariantError("a fully filled signal cannot be skipped")

            existing_by_key = connection.execute(
                "SELECT * FROM signal_dispositions WHERE idempotency_key = ?", (key,)
            ).fetchone()
            expected_id = canonical_sha256(
                {"event": "signal_skip", "track": portfolio_track, "key": key}
            )
            if existing_by_key is not None:
                expected = (
                    expected_id,
                    signal_id,
                    portfolio_track.value,
                    "skipped",
                    cleaned_reason,
                    _datetime_text(occurred),
                )
                actual = tuple(existing_by_key[column] for column in (
                    "disposition_id",
                    "signal_id",
                    "portfolio_track",
                    "disposition",
                    "reason",
                    "occurred_at",
                ))
                if actual != expected:
                    raise LedgerConflictError("skip idempotency key has different content")
            else:
                existing_signal = connection.execute(
                    """
                    SELECT 1 FROM signal_dispositions
                    WHERE signal_id = ? AND portfolio_track = ?
                    """,
                    (signal_id, portfolio_track.value),
                ).fetchone()
                if existing_signal is not None:
                    raise LedgerConflictError("signal already has a disposition")
                connection.execute(
                    """
                    INSERT INTO signal_dispositions (
                        disposition_id, signal_id, portfolio_track, disposition,
                        reason, occurred_at, idempotency_key
                    ) VALUES (?, ?, ?, 'skipped', ?, ?, ?)
                    """,
                    (
                        expected_id,
                        signal_id,
                        portfolio_track.value,
                        cleaned_reason,
                        _datetime_text(occurred),
                        key,
                    ),
                )
            return self._signal_view(
                connection,
                signal,
                intent,
                as_of=max(datetime.now(UTC), occurred),
                execution_track=portfolio_track,
            )

    def dashboard(self, *, as_of: datetime | None = None) -> LedgerDashboard:
        current = as_utc(as_of or datetime.now(UTC), field="as_of")
        with self._connect() as connection:
            tracks = tuple(
                self._track_view(connection, track, current) for track in PortfolioTrack
            )
            signals = self._list_signals(
                connection,
                as_of=current,
                source_track=PortfolioTrack.BASE,
                execution_track=PortfolioTrack.ACTUAL,
            )
            by_track = {track.track: track for track in tracks}
            base = by_track[PortfolioTrack.BASE]
            actual = by_track[PortfolioTrack.ACTUAL]
            ai_shadow = by_track[PortfolioTrack.AI_SHADOW]
            quantities = {
                track.track: {position.code: position.quantity for position in track.positions}
                for track in tracks
            }
            codes = sorted(set().union(*(set(items) for items in quantities.values())))
            rows = tuple(
                ReconciliationRow(
                    code=code,
                    base_quantity=quantities[PortfolioTrack.BASE].get(code, 0),
                    ai_shadow_quantity=quantities[PortfolioTrack.AI_SHADOW].get(code, 0),
                    actual_quantity=quantities[PortfolioTrack.ACTUAL].get(code, 0),
                    actual_vs_base=quantities[PortfolioTrack.ACTUAL].get(code, 0)
                    - quantities[PortfolioTrack.BASE].get(code, 0),
                )
                for code in codes
            )
            equity_delta = (
                _subtract(actual.equity, base.equity)
                if actual.equity is not None and base.equity is not None
                else None
            )
            reconciliation = ReconciliationView(
                cash_actual_vs_base=_subtract(actual.cash, base.cash),
                equity_actual_vs_base=equity_delta,
                rows=rows,
            )
            return LedgerDashboard(
                as_of=current,
                tracks=(base, ai_shadow, actual),
                signals=signals,
                reconciliation=reconciliation,
            )

    def signal_detail(
        self,
        signal_id: str,
        *,
        as_of: datetime | None = None,
    ) -> SignalDetail:
        current = as_utc(as_of or datetime.now(UTC), field="as_of")
        with self._connect() as connection:
            signal = self._signal_row(connection, signal_id)
            if _parse_datetime(signal["created_at"]) > current:
                raise LedgerNotFoundError("signal is unavailable at as_of")
            intent = connection.execute(
                "SELECT * FROM order_intents WHERE signal_id = ?", (signal_id,)
            ).fetchone()
            assert intent is not None
            source_track = PortfolioTrack(signal["portfolio_track"])
            execution_track = (
                PortfolioTrack.ACTUAL
                if source_track is PortfolioTrack.BASE
                else source_track
            )
            view = self._signal_view(
                connection,
                signal,
                intent,
                as_of=current,
                execution_track=execution_track,
            )
            decision = connection.execute(
                "SELECT * FROM decision_runs WHERE decision_id = ?",
                (signal["decision_id"],),
            ).fetchone()
            assert decision is not None
            price_rows = connection.execute(
                """
                SELECT trade_date, signal_close, execution_close
                FROM decision_prices
                WHERE decision_id = ? AND code = ?
                ORDER BY trade_date
                """,
                (signal["decision_id"], signal["code"]),
            ).fetchall()
            return SignalDetail(
                signal=view,
                price_points=tuple(
                    PricePoint(
                        trade_date=date.fromisoformat(row["trade_date"]),
                        signal_close=_optional_decimal(row["signal_close"]),
                        execution_close=_optional_decimal(row["execution_close"]),
                    )
                    for row in price_rows
                ),
                trace=SignalTrace(
                    decision_id=decision["decision_id"],
                    snapshot_id=decision["snapshot_id"],
                    configuration_id=decision["configuration_id"],
                    pipeline_version=decision["pipeline_version"],
                    source_payloads=tuple(json.loads(decision["source_payloads_json"])),
                    recorded_at=_parse_datetime(decision["recorded_at"]),
                ),
            )

    def signal_id_for_order_intent(self, order_intent_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT signal_id FROM order_intents WHERE order_intent_id = ?",
                (order_intent_id,),
            ).fetchone()
            if row is None:
                raise LedgerNotFoundError("order intent does not exist")
            return str(row["signal_id"])

    def list_fills(
        self, *, portfolio_track: PortfolioTrack | None = None
    ) -> tuple[FillRecord, ...]:
        with self._connect() as connection:
            if portfolio_track is None:
                rows = connection.execute(
                    "SELECT * FROM fills ORDER BY occurred_at, rowid"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM fills WHERE portfolio_track = ?
                    ORDER BY occurred_at, rowid
                    """,
                    (portfolio_track.value,),
                ).fetchall()
            return tuple(self._fill_record(row) for row in rows)

    def list_cash_movements(
        self, *, portfolio_track: PortfolioTrack | None = None
    ) -> tuple[CashMovementRecord, ...]:
        with self._connect() as connection:
            if portfolio_track is None:
                rows = connection.execute(
                    "SELECT * FROM cash_movements ORDER BY occurred_at, rowid"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM cash_movements WHERE portfolio_track = ?
                    ORDER BY occurred_at, rowid
                    """,
                    (portfolio_track.value,),
                ).fetchall()
            return tuple(self._cash_record(row) for row in rows)

    def ensure_job_run(self, job_type: JobType, scheduled_for: datetime) -> str:
        scheduled = as_utc(scheduled_for, field="scheduled_for")
        run_id = canonical_sha256(
            {"job_type": job_type, "scheduled_for": scheduled, "version": "daily-jobs-v1"}
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO job_runs (
                    run_id, job_type, scheduled_for, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    run_id,
                    job_type.value,
                    _datetime_text(scheduled),
                    _datetime_text(datetime.now(UTC)),
                ),
            )
        return run_id

    def record_job_attempt_event(
        self,
        *,
        run_id: str,
        attempt_id: str,
        phase: str,
        occurred_at: datetime,
        result: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        if phase not in {"started", "succeeded", "failed"}:
            raise LedgerInvariantError("unknown job attempt phase")
        occurred = as_utc(occurred_at, field="occurred_at")
        if phase == "succeeded" and (result is None or error is not None):
            raise LedgerInvariantError("succeeded attempts require a result and no error")
        if phase == "failed" and (not error or result is not None):
            raise LedgerInvariantError("failed attempts require an error and no result")
        if phase == "started" and (result is not None or error is not None):
            raise LedgerInvariantError("started attempts cannot contain an outcome")
        result_json = _json_text(result) if result is not None else None

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT 1 FROM job_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise LedgerNotFoundError("job run does not exist")
            events = connection.execute(
                """
                SELECT phase FROM job_attempt_events
                WHERE run_id = ? AND attempt_id = ? ORDER BY rowid
                """,
                (run_id, attempt_id),
            ).fetchall()
            phases = [row["phase"] for row in events]
            if phase == "started" and phases:
                raise LedgerConflictError("job attempt already started")
            if phase != "started" and phases != ["started"]:
                raise LedgerInvariantError("job attempt outcome must follow one start event")
            event_id = canonical_sha256(
                {"run_id": run_id, "attempt_id": attempt_id, "phase": phase}
            )
            connection.execute(
                """
                INSERT INTO job_attempt_events (
                    event_id, run_id, attempt_id, phase, occurred_at,
                    result_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    attempt_id,
                    phase,
                    _datetime_text(occurred),
                    result_json,
                    error,
                ),
            )

    def job_run(self, run_id: str) -> JobRunView:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM job_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise LedgerNotFoundError("job run does not exist")
            return self._job_view(connection, run)

    def list_job_runs(self, *, limit: int = 50) -> tuple[JobRunView, ...]:
        if not 1 <= limit <= 500:
            raise LedgerInvariantError("job run limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_runs ORDER BY scheduled_for DESC LIMIT ?", (limit,)
            ).fetchall()
            return tuple(self._job_view(connection, row) for row in rows)

    def _initialize(self) -> None:
        with self._connect() as connection:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version not in (0, 1):
                raise LedgerInvariantError(
                    f"unsupported ledger schema version: {schema_version}"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS decision_runs (
                    decision_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    configuration_id TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    portfolio_track TEXT NOT NULL CHECK (
                        portfolio_track IN ('base', 'ai_shadow')
                    ),
                    as_of TEXT NOT NULL,
                    decision_date TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    source_payloads_json TEXT NOT NULL,
                    decision_payload_json TEXT NOT NULL,
                    snapshot_payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS decision_prices (
                    decision_id TEXT NOT NULL REFERENCES decision_runs(decision_id),
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    signal_close TEXT,
                    execution_close TEXT,
                    PRIMARY KEY (decision_id, code, trade_date)
                );

                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL REFERENCES decision_runs(decision_id),
                    portfolio_track TEXT NOT NULL CHECK (
                        portfolio_track IN ('base', 'ai_shadow')
                    ),
                    code TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                    suggested_quantity INTEGER NOT NULL CHECK (suggested_quantity > 0),
                    reference_price TEXT NOT NULL,
                    target_weight TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (decision_id, portfolio_track, code)
                );

                CREATE TABLE IF NOT EXISTS order_intents (
                    order_intent_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE REFERENCES signals(signal_id),
                    decision_id TEXT NOT NULL REFERENCES decision_runs(decision_id),
                    portfolio_track TEXT NOT NULL CHECK (
                        portfolio_track IN ('base', 'ai_shadow')
                    ),
                    code TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    reference_price TEXT NOT NULL,
                    estimated_fees TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    source_order_intent_id TEXT NOT NULL REFERENCES order_intents(order_intent_id),
                    portfolio_track TEXT NOT NULL CHECK (
                        portfolio_track IN ('base', 'ai_shadow', 'actual')
                    ),
                    code TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    price TEXT NOT NULL,
                    fees TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    note TEXT
                );

                CREATE TABLE IF NOT EXISTS cash_movements (
                    movement_id TEXT PRIMARY KEY,
                    portfolio_track TEXT NOT NULL CHECK (
                        portfolio_track IN ('base', 'ai_shadow', 'actual')
                    ),
                    kind TEXT NOT NULL CHECK (
                        kind IN ('deposit', 'withdrawal', 'trade', 'fee')
                    ),
                    amount TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    fill_id TEXT REFERENCES fills(fill_id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    note TEXT,
                    UNIQUE (fill_id, kind)
                );

                CREATE TABLE IF NOT EXISTS signal_dispositions (
                    disposition_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
                    portfolio_track TEXT NOT NULL CHECK (
                        portfolio_track IN ('base', 'ai_shadow', 'actual')
                    ),
                    disposition TEXT NOT NULL CHECK (disposition = 'skipped'),
                    reason TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    UNIQUE (signal_id, portfolio_track)
                );

                CREATE TABLE IF NOT EXISTS job_runs (
                    run_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL CHECK (
                        job_type IN ('eod_preparation', 'opening_decision')
                    ),
                    scheduled_for TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (job_type, scheduled_for)
                );

                CREATE TABLE IF NOT EXISTS job_attempt_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES job_runs(run_id),
                    attempt_id TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('started', 'succeeded', 'failed')),
                    occurred_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    UNIQUE (attempt_id, phase)
                );

                CREATE INDEX IF NOT EXISTS idx_fills_track_time
                    ON fills(portfolio_track, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_cash_track_time
                    ON cash_movements(portfolio_track, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_signals_track_expiry
                    ON signals(portfolio_track, expires_at);
                CREATE INDEX IF NOT EXISTS idx_job_events_run
                    ON job_attempt_events(run_id, occurred_at);
                """
            )
            for table in self._APPEND_ONLY_TABLES:
                connection.execute(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {table}_reject_update
                    BEFORE UPDATE ON {table}
                    BEGIN
                        SELECT RAISE(ABORT, 'append-only table');
                    END
                    """
                )
                connection.execute(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {table}_reject_delete
                    BEFORE DELETE ON {table}
                    BEGIN
                        SELECT RAISE(ABORT, 'append-only table');
                    END
                    """
                )
            if schema_version == 0:
                connection.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _insert_derived_cash_movement(
        self,
        connection: sqlite3.Connection,
        *,
        fill: FillRecord,
        kind: CashMovementKind,
        amount: Decimal,
    ) -> None:
        movement_id = canonical_sha256({"fill_id": fill.fill_id, "kind": kind})
        connection.execute(
            """
            INSERT INTO cash_movements (
                movement_id, portfolio_track, kind, amount, occurred_at,
                fill_id, idempotency_key, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                movement_id,
                fill.portfolio_track.value,
                kind.value,
                _decimal_text(amount),
                _datetime_text(fill.occurred_at),
                fill.fill_id,
                f"fill:{fill.fill_id}:{kind.value}",
            ),
        )

    def _signal_row(self, connection: sqlite3.Connection, signal_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM signals WHERE signal_id = ?", (signal_id,)
        ).fetchone()
        if row is None:
            raise LedgerNotFoundError("signal does not exist")
        return row

    def _list_signals(
        self,
        connection: sqlite3.Connection,
        *,
        as_of: datetime,
        source_track: PortfolioTrack,
        execution_track: PortfolioTrack,
    ) -> tuple[SignalView, ...]:
        rows = connection.execute(
            """
            SELECT s.*, o.order_intent_id, o.estimated_fees
            FROM signals AS s
            JOIN order_intents AS o ON o.signal_id = s.signal_id
            WHERE s.portfolio_track = ? AND s.created_at <= ?
            ORDER BY s.expires_at DESC, s.code
            LIMIT 200
            """,
            (source_track.value, _datetime_text(as_of)),
        ).fetchall()
        return tuple(
            self._signal_view(
                connection,
                row,
                row,
                as_of=as_of,
                execution_track=execution_track,
            )
            for row in rows
        )

    def _signal_view(
        self,
        connection: sqlite3.Connection,
        signal: sqlite3.Row,
        intent: sqlite3.Row,
        *,
        as_of: datetime,
        execution_track: PortfolioTrack,
    ) -> SignalView:
        filled = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(quantity), 0) FROM fills
                WHERE source_order_intent_id = ? AND portfolio_track = ?
                    AND occurred_at <= ?
                """,
                (
                    intent["order_intent_id"],
                    execution_track.value,
                    _datetime_text(as_of),
                ),
            ).fetchone()[0]
        )
        disposition = connection.execute(
            """
            SELECT reason FROM signal_dispositions
            WHERE signal_id = ? AND portfolio_track = ? AND occurred_at <= ?
            """,
            (signal["signal_id"], execution_track.value, _datetime_text(as_of)),
        ).fetchone()
        quantity = int(signal["suggested_quantity"])
        expires_at = _parse_datetime(signal["expires_at"])
        if disposition is not None:
            status = SignalStatus.SKIPPED
        elif filled >= quantity:
            status = SignalStatus.FILLED
        elif as_of >= expires_at:
            status = SignalStatus.EXPIRED
        elif filled:
            status = SignalStatus.PARTIAL
        else:
            status = SignalStatus.ACTIVE
        return SignalView(
            signal_id=signal["signal_id"],
            order_intent_id=intent["order_intent_id"],
            decision_id=signal["decision_id"],
            snapshot_id=connection.execute(
                "SELECT snapshot_id FROM decision_runs WHERE decision_id = ?",
                (signal["decision_id"],),
            ).fetchone()[0],
            portfolio_track=PortfolioTrack(signal["portfolio_track"]),
            code=signal["code"],
            side=signal["side"],
            suggested_quantity=quantity,
            filled_quantity=filled,
            remaining_quantity=max(quantity - filled, 0),
            reference_price=Decimal(signal["reference_price"]),
            target_weight=Decimal(signal["target_weight"]),
            estimated_fees=Decimal(intent["estimated_fees"]),
            expires_at=expires_at,
            status=status,
            skip_reason=disposition["reason"] if disposition is not None else None,
        )

    def _track_view(
        self,
        connection: sqlite3.Connection,
        track: PortfolioTrack,
        as_of: datetime,
    ) -> TrackView:
        cash, lots = self._project_track(connection, track, as_of=as_of)
        prices = self._latest_prices(connection, as_of)
        positions: list[PositionView] = []
        valuation_complete = True
        market_value = Decimal(0)
        view_date = as_of.astimezone(SHANGHAI).date()
        for code in sorted(lots):
            code_lots = lots[code]
            quantity = sum(lot["quantity"] for lot in code_lots)
            if quantity == 0:
                continue
            total_cost = _sum_decimal(lot["total_cost"] for lot in code_lots)
            sellable = sum(
                lot["quantity"] for lot in code_lots if lot["trade_date"] < view_date
            )
            last_price = prices.get(code)
            position_value = (
                _multiply(last_price, Decimal(quantity))
                if last_price is not None
                else None
            )
            if position_value is None:
                valuation_complete = False
            else:
                market_value = _add(market_value, position_value)
            positions.append(
                PositionView(
                    code=code,
                    quantity=quantity,
                    sellable_quantity=sellable,
                    average_cost=_divide(total_cost, Decimal(quantity)),
                    last_price=last_price,
                    market_value=position_value,
                )
            )
        complete_value = market_value if valuation_complete else None
        equity = _add(cash, market_value) if valuation_complete else None
        return TrackView(
            track=track,
            cash=cash,
            market_value=complete_value,
            equity=equity,
            positions=tuple(positions),
        )

    def _project_track(
        self,
        connection: sqlite3.Connection,
        track: PortfolioTrack,
        *,
        as_of: datetime | None = None,
    ) -> tuple[Decimal, dict[str, list[dict[str, Any]]]]:
        time_filter = " AND occurred_at <= ?" if as_of is not None else ""
        params: tuple[object, ...] = (
            (track.value, _datetime_text(as_of)) if as_of is not None else (track.value,)
        )
        cash_rows = connection.execute(
            "SELECT amount FROM cash_movements WHERE portfolio_track = ?"
            + time_filter
            + " ORDER BY occurred_at, rowid",
            params,
        ).fetchall()
        cash = Decimal(0)
        for row in cash_rows:
            cash = _add(cash, Decimal(row["amount"]))
            if cash < 0:
                raise LedgerInvariantError(f"{track.value} cash would become negative")

        fill_rows = connection.execute(
            "SELECT * FROM fills WHERE portfolio_track = ?"
            + time_filter
            + " ORDER BY occurred_at, rowid",
            params,
        ).fetchall()
        lots: dict[str, list[dict[str, Any]]] = {}
        for row in fill_rows:
            code = row["code"]
            quantity = int(row["quantity"])
            occurred = _parse_datetime(row["occurred_at"])
            trade_date = occurred.astimezone(SHANGHAI).date()
            code_lots = lots.setdefault(code, [])
            if row["side"] == "buy":
                total_cost = _add(
                    _multiply(Decimal(row["price"]), Decimal(quantity)),
                    Decimal(row["fees"]),
                )
                code_lots.append(
                    {
                        "quantity": quantity,
                        "total_cost": total_cost,
                        "trade_date": trade_date,
                    }
                )
                continue

            remaining = quantity
            for lot in code_lots:
                if lot["trade_date"] >= trade_date or lot["quantity"] == 0:
                    continue
                consumed = min(remaining, lot["quantity"])
                unit_cost = _divide(lot["total_cost"], Decimal(lot["quantity"]))
                lot["quantity"] -= consumed
                lot["total_cost"] = _subtract(
                    lot["total_cost"],
                    _multiply(unit_cost, Decimal(consumed)),
                )
                remaining -= consumed
                if remaining == 0:
                    break
            if remaining:
                raise LedgerInvariantError(
                    f"{track.value} sell exceeds T+1 sellable quantity for {code}"
                )
            lots[code] = [lot for lot in code_lots if lot["quantity"]]
        return cash, lots

    @staticmethod
    def _latest_prices(
        connection: sqlite3.Connection, as_of: datetime
    ) -> dict[str, Decimal]:
        rows = connection.execute(
            """
            SELECT p.code, p.execution_close, d.as_of, p.trade_date
            FROM decision_prices AS p
            JOIN decision_runs AS d ON d.decision_id = p.decision_id
            WHERE d.as_of <= ? AND p.execution_close IS NOT NULL
            ORDER BY p.code, d.as_of DESC, p.trade_date DESC
            """,
            (_datetime_text(as_of),),
        ).fetchall()
        prices: dict[str, Decimal] = {}
        for row in rows:
            prices.setdefault(row["code"], Decimal(row["execution_close"]))
        return prices

    @staticmethod
    def _fill_record(row: sqlite3.Row) -> FillRecord:
        return FillRecord(
            fill_id=row["fill_id"],
            source_order_intent_id=row["source_order_intent_id"],
            portfolio_track=PortfolioTrack(row["portfolio_track"]),
            code=row["code"],
            side=row["side"],
            quantity=int(row["quantity"]),
            price=Decimal(row["price"]),
            fees=Decimal(row["fees"]),
            occurred_at=_parse_datetime(row["occurred_at"]),
            note=row["note"],
        )

    @staticmethod
    def _cash_record(row: sqlite3.Row) -> CashMovementRecord:
        return CashMovementRecord(
            movement_id=row["movement_id"],
            portfolio_track=PortfolioTrack(row["portfolio_track"]),
            kind=CashMovementKind(row["kind"]),
            amount=Decimal(row["amount"]),
            occurred_at=_parse_datetime(row["occurred_at"]),
            fill_id=row["fill_id"],
            note=row["note"],
        )

    @staticmethod
    def _job_view(connection: sqlite3.Connection, run: sqlite3.Row) -> JobRunView:
        events = connection.execute(
            """
            SELECT * FROM job_attempt_events
            WHERE run_id = ? ORDER BY occurred_at, rowid
            """,
            (run["run_id"],),
        ).fetchall()
        attempts = len({row["attempt_id"] for row in events if row["phase"] == "started"})
        latest = events[-1] if events else None
        if latest is None:
            status = JobStatus.PENDING
        else:
            status = JobStatus(latest["phase"] if latest["phase"] != "started" else "running")
        return JobRunView(
            run_id=run["run_id"],
            job_type=JobType(run["job_type"]),
            scheduled_for=_parse_datetime(run["scheduled_for"]),
            status=status,
            attempts=attempts,
            latest_error=latest["error"] if latest is not None else None,
            latest_result=(
                json.loads(latest["result_json"])
                if latest is not None and latest["result_json"] is not None
                else None
            ),
        )


def _datetime_text(value: datetime) -> str:
    return (
        as_utc(value, field="datetime")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise LedgerInvariantError("money values must be finite")
    return format(value, "f")


def _optional_decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _add(left: Decimal, right: Decimal) -> Decimal:
    return LEDGER_CONTEXT.add(left, right)


def _subtract(left: Decimal, right: Decimal) -> Decimal:
    return LEDGER_CONTEXT.subtract(left, right)


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    return LEDGER_CONTEXT.multiply(left, right)


def _divide(left: Decimal, right: Decimal) -> Decimal:
    return LEDGER_CONTEXT.divide(left, right)


def _sum_decimal(values: Iterator[Decimal]) -> Decimal:
    total = Decimal(0)
    for value in values:
        total = _add(total, value)
    return total


def _require_positive_money(value: Decimal, *, field: str) -> None:
    if not value.is_finite() or value <= 0:
        raise LedgerInvariantError(f"{field} must be finite and positive")


def _require_non_negative_money(value: Decimal, *, field: str) -> None:
    if not value.is_finite() or value < 0:
        raise LedgerInvariantError(f"{field} must be finite and non-negative")


def _require_idempotency_key(value: str) -> str:
    key = value.strip()
    if not key or len(key) > 200:
        raise LedgerInvariantError("idempotency key must contain 1 to 200 characters")
    return key


def _clean_note(value: str | None) -> str | None:
    if value is None:
        return None
    note = value.strip()
    if len(note) > 1000:
        raise LedgerInvariantError("note cannot exceed 1000 characters")
    return note or None


def _json_text(value: object) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _datetime_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"unsupported JSON audit value: {type(value).__name__}")
