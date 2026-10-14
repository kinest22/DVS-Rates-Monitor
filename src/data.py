"""Persistence layer: every function that talks to the application database.

The connection state itself lives in :class:`core.DataStorage`, a
minimal class holding just a db path and per-thread connection bookkeeping.
Everything that actually touches SQLite -- schema setup, connection
handling, and CRUD -- is implemented here.

The module has two layers:

- Internal primitives (``_add_run``, ``_get_history``, ``_get_previous_run``,
  ``_get_actual_runs``, ``_add_curve``, ``_get_previous_day_curve``, ``_get_latest_curves``, ``_clear``, ``_close``, plus connection/schema
  helpers) each take an explicit ``store`` argument, so they work against any
  :class:`core.DataStorage` instance.
- Public functions (``register_run``, ``get_run_history``, etc.) are thin
  wrappers bound to the shared :data:`core.DATA` singleton, used
  everywhere else in the app.

Any future class that needs database access should follow the same split: a
small state-holding class in core.py, paired with its CRUD functions here.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import json
import sqlite3

import pandas as pd

import core

HistoryKey = tuple[str | None, str | None, str | None, str | None]


def _new_connection(store: core.DataStorage) -> sqlite3.Connection:
    """Open a fresh connection for the calling thread and register it on ``store``."""
    if store.db_path != ":memory:":
        Path(store.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if store.db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    with store._conns_lock:
        store._all_conns.append(conn)
    return conn


def _conn(store: core.DataStorage) -> sqlite3.Connection:
    """Return the calling thread's connection to ``store``, opening one if needed."""
    conn = getattr(store._local, "conn", None)
    if conn is None:
        conn = _new_connection(store)
        store._local.conn = conn
    return conn


def _init_schema(store: core.DataStorage) -> None:
    """Create the ``runs``/``quotes``/``curves`` tables on ``store`` if they don't exist yet,
    and migrate any pre-existing ``runs`` table that predates a given column."""
    conn = _conn(store)
    with conn:
        conn.executescript(core.SCHEMA_SQL)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        if "mode_override" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN mode_override TEXT")


def _row_to_run(store: core.DataStorage, row: sqlite3.Row) -> "core.QuotesRun":
    """Reconstruct a :class:`core.QuotesRun` from a ``runs`` row and its ``quotes`` rows."""
    run = core.QuotesRun(
        currency=row["currency"],
        run_name=row["run_name"],
        instrument=row["instrument"],
        underlying=row["underlying"],
        provider=row["provider"],
        header_tokens=json.loads(row["header_tokens"]) if row["header_tokens"] else [],
        unrecognized_tokens=json.loads(row["unrecognized_tokens"]) if row["unrecognized_tokens"] else [],
        size=row["size"],
        size_type=row["size_type"],
        size_currency=row["size_currency"],
        size_value=row["size_value"],
        size_unit=row["size_unit"],
        start_fx_rate=row["start_fx_rate"],
        id=row["id"],
        created_at=row["created_at"],
        mode_override=row["mode_override"],
    )
    quote_rows = _conn(store).execute(
        "SELECT * FROM quotes WHERE run_id = ? ORDER BY id", (row["id"],)
    ).fetchall()
    for quote_row in quote_rows:
        payload: dict[str, Any] = {"best_bid": quote_row["best_bid"], "best_offer": quote_row["best_offer"]}
        for column in core.QUOTE_OPTIONAL_COLUMNS:
            value = quote_row[column]
            if value is not None:
                payload[column] = value
        run.quotes[quote_row["tenor"]] = payload
    return run


def _add_run(store: core.DataStorage, run: "core.QuotesRun", timestamp: datetime | None = None) -> datetime:
    """Persist a run and its quotes on ``store``, returning the timestamp it was stored under."""
    stamp = timestamp or datetime.now()
    conn = _conn(store)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO runs (
                provider, currency, instrument, underlying, run_name,
                header_tokens, unrecognized_tokens, size, size_type,
                size_currency, size_value, size_unit, start_fx_rate, created_at,
                mode_override
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.provider,
                run.currency,
                run.instrument,
                run.underlying,
                run.run_name,
                json.dumps(run.header_tokens),
                json.dumps(run.unrecognized_tokens),
                run.size,
                run.size_type,
                run.size_currency,
                run.size_value,
                run.size_unit,
                run.start_fx_rate,
                stamp.isoformat(sep=" "),
                None,
            ),
        )
        run_id = cursor.lastrowid
        conn.executemany(
            """
            INSERT INTO quotes (
                run_id, tenor, worst_bid_provider, worst_bid, best_bid_provider, best_bid,
                mid, best_offer, best_offer_provider, worst_offer, worst_offer_provider, end_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    tenor,
                    quote.get("worst_bid_provider", run.provider),
                    quote.get("worst_bid"),
                    quote.get("best_bid_provider", run.provider),
                    quote["best_bid"],
                    (float(quote["best_bid"]) + float(quote["best_offer"])) / 2.0,
                    quote["best_offer"],
                    quote.get("best_offer_provider", run.provider),
                    quote.get("worst_offer"),
                    quote.get("worst_offer_provider", run.provider),
                    quote.get("end_date"),
                )
                for tenor, quote in run.quotes.items()
            ],
        )
    run.id = run_id
    run.created_at = stamp.isoformat(sep=" ")
    return stamp


def _get_history(
    store: core.DataStorage,
    provider: str | None = None,
    underlying: str | None = None,
    currency: str | None = None,
    instrument: str | None = None,
    as_of: datetime | None = None,
) -> list["core.QuotesRun"]:
    """Return the chronological history for a run identity, optionally up to a point in time."""
    clauses = []
    params: list[Any] = []
    for column, value in (
        ("provider", provider),
        ("underlying", underlying),
        ("currency", currency),
        ("instrument", instrument),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if as_of is not None:
        clauses.append("created_at <= ?")
        params.append(as_of.isoformat(sep=" "))

    query = "SELECT * FROM runs"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at, id"

    rows = _conn(store).execute(query, params).fetchall()
    return [_row_to_run(store, row) for row in rows]


def _get_previous_run(store: core.DataStorage, run: "core.QuotesRun") -> "core.QuotesRun | None":
    """Return the run stored immediately before ``run`` for the same identity.

    "Same identity" means matching ``provider``, ``underlying``, ``currency``
    and ``instrument``. Returns ``None`` if ``run`` hasn't been persisted
    (no ``id``/``created_at``) or if it's the first run for its identity.
    """
    if run.id is None or run.created_at is None:
        return None
    row = _conn(store).execute(
        """
        SELECT * FROM runs
        WHERE provider IS ? AND underlying IS ? AND currency IS ? AND instrument IS ?
          AND (created_at < ? OR (created_at = ? AND id < ?))
          AND EXISTS (SELECT 1 FROM quotes WHERE quotes.run_id = runs.id)
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (run.provider, run.underlying, run.currency, run.instrument, run.created_at, run.created_at, run.id),
    ).fetchone()
    return _row_to_run(store, row) if row is not None else None


def _get_actual_runs(store: core.DataStorage, as_of: datetime | None = None) -> list["core.QuotesRun"]:
    """Return the latest run for each identity that is active as of the supplied time.

    Runs stored without any quotes (e.g. a run whose lines the parser couldn't
    read) are skipped, so they never hide the identity's last usable run.
    """
    query = "SELECT * FROM runs WHERE EXISTS (SELECT 1 FROM quotes WHERE quotes.run_id = runs.id)"
    params: list[Any] = []
    if as_of is not None:
        query += " AND created_at <= ?"
        params.append(as_of.isoformat(sep=" "))
    query += " ORDER BY created_at, id"

    rows = _conn(store).execute(query, params).fetchall()
    candidates: dict[HistoryKey, sqlite3.Row] = {}
    for row in rows:
        key = (row["provider"], row["underlying"], row["currency"], row["instrument"])
        current = candidates.get(key)
        if current is None or row["created_at"] > current["created_at"]:
            candidates[key] = row

    ordered_rows = sorted(candidates.values(), key=lambda row: (row["created_at"], row["id"]))
    return [_row_to_run(store, row) for row in ordered_rows]


def _set_run_mode_override(store: core.DataStorage, run_id: int, mode: str | None) -> None:
    """Persist an explicit FIRM/VIA FRESH override on a run row."""
    conn = _conn(store)
    with conn:
        conn.execute("UPDATE runs SET mode_override = ? WHERE id = ?", (mode, run_id))


def _add_curve(store: core.DataStorage, curve: "core.Curve") -> bool:
    """Persist a curve's points on ``store``, one ``curves`` row per tenor.

    A curve is identified by its instrument, currency, type, compounding and
    time. If points for that identity are already stored and match exactly,
    nothing is written and ``False`` is returned; if they differ (e.g. a direct
    quote changed a level without a new run being parsed), they are replaced.
    """
    identity = (
        curve.Time.isoformat(sep=" "),
        curve.Instrument,
        curve.Currency,
        curve.CurveType.name,
        curve.Compounding.name,
    )
    points = [
        (str(row["Tenor"]), float(row["Term"]), float(row["Rate"]), float(row["DF"]))
        for _, row in curve.levels().iterrows()
    ]

    conn = _conn(store)
    identity_clause = "time = ? AND instrument IS ? AND currency IS ? AND type = ? AND cmpd = ?"
    stored_points = [
        tuple(row)
        for row in conn.execute(
            f"SELECT tenor, term, value, df FROM curves WHERE {identity_clause} ORDER BY id",
            identity,
        ).fetchall()
    ]
    if stored_points == points:
        return False

    with conn:
        conn.execute(f"DELETE FROM curves WHERE {identity_clause}", identity)
        conn.executemany(
            """
            INSERT INTO curves (time, instrument, currency, type, cmpd, tenor, term, value, df)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [identity + point for point in points],
        )
    return True


def _get_previous_day_curve(store: core.DataStorage, curve: "core.Curve") -> "core.Curve | None":
    """Return the last curve stored on the most recent day before ``curve``'s day.

    Only curves with the same instrument, currency, type and compounding are
    considered. "Most recent day" skips days with nothing stored, so on a
    Monday this is usually Friday's closing curve. Returns ``None`` if there is
    no earlier curve. The curve is rebuilt from its stored tenors and rates, so
    terms and discount factors are recomputed exactly as for a fresh curve.
    """
    conn = _conn(store)
    identity = (curve.Instrument, curve.Currency, curve.CurveType.name, curve.Compounding.name)
    identity_clause = "instrument IS ? AND currency IS ? AND type = ? AND cmpd = ?"
    row = conn.execute(
        f"SELECT MAX(time) AS time FROM curves WHERE {identity_clause} AND time < ?",
        identity + (curve.Time.date().isoformat(),),
    ).fetchone()
    if row["time"] is None:
        return None

    return _load_curve(conn, row["time"], *identity)


def _load_curve(
    conn: sqlite3.Connection, time: str, instrument: str, currency: str, curve_type: str, cmpd: str
) -> "core.Curve":
    """Rebuild a stored curve from its tenors and rates, so terms and discount
    factors are recomputed exactly as for a fresh curve."""
    points = conn.execute(
        "SELECT tenor, value FROM curves "
        "WHERE time = ? AND instrument IS ? AND currency IS ? AND type = ? AND cmpd = ? ORDER BY id",
        (time, instrument, currency, curve_type, cmpd),
    ).fetchall()
    term_struct = pd.DataFrame([{"Tenor": point["tenor"], "Rate": point["value"]} for point in points])
    return core.Curve(
        datetime.fromisoformat(time),
        instrument,
        currency,
        term_struct,
        core.RateType[curve_type],
        core.Compounding[cmpd],
    )


def _get_latest_curves(store: core.DataStorage) -> list["core.Curve"]:
    """Return the most recently stored curve for every instrument, currency,
    type and compounding, ordered by instrument and currency."""
    conn = _conn(store)
    rows = conn.execute(
        """
        SELECT MAX(time) AS time, instrument, currency, type, cmpd FROM curves
        GROUP BY instrument, currency, type, cmpd
        ORDER BY instrument, currency
        """
    ).fetchall()
    return [
        _load_curve(conn, row["time"], row["instrument"], row["currency"], row["type"], row["cmpd"])
        for row in rows
    ]


def _clear(store: core.DataStorage) -> None:
    """Delete all stored runs and quotes from ``store``."""
    conn = _conn(store)
    with conn:
        conn.execute("DELETE FROM quotes")
        conn.execute("DELETE FROM runs")


def _close(store: core.DataStorage) -> None:
    """Close every per-thread SQLite connection opened so far on ``store``."""
    with store._conns_lock:
        conns, store._all_conns = store._all_conns, []
    for conn in conns:
        conn.close()
    store._local.conn = None


# The singleton itself is created in core.py; ensure its schema exists.
_init_schema(core.DATA)


def register_run(run: "core.QuotesRun") -> None:
    """Persist a previously parsed run in the run history."""
    _add_run(core.DATA, run)


def set_run_mode_override(run_id: int, mode: str | None) -> None:
    """Persist an explicit FIRM/VIA FRESH choice for a run, so it survives page
    reloads. A fresh parse of the same identity starts a new row with no
    override, which is what clears it back to the date-based default."""
    _set_run_mode_override(core.DATA, run_id, mode)


def register_curve(curve: "core.Curve") -> bool:
    """Persist a curve unless an identical copy is already stored; return whether it was written."""
    return _add_curve(core.DATA, curve)


def get_previous_day_curve(curve: "core.Curve") -> "core.Curve | None":
    """Return the last stored curve from the most recent day before ``curve``'s day, if any."""
    return _get_previous_day_curve(core.DATA, curve)


def get_latest_curves() -> list["core.Curve"]:
    """Return the most recently stored curve for every instrument/currency/type/compounding."""
    return _get_latest_curves(core.DATA)


def get_run_history(
    provider: str | None = None,
    underlying: str | None = None,
    currency: str | None = None,
    instrument: str | None = None,
    as_of: datetime | None = None,
) -> list["core.QuotesRun"]:
    """Return the chronological history for a run identity.

    If no filters are supplied, the full run storage is returned in append order.
    """
    return _get_history(
        core.DATA,
        provider=provider,
        underlying=underlying,
        currency=currency,
        instrument=instrument,
        as_of=as_of,
    )


def get_previous_run(run: "core.QuotesRun") -> "core.QuotesRun | None":
    """Return the run stored immediately before ``run`` for the same identity."""
    return _get_previous_run(core.DATA, run)


def get_actual_runs(as_of: datetime | None = None) -> list["core.QuotesRun"]:
    """Return the latest run for each identity that is active as of the supplied time."""
    return _get_actual_runs(core.DATA, as_of=as_of)


def clear_run_storage() -> None:
    """Delete all stored runs and quotes from the SQLite-backed history."""
    _clear(core.DATA)
