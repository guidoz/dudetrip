"""
March Madness Kalshi Live Dashboard
------------------------------------
Streamlit app — deploy free at streamlit.io/cloud
Shows live Kalshi markets, order books, bid/ask spreads,
mid prices, and edge signals for NCAA Tournament games.

Setup:
  pip install streamlit requests pandas
  streamlit run kalshi_dashboard.py
  
Or deploy to Streamlit Cloud (free) and access from your phone.
Set your KALSHI_API_KEY in Streamlit Cloud > App Settings > Secrets.
"""

import streamlit as st
import requests
import pandas as pd
import time
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Pull key from Streamlit secrets (set in cloud dashboard) or sidebar input
def get_api_key():
    try:
        return st.secrets["KALSHI_API_KEY"]
    except Exception:
        return None

# ── Kalshi API helpers ──────────────────────────────────────────────────────

def kalshi_headers(api_key):
    return {
        "accept": "application/json",
        "KALSHI-ACCESS-KEY": api_key,
    }

@st.cache_data(ttl=30)
def fetch_ncaa_markets(api_key):
    """
    Fetch all open NCAA basketball markets by:
    1. Searching events with category=sports, paginating through all open events
    2. Filtering by NCAA/basketball/March Madness keywords in title
    3. Collecting all nested markets from matching events
    """
    NCAA_KEYWORDS = ["ncaa", "march madness", "basketball", "ncaab", "ncaamb",
                     "college basketball", "tournament", "kxncaamb"]
    
    all_markets = []
    seen_tickers = set()

    # --- Strategy 1: fetch by confirmed series ticker KXNCAAMB first ---
    CANDIDATE_SERIES = [
        "KXNCAAMB",  # confirmed March Madness ticker
        "KXNCAAB", "KXMARCHMADNESS", "KXCBB", "KXNCAATOURNEY",
        "KXNCAAT", "KXMM26", "KXNCAAB26",
    ]
    for series in CANDIDATE_SERIES:
        try:
            r = requests.get(
                f"{BASE_URL}/markets",
                params={"series_ticker": series, "status": "open", "limit": 200},
                headers=kalshi_headers(api_key),
                timeout=8,
            )
            if r.status_code == 200:
                for m in r.json().get("markets", []):
                    if m["ticker"] not in seen_tickers:
                        seen_tickers.add(m["ticker"])
                        all_markets.append(m)
        except Exception:
            pass

    # If we found markets from series, return early — no need to paginate
    if all_markets:
        return all_markets

    # --- Strategy 2: paginate through open sports events, keyword filter ---
    cursor = None
    pages = 0
    while pages < 10:
        params = {
            "status": "open",
            "limit": 200,
            "with_nested_markets": "true",
        }
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(
                f"{BASE_URL}/events",
                params=params,
                headers=kalshi_headers(api_key),
                timeout=10,
            )
            if r.status_code != 200:
                break
            data = r.json()
            events = data.get("events", [])
            for event in events:
                title = (event.get("title", "") + " " + event.get("sub_title", "") + " " + event.get("series_ticker", "")).lower()
                if any(kw in title for kw in NCAA_KEYWORDS):
                    for m in event.get("markets", []):
                        if m["ticker"] not in seen_tickers:
                            seen_tickers.add(m["ticker"])
                            all_markets.append(m)
            cursor = data.get("cursor")
            if not cursor or not events:
                break
            pages += 1
        except Exception:
            break

    # --- Strategy 3: broad open markets scan, keyword filter ---
    if not all_markets:
        try:
            r = requests.get(
                f"{BASE_URL}/markets",
                params={"status": "open", "limit": 1000},
                headers=kalshi_headers(api_key),
                timeout=10,
            )
            if r.status_code == 200:
                for m in r.json().get("markets", []):
                    title = (m.get("title", "") + " " + m.get("ticker", "")).lower()
                    if any(kw in title for kw in NCAA_KEYWORDS):
                        if m["ticker"] not in seen_tickers:
                            seen_tickers.add(m["ticker"])
                            all_markets.append(m)
        except Exception:
            pass

    return all_markets

@st.cache_data(ttl=15)
def fetch_orderbook(api_key, ticker):
    """Fetch order book for a specific market ticker."""
    url = f"{BASE_URL}/markets/{ticker}/orderbook"
    try:
        r = requests.get(url, headers=kalshi_headers(api_key), timeout=8)
        if r.status_code == 200:
            return r.json().get("orderbook_fp", {})
    except Exception:
        pass
    return {}

@st.cache_data(ttl=15)
def fetch_market_detail(api_key, ticker):
    """Fetch full market detail including yes/no bid/ask prices."""
    url = f"{BASE_URL}/markets/{ticker}"
    try:
        r = requests.get(url, headers=kalshi_headers(api_key), timeout=8)
        if r.status_code == 200:
            return r.json().get("market", {})
    except Exception:
        pass
    return {}

# ── Spread / edge calculations ──────────────────────────────────────────────

def compute_spread_metrics(market):
    """
    From a market dict, compute:
      - yes_bid, yes_ask (in cents / implied %)
      - mid price
      - spread width
      - implied probability
    Kalshi: yes_bid + no_bid = 100¢ (vig)
    yes_ask = 100 - no_bid
    """
    try:
        yes_bid = float(market.get("yes_bid_dollars", 0) or 0) * 100
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0) * 100
        no_bid  = float(market.get("no_bid_dollars", 0) or 0) * 100
        no_ask  = float(market.get("no_ask_dollars", 0) or 0) * 100

        # If ask missing, derive it
        if yes_ask == 0 and no_bid > 0:
            yes_ask = 100 - no_bid
        if no_ask == 0 and yes_bid > 0:
            no_ask = 100 - yes_bid

        mid = (yes_bid + yes_ask) / 2 if yes_ask > 0 else yes_bid
        spread = yes_ask - yes_bid if yes_ask > 0 else None

        # Vig = how much the book takes (sum of both sides' spreads)
        total_vig = (yes_ask - yes_bid) + (no_ask - no_bid) if (yes_ask > 0 and no_ask > 0) else None

        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "mid": mid,
            "spread": spread,
            "total_vig": total_vig,
        }
    except Exception:
        return {}

def edge_signal(mid, spread, volume):
    """Simple edge scoring: tight spread + active volume = worth watching."""
    if spread is None or mid == 0:
        return "⚪ No data"
    if spread <= 2 and volume > 1000:
        return "🟢 Sharp market"
    if spread <= 4 and volume > 500:
        return "🟡 Watch"
    if spread > 8:
        return "🔴 Wide / avoid"
    return "🟡 Moderate"

# ── UI ──────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="March Madness · Kalshi Live",
    page_icon="🏀",
    layout="wide",
)

st.title("🏀 March Madness — Kalshi Live Dashboard")
st.caption(f"Last updated: {datetime.now().strftime('%I:%M:%S %p')}  ·  Auto-refreshes every 30s")

# ── Sidebar ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Settings")
    
    api_key = get_api_key()
    if not api_key:
        api_key = st.text_input(
            "Kalshi API Key",
            type="password",
            help="Paste your key here, or set KALSHI_API_KEY in Streamlit secrets"
        )
    else:
        st.success("✅ API key loaded from secrets")

    refresh_rate = st.slider("Refresh interval (seconds)", 15, 120, 30)
    edge_threshold = st.slider("Edge alert threshold (spread ≤ ¢)", 1, 10, 4)
    show_orderbook = st.checkbox("Show order book depth", value=True)
    st.divider()
    st.markdown("**How to read this:**")
    st.markdown("""
- **Mid** = true market price (¢ out of 100)
- **Spread** = cost to cross (lower = sharper)
- **Vig** = total book take
- 🟢 = tight, liquid, worth tracking
- 🟡 = moderate liquidity
- 🔴 = wide spread, avoid
    """)

if not api_key:
    st.warning("👈 Enter your Kalshi API key in the sidebar to get started.")
    st.stop()

# ── Main content ─────────────────────────────────────────────────────────────

with st.spinner("Fetching Kalshi NCAA markets..."):
    markets = fetch_ncaa_markets(api_key)

if not markets:
    st.warning("No NCAA markets found via auto-discovery. Let's diagnose and search manually.")

    # Diagnostic: show a sample of what IS open on Kalshi right now
    with st.expander("🔍 Diagnostic — show all open Kalshi events (to find real tickers)"):
        try:
            r = requests.get(
                f"{BASE_URL}/events",
                params={"status": "open", "limit": 50, "with_nested_markets": "false"},
                headers=kalshi_headers(api_key),
                timeout=10,
            )
            if r.status_code == 200:
                events = r.json().get("events", [])
                if events:
                    diag_df = pd.DataFrame([
                        {"event_ticker": e.get("event_ticker"), "title": e.get("title"), "category": e.get("category")}
                        for e in events
                    ])
                    st.dataframe(diag_df, use_container_width=True, hide_index=True)
                    st.caption("Use an event_ticker from above to search for its markets below.")
                else:
                    st.write(r.json())
            else:
                st.error(f"API error {r.status_code}: {r.text}")
        except Exception as ex:
            st.error(f"Request failed: {ex}")

    st.subheader("🔍 Manual search by event or market ticker")
    col_a, col_b = st.columns(2)
    with col_a:
        manual_event = st.text_input("Event ticker (e.g. KXNCAAB-2026-DUKE)")
        if manual_event:
            try:
                r = requests.get(
                    f"{BASE_URL}/events/{manual_event.upper()}",
                    params={"with_nested_markets": "true"},
                    headers=kalshi_headers(api_key),
                    timeout=8,
                )
                if r.status_code == 200:
                    markets = r.json().get("event", {}).get("markets", [])
                    st.success(f"Found {len(markets)} markets in event {manual_event.upper()}")
                else:
                    st.error(f"Not found ({r.status_code})")
            except Exception as ex:
                st.error(str(ex))
    with col_b:
        manual_ticker = st.text_input("Market ticker (e.g. KXNCAAB-2026-DUKE-WIN)")
        if manual_ticker:
            detail = fetch_market_detail(api_key, manual_ticker.upper())
            if detail:
                markets = [detail]
                st.success(f"Found market: {detail.get('title')}")
            else:
                st.error(f"Could not find ticker: {manual_ticker}")

if markets:
    st.success(f"Found **{len(markets)}** open NCAA markets on Kalshi")

    # Build summary table
    rows = []
    for m in markets:
        metrics = compute_spread_metrics(m)
        volume = float(m.get("volume_fp", 0) or 0)
        rows.append({
            "Ticker": m.get("ticker", ""),
            "Title": m.get("title", "")[:60],
            "Yes Bid ¢": f"{metrics.get('yes_bid', 0):.1f}",
            "Yes Ask ¢": f"{metrics.get('yes_ask', 0):.1f}",
            "Mid ¢": f"{metrics.get('mid', 0):.1f}",
            "Spread ¢": f"{metrics.get('spread', '—'):.1f}" if metrics.get('spread') is not None else "—",
            "Vig ¢": f"{metrics.get('total_vig', '—'):.1f}" if metrics.get('total_vig') is not None else "—",
            "Volume": f"{volume:,.0f}",
            "Signal": edge_signal(metrics.get("mid", 0), metrics.get("spread"), volume),
        })

    df = pd.DataFrame(rows)

    # Highlight sharp markets
    sharp = df[df["Signal"] == "🟢 Sharp market"]
    if not sharp.empty:
        st.subheader("🟢 Sharp Markets — Tight Spreads, High Volume")
        st.dataframe(sharp, use_container_width=True, hide_index=True)

    st.subheader("📊 All Open NCAA Markets")
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ── Order book deep dive ──────────────────────────────────────────────────

    if show_orderbook and markets:
        st.divider()
        st.subheader("📖 Order Book Deep Dive")
        
        tickers = [m["ticker"] for m in markets]
        selected = st.selectbox("Select a market", tickers,
                                format_func=lambda t: next(
                                    (m["title"] for m in markets if m["ticker"] == t), t))

        ob = fetch_orderbook(api_key, selected)
        detail = fetch_market_detail(api_key, selected)
        metrics = compute_spread_metrics(detail) if detail else {}

        if ob or detail:
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Yes Bid", f"{metrics.get('yes_bid', '—'):.1f}¢")
            col2.metric("Yes Ask", f"{metrics.get('yes_ask', '—'):.1f}¢")
            col3.metric("Mid Price", f"{metrics.get('mid', '—'):.1f}¢",
                        help="Implied win probability")
            col4.metric("Spread", 
                        f"{metrics.get('spread', '—'):.1f}¢" if metrics.get('spread') is not None else "—",
                        delta=f"Vig: {metrics.get('total_vig', '—'):.1f}¢" if metrics.get('total_vig') is not None else None,
                        delta_color="inverse")

            yes_bids = ob.get("yes_dollars", [])
            no_bids  = ob.get("no_dollars", [])

            col_yes, col_no = st.columns(2)

            with col_yes:
                st.markdown("**YES Bids** (buyers)")
                if yes_bids:
                    yes_df = pd.DataFrame(yes_bids, columns=["Price ($)", "Size"])
                    yes_df["Price ($)"] = yes_df["Price ($)"].astype(float)
                    yes_df["Size"] = yes_df["Size"].astype(float)
                    yes_df["Implied %"] = (yes_df["Price ($)"] * 100).round(1)
                    st.dataframe(yes_df.head(10), use_container_width=True, hide_index=True)
                else:
                    st.info("No yes bids available")

            with col_no:
                st.markdown("**NO Bids** (= YES asks inverted)")
                if no_bids:
                    no_df = pd.DataFrame(no_bids, columns=["Price ($)", "Size"])
                    no_df["Price ($)"] = no_df["Price ($)"].astype(float)
                    no_df["Size"] = no_df["Size"].astype(float)
                    no_df["Implied YES ask %"] = ((1 - no_df["Price ($)"]) * 100).round(1)
                    st.dataframe(no_df.head(10), use_container_width=True, hide_index=True)
                else:
                    st.info("No no-bids available")

            # Spread visualisation
            if metrics.get("yes_bid") and metrics.get("yes_ask"):
                st.divider()
                st.markdown("**Bid/Ask Spread Visualisation**")
                spread_data = pd.DataFrame({
                    "Level": ["Yes Bid", "Mid", "Yes Ask"],
                    "Price (¢)": [
                        metrics["yes_bid"],
                        metrics["mid"],
                        metrics["yes_ask"],
                    ]
                })
                st.bar_chart(spread_data.set_index("Level"))

        else:
            st.warning("Could not fetch order book for this market.")

# ── Auto-refresh ──────────────────────────────────────────────────────────────

st.divider()
col_left, col_right = st.columns([3, 1])
with col_right:
    if st.button("🔄 Refresh now"):
        st.cache_data.clear()
        st.rerun()

# Auto-refresh via Streamlit rerun
time.sleep(refresh_rate)
st.cache_data.clear()
st.rerun()
