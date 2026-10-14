# =====================================================================================
# app.py -- Rates Monitor entry point (Streamlit page script)
# =====================================================================================
# Started with `streamlit run src/app.py` (see RatesMonitor.bat). Streamlit re-runs
# this script from top to bottom on every user interaction, so the page is built
# as a straight sequence of blocks:
#
#   1) page setup                -- title, wide layout, footer
#   2) top buttons               -- PARSE NEW RUN opens the parse dialog;
#                                   UPDATE CURVES is handled in the curves block
#   3) toast notifications       -- messages queued in session state by the
#                                   previous run (loaded runs, updated curves)
#   4) participant runs          -- each provider's latest active run, with a
#                                   FIRM / VIA FRESH switch per run
#   5) direct quotes             -- manually typed bid/offer levels, kept only in
#                                   the browser session
#   6) joint runs                -- FIRM runs and direct quotes combined into one
#                                   best-bid / best-offer run per instrument
#   7) tight quotes              -- narrow, crossed quotes grouped by provider
#   8) curves                    -- on UPDATE CURVES, curves are built from the
#                                   joint runs and stored; the latest stored curve
#                                   per instrument/currency is always displayed,
#                                   against the previous trading day's curve
#
# This file only wires the blocks together: rendering helpers live in ui.py,
# parsing in parsing.py, curve construction in curves.py, storage in data.py.
# =====================================================================================



import streamlit as st

from datetime import datetime
from core import DIRECT_QUOTE_INSTRUMENTS
from parsing import create_joint_runs
from curves import joint_run_to_curve

from data import (
    register_curve, 
    get_previous_day_curve, 
    get_latest_curves, 
    get_actual_runs
)

from ui import (
    section_caption, 
    merge_direct_quotes, 
    PARSE_RUN_DIALOG, 
    DISPLAY_RUNS,
    DIRECT_QUOTES_EDITOR, 
    DISPLAY_TIGHT_QUOTES, 
    DISPLAY_CURVE,
)


# =====================================================================================
# setting page
# =====================================================================================
st.set_page_config(page_title="Rates Monitor", layout="wide")
st.bottom.caption("© 2026 DVS Brokers · All rights reserved", text_alignment='center')



# =====================================================================================
# parse run / update curves buttons
# =====================================================================================
top_buttons = st.container(horizontal=True)
if top_buttons.button("PARSE NEW RUN"):
    st.session_state.pop('parsed_runs', None)
    PARSE_RUN_DIALOG()
# curves are only rebuilt and stored on demand -- handled in the curves block
# below, once the joint runs they're built from are known
update_curves = top_buttons.button("UPDATE CURVES")



# =====================================================================================
# toast notifications block
# =====================================================================================
for toast_message in st.session_state.pop('load_toasts', []):
    st.toast(toast_message, icon=":material/table_convert:")
for toast_message in st.session_state.pop('curve_toasts', []):
    st.toast(toast_message, icon=":material/show_chart:")



# =====================================================================================
# actual runs block
# =====================================================================================
section_caption('Participant runs')
runs = get_actual_runs(as_of=datetime.now())                            # fetching runs
if not runs:                                                            # warning if no runs
    st.info("There are no actual runs at the moment.  \n" \
    "Please use <PARSE NEW RUN> button above to add new run(s).")
rates_runs = [run for run in runs if run.instrument in ('OIS', 'KRS')]  # 1st row: OIS/KRS
fx_runs = [run for run in runs if run.instrument not in ('OIS', 'KRS')] # 2nd row: XCCY, FX swap, ...
for row_runs in (rates_runs, fx_runs):                                  # display runs
    if row_runs:
        DISPLAY_RUNS(row_runs, 'normal')



# =====================================================================================
# direct quotes block -- manually typed bid/offer levels, not tied to a parsed
# run and not persisted to the database; kept only in the browser session.
# =====================================================================================
section_caption('Direct quotes')
st.caption('Enter bid/offer levels you receive directly, outside of a parsed run. Filled-in rows feed into the joint runs below.')
direct_quote_cols = st.columns(len(DIRECT_QUOTE_INSTRUMENTS))
direct_quotes = {}
for col, instrument in zip(direct_quote_cols, DIRECT_QUOTE_INSTRUMENTS):
    with col:
        st.markdown(f'**{instrument}**')
        direct_quotes[instrument] = DIRECT_QUOTES_EDITOR(instrument)



# =====================================================================================
# joint runs block
# =====================================================================================
section_caption('Joint runs')
firm_runs = []
for run in runs:
    if st.session_state.get(f'run_mode_{run.id}', 'FIRM') == 'FIRM':
        firm_runs.append(run)
joint_runs = create_joint_runs(firm_runs)
joint_runs = merge_direct_quotes(joint_runs, direct_quotes)
if joint_runs:
    DISPLAY_RUNS(joint_runs, 'joint')
else:
    st.info('No firm runs selected for joint runs.')



# =====================================================================================
# tight quotes block
# =====================================================================================
if joint_runs:
    section_caption('Tight quotes')
    DISPLAY_TIGHT_QUOTES(joint_runs)



# =====================================================================================
# curves block
# =====================================================================================
if update_curves:
    # toasts and warnings are queued in session state and shown after the
    # rerun, since the toast notifications block above has already run
    curve_errors = st.session_state.setdefault('curve_errors', [])
    if not joint_runs:
        curve_errors.append('No joint runs to build curves from.')
    for run in joint_runs:
        try:
            curve = joint_run_to_curve(run)
        except ValueError as exc:
            curve_errors.append(f'{run.instrument}: {exc}')
            continue
        register_curve(curve)
        st.session_state.setdefault('curve_toasts', []).append(f'{curve.Instrument} {curve.Currency} curve updated.')
    st.rerun()



curve_errors = st.session_state.pop('curve_errors', [])

# curves are always shown from the database: the latest stored one per
# instrument/currency, so the previous trading day's curves stay visible
# until "UPDATE CURVES" is pressed with firm runs
curves = get_latest_curves()
if curves or curve_errors:
    section_caption('Curves')
    for error in curve_errors:
        st.warning(error)
    for curve in curves:
        DISPLAY_CURVE(curve, get_previous_day_curve(curve))
