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
from datetime import datetime, timezone

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

    # --- Strategy 1: fetch by confirmed series ticker KXNCAAMBGAME first ---
    CANDIDATE_SERIES = [
        "KXNCAAMBGAME",  # confirmed series ticker from URL
        "KXNCAAMB", "KXNCAAB", "KXMARCHMADNESS", "KXCBB",
        
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

    # --- Strategy 1b: fetch today's games directly by event ticker pattern ---
    # Pattern confirmed: KXNCAAMBGAME-26MAR19HOWMICH (away+home 3-letter codes)
    # Try fetching the parent series events list
    try:
        r = requests.get(
            f"{BASE_URL}/events",
            params={"series_ticker": "KXNCAAMBGAME", "status": "open", "limit": 200, "with_nested_markets": "true"},
            headers=kalshi_headers(api_key),
            timeout=10,
        )
        if r.status_code == 200:
            for event in r.json().get("events", []):
                for m in event.get("markets", []):
                    if m["ticker"] not in seen_tickers:
                        seen_tickers.add(m["ticker"])
                        all_markets.append(m)
        if all_markets:
            return all_markets
    except Exception:
        pass

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

    # Filter to only markets open right now (status=open) and closing today/soon
    # Also filter out markets whose close_time is in the future > 24h (upcoming games)
    # We want: games happening TODAY only
    now = datetime.now(timezone.utc)
    def is_today(m):
        close = m.get("close_time") or m.get("expiration_time") or ""
        if not close:
            return True  # no close time, include it
        try:
            ct = datetime.fromisoformat(close.replace("Z", "+00:00"))
            hours_until_close = (ct - now).total_seconds() / 3600
            # Keep markets closing within next 12 hours (today's games)
            # or already closed in last 2 hours (live/just finished)
            return -2 <= hours_until_close <= 12
        except Exception:
            return True

    all_markets = [m for m in all_markets if is_today(m)]
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
    from collections import defaultdict

    # ── Group by game ────────────────────────────────────────────────────────
    games = defaultdict(list)
    for m in markets:
        event = m.get("event_ticker", m.get("ticker", ""))
        games[event].append(m)

    # ── Build enriched game objects ──────────────────────────────────────────
    def implied_american(prob):
        """Convert 0-100 prob to American odds string."""
        if prob <= 0 or prob >= 100:
            return "N/A"
        if prob >= 50:
            return f"-{int(round(prob / (100 - prob) * 100))}"
        else:
            return f"+{int(round((100 - prob) / prob * 100))}"

    def bet_verdict(fav_mid, dog_mid, spread, vol, fav_book_ml, dog_book_ml):
        """
        Core logic: compare Kalshi mid (true prob) to BetMGM/El Cortez implied prob.
        Returns (verdict_str, color, detail_str)
        
        Without live book data we use Kalshi itself as the signal:
        - Tight spread + high volume = market is sharp = trust the mid
        - Big discrepancy between fav_mid + dog_mid and 100 = vig is wide = book edge
        - If fav_mid > 80: heavy chalk, low value
        - If dog_mid between 25-45: underdog has real value zone
        """
        if spread is None or vol < 100:
            return "⚪ NO DATA", "#555", "Insufficient liquidity to signal"

        total = fav_mid + dog_mid
        vig_pct = total - 100  # how much the market is taking

        # Assess market quality
        if spread <= 2 and vol > 5000:
            quality = "SHARP"
        elif spread <= 4 and vol > 1000:
            quality = "LIQUID"
        else:
            quality = "THIN"

        if quality == "THIN":
            return "⚪ SKIP", "#888", f"Thin market (spread {spread:.0f}¢, vol {vol:,.0f}) — signal unreliable"

        # Key insight: in a sharp Kalshi market, the mid IS the fair line.
        # Compare to what El Cortez typically posts:
        # Favorites are usually OVERPRICED at retail books (public bets chalk)
        # Underdogs 25-45¢ range are often UNDERPRICED at retail

        fav_ml = implied_american(fav_mid)
        dog_ml = implied_american(dog_mid)

        if fav_mid >= 85:
            verdict = "🟡 CHALK — LOW VALUE"
            color = "#b8860b"
            detail = (f"Kalshi has {fav_mid:.0f}% ({fav_ml}). "
                      f"Retail books likely at similar or worse price. "
                      f"Only bet if you find {implied_american(fav_mid+4)} or better at the window.")
        elif 25 <= dog_mid <= 45 and quality == "SHARP":
            verdict = "🟢 BET THE DOG"
            color = "#2e8b57"
            detail = (f"Kalshi sharp market says {dog_mid:.0f}% ({dog_ml}). "
                      f"Public undervalues underdogs in this range. "
                      f"Look for {implied_american(dog_mid - 5)} or better at El Cortez.")
        elif 55 <= fav_mid <= 72 and quality in ("SHARP","LIQUID"):
            verdict = "🟢 LIVE GAME WATCH"
            color = "#2e8b57"
            detail = (f"Close game ({fav_mid:.0f}/{dog_mid:.0f}). "
                      f"Sharp money split. Watch for live line lag — "
                      f"if momentum shifts, Kalshi reprices faster than the book.")
        elif fav_mid > 72 and fav_mid < 85:
            verdict = "🟡 FAV OK IF PRICE RIGHT"
            color = "#b8860b"
            detail = (f"Kalshi: {fav_mid:.0f}% ({fav_ml}). "
                      f"Fair bet only if book is at {implied_american(fav_mid+3)} or worse (giving you value). "
                      f"Skip if book is sharper than Kalshi.")
        else:
            verdict = "⚪ PASS"
            color = "#888"
            detail = f"No clear edge. Kalshi: {fav_mid:.0f}% fav / {dog_mid:.0f}% dog."

        return verdict, color, detail

    # ── Render game cards ────────────────────────────────────────────────────
    game_list = sorted(games.items())
    st.markdown(f"### 🏀 {len(game_list)} Games Today")

    for event_ticker, game_markets in game_list:
        game_markets.sort(key=lambda m: float(m.get("yes_bid_dollars", 0) or 0), reverse=True)
        if len(game_markets) < 2:
            continue

        fav  = game_markets[0]
        dog  = game_markets[1]
        fav_m = compute_spread_metrics(fav)
        dog_m = compute_spread_metrics(dog)

        fav_name = fav.get("yes_sub_title") or fav.get("subtitle") or fav.get("ticker","").split("-")[-1]
        dog_name = dog.get("yes_sub_title") or dog.get("subtitle") or dog.get("ticker","").split("-")[-1]
        if fav_name == dog_name:
            fav_name = fav.get("ticker","").split("-")[-1]
            dog_name = dog.get("ticker","").split("-")[-1]

        fav_vol  = float(fav.get("volume_fp", 0) or 0)
        dog_vol  = float(dog.get("volume_fp", 0) or 0)
        total_vol = fav_vol + dog_vol
        spread   = fav_m.get("spread")
        fav_mid  = fav_m.get("mid", 0)
        dog_mid  = dog_m.get("mid", 0)

        game_title = fav.get("title", event_ticker).replace(" Winner?","").replace(" winner?","").strip()

        verdict, vcolor, detail = bet_verdict(fav_mid, dog_mid, spread, total_vol, None, None)

        fav_american = implied_american(fav_mid)
        dog_american = implied_american(dog_mid)

        with st.container():
            st.markdown(f"""
<div style="border:1px solid #333; border-radius:12px; padding:16px; margin-bottom:12px; background:#1a1a1a;">
  <div style="font-size:1.05em; font-weight:600; color:#ddd; margin-bottom:8px;">🏀 {game_title}</div>
  <div style="font-size:1.6em; font-weight:800; color:{vcolor}; margin-bottom:10px;">{verdict}</div>
  <div style="display:flex; gap:16px; margin-bottom:10px;">
    <div style="flex:1; background:#222; border-radius:8px; padding:10px; text-align:center;">
      <div style="font-size:0.75em; color:#aaa; text-transform:uppercase;">FAV</div>
      <div style="font-size:1.1em; font-weight:700; color:#fff;">{fav_name}</div>
      <div style="font-size:1.4em; font-weight:800; color:#4fc3f7;">{fav_mid:.0f}¢</div>
      <div style="font-size:0.9em; color:#aaa;">{fav_american}</div>
    </div>
    <div style="flex:1; background:#222; border-radius:8px; padding:10px; text-align:center;">
      <div style="font-size:0.75em; color:#aaa; text-transform:uppercase;">DOG</div>
      <div style="font-size:1.1em; font-weight:700; color:#fff;">{dog_name}</div>
      <div style="font-size:1.4em; font-weight:800; color:#ff8a65;">{dog_mid:.0f}¢</div>
      <div style="font-size:0.9em; color:#aaa;">{dog_american}</div>
    </div>
    <div style="flex:1; background:#222; border-radius:8px; padding:10px; text-align:center;">
      <div style="font-size:0.75em; color:#aaa; text-transform:uppercase;">MARKET</div>
      <div style="font-size:0.9em; color:#ccc;">Spread: {f"{spread:.1f}¢" if spread else "—"}</div>
      <div style="font-size:0.9em; color:#ccc;">Vol: {total_vol:,.0f}</div>
      <div style="font-size:0.9em; color:#ccc;">Vig: {f"{fav_m.get('total_vig',0):.1f}¢" if fav_m.get('total_vig') else "—"}</div>
    </div>
  </div>
  <div style="background:#111; border-left:3px solid {vcolor}; padding:8px 12px; border-radius:4px; font-size:0.88em; color:#ccc;">
    💡 {detail}
  </div>
</div>
""", unsafe_allow_html=True)

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
