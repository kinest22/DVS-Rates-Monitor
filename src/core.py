"""Shared application constants and singleton state."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


# ------------------------------------
# SQLite database path used by the run history store
# ------------------------------------
DEFAULT_DB_PATH = os.environ.get(
    "RATES_MONITOR_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "runhist.db"),
)

# ------------------------------------
# SQLite schema for persisted runs and quotes
# ------------------------------------
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

# ------------------------------------
# Quote fields that are optional when loading persisted data
# ------------------------------------
QUOTE_OPTIONAL_COLUMNS = (
    "end_date",
    "worst_bid",
    "worst_bid_provider",
    "best_bid_provider",
    "best_offer_provider",
    "worst_offer",
    "worst_offer_provider",
)

# ------------------------------------
# Supported currency codes
# ------------------------------------
CURRENCY_CODES = frozenset({"RUB", "USD", "CNY", "CNH", "EUR"})

# ------------------------------------
# Supported instrument families
# ------------------------------------
INSTRUMENT_TYPES = frozenset({"OIS", "KRS", "IRS", "XCCY", "CCIRS", "FXSWAP"})

# ------------------------------------
# Supported underlying indicator codes
# ------------------------------------
INDICATOR_CODES = frozenset({"RUONIA", "SOFR", "RUSFAR", "RUSFARON", "KEYRATE", "KEY RATE"})

# ------------------------------------
# Header words ignored by metadata inference
# ------------------------------------
HEADER_IGNORE_TOKENS = frozenset({"RUN", "SWAP", "BID", "OFFER", "TERM", "END"})

# ------------------------------------
# Default metadata for keyword-led runs
# ------------------------------------
INSTRUMENT_DEFAULTS = {
    "OIS": {"currency": "RUB", "underlying": "RUONIA/RUB", "size": "1k", "size_type": "dv", "size_currency": "USD"},
    "KRS": {"currency": "RUB", "underlying": "KEYRATE/RUB", "size": "1k", "size_type": "dv", "size_currency": "USD"},
    "XCCY": {"currency": "CNH", "underlying": "RUONIA/CNH", "size": "100mio", "size_type": "par", "size_currency": "CNH"},
    "CCIRS": {"currency": "CNH", "underlying": "RUONIA/CNH", "size": "100mio", "size_type": "par", "size_currency": "CNH"},
    "FXSWAP": {"currency": "CNH", "underlying": "CNH/RUB", "size": "100mio", "size_type": "par", "size_currency": "CNH"},
}

# ------------------------------------
# Regular expression for currency-led headers
# ------------------------------------
HEADER_RE = re.compile(r"^(?P<currency>[A-Z]{3})\s+(?P<run_name>.+)$", flags=re.IGNORECASE)

# ------------------------------------
# Regular expression for size and start-rate footer metadata
# ------------------------------------
FOOTER_RE = re.compile(
    r"^(?P<size_value>\d+(?:[.,]\d+)?)\s*(?P<size_unit>mio|mln|bio|bln|b|k|m)?\s*"
    r"(?P<size_currency>RUB|USD|CNY|CNH|EUR)?\s*"
    r"(?P<size_type>DV|PAR|DV-CENTRIC|PAR-CENTRIC)?(?:\s*,?\s*(?P<tail>.*))?$",
    flags=re.IGNORECASE,
)

# ------------------------------------
# Regular expression for quote rows
# ------------------------------------
QUOTE_RE = re.compile(
    r"^(?P<tenor>[0-9]+[A-Za-z]+)\s+(?P<bid>\d+(?:[.,]\d+)?)\s*(?:/|\s+)"
    r"(?P<offer>\d+(?:[.,]\d+)?)\s*(?P<end_date>(?:\d{2}[./]\d{2}[./]\d{4}))?$",
    flags=re.IGNORECASE,
)

# ------------------------------------
# Run history singleton slot, initialized by rates_parser
# ------------------------------------
RUN_HISTORY: Any = None

# ------------------------------------
# Provider options shown in the Streamlit parser dialog
# ------------------------------------
POSSIBLE_RUN_PROVIDERS = ["SBER", "VTB", "NRB", "AKB"]

# ------------------------------------
# Joint-run spread thresholds by instrument, in basis points
# ------------------------------------
TIGHT_SPREAD_THRESHOLDS_BP = {"OIS": 12, "KRS": 12, "XCCY": 60}
