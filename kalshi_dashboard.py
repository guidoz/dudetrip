import streamlit as st
import requests
import pandas as pd
import time
import math
from datetime import datetime, timezone
from collections import defaultdict

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Historical NCAA Tournament upset rates by seed matchup (1985-2024)
SEED_UPSET_RATES = {
    (1, 16): 2, (2, 15): 6, (3, 14): 7, (4, 13): 21,
    (5, 12): 35, (6, 11): 37, (7, 10): 39, (8, 9): 48,
    (1, 8): 21, (1, 9): 17, (2, 7): 27, (2, 10): 22,
    (3, 6): 34, (3, 11): 28, (4, 5): 43,
    (1, 4): 30, (1, 5): 25, (1, 3): 28,
    (2, 3): 42, (1, 2): 38,
}

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

def kelly_fraction(true_prob, book_implied_prob):
    """Quarter-Kelly fraction. Both inputs on 0-100 scale."""
    if true_prob <= 0 or book_implied_prob <= 0:
        return 0.0
    if true_prob >= 100 or book_implied_prob >= 100:
        return 0.0
    p = true_prob / 100
    q = 1 - p
    b = (100 / book_implied_prob) - 1
    if b <= 0:
        return 0.0
    f = (b * p - q) / b
    return max(0.0, f)

def estimate_retail_implied(kalshi_mid):
    """Estimate what a retail book charges vs Kalshi fair price."""
    vig_rate = 0.045
    if kalshi_mid >= 50:
        return min(kalshi_mid + (kalshi_mid * vig_rate), 97)
    else:
        return max(kalshi_mid - (kalshi_mid * vig_rate * 0.7), 2)

def value_target(kalshi_mid, min_edge_pct=2.0):
    """Minimum American odds to have +EV at the window."""
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

# ── Book Conviction Scoring ────────────────────────────────────────────────

def shannon_entropy(distribution):
    """Compute normalized Shannon entropy of a distribution. Returns 0-1."""
    if not distribution:
        return 1.0
    total = sum(distribution)
    if total <= 0:
        return 1.0
    probs = [v / total for v in distribution if v > 0]
    if len(probs) <= 1:
        return 0.0
    raw_entropy = -sum(p * math.log2(p) for p in probs)
    max_entropy = math.log2(len(probs))
    if max_entropy == 0:
        return 0.0
    return raw_entropy / max_entropy


def compute_book_conviction(orderbook, fav_mid, dog_mid, team_seeds, fav_name, dog_name):
    """
    Collapse the order book into three signals, run Bayesian update,
    produce a conviction story.

    Returns dict with:
        bir        - Book Imbalance Ratio (0=all dog, 1=all fav)
        entropy    - Normalized Shannon entropy of order sizes (0=consensus, 1=chaos)
        depth_grad - Depth gradient / fragility (0=deep, 1=fragile cliff)
        posterior  - Bayesian-adjusted dog probability
        prior      - The prior used (historical or mid)
        confidence - Overall confidence (0-1)
        narrative  - One-liner story
        color      - Hex color for the card
        label      - Short verdict label
        target     - Target odds string for the window
        has_data   - Whether we had enough data to compute
    """
    result = {
        "bir": 0.5, "entropy": 1.0, "depth_grad": 0.5,
        "posterior": dog_mid, "prior": dog_mid,
        "confidence": 0.0, "narrative": "No order book data.",
        "color": "#555", "label": "NO DATA", "target": "N/A",
        "has_data": False,
    }

    yes_bids = orderbook.get("yes", {}).get("bids", []) if orderbook else []
    no_bids = orderbook.get("no", {}).get("bids", []) if orderbook else []

    if not yes_bids and not no_bids:
        return result

    # ── Signal 1: Book Imbalance Ratio ──
    # Sum dollar-volume on each side.  YES bids = people wanting the favorite.
    # NO bids = people wanting the dog (selling YES / buying NO).
    yes_volume = sum(float(b.get("count", 0)) * float(b.get("price", 0)) for b in yes_bids)
    no_volume = sum(float(b.get("count", 0)) * float(b.get("price", 0)) for b in no_bids)
    total_volume = yes_volume + no_volume

    if total_volume <= 0:
        return result

    bir = yes_volume / total_volume  # 1=all fav, 0=all dog

    # ── Signal 2: Book Entropy ──
    # Distribution of order sizes across ALL price levels, both sides combined.
    all_sizes = []
    for b in yes_bids:
        sz = float(b.get("count", 0))
        if sz > 0:
            all_sizes.append(sz)
    for b in no_bids:
        sz = float(b.get("count", 0))
        if sz > 0:
            all_sizes.append(sz)

    entropy = shannon_entropy(all_sizes)

    # ── Signal 3: Depth Gradient (fragility) ──
    # Ratio of volume at best bid vs total volume.  High = cliff, Low = deep.
    best_yes = float(yes_bids[0].get("count", 0)) * float(yes_bids[0].get("price", 0)) if yes_bids else 0
    best_no = float(no_bids[0].get("count", 0)) * float(no_bids[0].get("price", 0)) if no_bids else 0
    best_total = best_yes + best_no
    depth_grad = best_total / total_volume if total_volume > 0 else 0.5

    # ── Bayesian Update ──
    # Prior: historical upset rate if we have seeds, else Kalshi mid
    fav_seed = team_seeds.get(fav_name.upper().strip())
    dog_seed = team_seeds.get(dog_name.upper().strip())
    prior = dog_mid
    prior_source = "market mid"

    if fav_seed and dog_seed:
        key = (min(fav_seed, dog_seed), max(fav_seed, dog_seed))
        hist = SEED_UPSET_RATES.get(key)
        if hist:
            prior = hist
            prior_source = "#" + str(dog_seed) + " vs #" + str(fav_seed) + " history"

    # Book-implied dog probability from imbalance
    book_implied_dog = (1.0 - bir) * 100  # scale to 0-100

    # Confidence: high when entropy is low AND book is deep
    depth_stability = 1.0 - depth_grad  # 1=deep, 0=fragile
    confidence = (1.0 - entropy) * depth_stability
    confidence = max(0.0, min(1.0, confidence))

    # Weighted blend: posterior = prior * (1-conf) + book_implied * conf
    posterior = prior * (1.0 - confidence) + book_implied_dog * confidence
    posterior = max(1.0, min(99.0, posterior))

    # ── Generate Narrative ──
    pressure_dir = "dog" if bir < 0.47 else ("favorite" if bir > 0.53 else "split")
    pressure_pct = int(abs(bir - 0.5) * 200)

    conviction_word = "high" if entropy < 0.35 else ("moderate" if entropy < 0.65 else "low")
    fragility_word = "fragile cliff" if depth_grad > 0.6 else ("stable" if depth_grad < 0.35 else "moderate depth")

    # Divergence between prior and posterior
    shift = posterior - prior
    shift_dir = "up" if shift > 0 else "down"

    # Target odds
    target = prob_to_american(max(1, posterior - 2))

    # Build narrative
    parts = []

    if pressure_dir == "dog":
        parts.append("Book pressure is " + str(pressure_pct) + "% dog-side")
    elif pressure_dir == "favorite":
        parts.append("Book pressure is " + str(pressure_pct) + "% fav-side")
    else:
        parts.append("Book pressure is roughly even")

    parts.append(" with " + conviction_word + " conviction")
    parts.append(" (entropy " + str(round(entropy, 2)) + ").")

    if prior_source != "market mid":
        parts.append(" History says " + str(int(prior)) + "% upset rate.")

    parts.append(" Model says " + str(int(round(posterior))) + "%.")

    if abs(shift) > 3:
        parts.append(" Book shifted the estimate " + str(int(abs(shift))) + " pts " + shift_dir + " from " + prior_source + ".")

    narrative = "".join(parts)

    # Fragility note
    if depth_grad > 0.6:
        narrative += " Warning: price is sitting on a " + fragility_word + " — one order could move it."
    elif depth_grad < 0.3 and entropy < 0.4:
        narrative += " This is a deep, agreed-upon book. Highest confidence signal."

    # Verdict / color
    if confidence > 0.5 and pressure_dir == "dog" and posterior > prior + 3:
        label = "BOOK SAYS DOG"
        color = "#2ecc71"
    elif confidence > 0.5 and pressure_dir == "favorite" and posterior < prior - 3:
        label = "BOOK SAYS FAV"
        color = "#4fc3f7"
    elif entropy > 0.7:
        label = "CHAOS"
        color = "#e74c3c"
    elif depth_grad > 0.65:
        label = "THIN ICE"
        color = "#f39c12"
    elif confidence > 0.3 and abs(shift) > 2:
        label = "LEANING"
        color = "#d4a017"
    else:
        label = "NEUTRAL"
        color = "#888"

    # Action line
    if label == "BOOK SAYS DOG":
        action = "Sharp money likes the dog. Window target: " + target + " or better."
    elif label == "BOOK SAYS FAV":
        action = "Sharp money backing the chalk. Dog looks overpriced."
    elif label == "CHAOS":
        action = "Market is arguing. Max uncertainty = max fun if you want a flyer."
    elif label == "THIN ICE":
        action = "Price held up by thin support. Could be a whale who knows something, or a mirage."
    elif label == "LEANING":
        side = "dog" if shift > 0 else "fav"
        action = "Slight lean toward " + side + ". Moderate signal, half-size if you play."
    else:
        action = "Book is balanced. No strong read."

    return {
        "bir": bir,
        "entropy": entropy,
        "depth_grad": depth_grad,
        "posterior": posterior,
        "prior": prior,
        "prior_source": prior_source,
        "confidence": confidence,
        "narrative": narrative,
        "color": color,
        "label": label,
        "target": target,
        "action": action,
        "has_data": True,
        "pressure_dir": pressure_dir,
        "n_levels": len(all_sizes),
    }


# ── API fetchers ───────────────────────────────────────────────────────────

@st.cache_data(ttl=15)
def fetch_ncaa_markets(api_key):
    NCAA_KEYWORDS = [
        "ncaa", "march madness", "basketball", "ncaab", "ncaamb",
        "college basketball", "tournament", "kxncaamb",
    ]
    all_markets = []
    seen = set()

    # Strategy 1: confirmed series tickers — get everything, filter later
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

    # Strategy 1b: events endpoint with nested markets
    try:
        r = requests.get(
            BASE_URL + "/events",
            params={
                "series_ticker": "KXNCAAMBGAME",
                "limit": 200, "with_nested_markets": "true",
            },
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

    # If we have markets from direct series queries, filter and return
    if all_markets:
        return _filter_and_sort(all_markets)

    # Strategy 2: paginate all open events, keyword filter (fallback)
    cursor = None
    for _ in range(10):
        params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(
                BASE_URL + "/events", params=params,
                headers=kalshi_headers(api_key), timeout=10,
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
                        if m["ticker"] not in seen:
                            seen.add(m["ticker"])
                            all_markets.append(m)
            cursor = data.get("cursor")
            if not cursor or not events:
                break
        except Exception:
            break

    # Strategy 3: broad market scan
    if not all_markets:
        try:
            r = requests.get(
                BASE_URL + "/markets",
                params={"status": "open", "limit": 1000},
                headers=kalshi_headers(api_key), timeout=10,
            )
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
        r = requests.get(
            BASE_URL + "/markets/" + ticker + "/orderbook",
            headers=kalshi_headers(api_key), timeout=8,
        )
        if r.status_code == 200:
            return r.json().get("orderbook_fp", {})
    except Exception:
        pass
    return {}

@st.cache_data(ttl=10)
def fetch_market_detail(api_key, ticker):
    try:
        r = requests.get(
            BASE_URL + "/markets/" + ticker,
            headers=kalshi_headers(api_key), timeout=8,
        )
        if r.status_code == 200:
            return r.json().get("market", {})
    except Exception:
        pass
    return {}

# ── Spread metrics ─────────────────────────────────────────────────────────

def compute_spread_metrics(market):
    """Fixed from v1: vig = single spread, not doubled."""
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
        spread = yes_ask - yes_bid if yes_ask > 0 else None
        vig = spread

        return {
            "yes_bid": yes_bid, "yes_ask": yes_ask,
            "no_bid": no_bid, "no_ask": no_ask,
            "mid": mid, "spread": spread, "vig": vig,
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

# ── Core analysis engine ──────────────────────────────────────────────────

def analyze_game(fav_mid, dog_mid, spread, volume, fav_name, dog_name,
                 bankroll, kelly_mult, team_seeds):
    result = {
        "verdict": "— NO DATA", "color": "#555",
        "detail": "", "action_line": "No action.",
        "fav_target": "N/A", "dog_target": "N/A",
        "kelly_fav_dollars": 0, "kelly_dog_dollars": 0,
        "edge_fav_cents": 0, "edge_dog_cents": 0,
        "seed_note": None,
    }

    q_label, q_icon, q_score = market_quality(spread, volume)

    if q_score < 0 or (spread is None and volume < 100):
        result["detail"] = "Market too thin (spread " + str(spread or "?") + " cents, vol " + str(int(volume)) + "). Price is noise."
        result["verdict"] = "--- SKIP"
        result["color"] = "#666"
        return result

    fav_fair = prob_to_american(fav_mid)
    dog_fair = prob_to_american(dog_mid)

    retail_fav_implied = estimate_retail_implied(fav_mid)
    retail_dog_implied = estimate_retail_implied(dog_mid)

    retail_fav_odds = prob_to_american(retail_fav_implied)
    retail_dog_odds = prob_to_american(retail_dog_implied)

    fav_target = value_target(fav_mid)
    dog_target = value_target(dog_mid)

    result["fav_target"] = fav_target
    result["dog_target"] = dog_target

    result["edge_fav_cents"] = retail_fav_implied - fav_mid
    result["edge_dog_cents"] = dog_mid - retail_dog_implied

    k_fav = kelly_fraction(fav_mid, retail_fav_implied) * kelly_mult
    k_dog = kelly_fraction(dog_mid, retail_dog_implied) * kelly_mult

    result["kelly_fav_dollars"] = bankroll * min(k_fav, 0.25)
    result["kelly_dog_dollars"] = bankroll * min(k_dog, 0.25)

    # Seed context
    fav_seed = team_seeds.get(fav_name.upper().strip())
    dog_seed = team_seeds.get(dog_name.upper().strip())

    if fav_seed and dog_seed:
        key = (min(fav_seed, dog_seed), max(fav_seed, dog_seed))
        hist = SEED_UPSET_RATES.get(key)
        if hist:
            note = ("Since 1985: #" + str(dog_seed) + " seeds beat #" + str(fav_seed)
                    + " seeds " + str(hist) + "% of the time. Kalshi has this dog at "
                    + str(int(dog_mid)) + "%.")
            if dog_mid < hist - 5:
                note += (" Kalshi is " + str(int(hist - dog_mid))
                         + " cents BELOW historical rate - market might be underpricing the upset.")
            elif dog_mid > hist + 5:
                note += (" Market is " + str(int(dog_mid - hist))
                         + " cents ABOVE historical rate - this specific dog may be stronger than typical.")
            result["seed_note"] = note

    # Verdict logic
    if q_label in ("DEAD", "WIDE"):
        result["verdict"] = "--- SKIP"
        result["color"] = "#666"
        result["detail"] = "Spread is " + str(int(spread)) + " cents wide. Price could be off by 5-10 cents either way."
        result["action_line"] = "Don't use this as a signal."
        return result

    thin_warning = ""
    if q_label == "THIN":
        thin_warning = " (thin market - half your normal size)"

    if fav_mid >= 85:
        result["verdict"] = "HEAVY CHALK"
        result["color"] = "#b8860b"
        result["detail"] = (fav_name + " at " + str(int(fav_mid)) + "% (" + fav_fair + "). "
                            + "Retail probably posts " + retail_fav_odds + " or worse. "
                            + "Risk/reward is terrible." + thin_warning)
        result["action_line"] = ("Skip the favorite. The only play: if " + dog_name
                                 + " is on the board at " + dog_target + " or better, "
                                 + "that's a sprinkle for $" + str(int(result["kelly_dog_dollars"])) + ". Otherwise pass.")

    elif 20 <= dog_mid <= 45 and q_score >= 2:
        result["verdict"] = "DOG VALUE"
        result["color"] = "#2ecc71"
        result["detail"] = ("Sharp market says " + dog_name + " wins " + str(int(dog_mid)) + "% (" + dog_fair + "). "
                            + "Retail books typically post " + retail_dog_odds + ", underpricing by ~"
                            + str(round(result["edge_dog_cents"], 1)) + " cents." + thin_warning)
        result["action_line"] = ("AT THE WINDOW: " + dog_name + " at " + dog_target + " or better. "
                                 + "If the board shows " + retail_dog_odds + " or higher, that's +EV. "
                                 + "Size: $" + str(int(result["kelly_dog_dollars"])) + ".")

    elif 55 <= fav_mid <= 72 and q_score >= 1:
        result["verdict"] = "LIVE BET WATCH"
        result["color"] = "#3498db"
        result["detail"] = ("Close game: " + str(int(fav_mid)) + "/" + str(int(dog_mid)) + ". "
                            + "Sharp money is split. This is your live-line-lag play - "
                            + "Kalshi reprices in seconds, the book takes 30-90 sec after big runs." + thin_warning)
        max_kelly = max(result["kelly_fav_dollars"], result["kelly_dog_dollars"])
        result["action_line"] = ("Both apps open during the game. "
                                 + "Pregame fair: " + fav_name + " " + fav_fair + " / " + dog_name + " " + dog_fair + ". "
                                 + "When momentum shifts, bet whichever side the board hasn't caught up on. "
                                 + "Max size: $" + str(int(max_kelly)) + ".")

    elif 72 < fav_mid < 85:
        result["verdict"] = "PRICE CHECK"
        result["color"] = "#f39c12"
        result["detail"] = (fav_name + " at " + str(int(fav_mid)) + "% (" + fav_fair + "). "
                            + "Retail likely posts " + retail_fav_odds + ". "
                            + "Decent favorite - only worth it if the book is generous." + thin_warning)
        result["action_line"] = ("At the window - two options: "
                                 + "(1) " + fav_name + " at " + fav_target + " or better = bet $" + str(int(result["kelly_fav_dollars"])) + ". "
                                 + "(2) " + dog_name + " at " + dog_target + " or better = bet $" + str(int(result["kelly_dog_dollars"])) + ". "
                                 + "If neither hits the target, pass.")

    elif 45 <= fav_mid <= 55:
        result["verdict"] = "TOSS-UP"
        result["color"] = "#9b59b6"
        result["detail"] = ("Market says " + str(int(fav_mid)) + "/" + str(int(dog_mid))
                            + " - near even. Vig kills you on coin flips unless one side is mispriced." + thin_warning)
        result["action_line"] = ("Compare both sides on the board: "
                                 + fav_name + " fair = " + fav_fair + " / " + dog_name + " fair = " + dog_fair + ". "
                                 + "Bet whichever side the board gives you the biggest discount vs fair. "
                                 + "If neither side beats fair, skip.")

    elif 20 <= dog_mid <= 45 and q_score < 2:
        result["verdict"] = "DOG - LOW CONFIDENCE"
        result["color"] = "#d4a017"
        result["detail"] = (dog_name + " at " + str(int(dog_mid)) + "% (" + dog_fair + ") but market is "
                            + q_label.lower() + ". The mid could be off by 5+ cents. Don't size like a sharp signal.")
        result["action_line"] = ("Half size only. " + dog_name + " at " + dog_target
                                 + " or better = $" + str(int(result["kelly_dog_dollars"] * 0.5)) + " max.")

    else:
        result["verdict"] = "PASS"
        result["color"] = "#666"
        result["detail"] = "No clear edge. " + str(int(fav_mid)) + "/" + str(int(dog_mid)) + " split."
        result["action_line"] = "No action."

    return result

# ── UI ─────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="March Madness Kalshi v2", page_icon="", layout="wide")

st.markdown("""
<style>
.game-card {border:1px solid #333; border-radius:14px; padding:18px; margin-bottom:14px; background:#1a1a1a;}
.v-line {font-size:1.5em; font-weight:800; margin:6px 0 10px 0;}
.sbox {flex:1; background:#222; border-radius:8px; padding:10px; text-align:center;}
.slbl {font-size:0.72em; color:#aaa; text-transform:uppercase; letter-spacing:0.5px;}
.sname {font-size:1.05em; font-weight:700; color:#fff;}
.sprice {font-size:1.35em; font-weight:800;}
.sodds {font-size:0.85em; color:#aaa;}
.act-box {background:#111; border-left:3px solid; padding:10px 14px; border-radius:4px; font-size:0.92em; color:#eee; margin-top:8px; line-height:1.6;}
.det-box {font-size:0.82em; color:#999; padding:4px 0 6px 0;}
.kb {display:inline-block; background:#2a2a2a; border-radius:6px; padding:3px 8px; font-size:0.78em; color:#ccc; margin:2px 4px 2px 0;}
.conv-card {border:1px solid #333; border-radius:14px; padding:18px; margin-bottom:14px; background:#111; height:100%;}
.conv-label {font-size:1.3em; font-weight:800; margin:6px 0 12px 0;}
.conv-meter {background:#222; border-radius:6px; padding:8px 12px; margin:6px 0; font-size:0.88em; color:#ccc;}
.conv-meter-bar {height:6px; border-radius:3px; margin-top:4px;}
.conv-narrative {font-size:0.88em; color:#bbb; line-height:1.6; padding:8px 0;}
.conv-action {background:#0a0a0a; border-left:3px solid; padding:10px 14px; border-radius:4px; font-size:0.92em; color:#eee; margin-top:10px; line-height:1.6;}
.conv-posterior {font-size:2.2em; font-weight:900; text-align:center; margin:10px 0 2px 0;}
.conv-posterior-sub {font-size:0.78em; color:#888; text-align:center; margin-bottom:10px;}
</style>
""", unsafe_allow_html=True)

st.title("March Madness - Kalshi Live Dashboard v2")
st.caption("Last refresh: " + datetime.now().strftime("%I:%M:%S %p") + " | Auto-updates")

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

    st.subheader("Bankroll and Sizing")
    bankroll = st.number_input("Bankroll ($)", min_value=50, max_value=50000, value=500, step=50)
    kelly_mode = st.radio(
        "Sizing mode",
        ["Quarter Kelly (safe)", "Half Kelly", "Full Kelly (aggressive)"],
        index=0,
    )
    kelly_mult = {
        "Quarter Kelly (safe)": 0.25,
        "Half Kelly": 0.5,
        "Full Kelly (aggressive)": 1.0,
    }[kelly_mode]

    st.divider()

    st.subheader("Refresh")
    refresh_rate = st.slider("Refresh (sec)", 5, 120, 20, help="5-10 for live games, 30+ for pregame")

    st.divider()

    st.subheader("Seeds (optional)")
    st.caption("Add seeds for historical upset context. One per line: DUKE=1")
    seed_input = st.text_area("Team seeds", placeholder="DUKE=1\nTCU=8\nHOWARD=16\nMICH=1", height=100)

    team_seeds = {}
    if seed_input:
        for line in seed_input.strip().split("\n"):
            if "=" in line:
                parts = line.split("=")
                try:
                    team_seeds[parts[0].strip().upper()] = int(parts[1].strip())
                except Exception:
                    pass
    if team_seeds:
        st.success("Loaded " + str(len(team_seeds)) + " seeds")

    st.divider()

    st.markdown("""
**At the sportsbook:**
- Green verdict = go look at the board now
- Yellow verdict = only if the price is right
- Gray verdict = skip
- **Target** = minimum odds to bet
- **Kelly $** = how much to wager
- Board shows >= target = **bet it**
""")

if not api_key:
    st.warning("Enter your Kalshi API key in the sidebar to start.")
    st.stop()

# ── Main ───────────────────────────────────────────────────────────────────

with st.spinner("Pulling Kalshi NCAA markets…"):
    markets = fetch_ncaa_markets(api_key)

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
                        pd.DataFrame([
                            {"ticker": e.get("event_ticker"), "title": e.get("title")}
                            for e in events
                        ]),
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
            r = requests.get(
                BASE_URL + "/markets",
                params={"series_ticker": "KXNCAAMBGAME", "limit": 200},
                headers=kalshi_headers(api_key), timeout=8,
            )
            st.write("Status: " + str(r.status_code))
            data = r.json()
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
                r = requests.get(
                    BASE_URL + "/events/" + manual_event.upper(),
                    params={"with_nested_markets": "true"},
                    headers=kalshi_headers(api_key), timeout=8,
                )
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
        title = (m.get("title", "") + m.get("subtitle", "")).lower()
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
        fav_m = compute_spread_metrics(fav)
        dog_m = compute_spread_metrics(dog)

        fav_name = fav.get("yes_sub_title") or fav.get("subtitle") or fav.get("ticker", "").split("-")[-1]
        dog_name = dog.get("yes_sub_title") or dog.get("subtitle") or dog.get("ticker", "").split("-")[-1]

        if fav_name == dog_name:
            fav_name = fav.get("ticker", "").split("-")[-1]
            dog_name = dog.get("ticker", "").split("-")[-1]

        fav_vol = float(fav.get("volume_fp", 0) or 0)
        dog_vol = float(dog.get("volume_fp", 0) or 0)
        total_vol = fav_vol + dog_vol

        spread = fav_m.get("spread")

        fav_mid = fav_m.get("mid", 0)
        dog_mid = dog_m.get("mid", 0)

        game_title = fav.get("title", event_ticker).replace(" Winner?", "").replace(" winner?", "").strip()

        a = analyze_game(
            fav_mid, dog_mid, spread, total_vol,
            fav_name, dog_name, bankroll, kelly_mult, team_seeds,
        )

        # Fetch order book for the favorite market to compute conviction
        ob = fetch_orderbook(api_key, fav.get("ticker", ""))
        conv = compute_book_conviction(ob, fav_mid, dog_mid, team_seeds, fav_name, dog_name)

        analyses.append({
            "event_ticker": event_ticker, "title": game_title,
            "fav_name": fav_name, "dog_name": dog_name,
            "fav_mid": fav_mid, "dog_mid": dog_mid,
            "fav_m": fav_m, "dog_m": dog_m,
            "spread": spread, "vol": total_vol,
            "a": a,
            "quality": market_quality(spread, total_vol),
            "conviction": conv,
        })

    # Sort: actionable first (green > yellow > gray)
    def sort_key(g):
        v = g["a"]["verdict"]
        if "DOG VALUE" in v or "LIVE BET" in v:
            return 0
        if "TOSS" in v:
            return 1
        if "PRICE" in v or "CHALK" in v or "LOW CONF" in v:
            return 2
        return 3

    analyses.sort(key=sort_key)

    n_action = sum(1 for g in analyses if g["a"]["color"] in ("#2ecc71", "#3498db"))
    st.markdown("### " + str(len(analyses)) + " Games | " + str(n_action) + " Actionable")

    # Render cards
    for g in analyses:
        a = g["a"]
        fm = g["fav_mid"]
        dm = g["dog_mid"]
        ql, qi, qs = g["quality"]
        conv = g["conviction"]

        fav_odds = prob_to_american(fm)
        dog_odds = prob_to_american(dm)

        ret_fav = prob_to_american(estimate_retail_implied(fm))
        ret_dog = prob_to_american(estimate_retail_implied(dm))

        seed_html = ""
        if a.get("seed_note"):
            seed_html = '<div style="font-size:0.82em;color:#aaa;padding:4px 0;">' + a["seed_note"] + '</div>'

        spread_str = str(round(g["spread"], 1)) + " cents" if g["spread"] else "n/a"
        vig_str = str(round(g["fav_m"].get("vig", 0), 1)) + " cents" if g["fav_m"].get("vig") else "n/a"
        vol_str = "{:,.0f}".format(g["vol"])

        # ── LEFT: Original game card ──
        card_html = (
            '<div class="game-card">'
            + '<div style="font-size:1.05em;font-weight:600;color:#ddd;">'
            + g["title"]
            + '<span style="float:right;font-size:0.8em;">' + ql + '</span>'
            + '</div>'
            + '<div class="v-line" style="color:' + a["color"] + ';">' + a["verdict"] + '</div>'
            + '<div style="display:flex;gap:12px;margin-bottom:10px;">'
            + '<div class="sbox">'
            + '<div class="slbl">FAVORITE</div>'
            + '<div class="sname">' + g["fav_name"] + '</div>'
            + '<div class="sprice" style="color:#4fc3f7;">' + str(int(fm)) + ' cents</div>'
            + '<div class="sodds">Fair: ' + fav_odds + '</div>'
            + '<div class="sodds">Retail est: ' + ret_fav + '</div>'
            + '<div class="sodds" style="color:#4fc3f7;">Target: ' + a["fav_target"] + '</div>'
            + '</div>'
            + '<div class="sbox">'
            + '<div class="slbl">UNDERDOG</div>'
            + '<div class="sname">' + g["dog_name"] + '</div>'
            + '<div class="sprice" style="color:#ff8a65;">' + str(int(dm)) + ' cents</div>'
            + '<div class="sodds">Fair: ' + dog_odds + '</div>'
            + '<div class="sodds">Retail est: ' + ret_dog + '</div>'
            + '<div class="sodds" style="color:#ff8a65;">Target: ' + a["dog_target"] + '</div>'
            + '</div>'
            + '<div class="sbox">'
            + '<div class="slbl">MARKET</div>'
            + '<div style="font-size:0.85em;color:#ccc;margin-top:6px;">'
            + 'Spread: ' + spread_str + '<br>'
            + 'Vig: ' + vig_str + '<br>'
            + 'Vol: ' + vol_str
            + '</div></div></div>'
            + seed_html
            + '<div style="margin-bottom:8px;">'
            + '<span class="kb">Kelly ' + g["fav_name"] + ': $' + str(int(a["kelly_fav_dollars"])) + '</span>'
            + '<span class="kb">Kelly ' + g["dog_name"] + ': $' + str(int(a["kelly_dog_dollars"])) + '</span>'
            + '</div>'
            + '<div class="det-box">' + a["detail"] + '</div>'
            + '<div class="act-box" style="border-left-color:' + a["color"] + ';">'
            + a["action_line"]
            + '</div></div>'
        )

        # ── RIGHT: Book Conviction card ──
        bir_pct = int(conv["bir"] * 100)
        ent_pct = int(conv["entropy"] * 100)
        depth_pct = int(conv["depth_grad"] * 100)
        conf_pct = int(conv["confidence"] * 100)

        # BIR bar: green if dog-leaning, blue if fav-leaning
        bir_bar_color = "#ff8a65" if conv["bir"] < 0.47 else ("#4fc3f7" if conv["bir"] > 0.53 else "#888")
        # Entropy bar: red = chaos, green = consensus
        ent_bar_color = "#e74c3c" if conv["entropy"] > 0.65 else ("#2ecc71" if conv["entropy"] < 0.35 else "#f39c12")
        # Depth bar: red = fragile, green = deep
        depth_bar_color = "#e74c3c" if conv["depth_grad"] > 0.6 else ("#2ecc71" if conv["depth_grad"] < 0.35 else "#f39c12")

        conv_html = (
            '<div class="conv-card">'
            + '<div style="font-size:0.72em;color:#aaa;text-transform:uppercase;letter-spacing:0.5px;">Book Conviction</div>'
            + '<div class="conv-label" style="color:' + conv["color"] + ';">' + conv["label"] + '</div>'

            # Posterior big number
            + '<div class="conv-posterior" style="color:' + conv["color"] + ';">'
            + str(int(round(conv["posterior"]))) + '%</div>'
            + '<div class="conv-posterior-sub">adjusted dog probability'
            + (' (prior: ' + str(int(conv["prior"])) + '% from ' + conv.get("prior_source", "mid") + ')'
               if conv.get("prior_source") else '')
            + '</div>'

            # Signal meters
            + '<div class="conv-meter">'
            + '<div style="display:flex;justify-content:space-between;">'
            + '<span>Book Imbalance</span>'
            + '<span style="color:' + bir_bar_color + ';">'
            + (str(100 - bir_pct) + '% dog' if conv["bir"] < 0.47 else (str(bir_pct) + '% fav' if conv["bir"] > 0.53 else 'even'))
            + '</span></div>'
            + '<div class="conv-meter-bar" style="background:linear-gradient(to right, #ff8a65 ' + str(100 - bir_pct) + '%, #333 ' + str(100 - bir_pct) + '% ' + str(bir_pct) + '%, #4fc3f7 ' + str(bir_pct) + '%);"></div>'
            + '</div>'

            + '<div class="conv-meter">'
            + '<div style="display:flex;justify-content:space-between;">'
            + '<span>Entropy (agreement)</span>'
            + '<span style="color:' + ent_bar_color + ';">'
            + ('consensus' if conv["entropy"] < 0.35 else ('moderate' if conv["entropy"] < 0.65 else 'chaos'))
            + '</span></div>'
            + '<div class="conv-meter-bar" style="background:' + ent_bar_color + '; width:' + str(max(5, 100 - ent_pct)) + '%;"></div>'
            + '</div>'

            + '<div class="conv-meter">'
            + '<div style="display:flex;justify-content:space-between;">'
            + '<span>Book Depth</span>'
            + '<span style="color:' + depth_bar_color + ';">'
            + ('deep' if conv["depth_grad"] < 0.35 else ('moderate' if conv["depth_grad"] < 0.6 else 'fragile'))
            + '</span></div>'
            + '<div class="conv-meter-bar" style="background:' + depth_bar_color + '; width:' + str(max(5, 100 - depth_pct)) + '%;"></div>'
            + '</div>'

            + '<div class="conv-meter">'
            + '<div style="display:flex;justify-content:space-between;">'
            + '<span>Confidence</span>'
            + '<span>' + str(conf_pct) + '%</span></div>'
            + '<div class="conv-meter-bar" style="background:' + conv["color"] + '; width:' + str(max(5, conf_pct)) + '%;"></div>'
            + '</div>'

            # Narrative
            + '<div class="conv-narrative">' + conv["narrative"] + '</div>'

            # Action
            + '<div class="conv-action" style="border-left-color:' + conv["color"] + ';">'
            + conv.get("action", "No read.")
            + '</div>'
            + '</div>'
        )

        # ── Render side by side ──
        col_left, col_right = st.columns([3, 2])
        with col_left:
            st.markdown(card_html, unsafe_allow_html=True)
        with col_right:
            st.markdown(conv_html, unsafe_allow_html=True)


    # Order book viewer
    with st.expander("Order Book Depth"):
        ticker_opts = {}
        for g in analyses:
            for m in games[g["event_ticker"]]:
                label = g["title"] + " - " + m.get("yes_sub_title", m.get("ticker", ""))
                ticker_opts[label] = m["ticker"]

        if ticker_opts:
            sel = st.selectbox("Select market", list(ticker_opts.keys()))
            ob = fetch_orderbook(api_key, ticker_opts[sel])
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
            a = g["a"]
            conv = g["conviction"]
            rows.append({
                "Game": g["title"],
                "Verdict": a["verdict"],
                "Book Read": conv["label"],
                "Dog Post.": str(int(round(conv["posterior"]))) + "%",
                "Confidence": str(int(conv["confidence"] * 100)) + "%",
                "Fav": g["fav_name"],
                "Fav cents": str(int(g["fav_mid"])),
                "Fav Fair": prob_to_american(g["fav_mid"]),
                "Fav Target": a["fav_target"],
                "Dog": g["dog_name"],
                "Dog cents": str(int(g["dog_mid"])),
                "Dog Fair": prob_to_american(g["dog_mid"]),
                "Dog Target": a["dog_target"],
                "Quality": g["quality"][0],
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
