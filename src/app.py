import pandas as pd
import streamlit as st
from rates_parser import QuotesRun, RUN_HISTORY, parse_multiple_runs
from datetime import datetime



POSSIBLE_RUN_PROVIDERS = ['SBER', 'VTB', 'NRB', 'AKB']




def _format_change_bp(value: float) -> str:
    """Render a bp change as an arrow-prefixed, signed string for display."""
    if pd.isna(value):
        return ""
    if value > 0:
        return f"▲ +{value:.0f}"
    if value < 0:
        return f"▼ {value:.0f}"
    return f"→ {value:.0f}"


def _color_change_text(text: str) -> str:
    """Color an arrow-formatted change string: red for growth, green for decrease."""
    if text.startswith("▲"):
        return "color: #e74c3c"
    if text.startswith("▼"):
        return "color: #2ecc71"
    return ""


def style_run_frame(run_df: pd.DataFrame) -> tuple[pd.io.formats.style.Styler, dict]:
    """Format and style a run's quote frame for display in ``st.dataframe``.

    Applies, in one place:
      1) bp-scaled bid/offer changes (values are already bp-scaled by
         :meth:`QuotesRun.to_frame`; here they're rendered as text)
      2) arrow + color coding on the change columns (red = growth, green = decrease)
      3) bold tenor labels
      4) compact 2-decimal bid/offer formatting, avoiding trailing-zero float noise

    Returns the styled frame together with the ``column_config`` it expects,
    since bid/offer number formatting is handled by ``NumberColumn`` rather
    than the Styler (Streamlit renders those independently).
    """
    frame = run_df[['best_bid_change_bp', 'best_bid', 'tenor', 'best_offer', 'best_offer_change_bp']].copy()
    frame['best_bid_change_bp'] = frame['best_bid_change_bp'].map(_format_change_bp)
    frame['best_offer_change_bp'] = frame['best_offer_change_bp'].map(_format_change_bp)

    styled_frame = (
        frame.style
        .map(_color_change_text, subset=['best_bid_change_bp', 'best_offer_change_bp'])
        .set_properties(subset=['tenor'], **{'font-weight': 'bold'})
    )
    column_config = {
        'best_bid_change_bp': st.column_config.TextColumn("Δ bp", alignment='center'),
        'best_bid': st.column_config.NumberColumn("Bid", alignment='center', format="%.2f"),
        'tenor': st.column_config.TextColumn("Tenor", alignment='center'),
        'best_offer': st.column_config.NumberColumn("Offer", alignment='center', format="%.2f"),
        'best_offer_change_bp': st.column_config.TextColumn("Δ bp", alignment='center'),
    }
    return styled_frame, column_config




@st.dialog('Parse new run(s)')
def PARSE_RUN_DIALOG() -> None:
    provider = st.selectbox('Provider:', options=POSSIBLE_RUN_PROVIDERS)
    run_str = st.text_area('Run data:', placeholder='Paste run text here...')
    if st.button("Parse"):
        parse_multiple_runs(run_str, provider=provider)
        st.rerun()







def DISPLAY_RUNS(runs: list[QuotesRun]) -> None:
    runs_num = len(runs)
    cols = st.columns(runs_num)
    for i, run in enumerate(runs):
        run_df = run.to_frame(with_changes=True)
        styled_run_df, run_column_config = style_run_frame(run_df)
        time = datetime.strptime(run.created_at, "%Y-%m-%d %H:%M:%S.%f").strftime("%d.%m.%Y %H.%M")
        with cols[i]:
            st.markdown(f'''
            **PROVIDER**: {run.provider}  
            **INSTRUMENT**: {run.instrument}  
            **UNDERLYING**: {run.underlying}  
            **SIZE**: {run.size_value:,.0f}{run.size_unit} {run.size_type} {run.currency}  
            **TIME**: {time}''')
            st.toggle("Activate", value=True, key=i)
            st.dataframe(styled_run_df, hide_index=True, column_config=run_column_config)



# =====================================================================================
# setting page
# =====================================================================================
st.set_page_config(page_title="Rates Monitor", layout="wide")
st.bottom.caption("© 2026 DVS Brokers · All rights reserved", text_alignment='center')







# =====================================================================================
# parse run button
# =====================================================================================
if st.button("PARSE NEW RUN"):
    PARSE_RUN_DIALOG()








# =====================================================================================
# actual runs block
# =====================================================================================

# fetching runs
runs = RUN_HISTORY.get_actual_runs(as_of=datetime.now())



# warning if no runs
if not runs:
    st.info("There are no actual runs at the moment.  \n" \
    "Please use <PARSE NEW RUN> button above to add new run(s).")
    st.stop()



# display runs
DISPLAY_RUNS(runs)






