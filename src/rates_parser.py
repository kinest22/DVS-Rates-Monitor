"""Utilities for parsing swap-run text blocks into structured SwapRun objects.

The module is intentionally lightweight and works directly on raw text captured
from market-run reports. It exposes:

- a single-run parser, :func:`parse_swap_run`
- a multi-run parser, :func:`parse_multiple_runs`
- a small set of normalization/helpers used to infer instrument and size metadata

The public API returns a :class:`SwapRun` instance with a compact string
representation suitable for notebook or console inspection.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import json
import os
import re
import sqlite3
import threading

import pandas as pd


DEFAULT_DB_PATH = os.environ.get(
    "RATES_MONITOR_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "runhist.db"),
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT,
    currency TEXT,
    instrument TEXT,
    underlying TEXT,
    run_name TEXT,
    header_tokens TEXT,
    unrecognized_tokens TEXT,
    size TEXT,
    size_type TEXT,
    size_currency TEXT,
    size_value REAL,
    size_unit TEXT,
    start_fx_rate REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_identity
    ON runs (provider, underlying, currency, instrument, created_at);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    tenor TEXT NOT NULL,
    worst_bid_provider TEXT,
    worst_bid REAL,
    best_bid_provider TEXT,
    best_bid REAL NOT NULL,
    mid REAL NOT NULL,
    best_offer REAL NOT NULL,
    best_offer_provider TEXT,
    worst_offer REAL,
    worst_offer_provider TEXT,
    end_date TEXT
);

CREATE INDEX IF NOT EXISTS idx_quotes_run_id ON quotes (run_id);
"""

QUOTE_OPTIONAL_COLUMNS = (
    "end_date",
    "worst_bid",
    "worst_bid_provider",
    "best_bid_provider",
    "best_offer_provider",
    "worst_offer",
    "worst_offer_provider",
)


CURRENCY_CODES = frozenset({"RUB", "USD", "CNY", "CNH", "EUR"})
INSTRUMENT_TYPES = frozenset({"OIS", "KRS", "IRS", "XCCY", "CCIRS", "FXSWAP"})
INDICATOR_CODES = frozenset({"RUONIA", "SOFR", "RUSFAR", "RUSFARON", "KEYRATE", "KEY RATE"})
HEADER_IGNORE_TOKENS = frozenset({"RUN", "SWAP", "BID", "OFFER", "TERM", "END"})

HEADER_RE = re.compile(
    r"^(?P<currency>[A-Z]{3})\s+(?P<run_name>.+)$",
    flags=re.IGNORECASE,
)
FOOTER_RE = re.compile(
    r"^(?P<size_value>\d+(?:[.,]\d+)?)\s*(?P<size_unit>mio|mln|bio|bln|b|k|m)?\s*(?P<size_currency>RUB|USD|CNY|CNH|EUR)?\s*(?P<size_type>DV|PAR|DV-CENTRIC|PAR-CENTRIC)?(?:\s*,?\s*(?P<tail>.*))?$",
    flags=re.IGNORECASE,
)
QUOTE_RE = re.compile(
    r"^(?P<tenor>[0-9]+[A-Za-z]+)\s+(?P<bid>\d+(?:[.,]\d+)?)\s*(?:/|\s+)\s*(?P<offer>\d+(?:[.,]\d+)?)\s*(?P<end_date>(?:\d{2}[./]\d{2}[./]\d{4}))?$",
    flags=re.IGNORECASE,
)

HistoryKey = tuple[str | None, str | None, str | None, str | None]


class RunHistoryStore:
    """Persist parsed runs in SQLite and expose latest active runs by identity."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        with self._conn:
            self._conn.executescript(SCHEMA_SQL)

    def _new_connection(self) -> sqlite3.Connection:
        """Open a fresh connection for the calling thread.

        Streamlit reruns each session's script in its own worker thread, and
        sqlite3 connections are bound to the thread that created them, so a
        single shared connection silently breaks across reruns. Each thread
        gets its own connection to the same on-disk database instead; WAL
        mode lets those connections read/write concurrently without locking
        each other out.
        """
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def add(self, run: "QuotesRun", timestamp: datetime | None = None) -> datetime:
        """Persist a run and its quotes, returning the timestamp it was stored under."""
        stamp = timestamp or datetime.now()
        with self._conn:
            cursor = self._conn.execute(
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
            self._conn.executemany(
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

    def _row_to_run(self, row: sqlite3.Row) -> "QuotesRun":
        """Reconstruct a :class:`QuotesRun` from a ``runs`` row and its ``quotes`` rows."""
        run = QuotesRun(
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
        quote_rows = self._conn.execute(
            "SELECT * FROM quotes WHERE run_id = ? ORDER BY id", (row["id"],)
        ).fetchall()
        for quote_row in quote_rows:
            payload: dict[str, Any] = {"best_bid": quote_row["best_bid"], "best_offer": quote_row["best_offer"]}
            for column in QUOTE_OPTIONAL_COLUMNS:
                value = quote_row[column]
                if value is not None:
                    payload[column] = value
            run.quotes[quote_row["tenor"]] = payload
        return run

    def get_history(
        self,
        provider: str | None = None,
        underlying: str | None = None,
        currency: str | None = None,
        instrument: str | None = None,
        as_of: datetime | None = None,
    ) -> list["QuotesRun"]:
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

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_run(row) for row in rows]

    def get_previous_run(self, run: "QuotesRun") -> "QuotesRun | None":
        """Return the run stored immediately before ``run`` for the same identity.

        "Same identity" means matching ``provider``, ``underlying``, ``currency``
        and ``instrument``. Returns ``None`` if ``run`` hasn't been persisted
        (no ``id``/``created_at``) or if it's the first run for its identity.
        """
        if run.id is None or run.created_at is None:
            return None
        row = self._conn.execute(
            """
            SELECT * FROM runs
            WHERE provider IS ? AND underlying IS ? AND currency IS ? AND instrument IS ?
              AND (created_at < ? OR (created_at = ? AND id < ?))
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (run.provider, run.underlying, run.currency, run.instrument, run.created_at, run.created_at, run.id),
        ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def get_actual_runs(self, as_of: datetime | None = None) -> list["QuotesRun"]:
        """Return the latest run for each identity that is active as of the supplied time."""
        query = "SELECT * FROM runs"
        params: list[Any] = []
        if as_of is not None:
            query += " WHERE created_at <= ?"
            params.append(as_of.isoformat(sep=" "))
        query += " ORDER BY created_at, id"

        rows = self._conn.execute(query, params).fetchall()
        candidates: dict[HistoryKey, sqlite3.Row] = {}
        for row in rows:
            key = (row["provider"], row["underlying"], row["currency"], row["instrument"])
            current = candidates.get(key)
            if current is None or row["created_at"] > current["created_at"]:
                candidates[key] = row

        ordered_rows = sorted(candidates.values(), key=lambda row: (row["created_at"], row["id"]))
        return [self._row_to_run(row) for row in ordered_rows]

    def clear(self) -> None:
        """Delete all stored runs and quotes from the database."""
        with self._conn:
            self._conn.execute("DELETE FROM quotes")
            self._conn.execute("DELETE FROM runs")

    def close(self) -> None:
        """Close every per-thread SQLite connection opened so far."""
        with self._conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            conn.close()
        self._local.conn = None


RUN_HISTORY = RunHistoryStore()


class QuotesRun:
    """Parsed representation of an interest-rate swap run.

    Attributes:
        currency: three-letter currency code, e.g. ``RUB``.
        run_name: full header text without ``Bid``/``Offer`` words.
        instrument: swap family, e.g. ``OIS``, ``KRS``, ``XCCY`` or ``CCIRS``.
        underlying: swap reference curve(s), e.g. ``RUONIA`` or ``RUONIA/SOFR``.
        quotes: mapping from tenor such as ``3M`` to ``{"best_bid": ..., "best_offer": ...}``.
        size: quantity printed in the run footer, e.g. ``1k``.
        size_type: the sizing descriptor printed next to the quantity, e.g. ``DV``.
        size_currency: the currency descriptor printed next to the quantity, e.g. ``USD``.
    """

    def __init__(
        self,
        currency: str,
        run_name: str,
        instrument: str | None = None,
        underlying: str | None = None,
        provider: str | None = None,
        header_tokens: list[str] | None = None,
        unrecognized_tokens: list[str] | None = None,
        quotes: dict[str, dict[str, float]] | None = None,
        size: str | None = None,
        size_type: str | None = None,
        size_currency: str | None = None,
        size_value: float | None = None,
        size_unit: str | None = None,
        start_fx_rate: float | None = None,
        id: int | None = None,
        created_at: str | None = None,
    ) -> None:
        self.currency = currency
        self.run_name = run_name
        self.instrument = instrument
        self.underlying = underlying
        self.provider = provider
        self.header_tokens = header_tokens or []
        self.unrecognized_tokens = unrecognized_tokens or []
        self.quotes = quotes or {}
        self.size = size
        self.size_type = size_type
        self.size_currency = size_currency
        self.size_value = size_value
        self.size_unit = size_unit
        self.start_fx_rate = start_fx_rate
        self.id = id
        self.created_at = created_at

    def to_frame(self, with_changes: bool = False) -> pd.DataFrame:
        """Return the parsed quote matrix as a pandas DataFrame.

        The frame uses one row per tenor and expands the quote payload to a
        provider-aware ranking layout where the bid/offer descriptors are kept
        explicit. For a single-provider run, the best and bad levels propagate the
        same numeric values and provider label, matching the expected display.

        A quote payload may optionally carry ``worst_bid``, ``worst_offer`` and their
        matching ``*_provider`` keys (used for joint runs built across several
        providers, see :func:`create_joint_runs`); when absent, the worst levels and
        their providers default to the best bid/offer and this run's provider.

        When ``with_changes`` is True, the frame gains ``best_bid_change_bp`` and
        ``best_offer_change_bp`` columns holding the per-tenor delta, in basis
        points, against this run's previous run for the same identity (see
        :meth:`RunHistoryStore.get_previous_run`). Both columns are ``NA`` when
        there is no previous run, or for tenors absent from it.
        """
        rows = []
        has_end_dates = any(
            isinstance(quote, dict) and quote.get("end_date") is not None
            for quote in self.quotes.values()
        )
        provider = self.provider
        is_fx_swap = self.instrument == "FXSWAP"

        for tenor, quote in self.quotes.items():
            best_bid = float(quote["best_bid"])
            best_offer = float(quote["best_offer"])
            worst_bid = float(quote.get("worst_bid", best_bid))
            worst_offer = float(quote.get("worst_offer", best_offer))
            mid = (best_bid + best_offer) / 2.0
            spread = best_offer - best_bid
            spread_bp = spread * 100.0 if not is_fx_swap else spread
            worst_spread = worst_offer - worst_bid
            worst_spread_bp = worst_spread * 100.0 if not is_fx_swap else worst_spread

            row = {
                "tenor": tenor,
                "worst_bid_provider": quote.get("worst_bid_provider", provider),
                "worst_bid": worst_bid,
                "best_bid_provider": quote.get("best_bid_provider", provider),
                "best_bid": best_bid,
                "mid": mid,
                "best_offer": best_offer,
                "best_offer_provider": quote.get("best_offer_provider", provider),
                "worst_offer": worst_offer,
                "worst_offer_provider": quote.get("worst_offer_provider", provider),
                "best_bid_offer_spread": spread_bp,
                "worst_bid_offer_spread": worst_spread_bp,
            }
            if has_end_dates:
                row["end_date"] = quote.get("end_date")
            rows.append(row)
        frame = pd.DataFrame(rows)

        if with_changes and not frame.empty:
            previous_run = RUN_HISTORY.get_previous_run(self)
            bp_scale = 1.0 if is_fx_swap else 100.0
            if previous_run is not None:
                previous_levels = previous_run.to_frame()[["tenor", "best_bid", "best_offer"]].rename(
                    columns={"best_bid": "_prev_best_bid", "best_offer": "_prev_best_offer"}
                )
                frame = frame.merge(previous_levels, on="tenor", how="left")
                frame["best_bid_change_bp"] = (frame["best_bid"] - frame["_prev_best_bid"]) * bp_scale
                frame["best_offer_change_bp"] = (frame["best_offer"] - frame["_prev_best_offer"]) * bp_scale
                frame = frame.drop(columns=["_prev_best_bid", "_prev_best_offer"])
            else:
                frame["best_bid_change_bp"] = pd.NA
                frame["best_offer_change_bp"] = pd.NA

        return frame

    def as_dict(self) -> dict[str, Any]:
        """Expose the parsed object as a plain dictionary.

        This is useful for notebook inspection, serialization, or exporting the
        parsed run to JSON-like structures.
        """
        return {
            "currency": self.currency,
            "run_name": self.run_name,
            "instrument": self.instrument,
            "underlying": self.underlying,
            "provider": self.provider,
            "header_tokens": self.header_tokens,
            "unrecognized_tokens": self.unrecognized_tokens,
            "size": self.size,
            "size_type": self.size_type,
            "size_currency": self.size_currency,
            "size_value": self.size_value,
            "size_unit": self.size_unit,
            "start_fx_rate": self.start_fx_rate,
            "quotes": self.quotes,
        }

    def __repr__(self) -> str:
        """Return the same readable representation used for display."""
        return self.__str__()

    def __str__(self) -> str:
        """Render a compact multi-line summary of the run.

        The output keeps the metadata block on separate lines, puts the size in a
        single compact row, and follows it with a quote table.
        """
        frame = self.to_frame()
        quote_table = frame.to_string(index=False) if not frame.empty else "(no quotes)"

        metadata_lines = ["SwapRun"]
        metadata_pairs = [
            ("instrument", self.instrument),
            ("currency", self.currency),
            ("underlying", self.underlying),
            ("provider", self.provider),
            ("start_fx_rate", self.start_fx_rate),
        ]

        for field_name, value in metadata_pairs:
            if value in (None, "", [], {}):
                continue
            metadata_lines.append(f"  {field_name}: {value}")

        size_display_parts = []
        if self.size is not None:
            size_display_parts.append(self.size)
        if self.size_type is not None:
            size_display_parts.append(self.size_type.upper())
        if self.size_currency is not None:
            size_display_parts.append(self.size_currency)

        if size_display_parts:
            metadata_lines.append(f"  size: {' '.join(size_display_parts)}")

        metadata_block = "\n".join(metadata_lines)
        return f"{metadata_block}\n\n{quote_table}"


def _normalize_decimal(value: str) -> float:
    """Convert a decimal string that may use comma separators into a float."""
    return float(value.replace(",", "."))


def _normalize_size(raw_size: str | None, raw_unit: str | None) -> tuple[str | None, float | None, str | None]:
    """Normalize and display the footer size payload.

    The parser keeps the original numeric string for presentation while also
    returning the normalized float value and canonical unit token.
    """
    if raw_size is None:
        return None, None, None

    numeric = float(raw_size.replace(",", "."))
    unit = (raw_unit or "").lower()
    if unit in {"mio", "mln"}:
        canonical_unit = "mio"
    elif unit in {"bio", "bln", "b"}:
        canonical_unit = "bio"
    elif unit in {"k", "m"}:
        canonical_unit = unit
    else:
        canonical_unit = None

    display_size = raw_size
    if canonical_unit:
        display_size = f"{raw_size}{canonical_unit}"

    return display_size, numeric, canonical_unit


def _normalize_size_type(raw_size_type: str | None) -> str | None:
    """Normalize the footer size descriptor into the canonical DV/PAR form."""
    if raw_size_type is None:
        return "par"
    normalized = raw_size_type.strip().upper()
    if normalized in {"DV", "DV-CENTRIC"}:
        return "dv"
    return "par"


def _normalize_token(token: str) -> str:
    """Normalize header aliases such as ``KEY RATE`` and ``FX SWAP``.

    The function removes punctuation and collapses some multi-word identifiers to
    a single canonical token so that header inference is stable.
    """
    token = token.strip().upper()
    token = token.replace("KEY RATE", "KEYRATE")
    token = token.replace("RUSFAR ON", "RUSFARON")
    token = token.replace("FX SWAP", "FXSWAP")
    token = token.replace("-", "")
    return token


def _tokenize_header(header_text: str) -> list[str]:
    """Split a header into normalized tokens for instrument/indicator inference."""
    prepared = header_text.strip()
    prepared = re.sub(r"\bKEY\s+RATE\b", "KEYRATE", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"\bRUSFAR\s+ON\b", "RUSFARON", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"\bFX\s+SWAP\b", "FXSWAP", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"\bBID\s+OFFER\b", "", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"\s+", " ", prepared)
    return [_normalize_token(token) for token in prepared.split(" ") if token.strip()]


def _is_indicator_token(token: str) -> bool:
    """Return True when the token represents a valid underlying indicator."""
    if token in INDICATOR_CODES:
        return True
    valid_parts = INDICATOR_CODES | CURRENCY_CODES
    return "/" in token and all(part in valid_parts for part in token.split("/"))


def _is_currency_token(token: str) -> bool:
    """Return True when the token is one of the supported currency codes."""
    return token in CURRENCY_CODES


def _is_instrument_token(token: str) -> bool:
    """Return True when the token is a known swap instrument family."""
    return token in INSTRUMENT_TYPES


def _normalize_pair(lhs: str, rhs: str) -> str:
    """Create a canonical ``lhs/rhs`` pair string used for underlyings."""
    return f"{lhs}/{rhs}"


def _infer_header_metadata(header_tokens: list[str]) -> tuple[str | None, str | None, str | None, list[str], list[str]]:
    """Infer the currency, instrument family, and underlying indicator from a token list.

    The function keeps track of which tokens were recognized and which were
    left unrecognized so the caller can preserve the original header structure
    without losing information.
    """
    currency: str | None = None
    instrument_type: str | None = None
    underlying_indicator: str | None = None
    recognized_tokens: list[str] = []
    unrecognized_tokens: list[str] = []

    for index, token in enumerate(header_tokens):
        if token in HEADER_IGNORE_TOKENS:
            unrecognized_tokens.append(token)
            continue

        if _is_instrument_token(token):
            instrument_type = token
            recognized_tokens.append(token)
            continue

        if _is_currency_token(token) and index == 0:
            currency = token
            recognized_tokens.append(token)
            continue

        if _is_currency_token(token):
            # keep non-leading currency tokens for pair inference rather than overwriting the run currency
            continue

        if _is_indicator_token(token):
            underlying_indicator = token
            recognized_tokens.append(token)
            continue

        unrecognized_tokens.append(token)

    if currency is None:
        currency = "RUB"
        if instrument_type == "FXSWAP":
            fx_currency_tokens = [token for token in header_tokens if _is_currency_token(token)]
            if fx_currency_tokens:
                currency = fx_currency_tokens[0]

    if instrument_type == "FXSWAP":
        currency_tokens = [token for token in header_tokens if _is_currency_token(token)]
        if len(currency_tokens) >= 2:
            underlying_indicator = _normalize_pair(currency_tokens[0], currency_tokens[-1])
        elif len(currency_tokens) == 1:
            underlying_indicator = _normalize_pair(currency_tokens[0], "RUB")
        else:
            underlying_indicator = _normalize_pair(currency, "RUB")

    elif instrument_type in {"OIS", "KRS", "CCIRS", "XCCY"}:
        if underlying_indicator is None:
            indicator_tokens = [token for token in header_tokens if _is_indicator_token(token)]
            if indicator_tokens:
                underlying_indicator = indicator_tokens[0]

        if underlying_indicator is not None and "/" not in underlying_indicator:
            underlying_indicator = _normalize_pair(underlying_indicator, currency)

        if instrument_type == "XCCY" and underlying_indicator is not None and "/" in underlying_indicator:
            lhs, rhs = underlying_indicator.split("/", 1)
            if lhs in CURRENCY_CODES and rhs in INDICATOR_CODES:
                underlying_indicator = _normalize_pair(rhs, lhs)

        if instrument_type == "KRS" and underlying_indicator is None:
            underlying_indicator = _normalize_pair("KEYRATE", currency)

    elif instrument_type == "IRS":
        indicator_tokens = [token for token in header_tokens if _is_indicator_token(token)]
        if len(indicator_tokens) >= 2:
            underlying_indicator = _normalize_pair(indicator_tokens[0], indicator_tokens[1])
        elif indicator_tokens:
            underlying_indicator = _normalize_pair(indicator_tokens[0], currency)

    return currency, instrument_type, underlying_indicator, recognized_tokens, unrecognized_tokens


def _is_run_header_line(line: str) -> bool:
    """Return True when a line appears to begin a new swap run block."""
    if HEADER_RE.match(line):
        return True

    parts = line.split()
    if len(parts) < 2:
        return False

    # Headers may lead with a currency (``RUB KRS run``) or, for cross-currency
    # instruments, with the instrument family instead (``XCCY CNH/RUONIA run``).
    leading_token = parts[0].upper()
    return leading_token in CURRENCY_CODES or leading_token in INSTRUMENT_TYPES


def _register_run(run: "QuotesRun") -> None:
    """Store a parsed run globally and attach it to its identity-based history."""
    RUN_HISTORY.add(run)


def get_run_history(
    provider: str | None = None,
    underlying: str | None = None,
    currency: str | None = None,
    instrument: str | None = None,
    as_of: datetime | None = None,
) -> list[QuotesRun]:
    """Return the chronological history for a run identity.

    If no filters are supplied, the full run storage is returned in append order.
    """
    return RUN_HISTORY.get_history(
        provider=provider,
        underlying=underlying,
        currency=currency,
        instrument=instrument,
        as_of=as_of,
    )


def get_actual_runs(as_of: datetime | None = None) -> list[QuotesRun]:
    """Return the latest run for each identity that is active as of the supplied time."""
    return RUN_HISTORY.get_actual_runs(as_of=as_of)


def create_joint_runs(actual_runs: list[QuotesRun]) -> list[QuotesRun]:
    """Group the actual runs by instrument, currency, and underlying and join them.

    Each provider's latest active run (see :meth:`RunHistoryStore.get_actual_runs`)
    is bucketed by its ``(instrument, currency, underlying)`` identity, ignoring
    ``provider``. Each bucket becomes a single :class:`QuotesRun` combining every
    provider quoting that identity: for each tenor, the best bid is the highest
    bid across the joined providers and the worst bid is the lowest; the
    best offer is the lowest offer and the worst offer is the highest. Provider
    labels are rewritten to whoever actually quoted the selected level, and
    ``mid``/spreads are recalculated from the best bid and best offer by
    :meth:`QuotesRun.to_frame`. Each resulting joint run is registered in
    :data:`RUN_HISTORY` under the ``"joint"`` provider, same as a parsed run.
    """
    groups: dict[tuple[str | None, str | None, str | None], list[QuotesRun]] = {}
    for run in actual_runs:
        key = (run.instrument, run.currency, run.underlying)
        groups.setdefault(key, []).append(run)

    joint_runs = []
    for (instrument, currency, underlying), runs in groups.items():
        #providers = sorted({run.provider for run in runs if run.provider is not None})
        joint_run = QuotesRun(
            currency=currency,
            run_name=runs[0].run_name,
            instrument=instrument,
            underlying=underlying,
            provider="joint",
        )

        tenor_entries: dict[str, list[tuple[str | None, float, float, str | None]]] = {}
        for run in runs:
            for tenor, quote in run.quotes.items():
                tenor_entries.setdefault(tenor, []).append(
                    (run.provider, float(quote["best_bid"]), float(quote["best_offer"]), quote.get("end_date"))
                )

        for tenor, entries in tenor_entries.items():
            best_bid_provider, best_bid, _, _ = max(entries, key=lambda entry: entry[1])
            worst_bid_provider, worst_bid, _, _ = min(entries, key=lambda entry: entry[1])
            best_offer_provider, _, best_offer, _ = min(entries, key=lambda entry: entry[2])
            worst_offer_provider, _, worst_offer, _ = max(entries, key=lambda entry: entry[2])
            end_dates = [entry[3] for entry in entries if entry[3] is not None]

            quote_payload = {
                "best_bid": best_bid,
                "best_offer": best_offer,
                "best_bid_provider": best_bid_provider,
                "worst_bid": worst_bid,
                "worst_bid_provider": worst_bid_provider,
                "best_offer_provider": best_offer_provider,
                "worst_offer": worst_offer,
                "worst_offer_provider": worst_offer_provider,
            }
            if end_dates:
                quote_payload["end_date"] = end_dates[0]
            joint_run.quotes[tenor] = quote_payload
        joint_runs.append(joint_run)

    return joint_runs


def clear_run_storage() -> None:
    """Delete all stored runs and quotes from the SQLite-backed history."""
    RUN_HISTORY.clear()


def parse_multiple_runs(run_text: str, provider: str | None = None) -> list[QuotesRun]:
    """Parse a text blob containing multiple swap runs from a single provider.

    The function locates each header line, slices the text between headers,
    and then delegates each block to :func:`parse_swap_run`.
    """
    lines = [line.strip() for line in run_text.strip().splitlines() if line.strip()]
    if not lines:
        return []

    header_indexes = [index for index, line in enumerate(lines) if _is_run_header_line(line)]
    if not header_indexes:
        raise ValueError("Could not locate any swap run headers in the supplied text.")

    runs: list[QuotesRun] = []
    for index, start_index in enumerate(header_indexes):
        end_index = header_indexes[index + 1] if index + 1 < len(header_indexes) else len(lines)
        run_block = "\n".join(lines[start_index:end_index])
        runs.append(parse_swap_run(run_block, provider=provider))

    return runs


def parse_swap_run(run_text: str, provider: str | None = None) -> QuotesRun:
    """Parse a single swap-run text block into a structured :class:`SwapRun`.

    Args:
        run_text: Raw multi-line swap-run text starting with a header line.
        provider: Optional provider label attached to the parsed run.

    Returns:
        A fully-populated :class:`SwapRun` object.
    """
    lines = [line.strip() for line in run_text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("Run text must contain at least a header line and one tenor row.")

    # The first non-empty line must contain the currency and run header.
    header_line = lines[0]
    header_match = HEADER_RE.match(header_line)
    if header_match is None:
        full_header = header_line
        parts = full_header.split()
        if len(parts) < 2:
            raise ValueError(f"Could not parse swap run header: {header_line!r}")
        currency = "RUB"
        if parts[0] in CURRENCY_CODES:
            currency = parts[0]
        run_name = full_header
    else:
        currency = header_match.group("currency")
        run_name = header_match.group("run_name")
        full_header = f"{currency} {run_name}".strip()

    run_name = re.sub(r"\s+Bid\s+Offer\b", "", full_header, flags=re.IGNORECASE).strip()
    header_tokens = _tokenize_header(full_header)
    parsed_currency, instrument_type, underlying_indicator, recognized_tokens, unrecognized_tokens = _infer_header_metadata(header_tokens)

    swap_run = QuotesRun(
        currency=(parsed_currency or currency).upper(),
        run_name=run_name,
        instrument=instrument_type,
        underlying=underlying_indicator,
        provider=provider,
        header_tokens=header_tokens,
        unrecognized_tokens=unrecognized_tokens,
    )

    # Everything after the header is treated as a quote body, with an optional
    # footer that contains size and start-rate metadata.
    body_lines = lines[1:]
    if body_lines:
        last_line = body_lines[-1]
        footer_match = FOOTER_RE.match(last_line)
        if footer_match is not None:
            normalized_size, numeric_size, canonical_unit = _normalize_size(
                footer_match.group("size_value"),
                footer_match.group("size_unit"),
            )
            swap_run.size = normalized_size
            swap_run.size_type = _normalize_size_type(footer_match.group("size_type"))
            swap_run.size_currency = (footer_match.group("size_currency") or swap_run.currency).upper()
            swap_run.size_value = numeric_size
            swap_run.size_unit = canonical_unit

            tail = footer_match.group("tail") or ""
            start_match = re.search(r"\bstart\s+(?P<rate>\d+(?:[.,]\d+)?)", tail, flags=re.IGNORECASE)
            if start_match is not None:
                swap_run.start_fx_rate = _normalize_decimal(start_match.group("rate"))

            body_lines = body_lines[:-1]

    # The quote parser keeps only valid tenor lines and ignores standard header
    # placeholders such as ``Term Bid Offer``.
    for raw_line in body_lines:
        lower = raw_line.lower()
        if lower in {"term bid offer", "bid offer", "term / bid / offer"}:
            continue
        quote_match = QUOTE_RE.match(raw_line)
        if quote_match is None:
            continue

        tenor = quote_match.group("tenor").upper()
        bid = _normalize_decimal(quote_match.group("bid"))
        offer = _normalize_decimal(quote_match.group("offer"))
        quote_payload = {"best_bid": bid, "best_offer": offer}
        end_date = quote_match.group("end_date")
        if end_date is not None:
            quote_payload["end_date"] = end_date
        swap_run.quotes[tenor] = quote_payload

    _register_run(swap_run)
    return swap_run
