"""
app.py — Web interface for the support-level stock scanner
=============================================================

A small local web app around support_scanner.py: adjustable filters in
the sidebar, a "Refresh live data" button that re-runs the scan against
Yahoo Finance, a sortable results table, a per-stock support chart, and
a CSV download — so you (or anyone you share this with) can use it
without touching a terminal after the first launch.

RUN IT
------
    pip install -r requirements.txt
    streamlit run app.py

It opens automatically at http://localhost:8501 in your browser.

SHARING IT WITH SOMEONE ELSE (e.g. your dad)
----------------------------------------------
This app runs on your own machine — there's no server to pay for, but
that also means it only stays reachable while your machine is on and
the command above is running. A few free ways to actually share it:

1. Same house / same WiFi (easiest):
   Run:  streamlit run app.py --server.address 0.0.0.0
   Then have them open http://<your-computer's-LAN-IP>:8501 in their
   browser, on the same network. Find your LAN IP with `ipconfig`
   (Windows) or `ifconfig`/`ip a` (Mac/Linux).

2. Anywhere, temporarily, via a free tunnel (e.g. ngrok):
   Install ngrok (free tier), run `streamlit run app.py` in one
   terminal and `ngrok http 8501` in another. ngrok gives you a
   temporary public URL you can send your dad — it stops working once
   you close ngrok or your machine sleeps.

3. Anywhere, persistently, for free — Streamlit Community Cloud:
   Push this folder to a public (or free-tier private) GitHub repo,
   then deploy it at https://share.streamlit.io. You get a permanent
   URL your dad can bookmark, and it doesn't depend on your computer
   being on. This is the closest to a "real" shared app and costs
   nothing, but does require a GitHub account and a few minutes of
   setup outside this script.

Whichever route you pick, nothing here requires a paid plan — see
support_scanner.py's own docstring for the data-source details and
their limitations.
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")

import pandas as pd
import streamlit as st

import support_scanner as scanner

st.set_page_config(
    page_title="Support-Level Stock Scanner",
    layout="wide",
)

# Hide Streamlit's default chrome (the "hamburger" menu with
# Rerun/Settings/About, and the "Made with Streamlit" footer) for a
# cleaner, more app-like presentation. Deliberately NOT hiding the whole
# <header> element — on mobile, the sidebar's open/close toggle lives
# inside it, so hiding the header entirely makes the sidebar unreachable
# on a phone.
st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------
# Sidebar — all the CONFIG knobs from support_scanner.py, live-editable
# ----------------------------------------------------------------------

st.sidebar.header("Universe")
universe_mode = st.sidebar.radio(
    "Stocks to scan",
    ["S&P 500 + Nasdaq 100 (full, slower)", "Nasdaq 100 only (fast)"],
    index=0,
    help="Nasdaq-100 only is much faster (~100 tickers vs ~600) — good "
         "for quick refreshes during the day.",
)
extra_tickers_raw = st.sidebar.text_input(
    "Extra tickers (comma-separated)", value="",
    help="Add any tickers not already in the chosen universe, e.g. CRWV, RKLB",
)

st.sidebar.header("Support detection")
lookback_days = st.sidebar.slider("Lookback (days)", 60, 365, 180, step=10)
swing_lr = st.sidebar.slider("Swing low window (candles each side)", 1, 8, 3)
cluster_tol_pct = st.sidebar.slider("Cluster tolerance (%)", 0.5, 5.0, 2.0, step=0.5)
min_touches = st.sidebar.slider("Min touches for a valid support zone", 2, 6, 2)

st.sidebar.header("The scan filter")
max_distance_pct = st.sidebar.slider(
    "Max distance above support (%)", 1.0, 15.0, 5.0, step=0.5,
    help="This is the core filter — only stocks within this % above "
         "their nearest support zone are shown.",
)

st.sidebar.header("Liquidity filters")
min_price = st.sidebar.number_input("Minimum price ($)", value=10.0, step=1.0)
min_avg_volume = st.sidebar.number_input(
    "Minimum 20-day avg volume", value=500_000, step=50_000, format="%d"
)

st.sidebar.header("Volatility (useful for options)")
min_volatility_pct = st.sidebar.slider(
    "Min annualized realized volatility (%)", 0, 150, 0, step=5,
    help="0 = no filter. Realized volatility is computed from historical "
         "price swings (not implied volatility from options prices, which "
         "isn't available for free) — a rough proxy for how much a stock "
         "moves, useful context for premium-selling candidates.",
)

st.sidebar.divider()
refresh_clicked = st.sidebar.button("Refresh live data", type="primary",
                                     use_container_width=True)


def _apply_config_to_scanner():
    """Push the sidebar's current values into support_scanner's module
    globals, since its functions read those directly (see its CONFIG
    section) — this is how the same scan logic gets reused unmodified."""
    scanner.INCLUDE_SP500 = universe_mode.startswith("S&P 500")
    scanner.INCLUDE_NASDAQ100 = True
    scanner.EXTRA_TICKERS = [
        t.strip().upper() for t in extra_tickers_raw.split(",") if t.strip()
    ]
    scanner.LOOKBACK_DAYS = lookback_days
    scanner.SWING_LEFT = swing_lr
    scanner.SWING_RIGHT = swing_lr
    scanner.SUPPORT_CLUSTER_TOLERANCE = cluster_tol_pct / 100.0
    scanner.MIN_TOUCHES = min_touches
    scanner.MAX_DISTANCE_FROM_SUPPORT = max_distance_pct / 100.0
    scanner.MIN_PRICE = min_price
    scanner.MIN_AVG_VOLUME = min_avg_volume
    scanner.MIN_VOLATILITY_PCT = min_volatility_pct


# ----------------------------------------------------------------------
# Main panel
# ----------------------------------------------------------------------

st.title("Support-Level Stock Scanner")
st.caption(
    "Finds US stocks currently trading close to a meaningful, multi-touch "
    "horizontal support zone. This flags a **technical setup only** — not "
    "a trade recommendation. Always verify manually in TradingView before "
    "acting on anything shown here."
)

if "scan_result" not in st.session_state:
    st.session_state.scan_result = None
    st.session_state.last_updated = None

if refresh_clicked:
    _apply_config_to_scanner()

    progress_bar = st.progress(0.0, text="Building stock universe...")
    status_box = st.empty()
    log_lines = []

    def ui_log(msg):
        log_lines.append(str(msg))
        # Keep the visible log short — just the most recent lines
        status_box.code("\n".join(log_lines[-8:]))

    def ui_progress(done, total):
        pct = min(done / max(total, 1), 1.0)
        progress_bar.progress(pct, text=f"Downloading price data... {done}/{total} tickers")

    with st.spinner("Scanning — this can take a few minutes for the full universe"):
        result = scanner.run_scan(progress_callback=ui_progress, log=ui_log)

    progress_bar.progress(1.0, text="Done")
    time.sleep(0.3)
    progress_bar.empty()
    status_box.empty()

    st.session_state.scan_result = result
    st.session_state.last_updated = datetime.now(SGT)
    # Clear any prior table/dropdown selection — it refers to row positions
    # in the *previous* scan's results and would be meaningless (or
    # actively wrong) against this new set of candidates.
    for stale_key in ("candidates_table", "ticker_dropdown", "chart_ticker"):
        st.session_state.pop(stale_key, None)

result = st.session_state.scan_result

if result is None:
    st.info("Click **Refresh live data** in the sidebar to run the first scan.")
    st.stop()

# --- summary row ---
c1, c2, c3, c4 = st.columns(4)
c1.metric("Candidates found", len(result.candidates))
c2.metric("Universe scanned", result.universe_size)
c3.metric("Usable price data", result.usable_data_count)
c4.metric("Last updated", st.session_state.last_updated.strftime("%I:%M:%S %p").lstrip("0") + " SGT")

if not result.candidates:
    st.warning(
        "No stocks currently fall within the distance window. Try widening "
        "**Max distance above support** or lowering **Min touches** in the "
        "sidebar, then refresh again."
    )
    with st.expander("Why were stocks excluded? (diagnostic breakdown)"):
        st.json(result.stats)
    st.stop()

# --- results table ---
sort_choice = st.radio(
    "Sort by", ["Distance to support (closest first)", "Volatility (highest first)"],
    horizontal=True,
)
if sort_choice.startswith("Volatility"):
    sorted_candidates = sorted(
        result.candidates,
        key=lambda c: (c.volatility_pct if not pd.isna(c.volatility_pct) else -1),
        reverse=True,
    )
else:
    sorted_candidates = result.candidates  # already sorted by distance from run_scan()

ticker_options = [c.ticker for c in sorted_candidates]

rows = []
for i, c in enumerate(sorted_candidates, 1):
    rows.append({
        "Rank": i,
        "Ticker": c.ticker,
        "Price": c.price,
        "Support": c.support,
        "Distance %": c.distance_pct,
        "Touches": c.touches,
        "Strength": c.strength,
        "Volatility %": c.volatility_pct,
        "Vol Tier": c.volatility_tier,
        "Avg Volume (20d)": int(c.avg_volume),
    })
df_results = pd.DataFrame(rows)

# ------------------------------------------------------------------
# Keep the table's row selection and the search dropdown in sync, in
# both directions: clicking a row updates the dropdown, and picking
# from the dropdown highlights the matching row. Both widgets use a
# stable `key`, so Streamlit's own session state for each is readable
# here before they're drawn — whichever one the user just interacted
# with is what changed since our last resolved "chart_ticker", and we
# push that value into the *other* widget's state before it renders.
# ------------------------------------------------------------------
TABLE_KEY = "candidates_table"
DROPDOWN_KEY = "ticker_dropdown"

if "chart_ticker" not in st.session_state or st.session_state.chart_ticker not in ticker_options:
    st.session_state.chart_ticker = ticker_options[0]

prior_table_rows = st.session_state.get(TABLE_KEY, {}).get("selection", {}).get("rows", [])
table_ticker = sorted_candidates[prior_table_rows[0]].ticker if prior_table_rows else None
dropdown_ticker = st.session_state.get(DROPDOWN_KEY)

if table_ticker is not None and table_ticker != st.session_state.chart_ticker:
    st.session_state.chart_ticker = table_ticker
elif dropdown_ticker is not None and dropdown_ticker != st.session_state.chart_ticker:
    st.session_state.chart_ticker = dropdown_ticker

# Push the resolved ticker into both widgets' state before either is
# instantiated below, so they open already showing/highlighting it.
resolved_row = ticker_options.index(st.session_state.chart_ticker)
st.session_state[TABLE_KEY] = {"selection": {"rows": [resolved_row], "columns": []}}
st.session_state[DROPDOWN_KEY] = st.session_state.chart_ticker

st.subheader("Candidates")
st.caption("Click a row to load its support chart below — or use the dropdown to search.")
st.dataframe(
    df_results,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key=TABLE_KEY,
    column_config={
        "Price": st.column_config.NumberColumn(format="$%.2f"),
        "Support": st.column_config.NumberColumn(format="$%.2f"),
        "Distance %": st.column_config.NumberColumn(format="%.2f%%"),
        "Volatility %": st.column_config.NumberColumn(
            format="%.1f%%", help="Annualized realized volatility — not implied volatility."
        ),
        "Avg Volume (20d)": st.column_config.NumberColumn(format="%d"),
    },
)
st.caption(
    "Volatility % is annualized **realized** (historical) volatility from "
    "price data — not implied volatility from options prices. Useful as a "
    "rough proxy for premium potential, not a substitute for checking the "
    "actual options chain."
)

csv_bytes = df_results.to_csv(index=False).encode("utf-8")
st.download_button(
    "Download results as CSV",
    data=csv_bytes,
    file_name=f"support_scan_{st.session_state.last_updated.strftime('%Y%m%d_%H%M')}.csv",
    mime="text/csv",
)

# --- per-stock chart ---
st.subheader("Support chart")
st.selectbox("Or search for a candidate", ticker_options, key=DROPDOWN_KEY)

selected = next(c for c in sorted_candidates if c.ticker == st.session_state.chart_ticker)

fig = scanner.build_chart_figure(selected)
if fig is not None:
    st.pyplot(fig, use_container_width=True)
else:
    st.warning("matplotlib isn't installed, so charts can't be rendered here.")

with st.expander("Diagnostic breakdown (why other tickers were excluded)"):
    st.json(result.stats)

st.divider()
st.caption(
    "Reminder: this identifies a candidate technical setup only — proximity "
    "to a historical support zone — not a prediction that the level will "
    "hold, and not a trade or options recommendation. Verify support "
    "quality, trend, volume, and options liquidity yourself before acting."
)
