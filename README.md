# DVS Rates Monitor

An internal (Streamlit based) app for DVS Brokers that collects interbank interest-rate
swap runs pasted in from market-run reports, keeps a running history of them,
and builds a consolidated best-bid/best-offer market view across providers —
flagging tenors where the market is tight and crossed between two providers.

## Features

- **Free-text run parsing** — paste a raw multi-provider run block (OIS, KRS,
  IRS, XCCY, CCIRS, FXSWAP) and the app infers currency, instrument,
  underlying indicator, size and per-tenor bid/offer levels from it.
- **Run history** — every parsed run is persisted (SQLite) and compared
  against the previous run for the same provider/instrument/underlying, so
  bid/offer moves are shown as a signed, color-coded bp change.
- **Participant runs** — the latest run per provider/instrument, with a
  **FIRM / VIA FRESH** toggle per run that decides whether it feeds into the
  joint market view. A run defaults to FIRM on the day it was parsed and
  automatically defaults to VIA FRESH once that day has passed; manually
  toggling a run persists the choice (it survives a page reload) until a new
  run is parsed for the same provider/instrument/underlying.
- **Joint runs** — FIRM runs sharing the same instrument/currency/underlying
  are combined into one table per instrument: best bid/offer across providers,
  the provider that quoted each side, and the mid. Rows where the spread is
  tight and crossed between two different providers are highlighted.
- **Tight quotes** — a separate block below the joint runs that collects every
  tight/crossed row across all instruments and groups it by provider (one
  column per provider, spanning all their instruments), plus an anonymous
  MARKET column.

## Project structure

```
src/
  app.py       Streamlit UI and page entry point
  core.py      Shared constants, the QuotesRun model, and the SQLite schema/connection state
  data.py      Persistence layer: all SQLite reads/writes for run history
  parsing.py   Run-text parsing, joint-run construction, and display-frame helpers
  curves.py    Discounting/curve-construction utilities (work in progress)
  test.ipynb   Scratch notebook for exercising the parsing/history API
data/
  runhist.db   SQLite run history (created on first run, not committed)
.streamlit/
  config.toml  Streamlit runtime config (logger level)
RatesMonitor.bat  Windows launcher: activates .venv and runs the app
                  (gitignored — not tracked, see "Running the app" below)
```

## Requirements

- Python 3.10+ (developed/tested on 3.14)
- Dependencies pinned in [requirements.txt](requirements.txt). 
  The app itself only needs the packages listed at the top of
  the file:
  - `streamlit` — web UI
  - `pandas` — run/quote data frames
  - `daycounts` — day-count conventions used by the curve utilities
  - `numpy`
  - `ipykernel` 

  Everything below the `# secondary / transitive dependencies` line is pulled
  in automatically by the packages above; it's pinned for a reproducible
  environment, not because the app imports it directly.

## Setup

```powershell
# from the repository root
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Running the app

```powershell
streamlit run src\app.py
```

## Configuration

- `RATES_MONITOR_DB_PATH` — overrides the SQLite history file location
  (defaults to `data/runhist.db` relative to the repository root).
- `.streamlit/config.toml` — Streamlit runtime settings; currently just sets
  the logger level to `error`.

## Data model

Two SQLite tables back the run history:

- `runs` — one row per parsed run: provider, currency, instrument,
  underlying, size metadata, `created_at` timestamp, and a `mode_override`
  column that persists an explicit FIRM/VIA FRESH choice.
- `quotes` — one row per tenor within a run (best/worst bid and offer plus
  the provider on each side), linked to `runs` by `run_id`.

A run's **identity** is the `(provider, underlying, currency, instrument)`
tuple. `get_actual_runs` returns the latest row per identity; parsing a new
run for the same identity inserts a fresh row (resetting `mode_override`)
rather than overwriting the previous one, so history is preserved.

## License

MIT — see [LICENSE](LICENSE).
