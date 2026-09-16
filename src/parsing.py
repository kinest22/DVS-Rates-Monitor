"""Utilities for parsing swap-run text blocks into structured QuotesRun objects.

The module is intentionally lightweight and works directly on raw text captured
from market-run reports. It exposes:

- a single-run parser, :func:`parse_swap_run`
- a multi-run parser, :func:`parse_multiple_runs`
- a small set of normalization/helpers used to infer instrument and size metadata
- presentation helpers (:func:`run_to_frame`, :func:`run_as_dict`, :func:`format_run`)
  operating on :class:`core.QuotesRun` instances, which are otherwise a plain
  data holder defined in :mod:`core`

The public API returns a :class:`core.QuotesRun` instance with a compact string
representation (see :func:`format_run`) suitable for notebook or console inspection.
"""

from __future__ import annotations

from typing import Any
import re

import pandas as pd
import core
import data
from core import QuotesRun


def run_to_frame(run: QuotesRun, with_changes: bool = False) -> pd.DataFrame:
    """Return a run's parsed quote matrix as a pandas DataFrame.

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
    points, against ``run``'s previous run for the same identity (see
    :func:`data.get_previous_run`). Both columns are ``NA`` when there is no
    previous run, or for tenors absent from it.
    """
    rows = []
    has_end_dates = any(
        isinstance(quote, dict) and quote.get("end_date") is not None
        for quote in run.quotes.values()
    )
    provider = run.provider
    is_fx_swap = run.instrument == "FXSWAP"

    for tenor, quote in run.quotes.items():
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
        previous_run = data.get_previous_run(run)
        bp_scale = 1.0 if is_fx_swap else 100.0
        if previous_run is not None:
            previous_levels = run_to_frame(previous_run)[["tenor", "best_bid", "best_offer"]].rename(
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


def run_as_dict(run: QuotesRun) -> dict[str, Any]:
    """Expose a run as a plain dictionary.

    This is useful for notebook inspection, serialization, or exporting the
    parsed run to JSON-like structures.
    """
    return {
        "currency": run.currency,
        "run_name": run.run_name,
        "instrument": run.instrument,
        "underlying": run.underlying,
        "provider": run.provider,
        "header_tokens": run.header_tokens,
        "unrecognized_tokens": run.unrecognized_tokens,
        "size": run.size,
        "size_type": run.size_type,
        "size_currency": run.size_currency,
        "size_value": run.size_value,
        "size_unit": run.size_unit,
        "start_fx_rate": run.start_fx_rate,
        "quotes": run.quotes,
    }


def format_run(run: QuotesRun) -> str:
    """Render a compact multi-line summary of a run.

    The output keeps the metadata block on separate lines, puts the size in a
    single compact row, and follows it with a quote table.
    """
    frame = run_to_frame(run)
    quote_table = frame.to_string(index=False) if not frame.empty else "(no quotes)"

    metadata_lines = ["QuotesRun"]
    metadata_pairs = [
        ("instrument", run.instrument),
        ("currency", run.currency),
        ("underlying", run.underlying),
        ("provider", run.provider),
        ("start_fx_rate", run.start_fx_rate),
    ]

    for field_name, value in metadata_pairs:
        if value in (None, "", [], {}):
            continue
        metadata_lines.append(f"  {field_name}: {value}")

    size_display_parts = []
    if run.size is not None:
        size_display_parts.append(run.size)
    if run.size_type is not None:
        size_display_parts.append(run.size_type.upper())
    if run.size_currency is not None:
        size_display_parts.append(run.size_currency)

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


def _parse_header_size(header_text: str, default: dict[str, str] | None) -> tuple[str | None, str | None, str | None, float | None, str | None]:
    """Read an optional size declaration embedded in a run header."""
    if default is None:
        return None, None, None, None, None

    size_match = re.search(
        r"\b(?:IN|SIZE)\s+(?P<size_value>\d+(?:[.,]\d+)?)\s*"
        r"(?P<size_unit>mio|mln|bio|bln|b|k|m)?\s*"
        r"(?P<size_currency>RUB|USD|CNY|CNH|EUR)?",
        header_text,
        flags=re.IGNORECASE,
    )
    if size_match is None:
        return None, None, None, None, None

    default_size_match = re.fullmatch(
        r"(?P<size_value>\d+(?:[.,]\d+)?)(?P<size_unit>mio|mln|bio|bln|b|k|m)",
        default["size"],
        flags=re.IGNORECASE,
    )
    default_unit = default_size_match.group("size_unit") if default_size_match else None
    raw_unit = size_match.group("size_unit") or default_unit
    normalized_size, numeric_size, canonical_unit = _normalize_size(
        size_match.group("size_value"), raw_unit
    )
    return (
        normalized_size,
        _normalize_size_type(default["size_type"]),
        (size_match.group("size_currency") or default["size_currency"]).upper(),
        numeric_size,
        canonical_unit,
    )


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
    if token in core.INDICATOR_CODES:
        return True
    valid_parts = core.INDICATOR_CODES | core.CURRENCY_CODES
    return "/" in token and all(part in valid_parts for part in token.split("/"))


def _is_currency_token(token: str) -> bool:
    """Return True when the token is one of the supported currency codes."""
    return token in core.CURRENCY_CODES


def _is_instrument_token(token: str) -> bool:
    """Return True when the token is a known swap instrument family."""
    return token in core.INSTRUMENT_TYPES


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
        if token in core.HEADER_IGNORE_TOKENS:
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
            if lhs in core.CURRENCY_CODES and rhs in core.INDICATOR_CODES:
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
    if core.HEADER_RE.match(line):
        return True

    parts = line.split()
    if len(parts) < 2:
        return parts[0].upper() in core.INSTRUMENT_TYPES

    # Headers may lead with a currency (``RUB KRS run``) or, for cross-currency
    # instruments, with the instrument family instead (``XCCY CNH/RUONIA run``).
    leading_token = parts[0].upper()
    return leading_token in core.CURRENCY_CODES or leading_token in core.INSTRUMENT_TYPES


def create_joint_runs(actual_runs: list[QuotesRun]) -> list[QuotesRun]:
    """Group the actual runs by instrument, currency, and underlying and join them.

    Each provider's latest active run (see :func:`data.get_actual_runs`) is
    bucketed by its ``(instrument, currency, underlying)`` identity, ignoring
    ``provider``. Each bucket becomes a single :class:`core.QuotesRun` combining
    every provider quoting that identity: for each tenor, the best bid is the
    highest bid across the joined providers and the worst bid is the lowest; the
    best offer is the lowest offer and the worst offer is the highest. Provider
    labels are rewritten to whoever actually quoted the selected level, and
    ``mid``/spreads are recalculated from the best bid and best offer by
    :func:`run_to_frame`.
    """
    groups: dict[tuple[str | None, str | None, str | None], list[QuotesRun]] = {}
    for run in actual_runs:
        key = (run.instrument, run.currency, run.underlying)
        groups.setdefault(key, []).append(run)

    joint_runs = []
    for (instrument, currency, underlying), runs in groups.items():
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
    """Parse a single swap-run text block into a structured :class:`core.QuotesRun`.

    Args:
        run_text: Raw multi-line swap-run text starting with a header line.
        provider: Optional provider label attached to the parsed run.

    Returns:
        A fully-populated :class:`core.QuotesRun` object.
    """
    lines = [line.strip() for line in run_text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("Run text must contain at least a header line and one tenor row.")

    # The first non-empty line must contain the currency and run header.
    header_line = lines[0]
    header_match = core.HEADER_RE.match(header_line)
    if header_match is None:
        full_header = header_line
        parts = full_header.split()
        if len(parts) < 2 and not _is_instrument_token(parts[0].upper()):
            raise ValueError(f"Could not parse swap run header: {header_line!r}")
        currency = "RUB"
        if parts[0] in core.CURRENCY_CODES:
            currency = parts[0]
        run_name = full_header
    else:
        currency = header_match.group("currency")
        run_name = header_match.group("run_name")
        full_header = f"{currency} {run_name}".strip()

    run_name = re.sub(r"\s+Bid\s+Offer\b", "", full_header, flags=re.IGNORECASE).strip()
    header_tokens = _tokenize_header(full_header)
    parsed_currency, instrument_type, underlying_indicator, recognized_tokens, unrecognized_tokens = _infer_header_metadata(header_tokens)

    defaults = core.INSTRUMENT_DEFAULTS.get(instrument_type or "")
    explicit_currency = None
    explicit_underlying_token = next(
        (token for token in header_tokens if _is_indicator_token(token)), None
    )
    explicit_underlying = explicit_underlying_token is not None
    if header_tokens:
        if _is_currency_token(header_tokens[0]):
            explicit_currency = header_tokens[0]
        elif instrument_type in {"XCCY", "CCIRS", "FXSWAP"}:
            currency_tokens = [token for token in header_tokens if _is_currency_token(token)]
            if currency_tokens:
                explicit_currency = currency_tokens[0]
            if instrument_type == "FXSWAP" and len(currency_tokens) >= 2:
                explicit_underlying = True

    effective_currency = explicit_currency or (defaults["currency"] if defaults else parsed_currency or currency)
    effective_underlying = (
        underlying_indicator
        if explicit_underlying
        else (defaults["underlying"] if defaults else underlying_indicator)
    )
    swap_run = QuotesRun(
        currency=effective_currency.upper(),
        run_name=run_name,
        instrument=instrument_type,
        underlying=effective_underlying,
        provider=provider,
        header_tokens=header_tokens,
        unrecognized_tokens=unrecognized_tokens,
    )

    if defaults is not None:
        default_size_match = re.fullmatch(
            r"(?P<size_value>\d+(?:[.,]\d+)?)(?P<size_unit>mio|mln|bio|bln|b|k|m)",
            defaults["size"],
            flags=re.IGNORECASE,
        )
        if default_size_match is None:
            raise ValueError(f"Invalid default size for {instrument_type}: {defaults['size']!r}")
        default_size, default_value, default_unit = _normalize_size(
            default_size_match.group("size_value"), default_size_match.group("size_unit")
        )
        swap_run.size = default_size
        swap_run.size_type = defaults["size_type"]
        swap_run.size_currency = defaults["size_currency"]
        swap_run.size_value = default_value
        swap_run.size_unit = default_unit

        header_size = _parse_header_size(header_line, defaults)
        if header_size[0] is not None:
            (
                swap_run.size,
                swap_run.size_type,
                swap_run.size_currency,
                swap_run.size_value,
                swap_run.size_unit,
            ) = header_size

    # Everything after the header is treated as a quote body, with an optional
    # footer that contains size and start-rate metadata.
    body_lines = lines[1:]
    if body_lines:
        last_line = body_lines[-1]
        footer_match = core.FOOTER_RE.match(last_line)
        if footer_match is not None and core.QUOTE_RE.match(last_line) is None:
            normalized_size, numeric_size, canonical_unit = _normalize_size(
                footer_match.group("size_value"),
                footer_match.group("size_unit"),
            )
            swap_run.size = normalized_size
            swap_run.size_type = _normalize_size_type(footer_match.group("size_type"))
            swap_run.size_currency = (
                footer_match.group("size_currency")
                or (defaults["size_currency"] if defaults else swap_run.currency)
            ).upper()
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
        quote_match = core.QUOTE_RE.match(raw_line)
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

    return swap_run
