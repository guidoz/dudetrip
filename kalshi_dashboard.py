import streamlit as st
import requests
import pandas as pd
import time
import math
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

# ── Book Conviction Scoring ────────────────────────────────────────────────

def _extract_levels(orderbook, side):
    """
    Extract list of (price_cents, quantity) from the orderbook.
    Handles multiple Kalshi orderbook_fp formats:
      - {"yes": {"bids": [[p,q], ...]}}       (nested with bids key)
      - {"yes": [[p,q], ...]}                  (flat list)
      - {"yes": [{"price":p, "quantity":q}]}   (list of dicts)
    """
    levels = []
    try:
        side_data = orderbook.get(side, {})
        if side_data is None:
            return levels

        # If side_data is a list directly: [entries...]
        if isinstance(side_data, list):
            raw = side_data
        elif isinstance(side_data, dict):
            # Try common sub-keys: bids, asks, or just take all list values
            raw = side_data.get("bids", [])
            if not raw:
                raw = side_data.get("asks", [])
            if not raw:
                # Maybe the dict values are themselves lists of levels
                for v in side_data.values():
                    if isinstance(v, list) and v:
                        raw = v
                        break
        else:
            return levels

        for entry in raw:
            price = 0
            qty   = 0
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price = float(entry[0])
                qty   = float(entry[1])
            elif isinstance(entry, dict):
                # Try multiple possible key names
                price = float(entry.get("price", 0) or entry.get("price_cents", 0) or 0)
                qty   = float(entry.get("quantity", 0) or entry.get("count", 0) or entry.get("size", 0) or 0)
            else:
                continue
            if qty > 0 and price > 0:
                levels.append((price, qty))
    except Exception:
        pass
    return levels


def compute_book_conviction(ob_fav, ob_dog, fav_mid, dog_mid):
    """
    Compute Book Conviction Scoring from order books.

    Returns dict with:
      bir        - Book Imbalance Ratio (0-1, <0.5 = dog-side pressure)
      entropy    - Shannon entropy of order sizes, normalized 0-1
      depth_grad - Depth gradient (0-1, high = fragile/cliff)
      confidence - Combined confidence weight
      adjusted_prob - Bayesian-adjusted dog probability
      narrative  - One-liner explanation
      signal     - 'DOG', 'FAV', or 'NEUTRAL'
      strength   - 'STRONG', 'MODERATE', 'WEAK', or 'NOISE'
    """
    result = {
        "bir": 0.5, "entropy": 1.0, "depth_grad": 0.5,
        "confidence": 0.0, "adjusted_prob": dog_mid,
        "narrative": "No order book data.", "signal": "NEUTRAL",
        "strength": "NOISE", "has_data": False, "debug": "",
    }

    # Debug: capture the structure of what we got
    def _describe_ob(ob):
        if not ob:
            return "empty"
        parts = []
        for k, v in ob.items():
            if isinstance(v, list):
                parts.append(k + ":[" + str(len(v)) + " items]")
                if v:
                    parts.append("  sample:" + str(v[0])[:80])
            elif isinstance(v, dict):
                sub_keys = list(v.keys())
                parts.append(k + ":{" + ",".join(sub_keys) + "}")
                for sk, sv in v.items():
                    if isinstance(sv, list) and sv:
                        parts.append("  " + sk + ":[" + str(len(sv)) + "] sample:" + str(sv[0])[:80])
            else:
                parts.append(k + ":" + str(v)[:40])
        return " | ".join(parts)

    result["debug"] = "fav_ob=" + _describe_ob(ob_fav) + " /// dog_ob=" + _describe_ob(ob_dog)

    # Collect all order levels from both books
    # YES bids on fav = money backing the favorite
    # YES bids on dog = money backing the dog
    # NO bids on fav  = also money backing the dog (betting against fav)
    # NO bids on dog  = also money backing the fav (betting against dog)
    fav_yes_bids = _extract_levels(ob_fav, "yes")
    fav_no_bids  = _extract_levels(ob_fav, "no")
    dog_yes_bids = _extract_levels(ob_dog, "yes")
    dog_no_bids  = _extract_levels(ob_dog, "no")

    # All levels combined for entropy calculation
    all_levels = fav_yes_bids + fav_no_bids + dog_yes_bids + dog_no_bids
    if not all_levels:
        return result

    result["has_data"] = True

    # ── Signal 1: Book Imbalance Ratio ──────────────────────────────────
    # Dollar-volume on the YES/fav side vs YES/dog side
    # fav_side_dollars = fav YES bids + dog NO bids (both want fav to win)
    # dog_side_dollars = dog YES bids + fav NO bids (both want dog to win)
    fav_side_dollars = sum(p * q for p, q in fav_yes_bids) + sum(p * q for p, q in dog_no_bids)
    dog_side_dollars = sum(p * q for p, q in dog_yes_bids) + sum(p * q for p, q in fav_no_bids)
    total_dollars    = fav_side_dollars + dog_side_dollars

    if total_dollars > 0:
        bir = fav_side_dollars / total_dollars  # >0.5 = fav leaning, <0.5 = dog leaning
    else:
        bir = 0.5
    result["bir"] = round(bir, 3)

    # ── Signal 2: Book Entropy ──────────────────────────────────────────
    # Shannon entropy of order quantities across all price levels
    quantities = [q for _, q in all_levels]
    total_qty  = sum(quantities)

    if total_qty > 0 and len(quantities) > 1:
        probs   = [q / total_qty for q in quantities]
        raw_ent = -sum(p * math.log2(p) for p in probs if p > 0)
        max_ent = math.log2(len(quantities))
        entropy = raw_ent / max_ent if max_ent > 0 else 1.0
    else:
        entropy = 1.0
    result["entropy"] = round(entropy, 3)

    # ── Signal 3: Depth Gradient ────────────────────────────────────────
    # How much volume is concentrated at the best (top) level vs total
    # Compute separately for each side, take the worse (more fragile) one
    def _depth_ratio(levels):
        if not levels:
            return 0.5
        # Sort by price descending (best bid = highest price)
        sorted_lvls = sorted(levels, key=lambda x: x[0], reverse=True)
        best_qty    = sorted_lvls[0][1]
        total       = sum(q for _, q in sorted_lvls)
        if total <= 0:
            return 0.5
        return best_qty / total

    depth_fav = _depth_ratio(fav_yes_bids + dog_no_bids)
    depth_dog = _depth_ratio(dog_yes_bids + fav_no_bids)
    # Overall fragility: max of both sides (worst case)
    depth_grad = max(depth_fav, depth_dog)
    result["depth_grad"] = round(depth_grad, 3)

    # ── Bayesian Engine ─────────────────────────────────────────────────
    # prior = Kalshi mid-price for the dog (already our best estimate)
    prior = dog_mid / 100.0  # Convert to 0-1

    # Book-implied dog probability from BIR
    book_implied_dog = 1.0 - bir  # If bir=0.4, 60% of money is dog-side

    # Confidence = (1 - entropy) × (1 - depth_grad)
    # Low entropy + low fragility = high confidence
    depth_stability = 1.0 - depth_grad
    confidence = (1.0 - entropy) * depth_stability
    confidence = max(0.0, min(1.0, confidence))
    result["confidence"] = round(confidence, 3)

    # Weighted blend: adjusted = prior × (1 - confidence) + book_implied × confidence
    adjusted = prior * (1.0 - confidence) + book_implied_dog * confidence
    adjusted = max(0.01, min(0.99, adjusted))
    adjusted_pct = adjusted * 100.0
    result["adjusted_prob"] = round(adjusted_pct, 1)

    # ── Signal classification ───────────────────────────────────────────
    imbalance_magnitude = abs(bir - 0.5)

    if confidence >= 0.4 and imbalance_magnitude >= 0.08:
        result["strength"] = "STRONG"
    elif confidence >= 0.2 and imbalance_magnitude >= 0.05:
        result["strength"] = "MODERATE"
    elif imbalance_magnitude >= 0.03:
        result["strength"] = "WEAK"
    else:
        result["strength"] = "NOISE"

    if bir < 0.47:
        result["signal"] = "DOG"
    elif bir > 0.53:
        result["signal"] = "FAV"
    else:
        result["signal"] = "NEUTRAL"

    # ── Narrative ───────────────────────────────────────────────────────
    dog_pct = round((1.0 - bir) * 100)
    fav_pct = round(bir * 100)

    if result["strength"] == "NOISE":
        result["narrative"] = (
            "Book is " + str(fav_pct) + "/" + str(dog_pct) + " but entropy is "
            + str(round(entropy, 2)) + " — nobody agrees. "
            + "Model defaults to mid: " + str(round(adjusted_pct, 1)) + "%. Pass unless you want chaos."
        )
    elif result["signal"] == "DOG" and result["strength"] == "STRONG":
        if depth_grad > 0.7:
            result["narrative"] = (
                "Pressure " + str(dog_pct) + "% dog-side, high conviction (ent " + str(round(entropy, 2))
                + ") — but it's all on one cliff (depth " + str(round(depth_grad, 2))
                + "). One whale holding the price. High risk, high story."
            )
        else:
            result["narrative"] = (
                "Pressure " + str(dog_pct) + "% dog-side with high conviction (ent "
                + str(round(entropy, 2)) + ", depth " + str(round(depth_grad, 2))
                + "). Model says " + str(round(adjusted_pct, 1)) + "%. Book is leaning."
            )
    elif result["signal"] == "DOG" and result["strength"] == "MODERATE":
        result["narrative"] = (
            "Pressure " + str(dog_pct) + "% dog-side, moderate conviction (ent "
            + str(round(entropy, 2)) + "). Model nudges to " + str(round(adjusted_pct, 1))
            + "%. Lean dog but size down."
        )
    elif result["signal"] == "FAV" and result["strength"] in ("STRONG", "MODERATE"):
        result["narrative"] = (
            "Pressure " + str(fav_pct) + "% fav-side"
            + (" with strong conviction" if result["strength"] == "STRONG" else "")
            + " (ent " + str(round(entropy, 2))
            + "). Book confirms the chalk. Dog prob drops to "
            + str(round(adjusted_pct, 1)) + "%."
        )
    elif result["signal"] == "NEUTRAL":
        result["narrative"] = (
            "Book is split " + str(fav_pct) + "/" + str(dog_pct)
            + " — market hasn't picked a side. Entropy " + str(round(entropy, 2))
            + ". Model stays near mid: " + str(round(adjusted_pct, 1)) + "%."
        )
    else:
        result["narrative"] = (
            "Weak lean " + result["signal"].lower() + "-side (" + str(dog_pct) + "% dog). "
            + "Low confidence. Model: " + str(round(adjusted_pct, 1)) + "%."
        )

    return result


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
.game-row {
    display: flex;
    gap: 0;
    align-items: stretch;
    margin-bottom: 10px;
}
.game-main {
    flex: 1;
    border: 1px solid #2a2a2a;
    border-radius: 12px 0 0 12px;
    padding: 14px 16px;
    background: #161616;
    min-width: 0;
}
.conv-panel {
    width: 280px;
    flex-shrink: 0;
    border: 1px solid #2a2a2a;
    border-left: none;
    border-radius: 0 12px 12px 0;
    padding: 12px 14px;
    background: #111;
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.conv-header {
    font-size: 0.68em;
    font-weight: 800;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    color: #555;
    margin-bottom: 2px;
    border-bottom: 1px solid #222;
    padding-bottom: 4px;
}
.conv-signal-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 3px 0;
}
.conv-label {
    font-size: 0.7em;
    color: #666;
    font-weight: 600;
}
.conv-value {
    font-size: 0.78em;
    font-weight: 700;
    font-family: 'SF Mono', 'Fira Code', monospace;
}
.conv-bar-track {
    height: 6px;
    background: #222;
    border-radius: 3px;
    margin: 2px 0 4px 0;
    position: relative;
    overflow: hidden;
}
.conv-bar-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.3s;
}
.conv-prob-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 4px 0;
    border-top: 1px solid #222;
    margin-top: 2px;
}
.conv-prob-label {
    font-size: 0.68em;
    color: #666;
}
.conv-prob-value {
    font-size: 1.1em;
    font-weight: 800;
}
.conv-narrative {
    font-size: 0.72em;
    color: #888;
    line-height: 1.45;
    margin-top: 2px;
    border-top: 1px solid #222;
    padding-top: 6px;
}
.conv-badge {
    display: inline-block;
    font-size: 0.65em;
    font-weight: 800;
    padding: 2px 7px;
    border-radius: 10px;
    letter-spacing: 0.4px;
}
.conv-nodata {
    font-size: 0.75em;
    color: #444;
    font-style: italic;
    padding: 20px 0;
    text-align: center;
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

**Book Conviction Panel**
- BIR = Book Imbalance Ratio (< 50% = dog pressure)
- ENT = Shannon entropy (low = market agrees)
- DEPTH = Fragility (high = price on a cliff)
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

        # ── Book Conviction Scoring ──────────────────────────────
        ob_fav = fetch_orderbook(api_key, fav["ticker"])
        ob_dog = fetch_orderbook(api_key, dog["ticker"])
        conviction = compute_book_conviction(ob_fav, ob_dog, fav_mid, dog_mid)

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
            "conviction":   conviction,
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
            "Sort", ["Smart", "Start time", "Dog edge ↓", "Volume ↓", "Quality ↓", "Conviction ↓"],
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
    elif sort_choice == "Conviction ↓":
        shown.sort(key=lambda g: -g["conviction"].get("confidence", 0))

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
        conv       = g["conviction"]

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

        # ── Build conviction panel HTML ──────────────────────────────
        if conv["has_data"]:
            bir_pct   = round(conv["bir"] * 100)
            dog_pct   = 100 - bir_pct
            ent_val   = conv["entropy"]
            depth_val = conv["depth_grad"]
            conf_val  = conv["confidence"]
            adj_prob  = conv["adjusted_prob"]
            signal    = conv["signal"]
            strength  = conv["strength"]

            # Colors for signal
            if signal == "DOG":
                sig_color = "#ff8a65"
                sig_bg    = "#ff8a6518"
            elif signal == "FAV":
                sig_color = "#4fc3f7"
                sig_bg    = "#4fc3f718"
            else:
                sig_color = "#888"
                sig_bg    = "#88888818"

            # Strength colors
            str_colors = {"STRONG": "#2ecc71", "MODERATE": "#f1c40f", "WEAK": "#e67e22", "NOISE": "#666"}
            str_color  = str_colors.get(strength, "#666")

            # BIR bar: show fav% on left (blue), dog% on right (orange)
            bir_bar = (
                '<div class="conv-bar-track">'
                + '<div class="conv-bar-fill" style="width:' + str(bir_pct) + '%;background:linear-gradient(90deg,#4fc3f7,' + ('#4fc3f7' if bir_pct > 50 else '#ff8a65') + ');"></div>'
                + '</div>'
            )

            # Entropy bar: low = green (good), high = red (bad)
            ent_pct = round(ent_val * 100)
            ent_color = "#2ecc71" if ent_val < 0.4 else "#f1c40f" if ent_val < 0.7 else "#e74c3c"
            ent_bar = (
                '<div class="conv-bar-track">'
                + '<div class="conv-bar-fill" style="width:' + str(ent_pct) + '%;background:' + ent_color + ';"></div>'
                + '</div>'
            )

            # Depth bar: high = red (fragile), low = green (stable)
            dep_pct = round(depth_val * 100)
            dep_color = "#e74c3c" if depth_val > 0.7 else "#f1c40f" if depth_val > 0.4 else "#2ecc71"
            dep_bar = (
                '<div class="conv-bar-track">'
                + '<div class="conv-bar-fill" style="width:' + str(dep_pct) + '%;background:' + dep_color + ';"></div>'
                + '</div>'
            )

            # Adjusted prob with American odds
            adj_odds = prob_to_american(adj_prob)
            mid_odds = prob_to_american(dm)
            delta    = round(adj_prob - dm, 1)
            delta_str = ("+" if delta > 0 else "") + str(delta)
            delta_color = "#2ecc71" if delta > 0 else "#e74c3c" if delta < 0 else "#888"

            conv_html = (
                '<div class="conv-panel">'
                + '<div class="conv-header">BOOK CONVICTION</div>'

                # Signal + Strength badges
                + '<div style="display:flex;gap:4px;margin-bottom:4px;">'
                + '<span class="conv-badge" style="color:' + sig_color + ';background:' + sig_bg + ';">' + signal + '</span>'
                + '<span class="conv-badge" style="color:' + str_color + ';background:' + str_color + '18;">' + strength + '</span>'
                + '</div>'

                # BIR
                + '<div class="conv-signal-row">'
                + '<span class="conv-label">BIR (imbalance)</span>'
                + '<span class="conv-value" style="color:#4fc3f7;">' + str(bir_pct) + 'F</span>'
                + '</div>'
                + bir_bar

                # Entropy
                + '<div class="conv-signal-row">'
                + '<span class="conv-label">ENT (agreement)</span>'
                + '<span class="conv-value" style="color:' + ent_color + ';">' + str(round(ent_val, 2)) + '</span>'
                + '</div>'
                + ent_bar

                # Depth
                + '<div class="conv-signal-row">'
                + '<span class="conv-label">DEPTH (fragility)</span>'
                + '<span class="conv-value" style="color:' + dep_color + ';">' + str(round(depth_val, 2)) + '</span>'
                + '</div>'
                + dep_bar

                # Confidence
                + '<div class="conv-signal-row" style="margin-top:2px;">'
                + '<span class="conv-label">Confidence</span>'
                + '<span class="conv-value" style="color:' + str_color + ';">' + str(round(conf_val, 2)) + '</span>'
                + '</div>'

                # Adjusted probability
                + '<div class="conv-prob-row">'
                + '<div>'
                + '<div class="conv-prob-label">Dog adj. prob</div>'
                + '<div class="conv-prob-value" style="color:' + sig_color + ';">' + str(adj_prob) + '%</div>'
                + '</div>'
                + '<div style="text-align:right;">'
                + '<div class="conv-prob-label">' + adj_odds + '</div>'
                + '<div style="font-size:0.72em;color:' + delta_color + ';font-weight:700;">Δ ' + delta_str + '</div>'
                + '</div>'
                + '</div>'

                # Narrative
                + '<div class="conv-narrative">' + conv["narrative"] + '</div>'
                + '</div>'
            )
        else:
            conv_html = (
                '<div class="conv-panel">'
                + '<div class="conv-header">BOOK CONVICTION</div>'
                + '<div class="conv-nodata">No order book data</div>'
                + '<div style="font-size:0.55em;color:#333;word-break:break-all;padding:4px;">' + conv.get("debug", "") + '</div>'
                + '</div>'
            )

        # ── Build main card HTML (left side) ─────────────────────────
        main_html = (
            '<div class="game-main">'
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

        # ── Combine into row layout ──────────────────────────────────
        card = (
            '<div class="game-row">'
            + main_html
            + conv_html
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
            a    = g["a"]
            lag  = g.get("lag_info") or {}
            conv = g.get("conviction", {})
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
                "BIR":           str(round(conv.get("bir", 0.5) * 100)) + "F" if conv.get("has_data") else "—",
                "Entropy":       str(round(conv.get("entropy", 1.0), 2)) if conv.get("has_data") else "—",
                "Signal":        conv.get("signal", "—") + "/" + conv.get("strength", "—") if conv.get("has_data") else "—",
                "Adj Dog%":      str(conv.get("adjusted_prob", "—")) if conv.get("has_data") else "—",
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
