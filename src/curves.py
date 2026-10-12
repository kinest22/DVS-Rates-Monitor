"""Curve construction from parsed quote runs."""

from __future__ import annotations

import numpy as np
import pandas as pd

from datetime import datetime
from core import QuotesRun, Curve, RateType, Compounding, tenor_sort_key


def joint_run_to_curve(joint_run: QuotesRun) -> Curve:
    """Build a par, simply compounded :class:`core.Curve` from the mid levels of a joint run.

    Each tenor's rate is the mid of its best bid and best offer (in percent).
    Tenors that lack either side are skipped, and the remaining points are
    ordered from shortest to longest tenor. The curve is timed at the joint
    run's ``created_at`` and identified by its instrument and currency.
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

    curve_time = datetime.fromisoformat(joint_run.created_at)
    return Curve(curve_time, joint_run.instrument, joint_run.currency, term_struct, RateType.PAR, Compounding.SIMPLE)


def cubic_spline(x: pd.Series, y: pd.Series, points: int = 200) -> pd.DataFrame:
    """Interpolate ``y`` over ``x`` with a natural cubic spline, for smooth plotting.

    The spline passes through every ``(x, y)`` knot, has continuous first and
    second derivatives, and zero curvature at both ends (the "natural"
    boundary condition). ``x`` must be strictly increasing. Returns a frame
    with columns named after ``x`` and ``y`` holding ``points`` evenly spaced
    samples between the first and last knot, with the knots themselves
    included. With fewer than three knots there is nothing to bend, so the
    knots are returned as they are.
    """
    xs = x.to_numpy(dtype=float)
    ys = y.to_numpy(dtype=float)
    if len(xs) < 3:
        return pd.DataFrame({x.name: xs, y.name: ys})
    if np.any(np.diff(xs) <= 0):
        raise ValueError("cubic_spline needs strictly increasing x values")

    # Solve the tridiagonal system for the second derivatives M at the inner
    # knots; the natural boundary condition fixes M = 0 at both ends.
    h = np.diff(xs)
    slopes = np.diff(ys) / h
    system = np.diag(2 * (h[:-1] + h[1:])) + np.diag(h[1:-1], 1) + np.diag(h[1:-1], -1)
    m = np.zeros(len(xs))
    m[1:-1] = np.linalg.solve(system, 6 * np.diff(slopes))

    grid = np.union1d(np.linspace(xs[0], xs[-1], points), xs)
    i = np.clip(np.searchsorted(xs, grid, side="right") - 1, 0, len(xs) - 2)
    left, right, hi = grid - xs[i], xs[i + 1] - grid, h[i]
    values = (
        m[i] * right**3 / (6 * hi)
        + m[i + 1] * left**3 / (6 * hi)
        + (ys[i] / hi - m[i] * hi / 6) * right
        + (ys[i + 1] / hi - m[i + 1] * hi / 6) * left
    )
    return pd.DataFrame({x.name: grid, y.name: values})
