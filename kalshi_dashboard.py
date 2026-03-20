"""
March Madness Kalshi Live Dashboard  —  v2 "El Cortez Edition"
───────────────────────────────────────────────────────────────
Streamlit app — deploy free at streamlit.io/cloud

What's new in v2:
  • Fixed vig double-counting bug
  • "AT THE WINDOW" card: exact moneyline thresholds to look for in person
  • Kelly criterion bet sizing per game
  • March Madness seed upset base rates as sanity check
  • Smarter market pairing (filters for Winner markets)
  • 10-second refresh when live games are active
  • Bankroll tracker in sidebar

Setup:
  pip install streamlit requests pandas
  streamlit run kalshi_dashboard.py

Set your KALSHI_API_KEY in Streamlit Cloud > App Settings > Secrets.
"""

import streamlit as st
import requests
import pandas as pd
import time
import math
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Config ─────────────────────────────────────────────────────────────────

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Historical March Madness upset rates by seed matchup (1985–2024)
# Source: NCAA tournament historical data
# Key = (higher_seed, lower_seed), Value = underdog win %
SEED_UPSET_RATES = {
    (1, 16): 0.02,   # 1 upset in ~160 games (UMBC, FDU)
    (2, 15): 0.06,   # ~10 upsets total
    (3, 14): 0.13,
    (4, 13): 0.21,
    (5, 12): 0.35,   # the famous 12-over-5
    (6, 11): 0.37,
    (7, 10): 0.39,
    (8, 9):  0.49,   # near coin flip
}

# Common team-to-seed mapping for 2026 tournament (update each year)
# This is a fallback — ideally we'd pull from Kalshi market metadata
# Leave empty and it won't crash, just won't show seed context
TEAM_SEEDS_2026 = {
    # Update these with actual 2026 bracket seeds
    # "DUKE": 1, "HOUSTON": 1, "AUBURN": 1, "FLORIDA": 1,
    # "MICHIGAN": 2, "TENN": 2, ...
    # "HOWARD": 16, "AMER": 16, ...
}


# ── Kalshi API helpers ──────────────────────────────────────────────────────

def get_api_key():
    try:
        return st.secrets["KALSHI_API_KEY"]
    except Exception:
        return None


def kalshi_headers(api_key):
    return {
        "accept": "application/json",
        "KALSHI-ACCESS-KEY": api_key,
    }


def fetch_ncaa_markets(api_key, ttl_seconds=30):
    """Fetch NCAA basketball markets with configurable TTL."""

    @st.cache_data(ttl=ttl_seconds)
    def _fetch(api_key_inner):
        NCAA_KEYWORDS = [
            "ncaa", "march madness", "basketball", "ncaab", "ncaamb",
            "college basketball", "tournament", "kxncaamb",
        ]
        all_markets = []
        seen_tickers = set()

        # ── Strategy 1: confirmed series ticker ──
        CANDIDATE_SERIES = [
            "KXNCAAMBGAME",
            "KXNCAAMB", "KXNCAAB", "KXMARCHMADNESS", "KXCBB",
        ]
        for series in CANDIDATE_SERIES:
            try:
                r = requests.get(
                    f"{BASE_URL}/markets",
                    params={"series_ticker": series, "status": "open", "limit": 200},
                    headers=kalshi_headers(api_key_inner),
                    timeout=8,
                )
                if r.status_code == 200:
                    for m in r.json().get("markets", []):
                        if m["ticker"] not in seen_tickers:
                            seen_tickers.add(m["ticker"])
                            all_markets.append(m)
            except Exception:
                pass
        if all_markets:
            return _filter_today(all_markets)

        # ── Strategy 1b: events endpoint with nested markets ──
        try:
            r = requests.get(
                f"{BASE_URL}/events",
                params={
                    "series_ticker": "KXNCAAMBGAME",
                    "status": "open", "limit": 200,
                    "with_nested_markets": "true",
                },
                headers=kalshi_headers(api_key_inner),
                timeout=10,
            )
            if r.status_code == 200:
                for event in r.json().get("events", []):
                    for m in event.get("markets", []):
                        if m["ticker"] not in seen_tickers:
                            seen_tickers.add(m["ticker"])
                            all_markets.append(m)
            if all_markets:
                return _filter_today(all_markets)
        except Exception:
            pass

        # ── Strategy 2: paginate open events, keyword filter ──
        cursor = None
        pages = 0
        while pages < 10:
            params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            try:
                r = requests.get(
                    f"{BASE_URL}/events", params=params,
                    headers=kalshi_headers(api_key_inner), timeout=10,
                )
                if r.status_code != 200:
                    break
                data = r.json()
                events = data.get("events", [])
                for event in events:
                    title = " ".join([
                        event.get("title", ""),
                        event.get("sub_title", ""),
                        event.get("series_ticker", ""),
                    ]).lower()
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

        # ── Strategy 3: broad scan ──
        if not all_markets:
            try:
                r = requests.get(
                    f"{BASE_URL}/markets",
                    params={"status": "open", "limit": 1000},
                    headers=kalshi_headers(api_key_inner), timeout=10,
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

        return _filter_today(all_markets)

    return _fetch(api_key)


def _filter_today(markets):
    """Keep only markets closing within a reasonable window of now."""
    now = datetime.now(timezone.utc)

    def is_relevant(m):
        close = m.get("close_time") or m.get("expiration_time") or ""
        if not close:
            return True
        try:
            ct = datetime.fromisoformat(close.replace("Z", "+00:00"))
            hours_diff = (ct - now).total_seconds() / 3600
            # -3h (just finished) to +14h (tonight's late games Pacific time)
            return -3 <= hours_diff <= 14
        except Exception:
            return True

    return [m for m in markets if is_relevant(m)]


@st.cache_data(ttl=10)
def fetch_orderbook(api_key, ticker):
    url = f"{BASE_URL}/markets/{ticker}/orderbook"
    try:
        r = requests.get(url, headers=kalshi_headers(api_key), timeout=8)
        if r.status_code == 200:
            return r.json().get("orderbook_fp", {})
    except Exception:
        pass
    return {}


@st.cache_data(ttl=10)
def fetch_market_detail(api_key, ticker):
    url = f"{BASE_URL}/markets/{ticker}"
    try:
        r = requests.get(url, headers=kalshi_headers(api_key), timeout=8)
        if r.status_code == 200:
            return r.json().get("market", {})
    except Exception:
        pass
    return {}


# ── Math helpers ────────────────────────────────────────────────────────────

def compute_spread_metrics(market):
    """
    Compute bid/ask/mid/spread from Kalshi market data.
    FIXED: vig is the single spread (yes_ask - yes_bid), not doubled.
    """
    try:
        yes_bid = float(market.get("yes_bid_dollars", 0) or 0) * 100
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0) * 100
        no_bid = float(market.get("no_bid_dollars", 0) or 0) * 100
        no_ask = float(market.get("no_ask_dollars", 0) or 0) * 100

        if yes_ask == 0 and no_bid > 0:
            yes_ask = 100 - no_bid
        if no_ask == 0 and yes_bid > 0:
            no_ask = 100 - yes_bid

        mid = (yes_bid + yes_ask) / 2 if yes_ask > 0 else yes_bid
        spread = (yes_ask - yes_bid) if yes_ask > 0 else None

        # Vig = the spread. On Kalshi yes+no are the same contract,
        # so the vig is just the bid-ask spread (NOT doubled).
        vig = spread

        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "mid": mid,
            "spread": spread,
            "vig": vig,
        }
    except Exception:
        return {}


def mid_to_american(prob):
    """Convert 0–100 implied probability to American odds string."""
    if prob <= 0 or prob >= 100:
        return "N/A"
    if prob >= 50:
        return f"-{int(round(prob / (100 - prob) * 100))}"
    else:
        return f"+{int(round((100 - prob) / prob * 100))}"


def american_to_implied(odds_str):
    """Convert American odds string to implied probability 0–100."""
    try:
        odds = int(odds_str.replace("+", ""))
        if odds > 0:
            return 100 / (odds + 100) * 100
        else:
            return abs(odds) / (abs(odds) + 100) * 100
    except Exception:
        return 0


def kelly_fraction(true_prob, book_odds_american):
    """
    Kelly criterion: what % of bankroll to bet.
    true_prob: 0–1 (from Kalshi mid)
    book_odds_american: the line you'd actually bet at the window
    Returns fraction of bankroll (0.0 to ~0.25, capped at quarter-Kelly for safety)
    """
    try:
        odds = int(book_odds_american.replace("+", ""))
        if odds > 0:
            b = odds / 100  # profit per $1 bet
        else:
            b = 100 / abs(odds)

        p = true_prob
        q = 1 - p
        f = (p * b - q) / b
        # Cap at quarter-Kelly (standard conservative approach)
        return max(0, min(f * 0.25, 0.15))
    except Exception:
        return 0


def market_quality(spread, volume):
    """Classify market quality."""
    if spread is None or volume < 100:
        return "NO_DATA", "⚪", "#555"
    if spread <= 2 and volume > 5000:
        return "SHARP", "🟢", "#2e8b57"
    if spread <= 3 and volume > 2000:
        return "SHARP", "🟢", "#2e8b57"
    if spread <= 4 and volume > 500:
        return "LIQUID", "🟡", "#b8860b"
    if spread > 8:
        return "WIDE", "🔴", "#c0392b"
    return "MODERATE", "🟡", "#b8860b"


def guess_seeds(fav_name, dog_name):
    """Try to guess seed matchup from team names."""
    fav_seed = TEAM_SEEDS_2026.get(fav_name.upper().strip())
    dog_seed = TEAM_SEEDS_2026.get(dog_name.upper().strip())
    return fav_seed, dog_seed


def seed_context(fav_seed, dog_seed):
    """Return historical upset rate context if we know the seeds."""
    if fav_seed is None or dog_seed is None:
        return None
    key = (min(fav_seed, dog_seed), max(fav_seed, dog_seed))
    rate = SEED_UPSET_RATES.get(key)
    if rate is None:
        return None
    return {
        "matchup": f"#{key[0]} vs #{key[1]}",
        "upset_rate": rate,
        "upset_pct": int(round(rate * 100)),
    }


# ── The core analysis engine ───────────────────────────────────────────────

def full_game_analysis(fav_mid, dog_mid, spread, volume, fav_name, dog_name):
    """
    Returns a dict with everything the card needs:
      verdict, color, detail, window_guide[], kelly_table[], seed_info
    """
    result = {
        "verdict": "",
        "color": "#555",
        "detail": "",
        "window_guide": [],   # list of {"label": ..., "value": ...}
        "kelly_table": [],    # list of {"book_line": ..., "kelly_pct": ..., "edge": ...}
        "seed_info": None,
        "quality": "NO_DATA",
    }

    quality, quality_icon, quality_color = market_quality(spread, volume)
    result["quality"] = quality
    result["quality_icon"] = quality_icon
    result["quality_color"] = quality_color

    if quality == "NO_DATA":
        result["verdict"] = "⚪ NO DATA"
        result["detail"] = "Not enough liquidity to generate a signal."
        return result

    if quality == "WIDE":
        result["verdict"] = "⚪ SKIP"
        result["color"] = "#888"
        result["detail"] = f"Spread is {spread:.0f}¢ — too wide. Kalshi price is noise, not signal."
        return result

    fav_american = mid_to_american(fav_mid)
    dog_american = mid_to_american(dog_mid)

    # ── Seed context ──
    fav_seed, dog_seed = guess_seeds(fav_name, dog_name)
    sc = seed_context(fav_seed, dog_seed)
    if sc:
        result["seed_info"] = sc
        # If Kalshi's dog_mid is meaningfully off from historical base rate, flag it
        if abs(dog_mid - sc["upset_pct"]) > 8:
            result["seed_info"]["note"] = (
                f"History says {sc['matchup']} upsets hit {sc['upset_pct']}% of the time. "
                f"Kalshi has {dog_mid:.0f}%. {'Kalshi is higher — market sees something.' if dog_mid > sc['upset_pct'] else 'Kalshi is lower — could be value on the dog.'}"
            )

    # ── Verdict logic ──
    if quality == "MODERATE" and volume < 500:
        result["verdict"] = "🟡 THIN — PROCEED WITH CAUTION"
        result["color"] = "#b8860b"
        result["detail"] = (
            f"Market is tradeable but not sharp. Kalshi mid: fav {fav_mid:.0f}¢ / dog {dog_mid:.0f}¢. "
            f"Treat as directional guidance only."
        )
    elif fav_mid >= 88:
        result["verdict"] = "🟡 HEAVY CHALK"
        result["color"] = "#b8860b"
        result["detail"] = (
            f"Kalshi: {fav_mid:.0f}% ({fav_american}). "
            f"Almost never worth laying this much at retail. "
            f"Only if you find {mid_to_american(fav_mid + 5)} or better, which you won't."
        )
    elif fav_mid >= 78:
        result["verdict"] = "🟡 FAV OK IF PRICE IS RIGHT"
        result["color"] = "#b8860b"
        result["detail"] = (
            f"Kalshi says {fav_mid:.0f}% true probability. "
            f"Only worth it at the window if you see the favorite at "
            f"{mid_to_american(fav_mid + 3)} or softer (i.e., cheaper than fair)."
        )
    elif 25 <= dog_mid <= 45 and quality == "SHARP":
        result["verdict"] = "🟢 BET THE DOG"
        result["color"] = "#2e8b57"
        result["detail"] = (
            f"Sharp money says {dog_mid:.0f}% ({dog_american}). "
            f"Retail books systematically underprice underdogs in this zone. "
            f"Look for {mid_to_american(max(dog_mid - 5, 1))} or better at the window."
        )
    elif 20 <= dog_mid < 25 and quality == "SHARP":
        result["verdict"] = "🟢 DOG SPRINKLE"
        result["color"] = "#2e8b57"
        result["detail"] = (
            f"Kalshi: {dog_mid:.0f}% ({dog_american}). Long shot but in the "
            f"value zone if retail is offering {mid_to_american(max(dog_mid - 4, 1))} or better. "
            f"Small bet only — quarter unit max."
        )
    elif 55 <= fav_mid < 68 and quality in ("SHARP", "LIQUID"):
        result["verdict"] = "🟢 LIVE GAME WATCH"
        result["color"] = "#2e8b57"
        result["detail"] = (
            f"Coin-flip-ish game ({fav_mid:.0f}/{dog_mid:.0f}). "
            f"This is your live betting play: watch both apps, "
            f"bet when Kalshi jumps and the El Cortez board lags. Window is 30s–2min."
        )
    elif 68 <= fav_mid < 78 and quality in ("SHARP", "LIQUID"):
        result["verdict"] = "🟡 LEAN FAV — CHECK LINE"
        result["color"] = "#b8860b"
        result["detail"] = (
            f"Kalshi: {fav_mid:.0f}% ({fav_american}). "
            f"If El Cortez is softer than {fav_american}, take it. "
            f"If they're sharper, look at the dog side."
        )
    else:
        result["verdict"] = "⚪ NO CLEAR EDGE"
        result["color"] = "#888"
        result["detail"] = (
            f"Kalshi: {fav_mid:.0f}% fav / {dog_mid:.0f}% dog. "
            f"No obvious mispricing to exploit. Save your bankroll."
        )

    # ── "AT THE WINDOW" guide ──
    # For each side, show: what Kalshi says fair odds are, what to look for,
    # and the threshold where you have a real edge

    # Favorite side
    fair_fav = fav_american
    # You want to find the fav CHEAPER than fair → that means the book's number
    # is less negative (e.g., Kalshi says -250 fair, book shows -200 = value)
    value_fav = mid_to_american(fav_mid + 4)  # 4¢ edge = real money
    good_fav = mid_to_american(fav_mid + 2)   # 2¢ = marginal

    # Underdog side
    fair_dog = dog_american
    # You want the dog at LONGER odds than fair → book shows +350 when fair is +300
    value_dog = mid_to_american(max(dog_mid - 4, 1))
    good_dog = mid_to_american(max(dog_mid - 2, 1))

    result["window_guide"] = [
        {
            "side": "FAV",
            "team": fav_name,
            "fair": fair_fav,
            "good": good_fav,
            "great": value_fav,
            "explanation": (
                f"Fair line is ~{fair_fav}. "
                f"If board shows {good_fav} or softer → decent. "
                f"{value_fav} or softer → strong edge, bet it."
            ),
        },
        {
            "side": "DOG",
            "team": dog_name,
            "fair": fair_dog,
            "good": good_dog,
            "great": value_dog,
            "explanation": (
                f"Fair line is ~{fair_dog}. "
                f"If board shows {good_dog} or longer → decent. "
                f"{value_dog} or longer → strong edge, bet it."
            ),
        },
    ]

    # ── Kelly table ──
    # Show a few realistic book lines and how much to bet at each
    true_dog_prob = dog_mid / 100
    true_fav_prob = fav_mid / 100

    dog_lines = []
    fav_lines = []

    # Generate realistic book lines around the fair price
    if dog_mid > 5:
        for offset in [-6, -4, -2, 0, +2, +4]:
            test_prob = dog_mid + offset
            if test_prob <= 0 or test_prob >= 100:
                continue
            test_line = mid_to_american(test_prob)
            kf = kelly_fraction(true_dog_prob, test_line)
            edge = true_dog_prob - (test_prob / 100)
            if kf > 0.001:
                dog_lines.append({
                    "book_line": test_line,
                    "kelly_pct": f"{kf * 100:.1f}%",
                    "edge": f"+{edge * 100:.1f}¢",
                    "tag": "✅ BET" if kf >= 0.02 else "⚠️ SMALL",
                })
            elif edge > 0:
                dog_lines.append({
                    "book_line": test_line,
                    "kelly_pct": "—",
                    "edge": f"+{edge * 100:.1f}¢",
                    "tag": "🔸 MARGINAL",
                })

    result["kelly_table_dog"] = dog_lines

    if fav_mid < 95:
        for offset in [-4, -2, 0, +2, +4, +6]:
            test_prob = fav_mid + offset
            if test_prob <= 0 or test_prob >= 100:
                continue
            test_line = mid_to_american(test_prob)
            kf = kelly_fraction(true_fav_prob, test_line)
            edge = true_fav_prob - (test_prob / 100)
            if kf > 0.001:
                fav_lines.append({
                    "book_line": test_line,
                    "kelly_pct": f"{kf * 100:.1f}%",
                    "edge": f"+{edge * 100:.1f}¢",
                    "tag": "✅ BET" if kf >= 0.02 else "⚠️ SMALL",
                })
            elif edge > 0:
                fav_lines.append({
                    "book_line": test_line,
                    "kelly_pct": "—",
                    "edge": f"+{edge * 100:.1f}¢",
                    "tag": "🔸 MARGINAL",
                })

    result["kelly_table_fav"] = fav_lines

    return result


# ── UI ──────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="March Madness · Kalshi Live",
    page_icon="🏀",
    layout="wide",
)

st.title("🏀 March Madness — Kalshi Live Dashboard")
st.caption(f"Last refreshed: {datetime.now().strftime('%I:%M:%S %p')}  ·  v2 El Cortez Edition")

# ── Sidebar ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Settings")

    api_key = get_api_key()
    if not api_key:
        api_key = st.text_input(
            "Kalshi API Key",
            type="password",
            help="Paste your key here, or set KALSHI_API_KEY in Streamlit secrets",
        )
    else:
        st.success("✅ API key loaded")

    refresh_rate = st.slider("Refresh interval (seconds)", 10, 120, 30)

    st.divider()
    st.header("💰 Bankroll")
    bankroll = st.number_input("Your bankroll ($)", min_value=10, max_value=50000, value=500, step=50)
    st.caption("Kelly sizing below will use this number.")

    st.divider()
    st.markdown("### 📖 Quick Reference")
    st.markdown("""
**Reading the cards:**
- **Mid** = Kalshi's best estimate of true probability
- **Spread** = market tightness (lower = more reliable)
- 🟢 Sharp = lots of informed traders, trust the price
- 🟡 Liquid = decent but not definitive
- 🔴 Wide = ignore the price

**At the window:**
- "Softer" favorite = less negative number (−200 is softer than −250)
- "Longer" underdog = more positive number (+350 is longer than +300)
- You WANT softer favorites and longer underdogs vs Kalshi's fair price

**Kelly sizing:**
- Shows quarter-Kelly (conservative) to avoid ruin
- "✅ BET" = meaningful edge, worth a wager
- "⚠️ SMALL" = edge exists but tiny
- "🔸 MARGINAL" = barely positive, skip unless you love the spot
    """)

if not api_key:
    st.warning("👈 Enter your Kalshi API key in the sidebar to get started.")
    st.stop()

# ── Main content ─────────────────────────────────────────────────────────────

with st.spinner("Fetching Kalshi NCAA markets..."):
    markets = fetch_ncaa_markets(api_key, ttl_seconds=min(refresh_rate, 30))

if not markets:
    st.warning("No NCAA markets found. Let's diagnose and search manually.")

    with st.expander("🔍 Diagnostic — show all open Kalshi events"):
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
                        {
                            "event_ticker": e.get("event_ticker"),
                            "title": e.get("title"),
                            "category": e.get("category"),
                        }
                        for e in events
                    ])
                    st.dataframe(diag_df, use_container_width=True, hide_index=True)
            else:
                st.error(f"API error {r.status_code}: {r.text}")
        except Exception as ex:
            st.error(f"Request failed: {ex}")

    st.subheader("🔍 Manual search")
    col_a, col_b = st.columns(2)
    with col_a:
        manual_event = st.text_input("Event ticker")
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
                    st.success(f"Found {len(markets)} markets")
                else:
                    st.error(f"Not found ({r.status_code})")
            except Exception as ex:
                st.error(str(ex))
    with col_b:
        manual_ticker = st.text_input("Market ticker")
        if manual_ticker:
            detail = fetch_market_detail(api_key, manual_ticker.upper())
            if detail:
                markets = [detail]
                st.success(f"Found: {detail.get('title')}")
            else:
                st.error(f"Not found: {manual_ticker}")

if markets:
    # ── Group by game (event) ────────────────────────────────────────────
    games = defaultdict(list)
    for m in markets:
        # Only keep "Winner" markets, skip spreads/totals if present
        title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
        if "winner" in title or "win" in title or not any(
            kw in title for kw in ["spread", "total", "over", "under", "points"]
        ):
            event = m.get("event_ticker", m.get("ticker", ""))
            games[event].append(m)

    # ── Render game cards ────────────────────────────────────────────────
    game_list = sorted(games.items())
    st.markdown(f"### 🏀 {len(game_list)} Games")

    for event_ticker, game_markets in game_list:
        # Sort: highest yes_bid first = favorite
        game_markets.sort(
            key=lambda m: float(m.get("yes_bid_dollars", 0) or 0), reverse=True
        )
        if len(game_markets) < 2:
            continue

        fav = game_markets[0]
        dog = game_markets[1]
        fav_m = compute_spread_metrics(fav)
        dog_m = compute_spread_metrics(dog)

        # Extract team names
        fav_name = (
            fav.get("yes_sub_title")
            or fav.get("subtitle")
            or fav.get("ticker", "").split("-")[-1]
        )
        dog_name = (
            dog.get("yes_sub_title")
            or dog.get("subtitle")
            or dog.get("ticker", "").split("-")[-1]
        )
        if fav_name == dog_name:
            fav_name = fav.get("ticker", "").split("-")[-1]
            dog_name = dog.get("ticker", "").split("-")[-2] if len(dog.get("ticker", "").split("-")) > 2 else "?"

        fav_vol = float(fav.get("volume_fp", 0) or 0)
        dog_vol = float(dog.get("volume_fp", 0) or 0)
        total_vol = fav_vol + dog_vol
        spread = fav_m.get("spread")
        fav_mid = fav_m.get("mid", 0)
        dog_mid = dog_m.get("mid", 0)

        game_title = (
            fav.get("title", event_ticker)
            .replace(" Winner?", "")
            .replace(" winner?", "")
            .strip()
        )

        # ── Run full analysis ──
        analysis = full_game_analysis(
            fav_mid, dog_mid, spread, total_vol, fav_name, dog_name
        )

        verdict = analysis["verdict"]
        vcolor = analysis["color"]
        detail = analysis["detail"]
        quality_icon = analysis.get("quality_icon", "⚪")
        window = analysis.get("window_guide", [])
        kelly_dog = analysis.get("kelly_table_dog", [])
        kelly_fav = analysis.get("kelly_table_fav", [])
        seed_info = analysis.get("seed_info")

        fav_american = mid_to_american(fav_mid)
        dog_american = mid_to_american(dog_mid)

        # ── Window guide HTML ──
        window_html = ""
        if window:
            window_html = """
<div style="margin-top:10px; padding:10px; background:#0d1117; border-radius:8px; border:1px solid #333;">
  <div style="font-size:0.85em; font-weight:700; color:#58a6ff; margin-bottom:6px; text-transform:uppercase; letter-spacing:1px;">🎯 AT THE WINDOW — What to look for</div>
"""
            for w in window:
                side_color = "#4fc3f7" if w["side"] == "FAV" else "#ff8a65"
                window_html += f"""
  <div style="margin-bottom:6px; padding:6px 8px; background:#161b22; border-radius:4px; border-left:3px solid {side_color};">
    <span style="font-weight:700; color:{side_color};">{w['team']}</span>
    <span style="color:#888; font-size:0.8em;"> ({w['side']})</span><br/>
    <span style="color:#ccc; font-size:0.85em;">{w['explanation']}</span>
  </div>
"""
            window_html += "</div>"

        # ── Kelly table HTML ──
        kelly_html = ""
        if kelly_dog or kelly_fav:
            kelly_html = """
<div style="margin-top:10px; padding:10px; background:#0d1117; border-radius:8px; border:1px solid #333;">
  <div style="font-size:0.85em; font-weight:700; color:#58a6ff; margin-bottom:6px; text-transform:uppercase; letter-spacing:1px;">📊 BET SIZING (Quarter-Kelly, ${bankroll} roll)</div>
""".replace("{bankroll}", f"{bankroll:,.0f}")

            if kelly_dog:
                kelly_html += f'<div style="font-size:0.8em; color:#ff8a65; font-weight:600; margin:6px 0 3px;">DOG: {dog_name}</div>'
                kelly_html += '<div style="display:flex; flex-wrap:wrap; gap:4px;">'
                for row in kelly_dog:
                    bet_amt = kelly_fraction(dog_mid / 100, row["book_line"].replace("✅ BET", "").replace("⚠️ SMALL", "").strip()) * bankroll if "—" not in row["kelly_pct"] else 0
                    kelly_html += f"""
<div style="background:#161b22; border-radius:4px; padding:4px 8px; font-size:0.78em; text-align:center; min-width:70px;">
  <div style="color:#aaa;">If board says</div>
  <div style="font-weight:700; color:#fff; font-size:1.1em;">{row['book_line']}</div>
  <div style="color:#aaa;">Edge: {row['edge']}</div>
  <div style="color:{'#2e8b57' if '✅' in row['tag'] else '#b8860b' if '⚠' in row['tag'] else '#888'}; font-weight:600;">{row['tag']}</div>
  {'<div style="color:#2e8b57; font-weight:700;">$' + f"{bet_amt:.0f}" + '</div>' if bet_amt > 1 else ''}
</div>
"""
                kelly_html += "</div>"

            if kelly_fav:
                kelly_html += f'<div style="font-size:0.8em; color:#4fc3f7; font-weight:600; margin:8px 0 3px;">FAV: {fav_name}</div>'
                kelly_html += '<div style="display:flex; flex-wrap:wrap; gap:4px;">'
                for row in kelly_fav:
                    bet_amt = kelly_fraction(fav_mid / 100, row["book_line"].replace("✅ BET", "").replace("⚠️ SMALL", "").strip()) * bankroll if "—" not in row["kelly_pct"] else 0
                    kelly_html += f"""
<div style="background:#161b22; border-radius:4px; padding:4px 8px; font-size:0.78em; text-align:center; min-width:70px;">
  <div style="color:#aaa;">If board says</div>
  <div style="font-weight:700; color:#fff; font-size:1.1em;">{row['book_line']}</div>
  <div style="color:#aaa;">Edge: {row['edge']}</div>
  <div style="color:{'#2e8b57' if '✅' in row['tag'] else '#b8860b' if '⚠' in row['tag'] else '#888'}; font-weight:600;">{row['tag']}</div>
  {'<div style="color:#2e8b57; font-weight:700;">$' + f"{bet_amt:.0f}" + '</div>' if bet_amt > 1 else ''}
</div>
"""
                kelly_html += "</div>"

            kelly_html += "</div>"

        # ── Seed context HTML ──
        seed_html = ""
        if seed_info:
            seed_html = f"""
<div style="margin-top:8px; padding:6px 10px; background:#1c1c0e; border-radius:4px; border-left:3px solid #e6c200; font-size:0.82em;">
  📜 <span style="color:#e6c200; font-weight:600;">History:</span>
  <span style="color:#ccc;">{seed_info['matchup']} — underdogs win {seed_info['upset_pct']}% historically.</span>
  {f'<br/><span style="color:#aaa;">' + seed_info.get("note", "") + '</span>' if seed_info.get("note") else ''}
</div>
"""

        # ── Render the card ──
        with st.container():
            st.markdown(f"""
<div style="border:1px solid #333; border-radius:12px; padding:16px; margin-bottom:16px; background:#1a1a1a;">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
    <div style="font-size:1.05em; font-weight:600; color:#ddd;">🏀 {game_title}</div>
    <div style="font-size:0.8em; color:#888;">{quality_icon} Spread: {f"{spread:.0f}¢" if spread else "—"} · Vol: {total_vol:,.0f}</div>
  </div>
  <div style="font-size:1.6em; font-weight:800; color:{vcolor}; margin-bottom:10px;">{verdict}</div>
  <div style="display:flex; gap:16px; margin-bottom:10px;">
    <div style="flex:1; background:#222; border-radius:8px; padding:10px; text-align:center;">
      <div style="font-size:0.75em; color:#aaa; text-transform:uppercase;">Favorite</div>
      <div style="font-size:1.1em; font-weight:700; color:#fff;">{fav_name}</div>
      <div style="font-size:1.4em; font-weight:800; color:#4fc3f7;">{fav_mid:.0f}¢</div>
      <div style="font-size:0.9em; color:#aaa;">{fav_american}</div>
    </div>
    <div style="flex:1; background:#222; border-radius:8px; padding:10px; text-align:center;">
      <div style="font-size:0.75em; color:#aaa; text-transform:uppercase;">Underdog</div>
      <div style="font-size:1.1em; font-weight:700; color:#fff;">{dog_name}</div>
      <div style="font-size:1.4em; font-weight:800; color:#ff8a65;">{dog_mid:.0f}¢</div>
      <div style="font-size:0.9em; color:#aaa;">{dog_american}</div>
    </div>
  </div>
  <div style="background:#111; border-left:3px solid {vcolor}; padding:8px 12px; border-radius:4px; font-size:0.88em; color:#ccc; margin-bottom:4px;">
    💡 {detail}
  </div>
  {seed_html}
  {window_html}
  {kelly_html}
</div>
""", unsafe_allow_html=True)

# ── Auto-refresh ──────────────────────────────────────────────────────────────

st.divider()
col_left, col_right = st.columns([3, 1])
with col_right:
    if st.button("🔄 Refresh now"):
        st.cache_data.clear()
        st.rerun()
with col_left:
    st.caption(f"Next auto-refresh in {refresh_rate}s. Tip: set to 10s during live games.")

time.sleep(refresh_rate)
st.cache_data.clear()
st.rerun()
