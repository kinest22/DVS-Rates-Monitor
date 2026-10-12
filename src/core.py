"""Shared application constants and singleton state."""

from __future__ import annotations

import os
import re
import numpy as np
import sqlite3
import threading
import pandas as pd

from pathlib import Path
from enum import Enum
from datetime import date, datetime, timedelta
from daycounts import actual_actual_isda


# ----------------------------------------------------------------------------------------------
# SQLite database path used by the application data storage
# ----------------------------------------------------------------------------------------------
DEFAULT_DB_PATH = os.environ.get(
    "RATES_MONITOR_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "runhist.db"),
)


# ----------------------------------------------------------------------------------------------
# SQLite schema for persisted runs and quotes
# ----------------------------------------------------------------------------------------------
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
    created_at TEXT NOT NULL,
    mode_override TEXT
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

CREATE TABLE IF NOT EXISTS curves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    instrument TEXT,
    currency TEXT,
    type TEXT NOT NULL,
    cmpd TEXT NOT NULL,
    tenor TEXT NOT NULL,
    term REAL NOT NULL,
    value REAL NOT NULL,
    df REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_curves_identity
    ON curves (instrument, currency, type, cmpd, time);
"""

# ----------------------------------------------------------------------------------------------
# Quote fields that are optional when loading persisted data
# ----------------------------------------------------------------------------------------------
QUOTE_OPTIONAL_COLUMNS = (
    "end_date",
    "worst_bid",
    "worst_bid_provider",
    "best_bid_provider",
    "best_offer_provider",
    "worst_offer",
    "worst_offer_provider",
)


# ----------------------------------------------------------------------------------------------
# Supported currency codes
# ----------------------------------------------------------------------------------------------
CURRENCY_CODES = frozenset({"RUB", "USD", "CNY", "CNH", "EUR"})


# ----------------------------------------------------------------------------------------------
# Supported instrument families
# ----------------------------------------------------------------------------------------------
INSTRUMENT_TYPES = frozenset({"OIS", "KRS", "IRS", "XCCY", "CCIRS", "FXSWAP"})


# ----------------------------------------------------------------------------------------------
# Supported underlying indicator codes
# ----------------------------------------------------------------------------------------------
INDICATOR_CODES = frozenset({"RUONIA", "SOFR", "RUSFAR", "RUSFARON", "KEYRATE", "KEY RATE"})


# ----------------------------------------------------------------------------------------------
# Header words ignored by metadata inference
# ----------------------------------------------------------------------------------------------
HEADER_IGNORE_TOKENS = frozenset({"RUN", "SWAP", "BID", "OFFER", "TERM", "END"})


# ----------------------------------------------------------------------------------------------
# Default metadata for keyword-led runs
# ----------------------------------------------------------------------------------------------
INSTRUMENT_DEFAULTS = {
    "OIS": {"currency": "RUB", "underlying": "RUONIA/RUB", "size": "1k", "size_type": "dv", "size_currency": "USD"},
    "KRS": {"currency": "RUB", "underlying": "KEYRATE/RUB", "size": "1k", "size_type": "dv", "size_currency": "USD"},
    "XCCY": {"currency": "CNH", "underlying": "RUONIA/CNH", "size": "100mio", "size_type": "par", "size_currency": "CNH"},
    "CCIRS": {"currency": "CNH", "underlying": "RUONIA/CNH", "size": "100mio", "size_type": "par", "size_currency": "CNH"},
    "FXSWAP": {"currency": "CNH", "underlying": "CNH/RUB", "size": "100mio", "size_type": "par", "size_currency": "CNH"},
}


# ----------------------------------------------------------------------------------------------
# Regular expression for currency-led headers
# ----------------------------------------------------------------------------------------------
HEADER_RE = re.compile(r"^(?P<currency>[A-Z]{3})\s+(?P<run_name>.+)$", flags=re.IGNORECASE)


# ----------------------------------------------------------------------------------------------
# Regular expression for size and start-rate footer metadata
# ----------------------------------------------------------------------------------------------
FOOTER_RE = re.compile(
    r"^(?P<size_value>\d+(?:[.,]\d+)?)\s*(?P<size_unit>mio|mln|bio|bln|b|k|m)?\s*"
    r"(?P<size_currency>RUB|USD|CNY|CNH|EUR)?\s*"
    r"(?P<size_type>DV|PAR|DV-CENTRIC|PAR-CENTRIC)?(?:\s*,?\s*(?P<tail>.*))?$",
    flags=re.IGNORECASE,
)


# ----------------------------------------------------------------------------------------------
# Regular expression for quote rows
# ----------------------------------------------------------------------------------------------
QUOTE_RE = re.compile(
    r"^(?P<tenor>[0-9]+[A-Za-z]+)\s+(?P<bid>\d+(?:[.,]\d+)?)\s*(?:/\s*|\s+)"
    r"(?P<offer>\d+(?:[.,]\d+)?)\s*(?P<end_date>(?:\d{2}[./]\d{2}[./]\d{4}))?$",
    flags=re.IGNORECASE,
)


# ----------------------------------------------------------------------------------------------
# Approximate tenor unit lengths in days, used only to order tenors by maturity
# ----------------------------------------------------------------------------------------------
TENOR_UNIT_DAYS = {"D": 1, "W": 7, "M": 30, "Y": 365}


# ----------------------------------------------------------------------------------------------
# Provider options shown in the Streamlit parser dialog
# ----------------------------------------------------------------------------------------------
POSSIBLE_RUN_PROVIDERS = ["SBER", "VTB", "NRB", "AKB"]


# ----------------------------------------------------------------------------------------------
# Joint-run spread thresholds by instrument, in basis points
# ----------------------------------------------------------------------------------------------
TIGHT_SPREAD_THRESHOLDS_BP = {"OIS": 12, "KRS": 12, "XCCY": 60}


# ----------------------------------------------------------------------------------------------
# Instruments covered by the manual "direct quotes" grids.
# ----------------------------------------------------------------------------------------------
DIRECT_QUOTE_INSTRUMENTS = ['OIS', 'KRS', 'XCCY']


# ----------------------------------------------------------------------------------------------
# The number of blank rows in the manual "direct quotes" grids.
# ----------------------------------------------------------------------------------------------
DIRECT_QUOTE_ROWS = 10


# ----------------------------------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------------------------------
def tenor_sort_key(tenor: str) -> tuple[int, float, str]:
    """Return a key ordering tenors by maturity (``1W < 2W < 1M < ... < 1Y < 10Y``).

    Tenors that don't match ``<number><D|W|M|Y>`` sort after all recognized
    ones, alphabetically.
    """
    match = re.fullmatch(r"(\d+)([A-Za-z]+)", str(tenor).strip())
    if match is not None:
        unit_days = TENOR_UNIT_DAYS.get(match.group(2).upper())
        if unit_days is not None:
            return 0, int(match.group(1)) * unit_days, ""
    return 1, 0.0, str(tenor)


# ----------------------------------------------------------------------------------------------
# Domain model classes
# ----------------------------------------------------------------------------------------------
class QuotesRun:
    """Parsed representation of an interest-rate swap run.

    This class is intentionally a plain data holder: parsing logic lives in
    :mod:`parsing` (see :func:`parsing.parse_swap_run`), persistence lives in
    :mod:`data` (see :class:`DataStorage`), and presentation helpers
    such as :func:`parsing.run_to_frame` and :func:`parsing.format_run`
    operate on instances of this class rather than being methods on it.

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
        mode_override: str | None = None,
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
        self.mode_override = mode_override

    def __repr__(self) -> str:
        """Return a compact, unambiguous representation for debugging/logging."""
        return (
            f"QuotesRun(id={self.id!r}, provider={self.provider!r}, instrument={self.instrument!r}, "
            f"currency={self.currency!r}, underlying={self.underlying!r})"
        )


class DataStorage:
    """Holds the connection state for the SQLite database behind the app (runs, quotes, curves).

    Like :class:`QuotesRun`, this class is a plain state holder: a target
    database path plus the bookkeeping needed to hand each thread its own
    connection (Streamlit reruns each session's script in its own worker
    thread, and sqlite3 connections are bound to the thread that created
    them). Schema setup and every read/write are implemented as functions in
    :mod:`data` that operate on instances of this class (e.g.
    :func:`data._add_run`, :func:`data._add_curve`) rather than as methods.
    Any future class needing database access should follow the same split.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = str(db_path)
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()


# ----------------------------------------------------------------------------------------------
# Shared data storage singleton. Its schema is created by data._init_schema
# when data.py is first imported, since that requires a connection.
# ----------------------------------------------------------------------------------------------
DATA: DataStorage = DataStorage()


class RateType(Enum):
    PAR     = 0
    FWD     = 1
    ZERO    = 2


class Compounding(Enum):
    SIMPLE  = 0
    ANNUAL  = 1
    CONTINUOUS = 2


class Curve:
    def __init__(self, curve_time:datetime, instrument:str|None, currency:str|None, term_struct:pd.DataFrame, curve_type:RateType, cmpd:Compounding) -> None:
        self.Time: datetime = curve_time
        self.Instrument: str | None = instrument
        self.Currency: str | None = currency
        self._value_date = curve_time.date() + timedelta(days=1)
        self._set_curve_terms(term_struct, self._value_date)
        self._calc_df(term_struct, cmpd)
        self._curve: pd.DataFrame = term_struct
        self.CurveType: RateType = curve_type
        self.Compounding: Compounding = cmpd


    def __str__(self) -> str:
        n = len(self._curve)
        head = (f"Curve(instrument={self.Instrument}, "
                f"currency={self.Currency}, "
                f"time={self.Time}, "
                f"type={self.CurveType.name}, "
                f"cmpd={self.Compounding.name}, "
                f"value_date={self._value_date}, n={n})")

        rows = []
        for _, r in self._curve.iterrows():
            rows.append(
                f"  {r['Tenor']:>5}  "
                f"T={r['Term']:8.4f}y  "
                f"R={r['Rate']:7.4f}%  "
                f"DF={r['DF']:.8f}"
            )
        return head + "\n" + "\n".join(rows)
    

    def levels(self) -> pd.DataFrame:
        return self._curve


    def _set_curve_terms(self, df: pd.DataFrame, value_date: date) -> None:
        end_date_finder = lambda t: self._add_period(value_date, t).date()
        df['Start'] = value_date
        df['End'] = df['Tenor'].apply(end_date_finder)
        df['Term'] = df.apply(lambda r: actual_actual_isda(r['Start'], r['End']), axis=1)
        del df['Start']
        del df['End']


    def _calc_df(self, df: pd.DataFrame, cmpd: Compounding) -> pd.DataFrame:
        match cmpd:
            case Compounding.SIMPLE:        df['DF'] = 1 / (1 + df['Rate']/100 * df['Term'])
            case Compounding.ANNUAL:        df['DF'] = 1 / (1 + df['Rate']/100) ** df['Term']
            case Compounding.CONTINUOUS:    df['DF'] = np.exp(-df['Rate']/100 * df['Term'])
            case _  :                       raise ValueError(f"Unknown compounding: '{cmpd}'")

        
    def _add_period(self, value_date: date, period: str) -> date:
        period = period.strip().upper()
        unit = period[-1]
        value = int(period[:-1])
        match unit:
            case 'W': return value_date + pd.DateOffset(weeks=value)
            case 'M': return value_date + pd.DateOffset(months=value)
            case 'Y': return value_date + pd.DateOffset(years=value)
            case _  : raise ValueError(f"Unknown period: '{period}'")

    