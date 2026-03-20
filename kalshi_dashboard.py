import streamlit as st
import requests
import pandas as pd
import time
import math
from datetime import datetime, timezone
from collections import defaultdict

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"

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

@st.cache_data(ttl=15)
def fetch_espn_games():
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

        start_str = event.get("date", "")
        try:
            start_dt    = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            start_local = start_dt.astimezone()
            tipoff      = start_local.strftime("%-I:%M %p")
        except Exception:
            tipoff = start_str[:16] if start_str else "TBD"

        status_obj    = event.get("status", {})
        status_type   = status_obj.get("type", {})
        status_state  = status_type.get("state", "pre")
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
    norm = normalize_name(kalshi_name)
    if norm in espn_by_team:
        game = espn_by_team[norm]
        return game, game["teams"][norm]
    for key, game in espn_by_team.items():
        if norm in key or key in norm:
            return game, game["teams"][key]
    return None, None

def format_score_line(game_info, fav_name, dog_name):
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
    half  = "1H" if game_info["period"] == 1 else "2H" if game_info["period"] == 2 else "OT"
    clock = game_info["display_clock"]
    if fav_t and dog_t and fav_t["score"] is not None:
        return str(fav_t["score"]) + "-" + str(dog_t["score"]) + " " + half + " " + clock
    return half + " " + clock

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

def _filter_and_sort(markets):
    return [m for m in markets if (m.get("status") or "").lower() in ("open", "active", "")]

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

QUALITY_LABELS = {
    "SHARP":  "Active market",
    "SOLID":  "Good market",
    "LIQUID": "Decent activity",
    "THIN":   "Low activity",
    "WIDE":   "Unreliable",
    "DEAD":   "No market",
    "OK":     "OK",
}

def _extract_levels(orderbook, side):
    levels = []
    try:
        raw = orderbook.get(side + "_dollars", [])
        if not raw:
            side_data = orderbook.get(side, {})
            if isinstance(side_data, list):
                raw = side_data
            elif isinstance(side_data, dict):
                raw = side_data.get("bids", []) or side_data.get("asks", [])
                if not raw:
                    for v in side_data.values():
                        if isinstance(v, list) and v:
                            raw = v
                            break

        for entry in raw:
            price = 0
            qty   = 0
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price = float(entry[0])
                qty   = float(entry[1])
            elif isinstance(entry, dict):
                price = float(entry.get("price", 0) or entry.get("price_cents", 0) or 0)
                qty   = float(entry.get("quantity", 0) or entry.get("count", 0) or entry.get("size", 0) or 0)
            else:
                continue
            if 0 < price <= 1.0:
                price = price * 100
            if qty > 0 and price > 0:
                levels.append((price, qty))
    except Exception:
        pass
    return levels


def compute_book_conviction(ob_fav, ob_dog, fav_mid, dog_mid):
    result = {
        "bir": 0.5, "entropy": 1.0, "depth_grad": 0.5,
        "confidence": 0.0, "adjusted_prob": dog_mid,
        "narrative": "No betting data available.", "signal": "EVEN",
        "strength": "NOISE", "has_data": False, "debug": "",
    }

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

    fav_yes_bids = _extract_levels(ob_fav, "yes")
    fav_no_bids  = _extract_levels(ob_fav, "no")
    dog_yes_bids = _extract_levels(ob_dog, "yes")
    dog_no_bids  = _extract_levels(ob_dog, "no")

    def _filter_dust(levels, lo=5, hi=95):
        return [(p, q) for p, q in levels if lo <= p <= hi]

    fav_yes_bids = _filter_dust(fav_yes_bids)
    fav_no_bids  = _filter_dust(fav_no_bids)
    dog_yes_bids = _filter_dust(dog_yes_bids)
    dog_no_bids  = _filter_dust(dog_no_bids)

    all_levels = fav_yes_bids + fav_no_bids + dog_yes_bids + dog_no_bids
    if not all_levels:
        return result

    result["has_data"] = True

    fav_side_dollars = sum(p * q for p, q in fav_yes_bids) + sum(p * q for p, q in dog_no_bids)
    dog_side_dollars = sum(p * q for p, q in dog_yes_bids) + sum(p * q for p, q in fav_no_bids)
    total_dollars    = fav_side_dollars + dog_side_dollars

    if total_dollars > 0:
        bir = fav_side_dollars / total_dollars
    else:
        bir = 0.5
    result["bir"] = round(bir, 3)

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

    def _depth_ratio(levels):
        if not levels:
            return 0.5
        sorted_lvls = sorted(levels, key=lambda x: x[0], reverse=True)
        best_qty    = sorted_lvls[0][1]
        total       = sum(q for _, q in sorted_lvls)
        if total <= 0:
            return 0.5
        return best_qty / total

    depth_fav = _depth_ratio(fav_yes_bids + dog_no_bids)
    depth_dog = _depth_ratio(dog_yes_bids + fav_no_bids)
    depth_grad = max(depth_fav, depth_dog)
    result["depth_grad"] = round(depth_grad, 3)

    prior = dog_mid / 100.0
    book_implied_dog = 1.0 - bir
    depth_stability = 1.0 - depth_grad
    confidence = (1.0 - entropy) * depth_stability
    confidence = max(0.0, min(1.0, confidence))
    result["confidence"] = round(confidence, 3)

    adjusted = prior * (1.0 - confidence) + book_implied_dog * confidence
    adjusted = max(0.01, min(0.99, adjusted))
    adjusted_pct = adjusted * 100.0
    result["adjusted_prob"] = round(adjusted_pct, 1)

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
        result["signal"] = "UNDERDOG"
    elif bir > 0.53:
        result["signal"] = "FAVORITE"
    else:
        result["signal"] = "EVEN"

    dog_pct = round((1.0 - bir) * 100)
    fav_pct = round(bir * 100)

    if result["strength"] == "NOISE":
        result["narrative"] = "🤷 Bettors are all over the place — no useful signal here."
    elif result["signal"] == "UNDERDOG" and result["strength"] == "STRONG":
        if depth_grad > 0.7:
            result["narrative"] = "🔥 Smart money is backing the underdog hard — but it might be one big bettor. Odds could vanish fast."
        else:
            result["narrative"] = "🔥 Smart money is quietly backing the underdog. This line won't last — act now."
    elif result["signal"] == "UNDERDOG" and result["strength"] == "MODERATE":
        result["narrative"] = "📈 More money is going on the underdog than you'd expect. Slight lean — bet smaller."
    elif result["signal"] == "FAVORITE" and result["strength"] in ("STRONG", "MODERATE"):
        result["narrative"] = "✋ Most money is on the favorite and bettors agree. The underdog is less of a value than it looks."
    elif result["signal"] == "EVEN":
        result["narrative"] = "⚖️ Nobody has picked a side yet. Wait and see, or go with the main recommendation."
    else:
        result["narrative"] = "💤 Weak signal — not enough conviction either way to change anything."

    return result


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


def analyze_game(fav_mid, dog_mid, spread, volume, fav_name, dog_name):
    result = {
        "verdict": "NO DATA", "color": "#555",
        "detail": "", "action_line": "No action.",
        "fav_target": "N/A", "dog_target": "N/A",
        "edge_fav_cents": 0, "edge_dog_cents": 0,
    }

    q_label, q_icon, q_score = market_quality(spread, volume)

    if q_score < 0 or (spread is None and volume < 100):
        result["detail"]  = "Not enough betting activity to trust this price (buy/sell gap: " + str(spread or "?") + " cents, total bets: " + str(int(volume)) + "). The odds here are unreliable."
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
        result["detail"]      = "The buy/sell gap is " + str(int(spread)) + " cents wide — the price could be off by 5–10 cents in either direction. Don't use this as a signal."
        result["action_line"] = "Don't use this as a signal."
        return result

    thin_warning = " (low activity — consider betting half your normal amount)" if q_label == "THIN" else ""

    if fav_mid >= 85:
        result["verdict"] = "BIG FAVORITE — SKIP"
        result["color"]   = "#b8860b"
        result["detail"]  = (fav_name + " is " + str(int(fav_mid)) + "% likely to win (" + fav_fair + "). "
                             + "Sportsbooks probably post " + retail_fav_odds + " or worse. The risk/reward is bad." + thin_warning)
        result["action_line"] = ("Skip betting the favorite. Only play: " + dog_name
                                 + " at " + dog_target + " or better odds. Otherwise pass.")

    elif 20 <= dog_mid <= 45 and q_score >= 2:
        result["verdict"] = "UNDERDOG OPPORTUNITY"
        result["color"]   = "#2ecc71"
        result["detail"]  = ("The informed market says " + dog_name + " has a " + str(int(dog_mid)) + "% chance to win (" + dog_fair + "). "
                             + "Sportsbooks typically post " + retail_dog_odds + ", meaning they're undervaluing this team by ~"
                             + str(round(result["edge_dog_cents"], 1)) + " cents." + thin_warning)
        result["action_line"] = ("RECOMMENDED BET: " + dog_name + " at " + dog_target + " or better odds. "
                                 + "If your sportsbook shows " + retail_dog_odds + " or higher = bet it.")

    elif 55 <= fav_mid <= 72 and q_score >= 1:
        result["verdict"] = "WATCH THIS GAME LIVE"
        result["color"]   = "#3498db"
        result["detail"]  = ("Close game: " + str(int(fav_mid)) + "/" + str(int(dog_mid)) + " win chances. "
                             + "This market updates in seconds during a run, while sportsbooks take 30–90 seconds to catch up." + thin_warning)
        result["action_line"] = ("Keep both apps open. Fair odds right now: " + fav_name + " " + fav_fair
                                 + " / " + dog_name + " " + dog_fair + ". "
                                 + "Bet whichever team the sportsbook is slow to update.")

    elif 72 < fav_mid < 85:
        result["verdict"] = "CHECK THE ODDS"
        result["color"]   = "#f39c12"
        result["detail"]  = (fav_name + " has a " + str(int(fav_mid)) + "% win chance (" + fav_fair + "). "
                             + "Sportsbooks will likely post " + retail_fav_odds + ". Only worth betting if the book is being generous." + thin_warning)
        result["action_line"] = (fav_name + " at " + fav_target + " or better — or — "
                                 + dog_name + " at " + dog_target + " or better. If neither, pass.")

    elif 45 <= fav_mid <= 55:
        result["verdict"] = "COIN FLIP"
        result["color"]   = "#9b59b6"
        result["detail"]  = ("Almost 50/50: " + str(int(fav_mid)) + "/" + str(int(dog_mid)) + " win chances. "
                             + "The house cut eats your edge on coin flips unless one side is clearly mispriced." + thin_warning)
        result["action_line"] = ("Fair odds: " + fav_name + " " + fav_fair + " / " + dog_name + " " + dog_fair + ". "
                                 + "Bet whichever side the sportsbook is giving you the biggest discount on. If neither beats fair odds, skip.")

    elif 20 <= dog_mid <= 45 and q_score < 2:
        result["verdict"] = "UNDERDOG — LOW CONFIDENCE"
        result["color"]   = "#d4a017"
        result["detail"]  = (dog_name + " is at " + str(int(dog_mid)) + "% (" + dog_fair + ") but betting activity is "
                             + QUALITY_LABELS.get(q_label, q_label).lower() + ". The price could be off by 5+ cents.")
        result["action_line"] = ("Half your normal bet size only: " + dog_name + " at " + dog_target + " or better.")

    else:
        result["verdict"]     = "PASS"
        result["color"]       = "#666"
        result["detail"]      = "No clear edge here. Win chances: " + str(int(fav_mid)) + "/" + str(int(dog_mid)) + "."
        result["action_line"] = "No action."

    return result


st.set_page_config(page_title="March Madness Betting Dashboard", layout="wide")
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

st.title("March Madness Betting Dashboard")
st.caption("Last updated: " + datetime.now().strftime("%I:%M:%S %p") + " · Refreshes automatically")

with st.sidebar:
    st.header("Settings")
    api_key = get_api_key()
    if not api_key:
        api_key = st.text_input(
            "Kalshi API Key", type="password",
            help="Paste your Kalshi API key here, or set KALSHI_API_KEY in Streamlit secrets",
        )
    else:
        st.success("API key loaded ✓")

    st.divider()
    st.subheader("Auto-Refresh")
    refresh_rate = st.slider("Update every (seconds)", 5, 120, 20, help="Use 5–10 during live games, 30+ before tip-off")

    st.divider()
    st.subheader("Odds Gap Alerts")
    lag_threshold = st.slider(
        "Alert when odds shift by (cents)", min_value=1, max_value=10, value=3,
        help="Fires an alert when this market's price moves this many cents — your sportsbook may not have caught up yet",
    )
    lag_lookback = st.slider(
        "Look back how far (seconds)", min_value=30, max_value=300, value=90,
        help="How far back to check for price movement. 90 seconds matches the typical sportsbook update delay.",
    )

    st.divider()
    st.markdown("""
**Color Guide**
- 🟢 Green = Bet this now
- 🟡 Yellow = Only if you get good enough odds
- ⚫ Gray = Skip it
- **Minimum Odds** = The worst odds you should accept before passing
- **⚡ ODDS GAP ALERT** = This market moved fast — your sportsbook may be behind

**Right Panel: What Bettors Are Doing**
One sentence explaining whether the smart money agrees with the recommendation or not.
""")

if not api_key:
    st.warning("Enter your Kalshi API key in the sidebar to get started.")
    st.stop()

with st.spinner("Loading March Madness markets…"):
    markets = fetch_ncaa_markets(api_key)

espn_by_team, espn_raw = fetch_espn_games()

if espn_by_team:
    n_espn_live = sum(1 for g in espn_raw if g["status_state"] == "in")
    st.caption("ESPN: " + str(len(espn_raw)) + " games today"
               + (" · 🔴 " + str(n_espn_live) + " in progress" if n_espn_live else ""))

if not markets:
    st.warning("No markets found. Diagnostics below.")

    with st.expander("Diagnostic — all open Kalshi events", expanded=True):
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
                    st.error("API returned 0 events. Your API key may not have sports access, or you may be geo-blocked.")
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
                st.write("After filter: " + str(len(filtered)))
                if not filtered and raw_markets:
                    st.warning("All markets were filtered out. Showing all anyway.")
                    markets = raw_markets
        except Exception as ex:
            st.error("Direct fetch failed: " + str(ex))

    st.subheader("Search by ticker")
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
    games = defaultdict(list)
    for m in markets:
        title         = (m.get("title", "") + m.get("subtitle", "")).lower()
        skip_keywords = ["spread", "total", "over", "under", "point", "half"]
        if any(kw in title for kw in skip_keywords):
            continue
        event = m.get("event_ticker", m.get("ticker", ""))
        games[event].append(m)

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

        fav_game, fav_team_data = espn_lookup(espn_by_team, fav_name)
        dog_game, dog_team_data = espn_lookup(espn_by_team, dog_name)
        espn_game   = fav_game or dog_game
        fav_seed    = fav_team_data["seed"] if fav_team_data else None
        dog_seed    = dog_team_data["seed"] if dog_team_data else None
        score_line  = format_score_line(espn_game, fav_name, dog_name)
        espn_state  = espn_game["status_state"] if espn_game else None
        espn_sort_ts = espn_game["sort_ts"] if espn_game else None

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
        if "UNDERDOG OPPORTUNITY" in v: return 1
        if "WATCH THIS GAME"      in v: return 2
        if "COIN FLIP"            in v: return 3
        if "CHECK THE ODDS"       in v: return 4
        if "BIG FAVORITE"         in v: return 5
        if "LOW CONFIDENCE"       in v: return 6
        return 7

    for g in analyses:
        g["_priority"] = verdict_priority(g)

    n_action = sum(1 for g in analyses if g["_priority"] <= 4)
    n_lag    = sum(1 for g in analyses if (g.get("lag_info") or {}).get("alert"))

    ctrl_col, sort_col = st.columns([3, 1])
    with ctrl_col:
        FILTER_OPTS = {
            "All":            lambda g: True,
            "Odds Gap ⚡":    lambda g: bool((g.get("lag_info") or {}).get("alert")),
            "Bet These":      lambda g: g["_priority"] <= 4,
            "Underdogs":      lambda g: "UNDERDOG" in g["a"]["verdict"],
            "In Progress":    lambda g: g.get("espn_state") == "in",
            "Next 3 Hours":   lambda g: g["close_ts"] is not None and g["close_ts"] <= time.time() + 10800,
            "Skip":           lambda g: g["_priority"] >= 7,
        }
        filter_choice = st.radio(
            "Show", list(FILTER_OPTS.keys()), index=0,
            horizontal=True, label_visibility="collapsed",
        )
    with sort_col:
        sort_choice = st.selectbox(
            "Sort by", ["Best first", "Start time", "Underdog edge ↓", "Most bets ↓", "Market quality ↓", "Smart money ↓"],
            label_visibility="collapsed",
        )

    shown = [g for g in analyses if FILTER_OPTS[filter_choice](g)]

    if sort_choice == "Best first":
        shown.sort(key=lambda g: g["_priority"])
    elif sort_choice == "Start time":
        shown.sort(key=lambda g: g["espn_sort_ts"] or g["close_ts"] or float("inf"))
    elif sort_choice == "Underdog edge ↓":
        shown.sort(key=lambda g: -g["a"].get("edge_dog_cents", 0))
    elif sort_choice == "Most bets ↓":
        shown.sort(key=lambda g: -g["vol"])
    elif sort_choice == "Market quality ↓":
        shown.sort(key=lambda g: -g["quality"][2])
    elif sort_choice == "Smart money ↓":
        shown.sort(key=lambda g: -g["conviction"].get("confidence", 0))

    summary = str(len(shown)) + " of " + str(len(analyses)) + " games"
    if n_lag:
        summary += " · " + str(n_lag) + " odds gap alert" + ("s" if n_lag > 1 else "")
    if n_action:
        summary += " · " + str(n_action) + " worth betting"
    st.caption(summary)

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
        vig_str    = (str(round(vig_val, 1)) + "¢ house cut") if vig_val else "—"
        spread_str = (str(round(g["spread"], 1)) + "¢ buy/sell gap") if g["spread"] else "—"
        vol_str    = "{:,.0f} total bets".format(g["vol"])
        ql_human   = QUALITY_LABELS.get(ql, ql)

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

        lag      = g.get("lag_info") or {}
        lag_html = ""
        if lag:
            direction_name = g["fav_name"] if lag["direction"] == "FAV" else g["dog_name"]
            moved_str = ("+" if lag["moved_cents"] >= 0 else "") + str(lag["moved_cents"])
            vel_str   = ("+" if lag["velocity"]    >= 0 else "") + str(lag["velocity"])
            if lag["alert"]:
                lag_html = (
                    '<div class="lag-alert">'
                    + '<strong>⚡ ODDS GAP ALERT</strong> — '
                    + direction_name + " moved " + moved_str + "¢ in the last "
                    + str(lag_lookback) + "s (" + vel_str + " ¢/min)"
                    + " — check your sportsbook NOW"
                    + '</div>'
                )
            else:
                lag_html = (
                    '<div class="lag-neutral">'
                    + "Price moved " + moved_str + "¢ recently · " + vel_str + " ¢/min"
                    + '</div>'
                )

        color    = a["color"]
        badge_bg = color + "22"

        if conv["has_data"]:
            narrative_text = conv["narrative"]
            conv_html = (
                '<div class="conv-panel" style="justify-content:center;">'
                + '<div class="conv-header">WHAT BETTORS ARE DOING</div>'
                + '<div style="font-size:0.95em;color:#ccc;line-height:1.6;padding:8px 0;">'
                + narrative_text
                + '</div>'
                + '</div>'
            )
        elif not conv["has_data"]:
            conv_html = (
                '<div class="conv-panel" style="justify-content:center;">'
                + '<div class="conv-header">WHAT BETTORS ARE DOING</div>'
                + '<div class="conv-nodata">Not enough data yet.</div>'
                + '</div>'
            )


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
            + '<div class="team-price" style="color:#4fc3f7;">' + str(int(fm)) + '¢ · ' + fav_fair + '</div>'
            + '<div class="team-target" style="color:#4fc3f7;">Minimum odds: ' + fav_target + '</div>'
            + '</div>'
            + '<div class="team-cell">'
            + '<div class="team-label">Underdog</div>'
            + '<div class="team-name">' + dog_seed_str + g["dog_name"] + '</div>'
            + '<div class="team-price" style="color:#ff8a65;">' + str(int(dm)) + '¢ · ' + dog_fair + '</div>'
            + '<div class="team-target" style="color:#ff8a65;">Minimum odds: ' + dog_target + '</div>'
            + '</div>'
            + '</div>'
            + '<div class="mkt-strip">'
            + time_html
            + '<span class="mkt-item" style="color:#444;">|</span>'
            + '<span class="mkt-item"><span>' + ql_human + '</span></span>'
            + '<span class="mkt-item">' + spread_str + '</span>'
            + '<span class="mkt-item">' + vig_str + '</span>'
            + '<span class="mkt-item">' + vol_str + '</span>'
            + '</div>'
            + '<div class="det-box">' + a["detail"] + '</div>'
            + '</div>'
        )

        card = (
            '<div class="game-row">'
            + main_html
            + conv_html
            + '</div>'
        )

        st.markdown(card, unsafe_allow_html=True)

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

    with st.expander("Quick Reference — All Games"):
        rows = []
        for g in analyses:
            a    = g["a"]
            lag  = g.get("lag_info") or {}
            conv = g.get("conviction", {})
            rows.append({
                "Game":              g["title"],
                "Verdict":           a["verdict"],
                "Tip-off":           g.get("score_line") or "—",
                "Favorite":          (("#" + str(g["fav_seed"]) + " ") if g["fav_seed"] else "") + g["fav_name"],
                "Fav win %":         str(int(g["fav_mid"])) + "%",
                "Fav odds":          prob_to_american(g["fav_mid"]),
                "Fav min. odds":     a["fav_target"],
                "Underdog":          (("#" + str(g["dog_seed"]) + " ") if g["dog_seed"] else "") + g["dog_name"],
                "Dog win %":         str(int(g["dog_mid"])) + "%",
                "Dog odds":          prob_to_american(g["dog_mid"]),
                "Dog min. odds":     a["dog_target"],
                "Market":            QUALITY_LABELS.get(g["quality"][0], g["quality"][0]),
                "Money on fav":      str(round(conv.get("bir", 0.5) * 100)) + "%" if conv.get("has_data") else "—",
                "Bettor agreement":  str(round(conv.get("entropy", 1.0), 2)) if conv.get("has_data") else "—",
                "Lean":              conv.get("signal", "—") + " / " + conv.get("strength", "—") if conv.get("has_data") else "—",
                "True underdog %":   str(conv.get("adjusted_prob", "—")) + "%" if conv.get("has_data") else "—",
                "Price moved":       (str(lag.get("moved_cents", "")) + "¢") if lag else "—",
                "Odds gap alert":    "YES ⚡" if lag.get("alert") else "—",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()
c1, c2 = st.columns([3, 1])
with c2:
    if st.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()
with c1:
    st.caption("Next auto-refresh in " + str(refresh_rate) + " seconds")

time.sleep(refresh_rate)
st.cache_data.clear()
st.rerun()
