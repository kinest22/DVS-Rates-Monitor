"""Curve construction from parsed quote runs."""

from __future__ import annotations

import pandas as pd

from datetime import datetime
from core import QuotesRun, Curve, RateType, Compounding, tenor_sort_key


def joint_run_to_curve(joint_run: QuotesRun) -> Curve:
    """Build a par, simply compounded :class:`core.Curve` from the mid levels of a joint run.

    Each tenor's rate is the mid of its best bid and best offer (in percent).
    Tenors that lack either side are skipped, and the remaining points are
    ordered from shortest to longest tenor. The curve date is the joint run's
    ``created_at`` date.
    """
    if joint_run.created_at is None:
        raise ValueError(f"Joint run {joint_run!r} has no created_at to date the curve with")

    rows = [
        {"Tenor": tenor.strip().upper(), "Rate": (float(quote["best_bid"]) + float(quote["best_offer"])) / 2}
        for tenor, quote in joint_run.quotes.items()
        if quote.get("best_bid") is not None and quote.get("best_offer") is not None
    ]
    if not rows:
        raise ValueError(f"Joint run {joint_run!r} has no two-sided quotes to build a curve from")

    term_struct = pd.DataFrame(rows)
    term_struct = term_struct.sort_values("Tenor", key=lambda s: s.map(tenor_sort_key)).reset_index(drop=True)

    curve_date = datetime.fromisoformat(joint_run.created_at).date()
    return Curve(curve_date, term_struct, RateType.PAR, Compounding.SIMPLE)
