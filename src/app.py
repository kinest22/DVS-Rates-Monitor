import streamlit as st
from rates_parser import RUN_HISTORY
from datetime import datetime

# =====================================================================================
# setting page
# =====================================================================================
st.set_page_config(
    page_title="Rates Monitor",
    layout="wide",
)


# =====================================================================================
# parse run button
# =====================================================================================
with st.popover("Parse new run"):
    st.button("parse")


# =====================================================================================
# actual runs block
# =====================================================================================
actual_runs = RUN_HISTORY.get_actual_runs(as_of=datetime.now())
runs_num = len(actual_runs)
cols = st.columns(runs_num)
for i, run in enumerate(actual_runs):    
    run_df = run.to_frame()
    run_df = run_df[['best_bid', 'tenor', 'best_offer']]
    with cols[i]:
        st.markdown(f'''
        **PROVIDER**: {run.provider}  
        **INSTRUMENT**: {run.instrument}  
        **UNDERLYING**: {run.underlying}''')
        st.dataframe(run_df, hide_index=True, column_config={
            'best_bid': st.column_config.NumberColumn("Bid", alignment='center'),   
            'tenor': st.column_config.TextColumn("Tenor", alignment='center'),   
            'best_offer': st.column_config.NumberColumn("Offer", alignment='center'),     
        })

