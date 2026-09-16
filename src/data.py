"""Persistence layer: every function that talks to the run-history database.

The connection state itself lives in :class:`core.RunHistoryStore`, a
minimal class holding just a db path and per-thread connection bookkeeping.
Everything that actually touches SQLite -- schema setup, connection
handling, and CRUD -- is implemented here.

The module has two layers:

- Internal primitives (``_add_run``, ``_get_history``, ``_get_previous_run``,
  ``_get_actual_runs``, ``_clear``, ``_close``, plus connection/schema
  helpers) each take an explicit ``store`` argument, so they work against any
  :class:`core.RunHistoryStore` instance.
- Public functions (``register_run``, ``get_run_history``, etc.) are thin
  wrappers bound to the shared :data:`core.RUN_HISTORY` singleton, used
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

import core

HistoryKey = tuple[str | None, str | None, str | None, str | None]


def _new_connection(store: core.RunHistoryStore) -> sqlite3.Connection:
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


def _conn(store: core.RunHistoryStore) -> sqlite3.Connection:
    """Return the calling thread's connection to ``store``, opening one if needed."""
    conn = getattr(store._local, "conn", None)
    if conn is None:
        conn = _new_connection(store)
        store._local.conn = conn
    return conn


def _init_schema(store: core.RunHistoryStore) -> None:
    """Create the ``runs``/``quotes`` tables on ``store`` if they don't exist yet."""
    conn = _conn(store)
    with conn:
        conn.executescript(core.SCHEMA_SQL)


def _row_to_run(store: core.RunHistoryStore, row: sqlite3.Row) -> "core.QuotesRun":
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


def _add_run(store: core.RunHistoryStore, run: "core.QuotesRun", timestamp: datetime | None = None) -> datetime:
    """Persist a run and its quotes on ``store``, returning the timestamp it was stored under."""
    stamp = timestamp or datetime.now()
    conn = _conn(store)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO runs (
                provider, currency, instrument, underlying, run_name,
                header_tokens, unrecognized_tokens, size, size_type,
                size_currency, size_value, size_unit, start_fx_rate, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    store: core.RunHistoryStore,
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


def _get_previous_run(store: core.RunHistoryStore, run: "core.QuotesRun") -> "core.QuotesRun | None":
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
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (run.provider, run.underlying, run.currency, run.instrument, run.created_at, run.created_at, run.id),
    ).fetchone()
    return _row_to_run(store, row) if row is not None else None


def _get_actual_runs(store: core.RunHistoryStore, as_of: datetime | None = None) -> list["core.QuotesRun"]:
    """Return the latest run for each identity that is active as of the supplied time."""
    query = "SELECT * FROM runs"
    params: list[Any] = []
    if as_of is not None:
        query += " WHERE created_at <= ?"
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


def _clear(store: core.RunHistoryStore) -> None:
    """Delete all stored runs and quotes from ``store``."""
    conn = _conn(store)
    with conn:
        conn.execute("DELETE FROM quotes")
        conn.execute("DELETE FROM runs")


def _close(store: core.RunHistoryStore) -> None:
    """Close every per-thread SQLite connection opened so far on ``store``."""
    with store._conns_lock:
        conns, store._all_conns = store._all_conns, []
    for conn in conns:
        conn.close()
    store._local.conn = None


# The singleton itself is created in core.py; ensure its schema exists.
_init_schema(core.RUN_HISTORY)


def register_run(run: "core.QuotesRun") -> None:
    """Persist a previously parsed run in the run history."""
    _add_run(core.RUN_HISTORY, run)


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
        core.RUN_HISTORY,
        provider=provider,
        underlying=underlying,
        currency=currency,
        instrument=instrument,
        as_of=as_of,
    )


def get_previous_run(run: "core.QuotesRun") -> "core.QuotesRun | None":
    """Return the run stored immediately before ``run`` for the same identity."""
    return _get_previous_run(core.RUN_HISTORY, run)


def get_actual_runs(as_of: datetime | None = None) -> list["core.QuotesRun"]:
    """Return the latest run for each identity that is active as of the supplied time."""
    return _get_actual_runs(core.RUN_HISTORY, as_of=as_of)


def clear_run_storage() -> None:
    """Delete all stored runs and quotes from the SQLite-backed history."""
    _clear(core.RUN_HISTORY)
