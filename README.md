# Free Support-Level Stock Scanner

Scans a broad US stock universe and shortlists stocks trading within a
configurable percentage (default 5%) **above** a meaningful, multi-touch
horizontal support zone — the kind of level you'd draw by hand on a
TradingView chart. No TradingView plan, no paid data, no API keys.

Two ways to use it:
- **`app.py`** — a web interface with a Refresh button, sortable table,
  charts, and CSV export. Use this if you want to click a button instead
  of running a script, or share it with someone else (e.g. your dad).
- **`support_scanner.py`** — the same engine as a command-line script,
  for scheduled/automated runs (e.g. a daily cron job).

Both call the exact same scanning logic (`support_scanner.run_scan()`),
so results are identical either way.

## Quick start — web interface

```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens at `http://localhost:8501`. Adjust filters in the sidebar, click
**🔄 Refresh live data** to pull a fresh scan from Yahoo Finance, browse
the results table, pick a ticker to see its support chart, and download
the CSV. Nothing runs until you click Refresh — reopening the page later
just shows whatever you last refreshed, it doesn't hit the network again
on its own.

### Sharing it with someone else (e.g. your dad)

This runs on your own machine — free, but only reachable while your
machine is on and the command above is running. Three free options,
easiest first:

1. **Same house / same WiFi.** Run
   `streamlit run app.py --server.address 0.0.0.0`, then have him open
   `http://<your-computer's-LAN-IP>:8501` in his browser on the same
   network. Find your LAN IP with `ipconfig` (Windows) or `ifconfig` /
   `ip a` (Mac/Linux).
2. **Anywhere, temporarily** — a free tunnel like
   [ngrok](https://ngrok.com): run `streamlit run app.py` in one
   terminal and `ngrok http 8501` in another. ngrok gives you a
   temporary public link to send him. Stops working once you close
   ngrok or your machine sleeps.
3. **Anywhere, permanently, still free** —
   [Streamlit Community Cloud](https://share.streamlit.io). Push this
   folder to a GitHub repo (public, or private on the free tier) and
   deploy it there. You get a permanent URL he can bookmark, and it
   doesn't depend on your computer being on. Takes a few extra minutes
   of setup (GitHub account, connecting the repo) but is the closest
   thing to a "real" shared app at zero cost.

## Quick start — command line

```bash
pip install -r requirements.txt
python support_scanner.py
```

Takes a few minutes for ~600 tickers (S&P 500 + Nasdaq 100) on free data.
Produces:

- **Console table** — ranked list, closest-to-support first
- **`results.csv`** — same data plus diagnostic columns
- **`charts/TICKER.png`** — one chart per candidate (candles, the swing
  lows used, the support zone, current price, and the max-distance line)
  so you can compare the algorithm's line against your own eyeballed one

## Data sources (all free)

| What | Source | Notes |
|---|---|---|
| Ticker universe | Wikipedia's S&P 500 and Nasdaq-100 pages | Community-maintained, usually accurate, not an official index feed — occasionally a few days stale after index changes |
| Price data | Yahoo Finance via `yfinance` | Free, no key. Unofficial — can rate-limit or occasionally return gaps. Fine for a once-daily scan, not for real-time/production use |

If you hit persistent Yahoo rate-limiting, reduce `BATCH_SIZE` in the
config and/or increase `REQUEST_PAUSE_SECONDS`.

## How "support" is calculated

This deliberately does **not** just use "lowest low of N days." It
approximates how a human draws a horizontal support line:

1. **Swing lows** — a candle's low counts as a swing low if it's lower
   than the lows of `SWING_LEFT` candles before and `SWING_RIGHT` after
   it (default 3/3). This finds local bottoms, not just the single
   lowest point in the window.
2. **Clustering** — swing lows within `SUPPORT_CLUSTER_TOLERANCE`
   (default 2%) of each other are merged into one **support zone**,
   whose price is the average of the swing lows in it. This is what
   turns "$68.80, $69.10, $69.25, $68.95" into "one zone around $69."
3. **Scoring** — each zone gets a strength score from:
   - **number of touches** (more = stronger)
   - **recency** — touches decay exponentially with an
     `RECENCY_HALF_LIFE_DAYS`-day half-life, so a zone price has ignored
     for months counts less than one it respected last week
   - **tightness** — a cluster of lows packed close together scores
     higher than a loose one
   Zones need at least `MIN_TOUCHES` (default 2) to count at all.
   Labeled "Strong" / "Moderate" / "Weak" from the score.
4. **Selection** — among zones below the current price with enough
   touches, the **nearest one below price** is chosen (not the
   strongest overall, and never the absolute lowest low in the window).
5. **Distance filter** — `distance_pct = (price - support) / support`.
   Only tickers with `0% ≤ distance_pct ≤ MAX_DISTANCE_FROM_SUPPORT`
   (default 5%) make the final list.

## Quality / liquidity filters

- `MIN_PRICE` (default $10) — excludes penny stocks
- `MIN_AVG_VOLUME` (default 500,000, over `AVG_VOLUME_WINDOW`=20 days) —
  excludes illiquid names
- ETFs and leveraged/inverse products are excluded by default
  (`INCLUDE_ETFS = False`); the S&P 500 / Nasdaq-100 lists are equity
  index constituents anyway, so this mainly guards manual `EXTRA_TICKERS`

## Volatility (for options context)

Each candidate also gets a **Volatility %** — annualized realized
(historical) volatility, computed from the standard deviation of daily
log returns over the last `VOLATILITY_WINDOW` days (default 20),
annualized (`× √252`), using the same price data already downloaded —
no extra API calls. Candidates are also bucketed into a `Vol Tier`
(Low / Moderate / High / Very High) for quick scanning.

**This is not implied volatility (IV).** IV comes from live options
prices, which isn't available for free the way daily bars are — realized
volatility is a proxy for "how much has this stock actually moved,"
useful context for gauging premium potential, but it's not a
replacement for checking the real options chain before trading.

Set `MIN_VOLATILITY_PCT` above 0 (in the script, or the sidebar slider
in `app.py`) to only show candidates above a volatility floor — i.e.
"meaningful support AND volatile enough to be interesting."

## Parameters (all in `support_scanner.py`, top of file — or the sidebar in `app.py`)

| Parameter | Default | Meaning |
|---|---|---|
| `LOOKBACK_DAYS` | 180 | Calendar days of daily history pulled per ticker (~6 months of trading days) |
| `SWING_LEFT` / `SWING_RIGHT` | 3 / 3 | Candles on each side a low must beat to count as a swing low |
| `SUPPORT_CLUSTER_TOLERANCE` | 0.02 | How close (fractionally) swing lows must be to merge into one zone |
| `MIN_TOUCHES` | 2 | Minimum swing lows required for a zone to be "meaningful" |
| `RECENCY_HALF_LIFE_DAYS` | 60 | How fast old touches lose weight in the strength score |
| `MAX_DISTANCE_FROM_SUPPORT` | 0.05 | The 5% filter — change to 0.03, 0.10, etc. Nothing else needs editing |
| `MIN_PRICE` | 10.0 | Minimum share price |
| `MIN_AVG_VOLUME` | 500,000 | Minimum 20-day average volume |
| `MIN_VOLATILITY_PCT` | 0 (off) | Minimum annualized realized volatility %; raise to only show volatile candidates (useful for options premium) |
| `INCLUDE_SP500` / `INCLUDE_NASDAQ100` | True / True | Which index lists to pull for the universe |
| `EXTRA_TICKERS` | `[]` | Any tickers to add manually |
| `GENERATE_CHARTS` / `MAX_CHARTS` | True / 40 | Whether to render PNG charts, and a cap for very large hit lists |

## What this does *not* do

- It does not recommend a trade, a strike, an expiration, or an IV
  read. It flags a **technical setup only** — price near a support zone
  it has bounced off before — for you to inspect manually in
  TradingView (trend, volume, options liquidity, IV, market conditions,
  etc.), exactly as you described.
- It does not guarantee the support will hold. Support is a pattern in
  historical price, not a promise about the future. The output
  includes touch count and a strength label precisely so you can judge
  how seriously to take a given zone yourself.

## Expanding the universe

To scan more than S&P 500 + Nasdaq 100, add tickers to `EXTRA_TICKERS`,
or swap `get_universe()` to pull from another free list (e.g. a Russell
1000 constituent CSV you have on hand). Nothing else in the script
depends on where the ticker list came from.
