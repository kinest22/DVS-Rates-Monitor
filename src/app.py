import math
import pandas as pd
import altair as alt
import streamlit as st
import core
from core import QuotesRun, Curve, DIRECT_QUOTE_INSTRUMENTS, DIRECT_QUOTE_ROWS
from parsing import parse_multiple_runs, create_joint_runs, run_to_frame
from curves import joint_run_to_curve, cubic_spline
from data import register_run, register_curve, get_previous_day_curve, get_actual_runs, set_run_mode_override
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


def _default_run_mode(run: QuotesRun) -> str:
    """FIRM for a run created today; VIA FRESH once the calendar day it was
    parsed on has passed (stale quotes shouldn't default back into joint runs)."""
    run_date = datetime.strptime(run.created_at, "%Y-%m-%d %H:%M:%S.%f").date()
    return 'FIRM' if run_date == datetime.now().date() else 'VIA FRESH'


def _effective_run_mode(run: QuotesRun) -> str:
    """The FIRM/VIA FRESH state to show for a run: an explicit, persisted
    override if one was set, otherwise the date-based default."""
    return run.mode_override or _default_run_mode(run)


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


def _empty_direct_quotes_frame() -> pd.DataFrame:
    """Build a blank direct-quotes grid: one row per manually entered quote,
    with separate bid/offer sides (each carrying its own provider and size).

    This frame only ever seeds the editor on its very first render -- once a
    key is attached (see :func:`_direct_quotes_editor`), Streamlit keeps the
    user's edits in session state and ignores this initial value afterwards.
    """
    return pd.DataFrame({
        'bid_provider':     ['']            * DIRECT_QUOTE_ROWS,
        'bid_size':         ['']            * DIRECT_QUOTE_ROWS,
        'bid':              [float('nan')]  * DIRECT_QUOTE_ROWS,
        'tenor':            ['']            * DIRECT_QUOTE_ROWS,
        'offer':            [float('nan')]  * DIRECT_QUOTE_ROWS,
        'offer_size':       ['']            * DIRECT_QUOTE_ROWS,
        'offer_provider':   ['']            * DIRECT_QUOTE_ROWS,
    })


def merge_direct_quotes(joint_runs: list[QuotesRun], direct_quotes: dict[str, pd.DataFrame]) -> list[QuotesRun]:
    """Fold manually entered direct quotes into the joint run for the same
    instrument, so a good direct bid/offer can win a tenor (or fill one in,
    if no participant run quotes it at all) alongside the parsed runs.

    A direct quote only ever replaces one side of a tenor if it's better than
    what's already there (higher bid / lower offer); a brand-new tenor is only
    added once both sides are known, since the rest of the app assumes every
    quoted tenor has both a bid and an offer.
    """
    joint_runs_by_instrument = {run.instrument: run for run in joint_runs}

    for instrument, quotes_df in direct_quotes.items():
        for _, row in quotes_df.iterrows():
            tenor = core.normalize_tenor(row['tenor'] or '')
            if not tenor:
                continue  # blank row -- nothing entered yet

            joint_run = joint_runs_by_instrument.get(instrument)
            if joint_run is None:
                # No participant runs for this instrument at all -- start a
                # joint run from scratch using the instrument's usual identity.
                defaults = core.INSTRUMENT_DEFAULTS.get(instrument, {})
                joint_run = QuotesRun(
                    currency=defaults.get('currency'),
                    run_name=f'{instrument} direct quotes',
                    instrument=instrument,
                    underlying=defaults.get('underlying'),
                    provider='joint',
                )
                joint_runs_by_instrument[instrument] = joint_run

            quote = joint_run.quotes.get(tenor, {})
            best_bid = quote.get('best_bid')
            best_offer = quote.get('best_offer')

            if pd.notna(row['bid']) and (best_bid is None or row['bid'] > best_bid):
                best_bid = float(row['bid'])
                quote['best_bid'] = best_bid
                quote['best_bid_provider'] = str(row['bid_provider'] or '').strip() or None
                quote['best_bid_size'] = str(row['bid_size'] or '').strip() or None

            if pd.notna(row['offer']) and (best_offer is None or row['offer'] < best_offer):
                best_offer = float(row['offer'])
                quote['best_offer'] = best_offer
                quote['best_offer_provider'] = str(row['offer_provider'] or '').strip() or None
                quote['best_offer_size'] = str(row['offer_size'] or '').strip() or None

            if best_bid is not None and best_offer is not None:
                joint_run.quotes[tenor] = quote

    return list(joint_runs_by_instrument.values())


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
            f"**QUOTES**: {len(run.quotes)}  \n"
        )
        if not run.quotes:
            col.warning('No quote lines recognized -- this run will not be saved.')
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
                if not run.quotes:
                    continue  # nothing usable to store -- see the warning in the dialog
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
                time = datetime.strptime(run.created_at, "%Y-%m-%d %H:%M:%S.%f").strftime("%d.%m.%Y %H:%M")
                st.markdown(f'''
                **PROVIDER**: {run.provider}  
                **INSTRUMENT**: {run.instrument}  
                **UNDERLYING**: {run.underlying}  
                **SIZE**: {run.size_value:,.0f}{run.size_unit} {run.size_type} {run.size_currency}  
                **TIME**: {time}''')
                default_mode = _effective_run_mode(run)
                selected_mode = st.segmented_control(
                    None,
                    options=['FIRM', 'VIA FRESH'],
                    default=default_mode,
                    key=f'run_mode_{run.id}',
                    width='stretch',
                    label_visibility='collapsed'
                )
                if selected_mode != default_mode:
                    set_run_mode_override(run.id, selected_mode)
            else:
                st.markdown(f'''
                **JOINT RUN**  
                **INSTRUMENT**: {run.instrument}  
                **UNDERLYING**: {run.underlying}''')
            st.dataframe(styled_run_df, hide_index=True, column_config=run_column_config, height='content')


def DIRECT_QUOTES_EDITOR(instrument: str) -> pd.DataFrame:
    """Render one instrument's editable direct-quotes grid and return its
    current contents (including whatever the user has typed so far).

    Not persisted anywhere -- it only lives in Streamlit's widget state for
    this browser session, per the current "no database for these yet" design.
    """
    return st.data_editor(
        _empty_direct_quotes_frame(),
        key=f'direct_quotes_{instrument}',
        hide_index=True,
        num_rows='fixed',
        width='stretch',
        column_config={
            'bid_provider': st.column_config.TextColumn('Provider', width='small'),
            'bid_size': st.column_config.TextColumn('Size', width='small'),
            'bid': st.column_config.NumberColumn('Bid', format='%.2f'),
            'tenor': st.column_config.TextColumn('Tenor', width='small'),
            'offer': st.column_config.NumberColumn('Offer', format='%.2f'),
            'offer_size': st.column_config.TextColumn('Size', width='small'),
            'offer_provider': st.column_config.TextColumn('Provider', width='small'),
        },
    )


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

    def join_by_instrument(lines: list[tuple[str, str]]) -> str:
        # line breaks within an instrument class, an empty line between classes
        groups: dict[str, list[str]] = {}
        for instrument, line in lines:
            groups.setdefault(instrument, []).append(line)
        return "\n\n".join("  \n".join(group) for group in groups.values())

    def size_suffix(*sizes) -> str:
        # " in 1k" for one size (or matching bid/offer sizes), " in 1k/2k" when
        # they differ, "-" standing in for an unknown side
        sizes = [str(size).strip() if pd.notna(size) and str(size).strip() else None for size in sizes]
        if all(size is None for size in sizes):
            return ""
        if len(set(sizes)) == 1:
            return f" in {sizes[0]}"
        return " in " + "/".join(size or "-" for size in sizes)

    for col, provider in zip(cols, providers):
        lines = []
        for _, row in narrow_rows.iterrows():
            instrument, tenor, bid, offer = row['instrument'], row['tenor'], row['best_bid'], row['best_offer']
            bid_size, offer_size = row['best_bid_size'], row['best_offer_size']
            if row['best_bid_provider'] == provider:
                lines.append((instrument, f"{instrument} {tenor} /{offer:.2f}{size_suffix(offer_size)}"))
            elif row['best_offer_provider'] == provider:
                lines.append((instrument, f"{instrument} {tenor} {bid:.2f}/{size_suffix(bid_size)}"))
            else:
                lines.append((instrument, f"{instrument} {tenor} {bid:.2f}/{offer:.2f}{size_suffix(bid_size, offer_size)}"))
        col.write(f"**{provider}**  \n" + join_by_instrument(lines))

    market_lines = [
        (row['instrument'], f"{row['instrument']} {row['tenor']} {row['best_bid']:.2f} / {row['best_offer']:.2f}"
                            f"{size_suffix(row['best_bid_size'], row['best_offer_size'])}")
        for _, row in narrow_rows.iterrows()
    ]
    cols[-1].write("**MARKET**  \n" + join_by_instrument(market_lines))


def _curve_chart_points(curve: Curve) -> pd.DataFrame:
    """Return the curve points to plot: below ``core.CURVE_CHART_MIN_TENOR`` only
    ``core.CURVE_CHART_SHORT_TENORS`` are kept, so the crowded short end stays
    readable. If that leaves nothing, every point is returned."""
    min_tenor_key = core.tenor_sort_key(core.CURVE_CHART_MIN_TENOR)
    levels = curve.levels()
    points = levels[
        levels['Tenor'].isin(core.CURVE_CHART_SHORT_TENORS)
        | (levels['Tenor'].map(core.tenor_sort_key) >= min_tenor_key)
    ].reset_index(drop=True)
    return points if not points.empty else levels


def DISPLAY_CURVE(curve: Curve, previous_curve: Curve | None = None) -> None:
    """Plot a curve's rates against term as dots joined by a natural cubic
    spline, each dot labelled with its rate, with one x-axis tick per tenor
    and y-axis ticks every whole percent (100 bp). If ``previous_curve`` is
    given (e.g. the previous day's curve), it is drawn on the same axes as a
    grey dashed line without labels, and a legend tells the two apart."""
    title = f'**{curve.Instrument} {curve.Currency} curve**'
    st.markdown(title)

    dark = st.context.theme.type == 'dark'
    series = [(curve, '#83C9FF' if dark else '#0068C9', [1, 0])]
    if previous_curve is not None:
        series.append((previous_curve, '#808495', [6, 4]))

    points_frames, spline_frames = [], []
    for series_curve, _, _ in series:
        name = f'{series_curve.Time:%d.%m.%Y %H:%M}'
        points = _curve_chart_points(series_curve)
        points_frames.append(points.assign(Curve=name))
        spline_frames.append(cubic_spline(points['Term'], points['Rate']).assign(Curve=name))
    # rows are drawn in order, so the previous curve goes first and the current one is painted over it
    points_df = pd.concat(points_frames[::-1], ignore_index=True)
    spline_df = pd.concat(spline_frames[::-1], ignore_index=True)
    current_points = points_frames[0]
    names = [frame['Curve'].iloc[0] for frame in points_frames]

    # x-axis: one tick per point of the current curve, labelled with its tenor instead of the term in years
    tenor_label_expr = ' : '.join(
        f"abs(datum.value - {term!r}) < 1e-9 ? '{tenor}'"
        for term, tenor in zip(current_points['Term'], current_points['Tenor'])
    ) + " : ''"
    # y-axis: whole-percent (100 bp) ticks spanning both splines, which can bend past the quoted rates
    rate_ticks = list(range(math.floor(spline_df['Rate'].min()), math.ceil(spline_df['Rate'].max()) + 1))
    x = alt.X('Term:Q', title='Tenor', axis=alt.Axis(values=list(current_points['Term']), labelExpr=tenor_label_expr))
    y = alt.Y(
        'Rate:Q', title='Rate, %',
        scale=alt.Scale(domain=[rate_ticks[0], rate_ticks[-1]], nice=False),
        axis=alt.Axis(values=rate_ticks, format='d'),
    )
    color = alt.Color(
        'Curve:N', sort=names,
        scale=alt.Scale(domain=names, range=[color for _, color, _ in series]),
        legend=alt.Legend(title=None, orient='top', symbolType='stroke', symbolStrokeWidth=2) if len(series) > 1 else None,
    )
    dash = alt.StrokeDash('Curve:N', sort=names, scale=alt.Scale(domain=names, range=[d for _, _, d in series]), legend=None)

    line = alt.Chart(spline_df).mark_line(strokeWidth=2).encode(x=x, y=y, color=color, strokeDash=dash)
    dots = alt.Chart(points_df).mark_point(filled=True, size=70, opacity=1).encode(
        x=x, y=y, color=color,
        tooltip=[
            alt.Tooltip('Curve:N'),
            alt.Tooltip('Tenor:N'),
            alt.Tooltip('Rate:Q', format='.4f', title='Rate, %'),
        ],
    )
    # Altair draws text black by default, which disappears on Streamlit's dark theme
    label_color = '#FAFAFA' if dark else '#31333F'
    labels = alt.Chart(current_points).mark_text(dy=-12, fontSize=11, color=label_color).encode(
        x=x, y=y, text=alt.Text('Rate:Q', format='.2f'),
    )
    st.altair_chart(line + dots + labels, width='stretch')



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
rates_runs = [run for run in runs if run.instrument in ('OIS', 'KRS')]  # 1st row: OIS/KRS
fx_runs = [run for run in runs if run.instrument not in ('OIS', 'KRS')] # 2nd row: XCCY, FX swap, ...
for row_runs in (rates_runs, fx_runs):                                  # display runs
    if row_runs:
        DISPLAY_RUNS(row_runs, 'normal')



# =====================================================================================
# direct quotes block -- manually typed bid/offer levels, not tied to a parsed
# run and not persisted to the database; kept only in the browser session.
# =====================================================================================
_section_caption('Direct quotes')
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
_section_caption('Joint runs')
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
    _section_caption('Tight quotes')
    DISPLAY_TIGHT_QUOTES(joint_runs)



# =====================================================================================
# curves block
# =====================================================================================
curves: list[Curve] = []
curve_errors: list[str] = []
for run in joint_runs:
    try:
        curve = joint_run_to_curve(run)
    except ValueError as exc:
        curve_errors.append(f'{run.instrument}: {exc}')
        continue
    register_curve(curve)
    curves.append(curve)

if curves or curve_errors:
    _section_caption('Curves')
    for error in curve_errors:
        st.warning(error)
    for curve in curves:
        DISPLAY_CURVE(curve, get_previous_day_curve(curve))
