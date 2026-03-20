import streamlit as st
import requests
import pandas as pd
import time
from datetime import datetime, timezone
from collections import defaultdict

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"

# ── ESPN name normalization ─────────────────────────────────────────────────

TEAM_NAME_ALIASES = {
    "connecticut": "uconn",
    "north carolina": "unc",
    "mississippi": "ole miss",
    "louisiana state": "lsu",
    "southern california": "usc",
    "texas christian": "tcu",
    "brigham young": "byu",
    "florida international": "fiu",
    "texas-el paso": "utep",
    "nevada-las vegas": "unlv",
    "miami (fl)": "miami",
    "saint mary's (ca)": "saint mary's",
    "ucl a": "ucla",
}

def normalize_name(name: str) -> str:
    n = name.lower().strip()
    return TEAM_NAME_ALIASES.get(n, n)

# ── ESPN data layer ─────────────────────────────────────────────────────────

@st.cache_data(ttl=15)
def fetch_espn_games():
    """
    Returns (by_team, raw_games).
    by_team: normalized team name -> game_info dict
    raw_games: list of all game_info dicts
    """
    try:
        r = requests.get(ESPN_URL, timeout=8)
        if r.status_code != 200:
            return {}, []
        events = r.json().get("events", [])
    except Exception:
        return {}, []

    by_team   = {}
    raw_games = []

    for event in events:
        competitions = event.get("competitions", [])
        if not competitions:
            continue
        comp = competitions[0]

        # Tip-off time
        start_str = event.get("date", "")
        try:
            start_dt    = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            start_local = start_dt.astimezone()
            tipoff      = start_local.strftime("%-I:%M %p")
        except Exception:
            tipoff = start_str[:16] if start_str else "TBD"

        # Status
        status_obj    = event.get("status", {})
        status_type   = status_obj.get("type", {})
        status_state  = status_type.get("state", "pre")   # pre / in / post
        status_detail = status_type.get("shortDetail", "")
        display_clock = status_obj.get("displayClock", "")
        period        = status_obj.get("period", 0)

        teams = {}
        for c in comp.get("competitors", []):
            team     = c.get("team", {})
            raw_name = team.get("displayName", team.get("shortDisplayName", ""))
            norm     = normalize_name(raw_name)
            try:
                seed = int(c.get("curatedRank", {}).get("current") or c.get("seed", ""))
            except Exception:
                seed = None
            try:
                score = int(c.get("score", ""))
            except Exception:
                score = None
            teams[norm] = {"display_name": raw_name, "seed": seed, "score": score}

        try:
            espn_sort_ts = datetime.fromisoformat(start_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            espn_sort_ts = None

        game_info = {
            "tipoff":        tipoff,
            "status_state":  status_state,
            "status_detail": status_detail,
            "display_clock": display_clock,
            "period":        period,
            "teams":         teams,
            "sort_ts":       espn_sort_ts,
        }
        for norm_name in teams:
            by_team[norm_name] = game_info
        raw_games.append(game_info)

    return by_team, raw_games

def espn_lookup(espn_by_team, kalshi_name: str):
    """Returns (game_info, team_dict) or (None, None)."""
    norm = normalize_name(kalshi_name)
    if norm in espn_by_team:
        game = espn_by_team[norm]
        return game, game["teams"][norm]
    for key, game in espn_by_team.items():
        if norm in key or key in norm:
            return game, game["teams"][key]
    return None, None

def format_score_line(game_info, fav_name, dog_name):
    """Short score/status string for the mkt-strip."""
    if game_info is None:
        return None
    state    = game_info["status_state"]
    teams    = game_info["teams"]
    fav_norm = normalize_name(fav_name)
    dog_norm = normalize_name(dog_name)
    fav_t    = teams.get(fav_norm) or next((t for k, t in teams.items() if fav_norm in k or k in fav_norm), None)
    dog_t    = teams.get(dog_norm) or next((t for k, t in teams.items() if dog_norm in k or k in dog_norm), None)

    if state == "pre":
        return "Tips " + game_info["tipoff"]
    if state == "post":
        if fav_t and dog_t and fav_t["score"] is not None:
            return "FINAL " + str(fav_t["score"]) + "-" + str(dog_t["score"])
        return "FINAL"
    # in-progress
    half  = "1H" if game_info["period"] == 1 else "2H" if game_info["period"] == 2 else "OT"
    clock = game_info["display_clock"]
    if fav_t and dog_t and fav_t["score"] is not None:
        return str(fav_t["score"]) + "-" + str(dog_t["score"]) + " " + half + " " + clock
    return half + " " + clock

# ── Helpers ────────────────────────────────────────────────────────────────

def get_api_key():
    try:
        return st.secrets["KALSHI_API_KEY"]
    except Exception:
        return None

def kalshi_headers(api_key):
    return {"accept": "application/json", "KALSHI-ACCESS-KEY": api_key}

def prob_to_american(prob):
    if prob <= 0 or prob >= 100:
        return "N/A"
    if prob >= 50:
        return "-" + str(int(round(prob / (100 - prob) * 100)))
    else:
        return "+" + str(int(round((100 - prob) / prob * 100)))

def estimate_retail_implied(kalshi_mid):
    vig_rate = 0.045
    if kalshi_mid >= 50:
        return min(kalshi_mid + (kalshi_mid * vig_rate), 97)
    else:
        return max(kalshi_mid - (kalshi_mid * vig_rate * 0.7), 2)

def value_target(kalshi_mid, min_edge_pct=2.0):
    if kalshi_mid <= 0 or kalshi_mid >= 100:
        return "N/A"
    target_implied = kalshi_mid - min_edge_pct
    if target_implied <= 0:
        target_implied = 1
    if target_implied >= 100:
        target_implied = 99
    return prob_to_american(target_implied)

# ── Time filter ────────────────────────────────────────────────────────────

def _filter_and_sort(markets):
    """Only return open/active markets. Never show settled or closed games."""
    return [m for m in markets if (m.get("status") or "").lower() in ("open", "active", "")]

# ── API fetchers ───────────────────────────────────────────────────────────

@st.cache_data(ttl=15)
def fetch_ncaa_markets(api_key):
    NCAA_KEYWORDS = [
        "ncaa", "march madness", "basketball", "ncaab", "ncaamb",
        "college basketball", "tournament", "kxncaamb",
    ]
    all_markets = []
    seen = set()

    for series in ["KXNCAAMBGAME", "KXNCAAMB", "KXNCAAB", "KXMARCHMADNESS", "KXCBB"]:
        try:
            r = requests.get(
                BASE_URL + "/markets",
                params={"series_ticker": series, "limit": 200},
                headers=kalshi_headers(api_key), timeout=8,
            )
            if r.status_code == 200:
                for m in r.json().get("markets", []):
                    if m["ticker"] not in seen:
                        seen.add(m["ticker"])
                        all_markets.append(m)
        except Exception:
            pass

    try:
        r = requests.get(
            BASE_URL + "/events",
            params={"series_ticker": "KXNCAAMBGAME", "limit": 200, "with_nested_markets": "true"},
            headers=kalshi_headers(api_key), timeout=10,
        )
        if r.status_code == 200:
            for event in r.json().get("events", []):
                for m in event.get("markets", []):
                    if m["ticker"] not in seen:
                        seen.add(m["ticker"])
                        all_markets.append(m)
    except Exception:
        pass

    if all_markets:
        return _filter_and_sort(all_markets)

    cursor = None
    for _ in range(10):
        params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(BASE_URL + "/events", params=params,
                             headers=kalshi_headers(api_key), timeout=10)
            if r.status_code != 200:
                break
            data   = r.json()
            events = data.get("events", [])
            for event in events:
                title = " ".join([event.get("title", ""), event.get("sub_title", ""),
                                  event.get("series_ticker", "")]).lower()
                if any(kw in title for kw in NCAA_KEYWORDS):
                    for m in event.get("markets", []):
                        if m["ticker"] not in seen:
                            seen.add(m["ticker"])
                            all_markets.append(m)
            cursor = data.get("cursor")
            if not cursor or not events:
                break
        except Exception:
            break

    if not all_markets:
        try:
            r = requests.get(BASE_URL + "/markets",
                             params={"status": "open", "limit": 1000},
                             headers=kalshi_headers(api_key), timeout=10)
            if r.status_code == 200:
                for m in r.json().get("markets", []):
                    title = (m.get("title", "") + " " + m.get("ticker", "")).lower()
                    if any(kw in title for kw in NCAA_KEYWORDS):
                        if m["ticker"] not in seen:
                            seen.add(m["ticker"])
                            all_markets.append(m)
        except Exception:
            pass

    return _filter_and_sort(all_markets)

@st.cache_data(ttl=10)
def fetch_orderbook(api_key, ticker):
    try:
        r = requests.get(BASE_URL + "/markets/" + ticker + "/orderbook",
                         headers=kalshi_headers(api_key), timeout=8)
        if r.status_code == 200:
            return r.json().get("orderbook_fp", {})
    except Exception:
        pass
    return {}

@st.cache_data(ttl=10)
def fetch_market_detail(api_key, ticker):
    try:
        r = requests.get(BASE_URL + "/markets/" + ticker,
                         headers=kalshi_headers(api_key), timeout=8)
        if r.status_code == 200:
            return r.json().get("market", {})
    except Exception:
        pass
    return {}

# ── Spread metrics ─────────────────────────────────────────────────────────

def compute_spread_metrics(market):
    try:
        yes_bid = float(market.get("yes_bid_dollars", 0) or 0) * 100
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0) * 100
        no_bid  = float(market.get("no_bid_dollars",  0) or 0) * 100
        no_ask  = float(market.get("no_ask_dollars",  0) or 0) * 100

        if yes_ask == 0 and no_bid > 0:
            yes_ask = 100 - no_bid
        if no_ask == 0 and yes_bid > 0:
            no_ask = 100 - yes_bid

        mid        = (yes_bid + yes_ask) / 2 if yes_ask > 0 else yes_bid
        yes_spread = yes_ask - yes_bid if yes_ask > 0 else None
        no_spread  = no_ask  - no_bid  if no_ask  > 0 else None

        if yes_spread is not None and no_spread is not None:
            two_sided_vig = yes_spread + no_spread
        elif yes_spread is not None:
            two_sided_vig = yes_spread * 2
        else:
            two_sided_vig = None

        return {
            "yes_bid":    yes_bid,
            "yes_ask":    yes_ask,
            "no_bid":     no_bid,
            "no_ask":     no_ask,
            "mid":        mid,
            "spread":     yes_spread,
            "yes_spread": yes_spread,
            "no_spread":  no_spread,
            "vig":        two_sided_vig,
        }
    except Exception:
        return {}

def market_quality(spread, volume):
    if spread is None or volume < 100:
        return "DEAD", "—", 0
    if spread <= 2 and volume > 5000:
        return "SHARP", "S", 3
    if spread <= 3 and volume > 2000:
        return "SOLID", "S", 2
    if spread <= 4 and volume > 500:
        return "LIQUID", "L", 1
    if spread <= 6 and volume > 200:
        return "THIN", "T", 0
    if spread > 6:
        return "WIDE", "W", -1
    return "OK", "O", 0

# ── Live lag detection ─────────────────────────────────────────────────────

MAX_HISTORY = 30

def record_price(ticker: str, mid: float):
    if "price_history" not in st.session_state:
        st.session_state["price_history"] = {}
    hist = st.session_state["price_history"].setdefault(ticker, [])
    hist.append((time.time(), mid))
    if len(hist) > MAX_HISTORY:
        st.session_state["price_history"][ticker] = hist[-MAX_HISTORY:]

def compute_live_lag(ticker: str, current_mid: float,
                     lag_threshold_cents: float = 3.0,
                     lookback_seconds: float = 90.0):
    hist = st.session_state.get("price_history", {}).get(ticker, [])
    if len(hist) < 2:
        return None

    now       = time.time()
    cutoff    = now - lookback_seconds
    reference = None
    for ts, mid in hist:
        if ts >= cutoff:
            reference = (ts, mid)
            break
    if reference is None:
        reference = hist[0]

    elapsed = now - reference[0]
    if elapsed < 5:
        return None

    moved    = current_mid - reference[1]
    velocity = (moved / elapsed) * 60

    return {
        "moved_cents": round(moved, 1),
        "velocity":    round(velocity, 1),
        "alert":       abs(moved) >= lag_threshold_cents,
        "direction":   "FAV" if moved > 0 else "DOG",
        "old_mid":     round(reference[1], 1),
    }

# ── Core analysis engine ──────────────────────────────────────────────────

def analyze_game(fav_mid, dog_mid, spread, volume, fav_name, dog_name):
    result = {
        "verdict": "NO DATA", "color": "#555",
        "detail": "", "action_line": "No action.",
        "fav_target": "N/A", "dog_target": "N/A",
        "edge_fav_cents": 0, "edge_dog_cents": 0,
    }

    q_label, q_icon, q_score = market_quality(spread, volume)

    if q_score < 0 or (spread is None and volume < 100):
        result["detail"]  = "Market too thin (spread " + str(spread or "?") + " cents, vol " + str(int(volume)) + "). Price is noise."
        result["verdict"] = "SKIP"
        result["color"]   = "#666"
        return result

    fav_fair           = prob_to_american(fav_mid)
    dog_fair           = prob_to_american(dog_mid)
    retail_fav_implied = estimate_retail_implied(fav_mid)
    retail_dog_implied = estimate_retail_implied(dog_mid)
    retail_fav_odds    = prob_to_american(retail_fav_implied)
    retail_dog_odds    = prob_to_american(retail_dog_implied)
    fav_target         = value_target(fav_mid)
    dog_target         = value_target(dog_mid)

    result["fav_target"]     = fav_target
    result["dog_target"]     = dog_target
    result["edge_fav_cents"] = retail_fav_implied - fav_mid
    result["edge_dog_cents"] = dog_mid - retail_dog_implied

    if q_label in ("DEAD", "WIDE"):
        result["verdict"]     = "SKIP"
        result["color"]       = "#666"
        result["detail"]      = "Spread is " + str(int(spread)) + " cents wide. Price could be off by 5-10 cents either way."
        result["action_line"] = "Don't use this as a signal."
        return result

    thin_warning = " (thin — half size)" if q_label == "THIN" else ""

    if fav_mid >= 85:
        result["verdict"] = "HEAVY CHALK"
        result["color"]   = "#b8860b"
        result["detail"]  = (fav_name + " at " + str(int(fav_mid)) + "% (" + fav_fair + "). "
                             + "Retail probably posts " + retail_fav_odds + " or worse. Risk/reward is bad." + thin_warning)
        result["action_line"] = ("Skip the fav. Only play: " + dog_name
                                 + " at " + dog_target + " or better. Otherwise pass.")

    elif 20 <= dog_mid <= 45 and q_score >= 2:
        result["verdict"] = "DOG VALUE"
        result["color"]   = "#2ecc71"
        result["detail"]  = ("Sharp market: " + dog_name + " wins " + str(int(dog_mid)) + "% (" + dog_fair + "). "
                             + "Retail typically posts " + retail_dog_odds + ", underpriced by ~"
                             + str(round(result["edge_dog_cents"], 1)) + " cents." + thin_warning)
        result["action_line"] = ("AT THE WINDOW: " + dog_name + " at " + dog_target + " or better. "
                                 + "Board shows " + retail_dog_odds + " or higher = bet it.")

    elif 55 <= fav_mid <= 72 and q_score >= 1:
        result["verdict"] = "LIVE BET WATCH"
        result["color"]   = "#3498db"
        result["detail"]  = ("Close game " + str(int(fav_mid)) + "/" + str(int(dog_mid)) + ". "
                             + "Kalshi reprices in seconds, books take 30-90s after big runs." + thin_warning)
        result["action_line"] = ("Both apps open. Fair: " + fav_name + " " + fav_fair
                                 + " / " + dog_name + " " + dog_fair + ". "
                                 + "Bet whichever side the book lags on.")

    elif 72 < fav_mid < 85:
        result["verdict"] = "PRICE CHECK"
        result["color"]   = "#f39c12"
        result["detail"]  = (fav_name + " at " + str(int(fav_mid)) + "% (" + fav_fair + "). "
                             + "Retail likely posts " + retail_fav_odds + ". Only bet if the book is generous." + thin_warning)
        result["action_line"] = (fav_name + " at " + fav_target + " or better — or — "
                                 + dog_name + " at " + dog_target + " or better. If neither, pass.")

    elif 45 <= fav_mid <= 55:
        result["verdict"] = "TOSS-UP"
        result["color"]   = "#9b59b6"
        result["detail"]  = ("Near even at " + str(int(fav_mid)) + "/" + str(int(dog_mid)) + ". "
                             + "Vig kills you on coin flips unless one side is mispriced." + thin_warning)
        result["action_line"] = ("Fair: " + fav_name + " " + fav_fair + " / " + dog_name + " " + dog_fair + ". "
                                 + "Bet whichever side the board gives you the biggest discount. If neither beats fair, skip.")

    elif 20 <= dog_mid <= 45 and q_score < 2:
        result["verdict"] = "DOG - LOW CONF"
        result["color"]   = "#d4a017"
        result["detail"]  = (dog_name + " at " + str(int(dog_mid)) + "% (" + dog_fair + ") but market is "
                             + q_label.lower() + ". Mid could be off by 5+ cents.")
        result["action_line"] = ("Half size only: " + dog_name + " at " + dog_target + " or better.")

    else:
        result["verdict"]     = "PASS"
        result["color"]       = "#666"
        result["detail"]      = "No clear edge. " + str(int(fav_mid)) + "/" + str(int(dog_mid)) + "."
        result["action_line"] = "No action."

    return result

# ── UI ─────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Kalshi MM", layout="wide")
st.markdown("""
<style>
.game-card {
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    padding: 14px 16px;
    margin-bottom: 10px;
    background: #161616;
}
.act-box {
    border-left: 3px solid;
    padding: 10px 13px;
    border-radius: 0 6px 6px 0;
    font-size: 1.0em;
    font-weight: 600;
    color: #eee;
    margin-bottom: 10px;
    line-height: 1.5;
    background: #111;
}
.card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 8px;
    gap: 8px;
}
.card-title { font-size: 0.95em; font-weight: 600; color: #ccc; flex: 1; line-height: 1.3; }
.verdict-badge {
    font-size: 0.72em;
    font-weight: 800;
    padding: 3px 8px;
    border-radius: 20px;
    white-space: nowrap;
    letter-spacing: 0.4px;
    border: 1px solid;
    flex-shrink: 0;
}
.price-row { display: flex; gap: 8px; margin-bottom: 8px; }
.team-cell { flex: 1; background: #1e1e1e; border-radius: 8px; padding: 8px 10px; min-width: 0; }
.team-label { font-size: 0.65em; color: #666; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }
.team-name { font-size: 0.9em; font-weight: 700; color: #ddd; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px; }
.team-price { font-size: 1.15em; font-weight: 800; margin-bottom: 1px; }
.team-target { font-size: 0.78em; font-weight: 600; }
.mkt-strip {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    font-size: 0.72em;
    color: #666;
    margin-bottom: 7px;
    padding: 5px 2px;
    border-top: 1px solid #222;
}
.mkt-item { white-space: nowrap; }
.mkt-item span { color: #999; }
.det-box { font-size: 0.78em; color: #777; line-height: 1.5; }
.lag-alert {
    background: #1c0e00;
    border: 1px solid #ff6b00;
    border-radius: 6px;
    padding: 8px 12px;
    margin-bottom: 8px;
    font-size: 0.85em;
    color: #ff9944;
    font-weight: 600;
}
.lag-neutral { font-size: 0.72em; color: #555; margin-bottom: 6px; }
.score-live { color: #7ecfff; font-weight: 600; }
.score-pre  { color: #555; }
</style>
""", unsafe_allow_html=True)

st.title("Kalshi MM")
st.caption("Last refresh: " + datetime.now().strftime("%I:%M:%S %p") + " · Auto-updates")

# ── Sidebar ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Settings")
    api_key = get_api_key()
    if not api_key:
        api_key = st.text_input(
            "Kalshi API Key", type="password",
            help="Paste here or set KALSHI_API_KEY in Streamlit secrets",
        )
    else:
        st.success("API key loaded")

    st.divider()
    st.subheader("Refresh")
    refresh_rate = st.slider("Refresh (sec)", 5, 120, 20, help="5-10 for live games, 30+ for pregame")

    st.divider()
    st.subheader("Lag Alerts")
    lag_threshold = st.slider(
        "Alert threshold (cents)", min_value=1, max_value=10, value=3,
        help="Fire when Kalshi mid moves this many cents inside the lookback window",
    )
    lag_lookback = st.slider(
        "Lookback window (sec)", min_value=30, max_value=300, value=90,
        help="How far back to compare. 90s matches typical sportsbook reprice lag.",
    )

    st.divider()
    st.markdown("""
**Legend**
- Green = actionable now
- Yellow = only if price is right
- Gray = skip
- Target = minimum odds to bet
- LAG ALERT = Kalshi moved; book may not have caught up
""")

if not api_key:
    st.warning("Enter your Kalshi API key in the sidebar to start.")
    st.stop()

# ── Fetch data ─────────────────────────────────────────────────────────────

with st.spinner("Pulling Kalshi NCAA markets…"):
    markets = fetch_ncaa_markets(api_key)

espn_by_team, espn_raw = fetch_espn_games()

if espn_by_team:
    n_espn_live = sum(1 for g in espn_raw if g["status_state"] == "in")
    st.caption("ESPN: " + str(len(espn_raw)) + " games today"
               + (" · 🔴 " + str(n_espn_live) + " live" if n_espn_live else ""))

# ── Diagnostics ────────────────────────────────────────────────────────────

if not markets:
    st.warning("No NCAA markets found. Diagnostics below.")

    with st.expander("Diagnostic - all open Kalshi events", expanded=True):
        st.caption("Look for anything with KXNCAAMB in the ticker.")
        try:
            r = requests.get(
                BASE_URL + "/events",
                params={"status": "open", "limit": 50, "with_nested_markets": "false"},
                headers=kalshi_headers(api_key), timeout=10,
            )
            if r.status_code == 200:
                events = r.json().get("events", [])
                if events:
                    st.dataframe(
                        pd.DataFrame([{"ticker": e.get("event_ticker"), "title": e.get("title")} for e in events]),
                        use_container_width=True, hide_index=True,
                    )
                else:
                    st.error("API returned 0 events. API key may lack sports access, or Nevada geo-block.")
            else:
                st.error("API error " + str(r.status_code))
        except Exception as ex:
            st.error("Request failed: " + str(ex))

    with st.expander("Direct KXNCAAMBGAME fetch (debug)", expanded=True):
        try:
            r = requests.get(BASE_URL + "/markets",
                             params={"series_ticker": "KXNCAAMBGAME", "limit": 200},
                             headers=kalshi_headers(api_key), timeout=8)
            st.write("Status: " + str(r.status_code))
            data        = r.json()
            raw_markets = data.get("markets", [])
            st.write("Raw markets returned: " + str(len(raw_markets)))
            if raw_markets:
                sample = {}
                for k in ["ticker", "title", "status", "close_time", "yes_bid_dollars"]:
                    if k in raw_markets[0]:
                        sample[k] = raw_markets[0][k]
                st.write("First market:", sample)
                filtered = _filter_and_sort(raw_markets)
                st.write("After time filter: " + str(len(filtered)))
                if not filtered and raw_markets:
                    st.warning("All markets filtered out by time window. Showing all anyway.")
                    markets = raw_markets
        except Exception as ex:
            st.error("Direct fetch failed: " + str(ex))

    st.subheader("Manual ticker search")
    c1, c2 = st.columns(2)
    with c1:
        manual_event = st.text_input("Event ticker")
        if manual_event:
            try:
                r = requests.get(BASE_URL + "/events/" + manual_event.upper(),
                                 params={"with_nested_markets": "true"},
                                 headers=kalshi_headers(api_key), timeout=8)
                if r.status_code == 200:
                    markets = r.json().get("event", {}).get("markets", [])
                    st.success("Found " + str(len(markets)) + " markets")
                else:
                    st.error("Not found (" + str(r.status_code) + ")")
            except Exception as ex:
                st.error(str(ex))
    with c2:
        manual_ticker = st.text_input("Market ticker")
        if manual_ticker:
            d = fetch_market_detail(api_key, manual_ticker.upper())
            if d:
                markets = [d]
                st.success("Found: " + str(d.get("title", "")))
            else:
                st.error("Not found")

if markets:
    # Group by game
    games = defaultdict(list)
    for m in markets:
        title         = (m.get("title", "") + m.get("subtitle", "")).lower()
        skip_keywords = ["spread", "total", "over", "under", "point", "half"]
        if any(kw in title for kw in skip_keywords):
            continue
        event = m.get("event_ticker", m.get("ticker", ""))
        games[event].append(m)

    # Analyze each game
    analyses = []
    for event_ticker, gm in games.items():
        gm.sort(key=lambda m: float(m.get("yes_bid_dollars", 0) or 0), reverse=True)
        if len(gm) < 2:
            continue

        fav, dog = gm[0], gm[1]
        fav_m    = compute_spread_metrics(fav)
        dog_m    = compute_spread_metrics(dog)

        fav_name = fav.get("yes_sub_title") or fav.get("subtitle") or fav.get("ticker", "").split("-")[-1]
        dog_name = dog.get("yes_sub_title") or dog.get("subtitle") or dog.get("ticker", "").split("-")[-1]
        if fav_name == dog_name:
            fav_name = fav.get("ticker", "").split("-")[-1]
            dog_name = dog.get("ticker", "").split("-")[-1]

        fav_vol   = float(fav.get("volume_fp", 0) or 0)
        dog_vol   = float(dog.get("volume_fp", 0) or 0)
        total_vol = fav_vol + dog_vol
        spread    = fav_m.get("spread")
        fav_mid   = fav_m.get("mid", 0)
        dog_mid   = dog_m.get("mid", 0)

        game_title = fav.get("title", event_ticker).replace(" Winner?", "").replace(" winner?", "").strip()

        close_ts  = None
        close_str = fav.get("close_time") or fav.get("expected_expiration_time") or ""
        if close_str:
            try:
                close_ts = datetime.fromisoformat(close_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass

        record_price(fav["ticker"], fav_mid)
        lag_info = compute_live_lag(
            fav["ticker"], fav_mid,
            lag_threshold_cents=float(lag_threshold),
            lookback_seconds=float(lag_lookback),
        )

        # ESPN enrichment
        fav_game, fav_team_data = espn_lookup(espn_by_team, fav_name)
        dog_game, dog_team_data = espn_lookup(espn_by_team, dog_name)
        espn_game   = fav_game or dog_game
        fav_seed    = fav_team_data["seed"] if fav_team_data else None
        dog_seed    = dog_team_data["seed"] if dog_team_data else None
        score_line  = format_score_line(espn_game, fav_name, dog_name)
        espn_state  = espn_game["status_state"] if espn_game else None
        espn_sort_ts = espn_game["sort_ts"] if espn_game else None

        a = analyze_game(fav_mid, dog_mid, spread, total_vol, fav_name, dog_name)
        analyses.append({
            "event_ticker": event_ticker,
            "title":        game_title,
            "fav_name":     fav_name,
            "dog_name":     dog_name,
            "fav_mid":      fav_mid,
            "dog_mid":      dog_mid,
            "fav_m":        fav_m,
            "dog_m":        dog_m,
            "spread":       spread,
            "vol":          total_vol,
            "a":            a,
            "quality":      market_quality(spread, total_vol),
            "lag_info":     lag_info,
            "close_ts":     close_ts,
            "fav_seed":     fav_seed,
            "dog_seed":     dog_seed,
            "score_line":   score_line,
            "espn_state":    espn_state,
            "espn_sort_ts":  espn_sort_ts,
        })

    def verdict_priority(g):
        lag = g.get("lag_info") or {}
        if lag.get("alert"):
            return 0
        v = g["a"]["verdict"]
        if "DOG VALUE" in v:   return 1
        if "LIVE BET"  in v:   return 2
        if "TOSS"      in v:   return 3
        if "PRICE"     in v:   return 4
        if "CHALK"     in v:   return 5
        if "LOW CONF"  in v:   return 6
        return 7

    for g in analyses:
        g["_priority"] = verdict_priority(g)

    n_action = sum(1 for g in analyses if g["_priority"] <= 4)
    n_lag    = sum(1 for g in analyses if (g.get("lag_info") or {}).get("alert"))

    ctrl_col, sort_col = st.columns([3, 1])
    with ctrl_col:
        FILTER_OPTS = {
            "All":        lambda g: True,
            "Lag":        lambda g: bool((g.get("lag_info") or {}).get("alert")),
            "Actionable": lambda g: g["_priority"] <= 4,
            "Dogs":       lambda g: "DOG" in g["a"]["verdict"],
            "Live":       lambda g: g.get("espn_state") == "in",
            "Next 3h":    lambda g: g["close_ts"] is not None and g["close_ts"] <= time.time() + 10800,
            "Skip":       lambda g: g["_priority"] >= 7,
        }
        filter_choice = st.radio(
            "Show", list(FILTER_OPTS.keys()), index=0,
            horizontal=True, label_visibility="collapsed",
        )
    with sort_col:
        sort_choice = st.selectbox(
            "Sort", ["Smart", "Start time", "Dog edge ↓", "Volume ↓", "Quality ↓"],
            label_visibility="collapsed",
        )

    shown = [g for g in analyses if FILTER_OPTS[filter_choice](g)]

    if sort_choice == "Smart":
        shown.sort(key=lambda g: g["_priority"])
    elif sort_choice == "Start time":
        shown.sort(key=lambda g: g["espn_sort_ts"] or g["close_ts"] or float("inf"))
    elif sort_choice == "Dog edge ↓":
        shown.sort(key=lambda g: -g["a"].get("edge_dog_cents", 0))
    elif sort_choice == "Volume ↓":
        shown.sort(key=lambda g: -g["vol"])
    elif sort_choice == "Quality ↓":
        shown.sort(key=lambda g: -g["quality"][2])

    summary = str(len(shown)) + " of " + str(len(analyses)) + " games"
    if n_lag:
        summary += " · " + str(n_lag) + " lag alert" + ("s" if n_lag > 1 else "")
    if n_action:
        summary += " · " + str(n_action) + " actionable"
    st.caption(summary)

    # ── Render cards ──────────────────────────────────────────────────────
    for g in shown:
        a          = g["a"]
        fm         = g["fav_mid"]
        dm         = g["dog_mid"]
        ql, qi, qs = g["quality"]

        fav_fair   = prob_to_american(fm)
        dog_fair   = prob_to_american(dm)
        fav_target = a["fav_target"]
        dog_target = a["dog_target"]

        fav_seed_str = ("#" + str(g["fav_seed"]) + " ") if g["fav_seed"] else ""
        dog_seed_str = ("#" + str(g["dog_seed"]) + " ") if g["dog_seed"] else ""

        vig_val    = g["fav_m"].get("vig")
        vig_str    = (str(round(vig_val, 1)) + "c vig") if vig_val else "—"
        spread_str = (str(round(g["spread"], 1)) + "c sprd") if g["spread"] else "—"
        vol_str    = "{:,.0f} vol".format(g["vol"])

        # Time display — ESPN score/tip-off takes priority over Kalshi close_time
        score_line = g.get("score_line")
        close_ts   = g.get("close_ts")
        if score_line:
            css       = "score-live" if g["espn_state"] == "in" else "score-pre"
            live_dot  = " 🔴" if g["espn_state"] == "in" else ""
            time_html = '<span class="mkt-item ' + css + '">' + score_line + live_dot + '</span>'
        elif close_ts:
            mins_away = int((close_ts - time.time()) / 60)
            if mins_away < 0:
                t = "live/closed"
            elif mins_away < 60:
                t = str(mins_away) + "m"
            elif mins_away < 1440:
                t = datetime.fromtimestamp(close_ts).strftime("%-I:%M %p")
            else:
                t = datetime.fromtimestamp(close_ts).strftime("%b %-d %-I:%M %p")
            time_html = '<span class="mkt-item">' + t + '</span>'
        else:
            time_html = ""

        # Lag HTML
        lag      = g.get("lag_info") or {}
        lag_html = ""
        if lag:
            direction_name = g["fav_name"] if lag["direction"] == "FAV" else g["dog_name"]
            moved_str = ("+" if lag["moved_cents"] >= 0 else "") + str(lag["moved_cents"])
            vel_str   = ("+" if lag["velocity"]    >= 0 else "") + str(lag["velocity"])
            if lag["alert"]:
                lag_html = (
                    '<div class="lag-alert">'
                    + '<strong>LAG ALERT</strong> — '
                    + direction_name + " " + moved_str + "c in "
                    + str(lag_lookback) + "s (" + vel_str + " c/min)"
                    + " — check the board NOW"
                    + '</div>'
                )
            else:
                lag_html = (
                    '<div class="lag-neutral">'
                    + "Δ " + moved_str + "¢ · " + vel_str + " ¢/min"
                    + '</div>'
                )

        color    = a["color"]
        badge_bg = color + "22"

        card = (
            '<div class="game-card">'
            + '<div class="act-box" style="border-left-color:' + color + ';margin-bottom:10px;">'
            + a["action_line"]
            + '</div>'
            + '<div class="card-header">'
            + '<div class="card-title">' + g["title"] + '</div>'
            + '<div class="verdict-badge" style="color:' + color + ';border-color:' + color + ';background:' + badge_bg + ';">'
            + a["verdict"]
            + '</div>'
            + '</div>'
            + lag_html
            + '<div class="price-row">'
            + '<div class="team-cell">'
            + '<div class="team-label">Favorite</div>'
            + '<div class="team-name">' + fav_seed_str + g["fav_name"] + '</div>'
            + '<div class="team-price" style="color:#4fc3f7;">' + str(int(fm)) + 'c · ' + fav_fair + '</div>'
            + '<div class="team-target" style="color:#4fc3f7;">Target: ' + fav_target + '</div>'
            + '</div>'
            + '<div class="team-cell">'
            + '<div class="team-label">Dog</div>'
            + '<div class="team-name">' + dog_seed_str + g["dog_name"] + '</div>'
            + '<div class="team-price" style="color:#ff8a65;">' + str(int(dm)) + 'c · ' + dog_fair + '</div>'
            + '<div class="team-target" style="color:#ff8a65;">Target: ' + dog_target + '</div>'
            + '</div>'
            + '</div>'
            + '<div class="mkt-strip">'
            + time_html
            + '<span class="mkt-item" style="color:#444;">|</span>'
            + '<span class="mkt-item"><span>' + ql + '</span></span>'
            + '<span class="mkt-item">' + spread_str + '</span>'
            + '<span class="mkt-item">' + vig_str + '</span>'
            + '<span class="mkt-item">' + vol_str + '</span>'
            + '</div>'
            + '<div class="det-box">' + a["detail"] + '</div>'
            + '</div>'
        )

        st.markdown(card, unsafe_allow_html=True)

    # Order book viewer
    with st.expander("Order Book Depth"):
        ticker_opts = {}
        for g in analyses:
            for m in games[g["event_ticker"]]:
                label = g["title"] + " - " + m.get("yes_sub_title", m.get("ticker", ""))
                ticker_opts[label] = m["ticker"]
        if ticker_opts:
            sel = st.selectbox("Select market", list(ticker_opts.keys()))
            ob  = fetch_orderbook(api_key, ticker_opts[sel])
            if ob:
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("**YES bids**")
                    yb = ob.get("yes", {}).get("bids", [])
                    if yb:
                        st.dataframe(pd.DataFrame(yb), use_container_width=True, hide_index=True)
                    else:
                        st.caption("Empty")
                with c2:
                    st.markdown("**NO bids**")
                    nb = ob.get("no", {}).get("bids", [])
                    if nb:
                        st.dataframe(pd.DataFrame(nb), use_container_width=True, hide_index=True)
                    else:
                        st.caption("Empty")

    # Quick reference table
    with st.expander("Quick Reference - All Games"):
        rows = []
        for g in analyses:
            a   = g["a"]
            lag = g.get("lag_info") or {}
            rows.append({
                "Game":          g["title"],
                "Verdict":       a["verdict"],
                "Tip-off":       g.get("score_line") or "—",
                "Fav":           (("#" + str(g["fav_seed"]) + " ") if g["fav_seed"] else "") + g["fav_name"],
                "Fav c":         str(int(g["fav_mid"])),
                "Fav Fair":      prob_to_american(g["fav_mid"]),
                "Fav Target":    a["fav_target"],
                "Dog":           (("#" + str(g["dog_seed"]) + " ") if g["dog_seed"] else "") + g["dog_name"],
                "Dog c":         str(int(g["dog_mid"])),
                "Dog Fair":      prob_to_american(g["dog_mid"]),
                "Dog Target":    a["dog_target"],
                "Quality":       g["quality"][0],
                "Vig (2-sided)": (str(round(g["fav_m"].get("vig", 0), 1)) + "c") if g["fav_m"].get("vig") else "n/a",
                "Lag":           (str(lag.get("moved_cents", "")) + "c") if lag else "—",
                "Alert":         "YES" if lag.get("alert") else "—",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ── Auto-refresh ───────────────────────────────────────────────────────────

st.divider()
c1, c2 = st.columns([3, 1])
with c2:
    if st.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()
with c1:
    st.caption("Auto-refresh in " + str(refresh_rate) + "s")

time.sleep(refresh_rate)
st.cache_data.clear()
st.rerun()
