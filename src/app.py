import pandas as pd
import streamlit as st
import core
from core import QuotesRun
from parsing import parse_multiple_runs, create_joint_runs, run_to_frame
from data import register_run, get_actual_runs
from datetime import datetime
from streamlit.delta_generator import DeltaGenerator
from pandas.io.formats.style import Styler



def _format_change_bp(value: float) -> str:
    """Render a bp change as an arrow-prefixed, signed string for display."""
    if pd.isna(value):
        return ""
    if value > 0:
        return f"▲ +{value:.0f}"
    if value < 0:
        return f"▼ {value:.0f}"
    return f"→ {value:.0f}"


def _section_caption(text: str) -> None:
    """Render a caption-style label immediately above a divider, as a single
    markdown element -- st.caption() + st.divider() are separate widgets and
    pick up Streamlit's normal ~1rem inter-widget gap between them."""
    st.markdown(
        f"<p style='font-size:1.1rem; color:var(--text-color); "
        f"opacity:0.6; margin:0 0 0.25rem 0'>{text}</p><hr style='margin:0 0 1rem 0'>",
        unsafe_allow_html=True,
    )


def _color_change_text(text: str) -> str:
    """Color an arrow-formatted change string: red for growth, green for decrease."""
    if text.startswith("▲"):
        return "color: #e74c3c"
    if text.startswith("▼"):
        return "color: #2ecc71"
    return ""


def _style_run_frame(run_df: pd.DataFrame) -> tuple[Styler, dict]:
    """Format and style a run's quote frame for display in ``st.dataframe``.

    Applies, in one place:
      1) bp-scaled bid/offer changes (values are already bp-scaled by
         :func:`parsing.run_to_frame`; here they're rendered as text)
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


def _style_joint_run_frame(
    run_df: pd.DataFrame, instrument: str | None = None
) -> tuple[Styler, dict]:
    """Format and style a run's quote frame for display in ``st.dataframe``.
    Applies, in one place:
      1) bold tenor labels
      2) compact 2-decimal bid/offer formatting, avoiding trailing-zero float noise
      3) a red row fill where the bid/offer spread is unusually tight for the
         instrument (< 12 bp for OIS/KRS, < 60 bp for XCCY; see
         :data:`TIGHT_SPREAD_THRESHOLDS_BP`) -- st.dataframe ignores Styler
         borders, so a background fill is used instead

    Returns the styled frame together with the ``column_config`` it expects,
    since bid/offer number formatting is handled by ``NumberColumn`` rather
    than the Styler (Streamlit renders those independently).
    """

    frame = run_df[['tenor', 'best_bid_provider', 'best_bid', 'mid', 'best_offer', 'best_offer_provider', 'best_bid_offer_spread']].copy()

    tight_spread_threshold = core.TIGHT_SPREAD_THRESHOLDS_BP.get(instrument)

    def _highlight_tight_spread(row: pd.Series) -> list[str]:
        if tight_spread_threshold is None or row['best_bid_offer_spread'] >= tight_spread_threshold:
            return [''] * len(row)
        return ['background-color: rgba(231, 76, 60, 0.25)'] * len(row)

    styled_frame = (
        frame.style
        .apply(_highlight_tight_spread, axis=1)
        .set_properties(subset=['tenor'], **{'font-weight': 'bold'})
        .set_properties(subset=['mid'], **{'color': "#2ecc71"})
    )
    column_config = {
        'tenor': st.column_config.TextColumn("Tenor", alignment='center'),
        'best_bid_provider': st.column_config.TextColumn("Provider", alignment='center'),
        'best_bid': st.column_config.NumberColumn("Bid", alignment='center', format="%.2f"),
        'mid': st.column_config.NumberColumn("Mid", alignment='center', format="%.3f"),
        'best_offer': st.column_config.NumberColumn("Offer", alignment='center', format="%.2f"),
        'best_offer_provider': st.column_config.TextColumn("Provider", alignment='center'),        
        'best_bid_offer_spread': st.column_config.NumberColumn("Spread", alignment='center', format="%.2f"),        
    }
    return styled_frame, column_config



def _narrow_spread_rows(run_df: pd.DataFrame, instrument: str | None) -> pd.DataFrame:
    """Return the joint-run rows whose bid/offer spread is tight and crossed
    between two different providers (see :data:`TIGHT_SPREAD_THRESHOLDS_BP`)."""
    threshold = core.TIGHT_SPREAD_THRESHOLDS_BP.get(instrument)
    if threshold is None:
        return run_df.iloc[0:0]
    return run_df[
        (run_df['best_bid_offer_spread'] <= threshold)
        & (run_df['best_bid_provider'] != run_df['best_offer_provider'])
    ]


def _collect_narrow_quotes(joint_runs: list[QuotesRun]) -> pd.DataFrame:
    """Gather tight-spread, crossed-provider rows across all joint runs,
    tagging each row with its instrument so they can be grouped by provider
    regardless of which instrument they came from."""
    frames = []
    for run in joint_runs:
        run_df = run_to_frame(run, with_changes=True)
        narrow_rows = _narrow_spread_rows(run_df, run.instrument)
        if narrow_rows.empty:
            continue
        narrow_rows = narrow_rows.copy()
        narrow_rows['instrument'] = run.instrument
        frames.append(narrow_rows)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _show_parsed_runs_in_dialog(runs: list[QuotesRun], col: DeltaGenerator, cont: DeltaGenerator) -> None:
    """Display parsed runs in the right-hand column of the parse dialog."""
    col.space()
    col.subheader(f'Runs parsed successfully: {len(runs)}')
    col.space('medium')
    for i, run in enumerate(runs, start=1):
        col.write(
            f"RUN #{i}  \n"
            f"**PROVIDER**: {run.provider}  \n"
            f"**INSTRUMENT**: {run.instrument}  \n"
            f"**UNDERLYING**: {run.underlying}  \n"
            f"**SIZE**: {run.size_value:,.0f}{run.size_unit} {run.size_type} {run.size_currency}  \n"
        )
        col.space()


@st.dialog('Parse new run(s)', width='medium')
def PARSE_RUN_DIALOG() -> None:
    cols = st.columns(2, gap='large')
    provider = cols[0].selectbox('Provider:', options=core.POSSIBLE_RUN_PROVIDERS)
    run_str = cols[0].text_area('Run data:', placeholder='Paste run text here...', height=750)
    cont = st.container(horizontal=True, horizontal_alignment="distribute")

    if cont.button("Parse"):
        st.session_state['parsed_runs'] = parse_multiple_runs(run_str, provider=provider)

    parsed_runs = st.session_state.get('parsed_runs')
    if parsed_runs:
        _show_parsed_runs_in_dialog(parsed_runs, cols[1], cont)
        if cont.button('Confirm'):
            for run in parsed_runs:
                register_run(run)
                st.session_state.setdefault('load_toasts', []).append(f"{run.instrument} run from {run.provider} loaded.")
            del st.session_state['parsed_runs']
            st.rerun()


def DISPLAY_RUNS(runs: list[QuotesRun], mode:str='normal') -> None:
    runs_num = len(runs)
    cols = st.columns(runs_num)
    for i, run in enumerate(runs):
        run_df = run_to_frame(run, with_changes=True)
        if mode == 'joint':
            styled_run_df, run_column_config = _style_joint_run_frame(run_df, run.instrument)
        else:
            styled_run_df, run_column_config = _style_run_frame(run_df)
        with cols[i]:
            if mode == 'normal':            
                time = datetime.strptime(run.created_at, "%Y-%m-%d %H:%M:%S.%f").strftime("%d.%m.%Y %H.%M")
                st.markdown(f'''
                **PROVIDER**: {run.provider}  
                **INSTRUMENT**: {run.instrument}  
                **UNDERLYING**: {run.underlying}  
                **SIZE**: {run.size_value:,.0f}{run.size_unit} {run.size_type} {run.size_currency}  
                **TIME**: {time}''')
                st.segmented_control(
                    None,
                    options=['FIRM', 'VIA FRESH'],
                    default='FIRM',
                    key=f'run_mode_{run.id}',
                    width='stretch',
                    label_visibility='collapsed'
                )
            else:
                st.markdown(f'''
                **JOINT RUN**  
                **INSTRUMENT**: {run.instrument}  
                **UNDERLYING**: {run.underlying}''')
            st.dataframe(styled_run_df, hide_index=True, column_config=run_column_config, height='content')


def DISPLAY_TIGHT_QUOTES(joint_runs: list[QuotesRun]) -> None:
    """Standalone block, separated from the joint runs above, showing narrow
    (tight-spread, crossed) quotes grouped by provider -- not by instrument.
    Each provider gets its own column, spanning all their instruments (OIS,
    KRS, XCCY, ...), plus a final anonymous MARKET column."""
    narrow_rows = _collect_narrow_quotes(joint_runs)
    if narrow_rows.empty:
        return

    providers = sorted(set(narrow_rows['best_bid_provider']) | set(narrow_rows['best_offer_provider']))
    cols = st.columns(len(providers) + 1)

    for col, provider in zip(cols, providers):
        lines = []
        for _, row in narrow_rows.iterrows():
            instrument, tenor, bid, offer = row['instrument'], row['tenor'], row['best_bid'], row['best_offer']
            if row['best_bid_provider'] == provider:
                lines.append(f"{instrument} {tenor} /{offer:.2f}")
            elif row['best_offer_provider'] == provider:
                lines.append(f"{instrument} {tenor} {bid:.2f}/")
            else:
                lines.append(f"{instrument} {tenor} {bid:.2f}/{offer:.2f}")
        col.write(f"**{provider}**  \n" + "  \n".join(lines))

    market_lines = [
        f"{row['instrument']} {row['tenor']} {row['best_bid']:.2f} / {row['best_offer']:.2f}"
        for _, row in narrow_rows.iterrows()
    ]
    cols[-1].write("**MARKET**  \n" + "  \n".join(market_lines))



# =====================================================================================
# setting page
# =====================================================================================
st.set_page_config(page_title="Rates Monitor", layout="wide")
st.bottom.caption("© 2026 DVS Brokers · All rights reserved", text_alignment='center')



# =====================================================================================
# parse run button
# =====================================================================================
if st.button("PARSE NEW RUN"):
    st.session_state.pop('parsed_runs', None)
    PARSE_RUN_DIALOG()



# =====================================================================================
# toast notifications block
# =====================================================================================
for toast_message in st.session_state.pop('load_toasts', []):
    st.toast(toast_message, icon=":material/table_convert:")



# =====================================================================================
# actual runs block
# =====================================================================================
_section_caption('Participant runs')
runs = get_actual_runs(as_of=datetime.now())                            # fetching runs
if not runs:                                                            # warning if no runs
    st.info("There are no actual runs at the moment.  \n" \
    "Please use <PARSE NEW RUN> button above to add new run(s).")
    st.stop()
DISPLAY_RUNS(runs, 'normal')                                            # display runs



# =====================================================================================
# joint runs block
# =====================================================================================
_section_caption('Joint runs')
firm_runs = []
for run in runs:
    if st.session_state.get(f'run_mode_{run.id}', 'FIRM') == 'FIRM':
        firm_runs.append(run)
joint_runs = create_joint_runs(firm_runs)
if joint_runs:
    DISPLAY_RUNS(joint_runs, 'joint')
else:
    st.info('No firm runs selected for joint runs.')



# =====================================================================================
# tight quotes block
# =====================================================================================
if joint_runs:
    _section_caption('Tight quotes')
    DISPLAY_TIGHT_QUOTES(joint_runs)

