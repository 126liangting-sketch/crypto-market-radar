import os
import csv
import json
import math
import time
import hashlib
import re
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import requests
import xml.etree.ElementTree as ET

OKX_BASE = "https://www.okx.com"
OKX_SYMBOL = "BTC-USDT-SWAP"
WEBHOOK = os.getenv("DISCORD_WEBHOOK")
MANUAL_RUN = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

VERSION = "LASER_SIGNAL_4_1_STRUCTURE_FIRST"
BUILD = "LASER_SIGNAL_4_1_INTEGRATED_REPAIR"
STATE_FILE = "radar_state_v31.json"
TRADES_JSON = "paper_trades_v31.json"
TRADES_CSV = "paper_trades_v31.csv"
BLOCKED_JSON = "blocked_setups_v31.json"
BLOCKED_CSV = "blocked_setups_v31.csv"
PREPARE_JSON = "prepare_validation_v31.json"
PREPARE_CSV = "prepare_validation_v31.csv"
SETUPS_JSON = "setup_lifecycle_v31.json"
SETUPS_CSV = "setup_lifecycle_v31.csv"

# ---- Laser Signal 4.1: structure-first formal entries ----
EMA_FAST = 34
EMA_SLOW = 50
VOL_LOOKBACK = 20
# Version 4.0: participation depends on the entry structure, not on a
# separate low-volume/rebound experiment. These are *research* thresholds.
VOL_PULLBACK_MIN = 0.55
VOL_BREAKOUT_MIN = 0.85
PULLBACK_EMA_DISTANCE_MAX_ATR = 1.00
PULLBACK_MIN_ATR = 0.25
IMPULSE_MIN_ATR = 0.60
BREAK_BUFFER_ATR = 0.05
BREAKOUT_BREAK_BUFFER_ATR = 0.01
BREAKOUT_ACCEPT_ATR = 0.02
BREAKOUT_ZONE_MERGE_ATR = 0.18
BREAKOUT_ZONE_LOOKBACK = 48
BREAKOUT_ZONE_MAX_AHEAD_ATR = 1.10
BREAKOUT_ZONE_MAX_BEHIND_ATR = 0.65
BREAKOUT_ZONE_RETIRE_BARS = 3
PREPARE_NOTIFY_VOL_MIN = 0.55
PREPARE_DISTANCE_ATR = 0.25
EXT_NORMAL_ATR = 0.30
EXT_WARN_ATR = 0.50
EXT_WAIT_ATR = 0.75
SPACE_MIN_R = 1.00
SPACE_GOOD_R = 1.50
STOP_BUFFER_ATR = 0.30
RANGE_EXTRA_BUFFER_ATR = 0.10
MIN_STOP_ATR = 0.85
RISK_WARN_ATR = 1.75
RISK_HIGH_ATR = 2.50
MAX_STOP_ATR = 3.25
EMA_DEFENSE_MAX_GAP_ATR = 1.25
PULLBACK_EMA_DEFENSE_MAX_GAP_ATR = 1.75
POSITION_RISK_BASE_ATR = 1.50
PREPARE_VALIDATE_MIN = 60
TRIGGER_RETEST_ATR = 0.10
EVENT_LOOKBACK_MIN = 12
TRIGGER_FRESH_MIN = 6
TRIGGER_AGING_MIN = 12
THESIS_HOLD_MIN = 20
ENTRY_IDEAL_MAX_ATR = 0.30
ENTRY_NORMAL_MAX_ATR = 0.50
ENTRY_EXTENDED_MAX_ATR = EXT_WAIT_ATR
DUPLICATE_THESIS_MIN = 90
DUPLICATE_TRIGGER_NEAR_ATR = 0.75
NEW_STRUCTURE_MIN = 15
REVERSAL_IMPULSE_MIN_ATR = 0.80
REVERSAL_RETRACE_MAX = 0.80
FEAS_TIGHT_R = 0.35
FEAS_CRITICAL_R = 0.18
OI_FLAT_PCT = 0.0010
CVD_FLAT_REL = 0.05
NEWS_NOTIFY_COOLDOWN = 12 * 3600
NEWS_MAX_AGE = 18 * 3600  # Only alert on recent published articles.
NEWS_SIMILAR_COOLDOWN = 24 * 3600

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]


def now_utc():
    return int(time.time())


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def get_json(url, params=None, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20,
                             headers={"User-Agent": "Laser-Signal-3.1/1.0"})
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            print(f"API 請求失敗 {i+1}/{retries}: {type(e).__name__}: {e}")
            if i < retries - 1:
                time.sleep(2 ** i)
    raise last


def okx_public(path, params=None):
    d = get_json(f"{OKX_BASE}{path}", params)
    if not isinstance(d, dict) or str(d.get("code", "0")) != "0":
        raise RuntimeError(f"OKX API error: {d}")
    return d.get("data", [])


def history_candles(bar, count=220):
    rows, after = [], None
    while len(rows) < count:
        limit = min(100, count - len(rows))
        p = {"instId": OKX_SYMBOL, "bar": bar, "limit": limit}
        if after:
            p["after"] = after
        batch = okx_public("/api/v5/market/history-candles", p)
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1][0]
        if len(batch) < limit:
            break
        time.sleep(0.03)

    out, seen = [], set()
    for x in rows:
        if len(x) < 6:
            continue
        ts = int(x[0]) // 1000
        if ts in seen:
            continue
        seen.add(ts)
        vol = num(x[6], num(x[5], 0.0)) if len(x) > 6 else num(x[5], 0.0)
        out.append({
            "time": ts,
            "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
            "close": float(x[4]), "volume": float(vol or 0.0),
            "confirm": str(x[8]) if len(x) > 8 else "1",
        })
    out.sort(key=lambda r: r["time"])
    out = [r for r in out if r.get("confirm", "1") == "1"] or out
    if not out:
        raise RuntimeError(f"OKX {bar} K線沒有資料")
    return out[-count:]


def current_15m_candle():
    data = okx_public("/api/v5/market/candles", {"instId": OKX_SYMBOL, "bar": "15m", "limit": 2})
    if not data:
        return None
    x = data[0]
    vol = num(x[6], num(x[5], 0.0)) if len(x) > 6 else num(x[5], 0.0)
    return {
        "time": int(x[0]) // 1000,
        "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
        "close": float(x[4]), "volume": float(vol or 0.0),
        "confirm": str(x[8]) if len(x) > 8 else "0",
    }



def recent_market_candles(bar="1m", count=20):
    """Recent candles including the currently forming bar. Used only for event detection.

    We do not persist the full 1m stream. This lets a 5-minute GitHub schedule detect
    a trigger/TP/SL that happened between runs without turning the repo into a tick store.
    """
    data = okx_public("/api/v5/market/candles", {"instId": OKX_SYMBOL, "bar": bar, "limit": count})
    out = []
    for x in data:
        if len(x) < 6:
            continue
        vol = num(x[6], num(x[5], 0.0)) if len(x) > 6 else num(x[5], 0.0)
        out.append({
            "time": int(x[0]) // 1000,
            "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
            "close": float(x[4]), "volume": float(vol or 0.0),
            "confirm": str(x[8]) if len(x) > 8 else "0",
        })
    out.sort(key=lambda r: r["time"])
    return out[-count:]


def event_window(state):
    """Return only the 1m bars that cover the gap since the previous scheduled run."""
    try:
        rows = recent_market_candles("1m", EVENT_LOOKBACK_MIN)
    except Exception as e:
        print("1m event window unavailable:", e)
        return []
    if not rows:
        return []
    last_scan = int(state.get("last_event_scan", 0) or 0)
    if last_scan:
        use = [r for r in rows if int(r["time"]) >= last_scan - 60]
    else:
        use = rows[-6:]
    state["last_event_scan"] = max(int(r["time"]) for r in rows)
    return use or rows[-2:]


def post_entry_event_bars(event_rows, opened_at):
    """Only use 1m bars whose entire interval begins at/after entry.

    A 1m OHLC bar that contains the entry timestamp includes pre-entry
    highs/lows. Without tick data we cannot safely resolve its sequence.
    This conservatively omits that partial bar, including possible exits.
    """
    opened_at = int(opened_at)
    first_safe_minute = ((opened_at + 59) // 60) * 60
    return [r for r in (event_rows or [])
            if int(r.get("time", 0)) >= first_safe_minute]


def window_high_low(rows, fallback):
    if not rows:
        return float(fallback), float(fallback)
    return max(float(r["high"]) for r in rows), min(float(r["low"]) for r in rows)

def ticker_price():
    d = okx_public("/api/v5/market/ticker", {"instId": OKX_SYMBOL})
    if not d:
        raise RuntimeError("OKX ticker unavailable")
    return float(d[0]["last"])


def ema_series(values, length):
    if len(values) < length:
        return []
    k = 2 / (length + 1)
    seed = sum(values[:length]) / length
    out = [None] * (length - 1) + [seed]
    v = seed
    for x in values[length:]:
        v = x * k + v * (1 - k)
        out.append(v)
    return out


def ema(values, length):
    s = ema_series(values, length)
    return s[-1] if s else None


def true_range(row, prev_close):
    h, l = float(row["high"]), float(row["low"])
    return max(h - l, abs(h - prev_close), abs(l - prev_close))


def atr(rows, length=14):
    if len(rows) < length + 1:
        return None
    trs = [true_range(rows[i], float(rows[i-1]["close"])) for i in range(1, len(rows))]
    return sum(trs[-length:]) / length


def local_pivots(rows, left=1, right=1):
    highs, lows = [], []
    for i in range(left, len(rows) - right):
        h, l = float(rows[i]["high"]), float(rows[i]["low"])
        if all(h > float(rows[j]["high"]) for j in range(i-left, i)) and \
           all(h >= float(rows[j]["high"]) for j in range(i+1, i+1+right)):
            highs.append((int(rows[i]["time"]), h, i))
        if all(l < float(rows[j]["low"]) for j in range(i-left, i)) and \
           all(l <= float(rows[j]["low"]) for j in range(i+1, i+1+right)):
            lows.append((int(rows[i]["time"]), l, i))
    return highs, lows




def trend_background(rows1h):
    closes = [float(r["close"]) for r in rows1h]
    e34, e50 = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    a = atr(rows1h)
    if e34 is None or e50 is None or not a:
        return {"state": "NEUTRAL", "ema34": e34, "ema50": e50, "distance_atr": 0.0}
    d = abs(e34 - e50) / a
    if d < 0.10:
        state = "NEUTRAL"
    else:
        state = "BULL" if e34 > e50 else "BEAR"
    return {"state": state, "ema34": e34, "ema50": e50, "distance_atr": d}


def ema_position(rows15, price, a):
    closes = [float(r["close"]) for r in rows15]
    e20, e34, e50 = ema(closes, 20), ema(closes, 34), ema(closes, 50)
    zl, zh = sorted([e34, e50])
    if zl <= price <= zh:
        dist = 0.0
    else:
        dist = min(abs(price-zl), abs(price-zh)) / a
    if dist < 0.25:
        label = "位置佳"
    elif dist <= 0.50:
        label = "一般"
    else:
        label = "偏離"
    return {"ema20": e20, "ema34": e34, "ema50": e50,
            "zone_low": zl, "zone_high": zh, "distance_atr": dist, "label": label}


def volume_context(rows15, current_bar=None):
    if len(rows15) < VOL_LOOKBACK + 1:
        return {"ratio": 1.0, "closed_ratio": 1.0, "pace_ratio": None, "label": "正常"}
    prev = [float(x.get("volume", 0) or 0) for x in rows15[-VOL_LOOKBACK:]]
    avg = sum(prev) / len(prev) if prev else 0.0
    last_closed = float(rows15[-1].get("volume", 0) or 0)
    closed_ratio = last_closed / avg if avg else 1.0
    pace_ratio = None
    if current_bar and avg > 0:
        elapsed = max(180, min(900, now_utc() - int(current_bar["time"])))
        pace_ratio = (float(current_bar.get("volume", 0) or 0) / avg) * (900 / elapsed)
        pace_ratio = min(pace_ratio, 4.0)
    ratio = max(closed_ratio, pace_ratio or 0.0)
    if ratio < 0.70:
        label = "太低"
    elif ratio < 1.00:
        label = "偏弱"
    elif ratio < 1.30:
        label = "正常"
    elif ratio < 1.50:
        label = "強"
    else:
        label = "很強"
    return {"ratio": ratio, "closed_ratio": closed_ratio, "pace_ratio": pace_ratio, "label": label}


def regime(rows15, a):
    closes = [float(r["close"]) for r in rows15]
    e34s, e50s = ema_series(closes, 34), ema_series(closes, 50)
    if not e34s or not e50s:
        return {"state": "RANGE", "ema_separation_atr": 0.0, "crossings": 0, "s34": 0.0, "s50": 0.0}
    e34, e50 = e34s[-1], e50s[-1]
    sep = abs(e34-e50)/a if a else 0.0

    # Compare every recent close with the EMA values that existed on that same bar.
    # The old version compared old candles with today's EMA values, which could
    # misclassify trend/range after a strong move.
    start_i = max(0, len(rows15)-12)
    crossings = 0
    last_side = None
    for i in range(start_i, len(rows15)):
        if i >= len(e34s) or i >= len(e50s) or e34s[i] is None or e50s[i] is None:
            continue
        c = float(rows15[i]["close"])
        lo, hi = sorted((float(e34s[i]), float(e50s[i])))
        side = 1 if c > hi else -1 if c < lo else 0
        if last_side not in (None, 0) and side not in (0, last_side):
            crossings += 1
        if side != 0:
            last_side = side

    vals34 = [x for x in e34s if x is not None]
    vals50 = [x for x in e50s if x is not None]
    s34 = (vals34[-1] - vals34[-5]) / a if a and len(vals34) >= 5 else 0.0
    s50 = (vals50[-1] - vals50[-5]) / a if a and len(vals50) >= 5 else 0.0
    same_slope = (s34 > 0 and s50 > 0) or (s34 < 0 and s50 < 0)
    state = "TREND" if sep >= 0.15 and crossings <= 2 and same_slope else "RANGE"
    return {"state": state, "ema_separation_atr": sep, "crossings": crossings, "s34": s34, "s50": s50}


def detect_pullback_setup(rows15, a, side):
    """Generalized continuation detector: impulse -> correction -> hold -> re-launch.

    It intentionally does not require an EMA touch, a long wick, or a rigid multi-pivot
    template. A single local turn anchors the correction while impulse size, retracement
    and structure survival decide whether it is meaningful.
    """
    if len(rows15) < 16 or not a or a <= 0:
        return None
    n = len(rows15)
    candidates = []

    # Look only at fresh correction turns. One confirming bar is enough; we do not wait
    # for a 2-2 pivot, which was a major source of late/missed continuation signals.
    for i in range(max(5, n-12), n-1):
        prev_r, r, next_r = rows15[i-1], rows15[i], rows15[i+1]
        prior = rows15[max(0, i-8):i]
        if len(prior) < 4:
            continue

        if side == "LONG":
            is_turn = float(r["low"]) < float(prev_r["low"]) and float(r["low"]) <= float(next_r["low"])
            if not is_turn:
                continue
            start_row = min(prior, key=lambda x: float(x["low"]))
            start_idx = rows15.index(start_row)
            pre_impulse = rows15[start_idx:i]
            if not pre_impulse:
                continue
            peak_row = max(pre_impulse, key=lambda x: float(x["high"]))
            peak = float(peak_row["high"]); start_price = float(start_row["low"]); pb = float(r["low"])
            impulse = peak - start_price
            correction = peak - pb
            if impulse < IMPULSE_MIN_ATR*a or correction < PULLBACK_MIN_ATR*a:
                continue
            # Keep at least 15% of the impulse; a near-full retrace is no longer a healthy continuation.
            if pb <= start_price + 0.15*impulse:
                continue
            trigger = float(r["high"])
            if trigger <= pb:
                continue
            candidates.append({
                "side": side, "kind": "PULLBACK", "label": "回踩再啟動",
                "trigger": trigger, "trigger_time": int(r["time"]),
                "defense": pb, "defense_time": int(r["time"]),
                "impulse_start": start_price, "impulse_end": peak,
                "setup_key": f"CONT:L:{int(r['time'])}"
            })
        else:
            is_turn = float(r["high"]) > float(prev_r["high"]) and float(r["high"]) >= float(next_r["high"])
            if not is_turn:
                continue
            start_row = max(prior, key=lambda x: float(x["high"]))
            start_idx = rows15.index(start_row)
            pre_impulse = rows15[start_idx:i]
            if not pre_impulse:
                continue
            trough_row = min(pre_impulse, key=lambda x: float(x["low"]))
            trough = float(trough_row["low"]); start_price = float(start_row["high"]); pb = float(r["high"])
            impulse = start_price - trough
            correction = pb - trough
            if impulse < IMPULSE_MIN_ATR*a or correction < PULLBACK_MIN_ATR*a:
                continue
            if pb >= start_price - 0.15*impulse:
                continue
            trigger = float(r["low"])
            if trigger >= pb:
                continue
            candidates.append({
                "side": side, "kind": "PULLBACK", "label": "回踩再啟動",
                "trigger": trigger, "trigger_time": int(r["time"]),
                "defense": pb, "defense_time": int(r["time"]),
                "impulse_start": start_price, "impulse_end": trough,
                "setup_key": f"CONT:S:{int(r['time'])}"
            })

    return candidates[-1] if candidates else None


def _breakout_zone_candidates(rows15, a, side):
    """Build recent support/resistance zones from repeated 15M swing reactions.

    Breakout is a zone problem, not a single last-pivot problem.  Nearby pivots are
    clustered; repeated touches and meaningful reactions increase importance, while
    zones already accepted several bars ago are retired so old triggers do not linger.
    """
    if len(rows15) < 12 or not a or a <= 0:
        return []
    highs, lows = local_pivots(rows15, 1, 1)
    pivots = highs if side == "LONG" else lows
    start_i = max(0, len(rows15) - BREAKOUT_ZONE_LOOKBACK)
    pivots = [x for x in pivots if x[2] >= start_i]
    if not pivots:
        return []

    groups = []
    for pt in sorted(pivots, key=lambda x: float(x[1])):
        price = float(pt[1])
        if not groups or abs(price - groups[-1]["center"]) > BREAKOUT_ZONE_MERGE_ATR * a:
            groups.append({"pivots": [pt], "center": price})
        else:
            groups[-1]["pivots"].append(pt)
            groups[-1]["center"] = sum(float(x[1]) for x in groups[-1]["pivots"]) / len(groups[-1]["pivots"])

    out = []
    closes = [float(r["close"]) for r in rows15]
    for g in groups:
        pts = g["pivots"]
        prices = [float(x[1]) for x in pts]
        last_pt = max(pts, key=lambda x: x[2])
        last_i = int(last_pt[2])
        # Resistance breaks at the upper edge; support breaks at the lower edge.
        trigger = max(prices) if side == "LONG" else min(prices)
        zone_low, zone_high = min(prices), max(prices)

        # Reaction after each touch: a useful boundary must have pushed price away.
        reactions = []
        for _, pp, ii in pts:
            future = rows15[ii+1:min(len(rows15), ii+6)]
            if not future:
                continue
            if side == "LONG":
                reactions.append((float(pp) - min(float(r["low"]) for r in future)) / a)
            else:
                reactions.append((max(float(r["high"]) for r in future) - float(pp)) / a)
        reaction = max(reactions) if reactions else 0.0
        if reaction < 0.25 and len(pts) < 2:
            continue

        # Retire a boundary only after multiple confirmed 15M closes have accepted
        # the other side. This keeps a fresh break actionable but removes stale levels.
        recent = closes[-BREAKOUT_ZONE_RETIRE_BARS:]
        if len(recent) >= BREAKOUT_ZONE_RETIRE_BARS:
            if side == "LONG" and all(c > trigger + BREAKOUT_ACCEPT_ATR*a for c in recent):
                continue
            if side == "SHORT" and all(c < trigger - BREAKOUT_ACCEPT_ATR*a for c in recent):
                continue

        recency = max(0.0, 1.0 - (len(rows15)-1-last_i)/BREAKOUT_ZONE_LOOKBACK)
        score = min(3, len(pts)) * 1.0 + min(2.0, reaction) + recency
        out.append({
            "trigger": float(trigger), "zone_low": float(zone_low), "zone_high": float(zone_high),
            "touches": len(pts), "reaction_atr": float(reaction), "score": float(score),
            "anchor_time": int(min(x[0] for x in pts)), "last_touch_time": int(last_pt[0]),
            "last_touch_index": last_i,
        })
    return out


def detect_breakout_setup(rows15, a, side, live_price=None):
    """Dynamic structure breakout using the currently relevant boundary zone.

    The old implementation always used the last significant pivot. That could leave
    an obsolete trigger active or miss a newer range edge. Here repeated swing zones
    compete by relevance, recency and reaction strength, and accepted old zones retire.
    """
    live_price = float(live_price if live_price is not None else rows15[-1]["close"])
    zones = _breakout_zone_candidates(rows15, a, side)
    if not zones:
        return None

    eligible = []
    for z in zones:
        rel = (float(z["trigger"]) - live_price) / a if side == "LONG" else (live_price - float(z["trigger"])) / a
        # Allow an approaching boundary and a freshly crossed boundary, but do not
        # resurrect a level far behind current price or target a remote level.
        if -BREAKOUT_ZONE_MAX_BEHIND_ATR <= rel <= BREAKOUT_ZONE_MAX_AHEAD_ATR:
            zz = dict(z); zz["relative_atr"] = rel
            eligible.append(zz)
    if not eligible:
        return None

    # Prefer the boundary that is actually in play now. Inside the active band,
    # stronger/repeated zones win; remote zones cannot hijack a nearer valid break.
    def _zone_key(z):
        rel = float(z["relative_atr"])
        active_band = 2 if -0.35 <= rel <= 0.55 else 1
        return (active_band, z["score"], -abs(rel), z["last_touch_time"])
    eligible.sort(key=_zone_key, reverse=True)
    z = eligible[0]
    trigger = float(z["trigger"])

    local_highs, local_lows = local_pivots(rows15, 1, 1)
    if side == "LONG":
        prior = [x for x in local_lows if x[0] <= z["last_touch_time"]]
        if not prior:
            return None
        defense = prior[-1]
        key = f"BOZ:L:{z['anchor_time']}:{z['last_touch_time']}:{round(trigger)}"
    else:
        prior = [x for x in local_highs if x[0] <= z["last_touch_time"]]
        if not prior:
            return None
        defense = prior[-1]
        key = f"BOZ:S:{z['anchor_time']}:{z['last_touch_time']}:{round(trigger)}"

    return {
        "side": side, "kind": "BREAKOUT", "label": "結構突破",
        "trigger": trigger, "trigger_time": int(z["last_touch_time"]),
        "defense": float(defense[1]), "defense_time": int(defense[0]),
        "zone_low": z["zone_low"], "zone_high": z["zone_high"],
        "zone_touches": int(z["touches"]), "zone_reaction_atr": float(z["reaction_atr"]),
        "breakout_stage": "APPROACH" if z["relative_atr"] >= 0 else "BREAK",
        "setup_key": key,
    }


def breakout_acceptance(side, setup, price, a, event_rows=None):
    """Separate a real structure break from a wick-only probe."""
    trigger = float(setup["trigger"])
    rows = event_rows or []
    if side == "LONG":
        wick_break = any(float(r["high"]) >= trigger + BREAKOUT_BREAK_BUFFER_ATR*a for r in rows) or price >= trigger + BREAKOUT_BREAK_BUFFER_ATR*a
        accepted = any(float(r["close"]) >= trigger + BREAKOUT_ACCEPT_ATR*a for r in rows) or price >= trigger + BREAKOUT_ACCEPT_ATR*a
        failed = price < trigger - TRIGGER_RETEST_ATR*a
    else:
        wick_break = any(float(r["low"]) <= trigger - BREAKOUT_BREAK_BUFFER_ATR*a for r in rows) or price <= trigger - BREAKOUT_BREAK_BUFFER_ATR*a
        accepted = any(float(r["close"]) <= trigger - BREAKOUT_ACCEPT_ATR*a for r in rows) or price <= trigger - BREAKOUT_ACCEPT_ATR*a
        failed = price > trigger + TRIGGER_RETEST_ATR*a
    return {"wick_break": wick_break, "accepted": accepted, "failed": failed,
            "stage": "ACCEPTANCE" if accepted else ("BREAK" if wick_break else "APPROACH")}



def collect_setups(rows15, a, side, live_price):
    """Only the supported trend-following pullback and breakout families."""
    candidates = []
    detectors = (
        detect_pullback_setup(rows15, a, side),
        detect_breakout_setup(rows15, a, side, live_price),
    )
    for setup in detectors:
        if not setup:
            continue
        item = dict(setup)
        item["extension_atr"] = ((live_price - item["trigger"]) / a) if side == "LONG" else ((item["trigger"] - live_price) / a)
        candidates.append(item)
    return candidates



def extension_quality(ext):
    if ext <= EXT_NORMAL_ATR:
        return "正常"
    if ext <= EXT_WARN_ATR:
        return "稍微延伸"
    if ext <= EXT_WAIT_ATR:
        return "偏追價"
    return "過度延伸"



def duplicate_thesis_guard(state, ev, a):
    """Suppress repeated same-wave entries while allowing genuinely new structure.

    A candidate is treated as a duplicate only when it appears soon after the last
    same-direction Formal, its trigger remains near the prior trigger, and it has not
    formed at least one new 15m structure after that trade opened. A very strong
    FRESH setup may override the suppression.
    """
    if not ev or not ev.get("ready") or a <= 0:
        return {"allow": True, "reason": None}
    side = ev.get("side")
    setup = ev.get("setup") or {}
    now = now_utc()
    prior = [x for x in (state.get("history", []) + state.get("trades", []))
             if x.get("side") == side and not x.get("excluded_from_stats") and x.get("opened_at")]
    if not prior:
        return {"allow": True, "reason": None}
    last = max(prior, key=lambda x: int(x.get("opened_at", 0)))
    age_min = (now - int(last.get("opened_at", 0))) / 60.0
    if age_min > DUPLICATE_THESIS_MIN:
        return {"allow": True, "reason": None}

    new_structure = int(setup.get("trigger_time", 0)) >= int(last.get("opened_at", 0)) + NEW_STRUCTURE_MIN * 60
    trigger_gap = abs(float(setup.get("trigger", 0.0)) - float(last.get("trigger", 0.0))) / a
    same_wave = (not new_structure) and trigger_gap <= DUPLICATE_TRIGGER_NEAR_ATR
    if not same_wave:
        return {"allow": True, "reason": None}

    timing = ev.get("entry_timing") or {}
    ctx = ev.get("context") or {}
    fresh = ev.get("freshness") or {}
    geom = ev.get("geometry") or {}
    vr = float((ev.get("volume") or {}).get("ratio") or 0.0)
    strong_reentry = (
        timing.get("class") == "IDEAL" and fresh.get("state") == "FRESH" and
        ctx.get("class") == "CONSISTENT" and geom.get("class") != "POOR" and vr >= 1.50
    )
    if strong_reentry:
        return {"allow": True, "reason": "同波段但出現新的高品質理想再啟動", "override": True}
    return {
        "allow": False,
        "reason": f"同方向重複 thesis：距前一張 #{last.get('id')} 僅 {age_min:.0f} 分，尚未形成新的獨立結構",
        "prior_trade_id": last.get("id"),
    }


def risk_quality(risk_atr):
    if risk_atr <= RISK_WARN_ATR:
        return "正常"
    if risk_atr <= RISK_HIGH_ATR:
        return "偏大"
    if risk_atr <= MAX_STOP_ATR:
        return "高風險"
    return "過大"


def risk_plan(setup, side, entry, a, ema_pos=None, reg=None):
    """Adaptive defensive stop for Laser Signal 3.1.

    Structure decides where the trade thesis is invalid. EMA34/50 is used as a
    secondary defensive reference only when it is reasonably close, and ATR adds
    breathing room outside that invalidation area. The minimum distance prevents a
    normal 15m candle from becoming an automatic stop. Position size is then scaled
    down as the stop gets wider so wider breathing room does not imply wider money risk.
    """
    if a <= 0:
        return None
    ema_pos = ema_pos or {}
    regime = (reg or {}).get("state", "TREND")
    kind = setup.get("kind", "PULLBACK")
    structure = float(setup["defense"])

    # Pullbacks benefit more from the EMA34/50 zone as a thesis-defense reference.
    # Breakouts primarily defend the breakout base; EMA is allowed to reinforce it
    # only when the zone is not far behind price. This avoids enormous stale stops.
    if side == "LONG":
        ema_anchor = ema_pos.get("zone_low")
        anchors = [("結構", structure)]
        if ema_anchor is not None:
            ema_anchor = float(ema_anchor)
            gap = max(0.0, (entry - ema_anchor) / a)
            ema_gap_cap = PULLBACK_EMA_DEFENSE_MAX_GAP_ATR if kind == "PULLBACK" else EMA_DEFENSE_MAX_GAP_ATR
            if gap <= ema_gap_cap:
                anchors.append(("EMA區域", ema_anchor))
        anchor_label, anchor = min(anchors, key=lambda x: x[1])
    else:
        ema_anchor = ema_pos.get("zone_high")
        anchors = [("結構", structure)]
        if ema_anchor is not None:
            ema_anchor = float(ema_anchor)
            gap = max(0.0, (ema_anchor - entry) / a)
            ema_gap_cap = PULLBACK_EMA_DEFENSE_MAX_GAP_ATR if kind == "PULLBACK" else EMA_DEFENSE_MAX_GAP_ATR
            if gap <= ema_gap_cap:
                anchors.append(("EMA區域", ema_anchor))
        anchor_label, anchor = max(anchors, key=lambda x: x[1])

    buffer_atr = STOP_BUFFER_ATR + (RANGE_EXTRA_BUFFER_ATR if regime == "RANGE" else 0.0)
    # Pullback structures often need slightly more wick room than clean breakout bases.
    if kind in ("PULLBACK", "REVERSAL"):
        buffer_atr += 0.05

    if side == "LONG":
        raw_sl = anchor - buffer_atr * a
        min_sl = entry - MIN_STOP_ATR * a
        sl = min(raw_sl, min_sl)
        risk = entry - sl
        tp1, tp2 = entry + risk, entry + 2*risk
    else:
        raw_sl = anchor + buffer_atr * a
        min_sl = entry + MIN_STOP_ATR * a
        sl = max(raw_sl, min_sl)
        risk = sl - entry
        tp1, tp2 = entry - risk, entry - 2*risk

    if risk <= 0:
        return None
    risk_atr = risk / a
    size_factor = min(1.0, POSITION_RISK_BASE_ATR / risk_atr) if risk_atr > 0 else 1.0
    return {
        "entry": entry, "sl": sl, "risk": risk, "risk_atr": risk_atr,
        "tp1": tp1, "tp2": tp2, "side": side, "tp1_r": 1.0, "tp2_r": 2.0,
        "defense_anchor": anchor, "defense_source": anchor_label,
        "stop_buffer_atr": buffer_atr, "position_size_factor": size_factor,
    }


def _pivot_reaction_atr(rows15, pivot, side, a):
    """How strongly price reacted after this pivot, measured in ATR.

    Space is treated as a zone problem, not a single-line problem. A pivot that
    produced almost no reaction should not become a hard wall by itself.
    """
    _, price, idx = pivot
    if a <= 0:
        return 0.0
    right = rows15[idx + 1:min(len(rows15), idx + 6)]
    if not right:
        return 0.0
    if side == "LONG":  # prior swing high acting as possible resistance
        excursion = price - min(float(x["low"]) for x in right)
    else:                # prior swing low acting as possible support
        excursion = max(float(x["high"]) for x in right) - price
    return max(0.0, excursion / a)


def _structure_zones(rows15, pivots, side, a):
    """Merge nearby pivots into structural zones and grade their importance.

    This avoids treating every tiny 15m swing as an equally important barrier.
    Zone grades:
      STRONG  -> can hard-block a Formal when it is inside 1R
      NORMAL  -> quality information only; never blocks by itself
      WEAK    -> informational only
    """
    if not pivots:
        return []
    zone_width = max(1e-9, 0.20 * a)
    pts = sorted(pivots, key=lambda x: float(x[1]))
    groups = []
    for pt in pts:
        price = float(pt[1])
        if not groups or abs(price - groups[-1]["center"]) > zone_width:
            groups.append({"pivots": [pt], "center": price})
        else:
            groups[-1]["pivots"].append(pt)
            groups[-1]["center"] = sum(float(x[1]) for x in groups[-1]["pivots"]) / len(groups[-1]["pivots"])

    zones = []
    for g in groups:
        reactions = [_pivot_reaction_atr(rows15, pt, side, a) for pt in g["pivots"]]
        max_reaction = max(reactions) if reactions else 0.0
        touches = len(g["pivots"])

        # Strong needs either a genuinely large rejection, or repeated tests plus
        # a meaningful reaction. Normal zones remain visible but do not hard-block.
        if max_reaction >= 0.75 or (touches >= 3 and max_reaction >= 0.30):
            strength = "STRONG"
        elif max_reaction >= 0.30 or touches >= 2:
            strength = "NORMAL"
        else:
            strength = "WEAK"

        prices = [float(x[1]) for x in g["pivots"]]
        # Use the first edge price would actually meet, not the zone midpoint.
        edge = min(prices) if side == "LONG" else max(prices)
        zones.append({
            "price": edge,
            "center": g["center"],
            "strength": strength,
            "touches": touches,
            "reaction_atr": max_reaction,
        })
    return zones


def space_context(rows15, side, entry, risk, trigger_time, a):
    """Describe forward structural space for exit planning and message context.

    Space is deliberately NOT an entry veto. Structural zones are still graded so
    the radar can shorten/omit ambitious targets and warn about nearby obstacles,
    while Formal entry remains controlled by trigger, participation, risk and chase.
    """
    highs, lows = local_pivots(rows15, 2, 2)
    pivots = highs if side == "LONG" else lows
    candidates = []
    for pivot in pivots:
        t, p, _ = pivot
        if t == trigger_time:
            continue
        if side == "LONG" and p <= entry:
            continue
        if side == "SHORT" and p >= entry:
            continue
        candidates.append(pivot)

    zones = _structure_zones(rows15, candidates, side, a)
    if side == "LONG":
        zones.sort(key=lambda z: z["price"])
    else:
        zones.sort(key=lambda z: z["price"], reverse=True)

    nearest = zones[0] if zones else None
    strong = next((z for z in zones if z["strength"] == "STRONG"), None)

    def r_to(z):
        if z is None or risk <= 0:
            return None
        return ((z["price"] - entry) / risk) if side == "LONG" else ((entry - z["price"]) / risk)

    nearest_r = r_to(nearest)
    strong_r = r_to(strong)
    # Space no longer blocks Formal. Keep the old field for state/backward
    # compatibility, but it is always False in the exit-management design.
    hard_block = False

    if nearest is None:
        label = "開放"
    elif nearest["strength"] == "STRONG":
        label = "強結構區・目標需保守" if nearest_r is not None and nearest_r < 1.0 else "強結構區"
    elif nearest["strength"] == "NORMAL":
        label = "一般結構區（不硬擋）"
    else:
        label = "弱結構（不硬擋）"

    # space_r remains the distance to the nearest STRONG obstacle for historical
    # comparability. It controls exit ambition/message context, never Formal entry.
    return {
        "target": strong["price"] if strong else None,
        "space_r": strong_r,
        "label": label,
        "hard_block": hard_block,
        "nearest_target": nearest["price"] if nearest else None,
        "nearest_r": nearest_r,
        "nearest_strength": nearest["strength"] if nearest else "OPEN",
        "reaction_atr": nearest["reaction_atr"] if nearest else None,
        "touches": nearest["touches"] if nearest else 0,
    }

def update_oi(state):
    hist = state.setdefault("oi_history", [])
    try:
        d = okx_public("/api/v5/public/open-interest", {"instType": "SWAP", "instId": OKX_SYMBOL})
        if d:
            r = d[0]
            snap = {"time": int(r.get("ts", now_utc()*1000))//1000,
                    "value": float(r.get("oiCcy") or r.get("oi"))}
            if not hist or abs(hist[-1]["time"] - snap["time"]) > 60:
                hist.append(snap)
            else:
                hist[-1] = snap
    except Exception as e:
        print("OI unavailable:", e)
    cutoff = now_utc() - 7*86400
    hist[:] = [x for x in hist if int(x["time"]) >= cutoff][-1200:]


def nearest_old(hist, target):
    xs = [x for x in hist if int(x["time"]) <= target]
    return xs[-1] if xs else None


def oi_context(state):
    hist = state.get("oi_history", [])
    if not hist:
        return {"dir": "UNAVAILABLE", "pct": None}
    cur = hist[-1]
    old = nearest_old(hist, int(cur["time"]) - 3600)
    if not old or not float(old["value"]):
        return {"dir": "UNAVAILABLE", "pct": None}
    pct = float(cur["value"])/float(old["value"]) - 1
    d = "FLAT" if abs(pct) < OI_FLAT_PCT else ("UP" if pct > 0 else "DOWN")
    return {"dir": d, "pct": pct}


def cvd_proxy():
    try:
        rows = history_candles("1m", 220)
    except Exception as e:
        print("CVD Proxy unavailable:", e)
        return {"dir": "UNAVAILABLE", "delta": 0.0, "rel": 0.0}
    ds = []
    for r in rows:
        h, l, c, v = r["high"], r["low"], r["close"], r["volume"]
        ds.append(0.0 if h <= l or v <= 0 else v * max(-1, min(1, (2*c-h-l)/(h-l))))
    if len(ds) < 180:
        return {"dir": "UNAVAILABLE", "delta": 0.0, "rel": 0.0}
    recent, broad = sum(ds[-60:]), sum(abs(x) for x in ds[-180:])
    rel = abs(recent)/broad if broad else 0.0
    d = "FLAT" if rel < CVD_FLAT_REL else ("UP" if recent > 0 else "DOWN")
    return {"dir": d, "delta": recent, "rel": rel}


def flow_quality(side, oi, cvd):
    od, cd = oi.get("dir"), cvd.get("dir")
    wanted = "UP" if side == "LONG" else "DOWN"
    if od == "UP" and cd == wanted:
        return "支持", "🟢"
    if od == "DOWN" and cd == wanted:
        return "可能偏回補/平倉推動", "🟡"
    if od == "UP" and cd not in (wanted, "FLAT", "UNAVAILABLE"):
        return "分歧", "🟠"
    if cd == wanted:
        return "偏支持", "🟢"
    if od in ("FLAT", "UNAVAILABLE") and cd in ("FLAT", "UNAVAILABLE"):
        return "普通", "⚪"
    return "普通", "⚪"


def trigger_freshness(side, setup, price, a, event_rows=None):
    """Describe how fresh and well-held the trigger is.

    GitHub still runs every few minutes, so this uses the 1m event window rather than
    pretending the current ticker alone tells us when the trigger happened. Freshness
    is context, not a standalone veto: only a deep failed retest remains a hard reject.
    """
    rows = sorted(event_rows or [], key=lambda r: int(r.get("time", 0)))
    buffer_atr = BREAKOUT_BREAK_BUFFER_ATR if setup.get("kind") == "BREAKOUT" else BREAK_BUFFER_ATR
    level = float(setup["trigger"]) + (buffer_atr * a if side == "LONG" else -buffer_atr * a)
    crossed_rows = []
    for r in rows:
        if side == "LONG" and float(r.get("high", price)) >= level:
            crossed_rows.append(r)
        if side == "SHORT" and float(r.get("low", price)) <= level:
            crossed_rows.append(r)

    crossed_at = int(crossed_rows[0]["time"]) if crossed_rows else None
    if crossed_at is not None:
        age = max(0.0, (now_utc() - crossed_at) / 60.0)
    else:
        # If price is already beyond the buffered trigger but the crossing happened
        # before our retained event window, treat it as aged rather than falsely fresh.
        beyond = price >= level if side == "LONG" else price <= level
        age = float(EVENT_LOOKBACK_MIN + 1) if beyond else None

    hold_atr = ((price - float(setup["trigger"])) / a) if side == "LONG" else ((float(setup["trigger"]) - price) / a)
    recent = rows[-3:]
    if recent:
        good = 0
        for r in recent:
            c = float(r.get("close", price))
            if (side == "LONG" and c >= float(setup["trigger"])) or (side == "SHORT" and c <= float(setup["trigger"])):
                good += 1
        hold_ratio = good / len(recent)
    else:
        hold_ratio = 1.0 if hold_atr >= 0 else 0.0

    if age is not None and age <= TRIGGER_FRESH_MIN and hold_atr >= -0.02 and hold_ratio >= 0.5:
        state, label = "FRESH", "新鮮"
    elif age is not None and age <= TRIGGER_AGING_MIN and hold_atr >= -0.05:
        state, label = "AGING", "仍有效"
    else:
        state, label = "STALE", "偏舊/承接不足"

    return {
        "state": state, "label": label, "age_min": age,
        "hold_atr": hold_atr, "hold_ratio": hold_ratio,
        "crossed_at": crossed_at,
    }


def trade_geometry(plan, space, ext):
    """Judge Entry/SL/forward-path as one geometry instead of separate micro filters."""
    risk_atr = float(plan.get("risk_atr") or 99.0)
    strong_r = space.get("space_r")
    strong_r = float(strong_r) if strong_r is not None else None

    poor = (
        (risk_atr > 2.0 and ext > EXT_NORMAL_ATR) or
        (risk_atr > RISK_WARN_ATR and ext > EXT_WARN_ATR) or
        (strong_r is not None and strong_r < 0.35 and (risk_atr > RISK_WARN_ATR or ext > EXT_NORMAL_ATR))
    )
    balanced = (
        risk_atr <= RISK_WARN_ATR and
        ext <= EXT_NORMAL_ATR and
        (strong_r is None or strong_r >= 0.80)
    )

    if poor:
        cls, label = "POOR", "偏差"
    elif balanced:
        cls, label = "BALANCED", "平衡"
    else:
        cls, label = "MANAGE", "可管理"

    notes = []
    if risk_atr > RISK_WARN_ATR:
        notes.append("停損較寬")
    if ext > EXT_NORMAL_ATR:
        notes.append("進場有延伸")
    if strong_r is not None and strong_r < 1.0:
        notes.append("前方強結構較近")
    if not notes:
        notes.append("Entry/SL/空間協調")
    return {"class": cls, "label": label, "reason": "＋".join(notes[:3]), "strong_r": strong_r}


def formal_context(side, setup, trend, reg, ema_pos, vol, oi, cvd, ext, space, freshness=None, geometry=None):
    """Classify whether the thesis and current market evidence agree.

    1H is context, never a universal direction gate. For a genuine REVERSAL thesis,
    being opposite the old 1H background is expected and therefore not treated as a
    conflict by itself; the reversal must instead prove itself through structure,
    trigger quality, participation and flow.
    """
    freshness = freshness or {"state": "AGING", "label": "仍有效"}
    geometry = geometry or {"class": "MANAGE", "label": "可管理"}
    trend_state = trend.get("state", "NEUTRAL")
    regime = reg.get("state", "RANGE")
    vr = float(vol.get("ratio") or 0.0)
    ema_d = float(ema_pos.get("distance_atr") or 0.0)
    flow, _ = flow_quality(side, oi, cvd)
    is_reversal = setup.get("kind") == "REVERSAL"
    aligned = ((side == "LONG" and trend_state == "BULL") or (side == "SHORT" and trend_state == "BEAR"))
    raw_opposite = ((side == "LONG" and trend_state == "BEAR") or (side == "SHORT" and trend_state == "BULL"))
    opposite = raw_opposite and not is_reversal
    neutral = trend_state == "NEUTRAL"
    strong_near = (space.get("nearest_strength") == "STRONG" and space.get("nearest_r") is not None and float(space.get("nearest_r")) < 1.0)
    stale = freshness.get("state") == "STALE"
    geom_poor = geometry.get("class") == "POOR"

    if is_reversal:
        if stale:
            return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "反轉Trigger已老化，失去首次結構翻轉優勢", "flow": flow}
        if vr < 0.85 and flow in ("普通", "分歧", "可能偏回補/平倉推動"):
            return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "反轉結構雖成立，但量能/資金流不足以支持結構翻轉", "flow": flow}
        if geom_poor and vr < 1.30:
            return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "反轉交易幾何偏差且缺乏足夠參與度", "flow": flow}
        if freshness.get("state") == "FRESH" and vr >= 1.20 and geom_poor is False and flow != "分歧":
            return {"class": "CONSISTENT", "label": "🟢 一致", "reason": "反轉結構完成＋Trigger新鮮＋市場參與支持", "flow": flow}
        notes = ["反轉結構成立"]
        if raw_opposite:
            notes.append("逆原1H背景（反轉預期）")
        if vr < 1.0:
            notes.append("成交量偏弱")
        if flow in ("分歧", "可能偏回補/平倉推動"):
            notes.append(f"資金流{flow}")
        if geom_poor:
            notes.append("幾何偏差")
        return {"class": "MIXED", "label": "🟡 混合", "reason": "＋".join(notes[:4]), "flow": flow}

    if opposite and regime == "RANGE" and vr < 1.0:
        return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "逆1H背景＋震盪環境＋成交量偏弱", "flow": flow}
    if stale and (regime == "RANGE" or vr < 1.0) and not aligned:
        return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "Trigger偏舊/承接不足＋背景未形成支持", "flow": flow}
    if geom_poor and (opposite or regime == "RANGE" or vr < 1.0):
        return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "交易幾何偏差＋市場情境未形成共振", "flow": flow}
    if opposite and ema_d > 1.25 and flow in ("普通", "分歧", "可能偏回補/平倉推動") and (vr < 1.0 or regime == "RANGE"):
        return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "逆1H背景＋EMA偏離較大＋資金流未形成支持", "flow": flow}
    if regime == "RANGE" and vr < 0.85 and ema_d > 1.25:
        return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "震盪環境＋量能偏弱＋位置過度偏離", "flow": flow}
    if flow == "分歧" and regime == "RANGE" and vr < 1.0:
        return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "資金流分歧＋震盪環境＋成交量不足", "flow": flow}
    if strong_near and opposite and vr < 1.0:
        return {"class": "CONFLICT", "label": "🔴 衝突", "reason": "逆1H背景＋前方強結構很近＋成交量不足", "flow": flow}

    fresh_ok = freshness.get("state") == "FRESH"
    geometry_ok = geometry.get("class") == "BALANCED"
    if aligned and regime == "TREND" and vr >= 1.0 and fresh_ok and geometry_ok and flow != "分歧":
        return {"class": "CONSISTENT", "label": "🟢 一致", "reason": "順1H＋趨勢＋Trigger新鮮＋交易幾何平衡", "flow": flow}
    if aligned and vr >= 1.30 and fresh_ok and geometry_ok and ext <= EXT_NORMAL_ATR and flow in ("支持", "偏支持", "普通"):
        return {"class": "CONSISTENT", "label": "🟢 一致", "reason": "順1H＋放量＋Trigger新鮮＋進場貼近", "flow": flow}
    if neutral and regime == "TREND" and vr >= 1.30 and fresh_ok and geometry_ok and flow != "分歧":
        return {"class": "CONSISTENT", "label": "🟢 一致", "reason": "1H中性但15M趨勢明確＋放量＋Trigger新鮮", "flow": flow}
    if setup.get("kind") == "PULLBACK" and aligned and vr >= 1.0 and ema_d <= 0.75 and fresh_ok and geometry_ok:
        return {"class": "CONSISTENT", "label": "🟢 一致", "reason": "順1H回踩延續＋EMA位置合理＋Trigger新鮮", "flow": flow}

    notes = []
    if opposite: notes.append("逆1H背景")
    elif neutral: notes.append("1H中性")
    if regime == "RANGE": notes.append("震盪環境")
    if vr < 1.0: notes.append("成交量偏弱")
    if ema_d > 1.0: notes.append("EMA偏離較大")
    if freshness.get("state") != "FRESH": notes.append(f"Trigger{freshness.get('label','仍有效')}")
    if geometry.get("class") != "BALANCED": notes.append(f"幾何{geometry.get('label','可管理')}")
    if flow in ("分歧", "可能偏回補/平倉推動"): notes.append(f"資金流{flow}")
    if strong_near: notes.append("前方強結構較近")
    if not notes: notes.append("Setup有效，但主要背景未形成完整共振")
    return {"class": "MIXED", "label": "🟡 混合", "reason": "＋".join(notes[:4]), "flow": flow}



def _penetration_quality(side, setup, context, vol, oi, cvd, freshness, geometry, trend, plan, ext):
    """Judge whether a thesis has evidence to push through nearby strong structure."""
    kind = setup.get("kind")
    vr = float((vol or {}).get("ratio") or 0.0)
    flow, _ = flow_quality(side, oi, cvd)
    fresh = (freshness or {}).get("state", "AGING")
    geom = (geometry or {}).get("class", "MANAGE")
    cls = (context or {}).get("class", "MIXED")
    risk_atr = float((plan or {}).get("risk_atr") or 99.0)
    ext = float(ext or 0.0)
    trend_state = (trend or {}).get("state", "NEUTRAL")
    aligned = ((side == "LONG" and trend_state == "BULL") or (side == "SHORT" and trend_state == "BEAR"))

    if geom == "POOR" or fresh == "STALE":
        return {"grade": "WEAK", "reason": "Trigger/幾何不足以攻擊近端強結構"}

    if kind == "BREAKOUT":
        strong = (fresh == "FRESH" and ext <= 0.25 and vr >= 1.40 and
                  flow in ("支持", "偏支持") and risk_atr <= 2.25)
        adequate = (fresh == "FRESH" and ext <= 0.30 and vr >= 1.20 and
                    flow != "分歧" and risk_atr <= 2.00 and cls != "CONFLICT")
        if strong:
            return {"grade": "STRONG", "reason": "突破新鮮＋放量＋資金流支持"}
        if adequate:
            return {"grade": "ADEQUATE", "reason": "突破新鮮且參與度足以測試近端結構"}
        return {"grade": "WEAK", "reason": "突破缺少新鮮度/量能/資金流其中一項關鍵穿透證據"}

    if kind == "REVERSAL":
        strong = (fresh in ("FRESH", "AGING") and ext <= 0.25 and vr >= 0.85 and
                  flow in ("支持", "偏支持") and risk_atr <= 3.00)
        adequate = (fresh == "FRESH" and ext <= 0.20 and vr >= 1.10 and
                    flow != "分歧" and risk_atr <= 2.50)
        if strong:
            return {"grade": "STRONG", "reason": "反轉結構＋方向資金流支持"}
        if adequate:
            return {"grade": "ADEQUATE", "reason": "反轉Trigger新鮮且參與度足夠"}
        return {"grade": "WEAK", "reason": "反轉尚未證明有足夠力量穿越近端結構"}

    # Continuation / pullback: nearby structure can still be attacked with a compact,
    # aligned entry even without explosive flow.
    strong = (aligned and fresh in ("FRESH", "AGING") and ext <= 0.12 and
              risk_atr <= 1.25 and vr >= 0.90)
    adequate = (aligned and fresh == "FRESH" and ext <= 0.20 and
                risk_atr <= 1.50 and vr >= 1.00 and flow != "可能偏回補/平倉推動")
    if strong:
        return {"grade": "STRONG", "reason": "順背景延續＋進場貼近＋防守結構緊湊"}
    if adequate:
        return {"grade": "ADEQUATE", "reason": "延續結構新鮮且風險/位置可支持近端穿透"}
    return {"grade": "WEAK", "reason": "延續單在近端強結構前缺乏足夠穿透優勢"}


def forward_feasibility(side, setup, space, context, vol, oi, cvd, freshness, geometry, reg, trend, plan, ext):
    """Judge forward room together with thesis-specific penetration evidence."""
    strong_r = space.get("space_r")
    strong_r = float(strong_r) if strong_r is not None else None
    pen = _penetration_quality(side, setup, context, vol, oi, cvd, freshness, geometry, trend, plan, ext)

    if strong_r is None or strong_r >= 1.0:
        return {"decision": "ACCEPT", "class": "OPEN", "label": "開放",
                "reason": "前方沒有近距離強結構壓縮發展空間", "space_r": strong_r,
                "penetration": pen["grade"], "penetration_reason": pen["reason"]}

    if strong_r < FEAS_CRITICAL_R and pen["grade"] != "STRONG":
        return {"decision": "REJECT", "class": "BLOCKED", "label": "發展空間不足",
                "reason": f"前方強結構僅 {strong_r:.2f}R；{pen['reason']}", "space_r": strong_r,
                "penetration": pen["grade"], "penetration_reason": pen["reason"]}

    if strong_r < 0.60:
        if pen["grade"] == "STRONG":
            return {"decision": "ACCEPT", "class": "POWER_THROUGH", "label": "具穿透力",
                    "reason": f"前方強結構 {strong_r:.2f}R，但{pen['reason']}", "space_r": strong_r,
                    "penetration": pen["grade"], "penetration_reason": pen["reason"]}
        if pen["grade"] == "ADEQUATE" and strong_r >= FEAS_TIGHT_R:
            return {"decision": "CAUTION", "class": "TIGHT", "label": "空間偏緊",
                    "reason": f"前方強結構 {strong_r:.2f}R；穿透證據尚可，保守處理", "space_r": strong_r,
                    "penetration": pen["grade"], "penetration_reason": pen["reason"]}
        return {"decision": "REJECT", "class": "BLOCKED", "label": "發展空間不足",
                "reason": f"前方強結構 {strong_r:.2f}R；{pen['reason']}", "space_r": strong_r,
                "penetration": pen["grade"], "penetration_reason": pen["reason"]}

    return {"decision": "ACCEPT", "class": "WORKABLE", "label": "可發展",
            "reason": f"前方強結構約 {strong_r:.2f}R，仍有可管理發展空間", "space_r": strong_r,
            "penetration": pen["grade"], "penetration_reason": pen["reason"]}

def apply_exit_profile(plan, context, space, reg, vol, freshness=None, geometry=None):
    """Quality Engine 2.1 dynamic exit.

    The first objective remains 1R so performance stays interpretable. A second
    target only exists when context, trigger freshness, geometry and forward
    structure justify keeping risk on. Strong trend setups may stretch TP2 to 2.5R.
    """
    freshness = freshness or {"state": "AGING"}
    geometry = geometry or {"class": "MANAGE"}
    cls = context["class"]
    vr = float(vol.get("ratio") or 0.0)
    strong_r = space.get("space_r")
    strong_r = float(strong_r) if strong_r is not None else None

    plan["tp1_r"] = 1.0
    plan["tp2_r"] = None
    if plan.get("side") == "SHORT":
        plan["tp1"] = plan["entry"] - plan["risk"]
    else:
        plan["tp1"] = plan["entry"] + plan["risk"]

    # Base ambition from overall quality.
    if (cls == "CONSISTENT" and reg.get("state") == "TREND" and vr >= 1.50 and
            freshness.get("state") == "FRESH" and geometry.get("class") == "BALANCED"):
        target_r, profile = 2.5, "趨勢延伸型"
    elif cls == "CONSISTENT" and freshness.get("state") == "FRESH" and geometry.get("class") != "POOR":
        target_r, profile = 2.0, "標準型"
    elif (cls == "MIXED" and reg.get("state") == "TREND" and vr >= 1.20 and
          freshness.get("state") == "FRESH" and geometry.get("class") == "BALANCED"):
        target_r, profile = 1.5, "保守延伸型"
    else:
        target_r, profile = None, "1R保守型"

    # Do not place TP2 through a meaningful strong structural zone. If a useful
    # target still exists before it, trim TP2; otherwise publish only the 1R target.
    if target_r is not None and strong_r is not None and strong_r < target_r:
        before_zone = strong_r - 0.15
        if before_zone >= 1.25:
            target_r = round(before_zone, 2)
            profile += "・結構前收斂"
        else:
            target_r = None
            profile = "1R保守型・前方結構近"

    if target_r is not None:
        plan["tp2_r"] = float(target_r)
        if plan.get("side") == "SHORT":
            plan["tp2"] = plan["entry"] - plan["risk"] * float(target_r)
        else:
            plan["tp2"] = plan["entry"] + plan["risk"] * float(target_r)
    else:
        plan["tp2"] = None
    return {"profile": profile, "tp1_r": 1.0, "tp2_r": plan.get("tp2_r")}

def zh_dir(d):
    return {"BULL": "偏多", "BEAR": "偏空", "NEUTRAL": "中性",
            "UP": "上升", "DOWN": "下降", "FLAT": "持平", "UNAVAILABLE": "資料不足"}.get(d, d)


def zh_side(side):
    return "做多" if side == "LONG" else "做空"


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            s = {}
    else:
        s = {}
    s["version"] = VERSION
    s.setdefault("next_trade_id", 1)
    s.setdefault("trades", [])
    s.setdefault("history", [])
    s.setdefault("blocked", [])
    s.setdefault("prepare_seen", [])
    s.setdefault("formal_seen", [])
    s.setdefault("next_prepare_id", 1)
    s.setdefault("prepare_active", [])
    s.setdefault("prepare_history", [])
    s.setdefault("oi_history", [])
    s.setdefault("news", {"seen": [], "last_notify": 0})
    s.setdefault("last_event_scan", 0)
    s.setdefault("setup_lifecycle", [])
    s.setdefault("active_thesis", None)
    return s


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def discord_card(msg):
    """Presentation-only adapter: keep signal generation and saved data unchanged."""
    import re

    lines = [line.strip() for line in str(msg).splitlines() if line.strip()]
    title_line = lines[0] if lines else "BTC 市場雷達"
    raw = str(msg)

    def value(prefixes, default="—"):
        for line in lines:
            for prefix in prefixes:
                if line.startswith(prefix):
                    return line[len(prefix):].strip()
        return default

    def price_in(label):
        match = re.search(re.escape(label) + r"\s*\$([\d,]+)", raw)
        return "$" + match.group(1) if match else "—"

    def embed(title, description, color, fields=None, footer=None):
        result = {"title": title[:256], "description": description[:4096],
                  "color": color, "fields": (fields or [])[:25],
                  "footer": {"text": footer or "LASER SIGNAL 4.0 · 模擬訊號"}}
        return {"embeds": [result], "allowed_mentions": {"parse": []}}

    green, red, blue, amber = 0x22C55E, 0xEF4444, 0x6485AE, 0xEAB85C
    if "手動查詢" in title_line:
        market = value(["市場狀態："])
        side = value(["1H背景："])
        oi_line = value(["OI："])
        oi, _, cvd = oi_line.partition("｜")
        cvd = cvd.replace("CVD Proxy：", "").strip() or "—"
        open_trade = value(["⚡ 雷射訊號正式單："])
        status = "正式交易追蹤中" if not open_trade.endswith("進行中 0") and "進行中" in open_trade else "等待交易機會"
        return embed("📡 BTC 市場雷達", f"# {price_in('BTC：')}\n**{side}** · {market}\n\n**{status}**",
                     green if "多" in side else red if "空" in side else blue,
                     [{"name": "OI（未平倉量）", "value": oi, "inline": True},
                      {"name": "CVD（累積量差）", "value": cvd, "inline": True}],
                     "LASER SIGNAL 4.0 · 手動查詢 · 詳細績效保留在執行日誌")

    if "正式" in title_line and "訊號" in title_line:
        short = "做空" in title_line
        tp = value(["止盈：", "TP1："])
        tp2 = value(["TP2："], "")
        quality = value(["交易品質："]).split("｜")[0]
        setup = value(["15M觸發："])
        fields = [{"name": "進場", "value": value(["進場："]), "inline": True},
                  {"name": "SL（止損）", "value": value(["停損："]), "inline": True},
                  {"name": "TP1（第一止盈）", "value": tp, "inline": True}]
        if tp2:
            fields.append({"name": "TP2（第二止盈）", "value": tp2, "inline": True})
        fields += [{"name": "訊號品質", "value": quality, "inline": True},
                   {"name": "1H（一小時）趨勢", "value": value(["1H背景："]), "inline": True}]
        ident = value(["🧾 已建立模擬單 "])
        return embed("⚡ BTC 正式訊號 · " + ("▼ 做空" if short else "▲ 做多"),
                     f"**{setup}**\n{ident} · 模擬交易", red if short else green, fields)

    if "準備" in title_line and "BTC" in title_line:
        short = "做空" in title_line
        fields = [{"name": "觸發價格", "value": price_in("觸發價："), "inline": True},
                  {"name": "目前價格", "value": price_in("目前："), "inline": True},
                  {"name": "1H（一小時）趨勢", "value": value(["1H背景："]), "inline": True}]
        return embed("👀 BTC 準備訊號 · " + ("做空" if short else "做多"),
                     f"**{value(['15M結構：'])}**\n等待條件確認，尚未建立正式單", amber, fields)

    if "模擬單 #" in title_line:
        is_sl = "停損" in title_line
        outcome = "SL（止損）" if is_sl else "TP2（第二止盈）" if "TP2" in title_line else "TP1（第一止盈）"
        stats = next((line for line in lines if "MFE：" in line), "")
        return embed(title_line.replace("模擬單", "模擬交易"),
                     f"**{outcome}**\n{stats.replace('MFE：', 'MFE（最大有利波動）：').replace('MAE：', 'MAE（最大不利波動）：')}",
                     red if is_sl else green)

    if "重大新聞" in title_line:
        direction = value(["方向："], "方向不明")
        impact = value(["影響："], "波動風險可能提高")
        zh = value(["中文：", "中文摘要："])
        source = value(["原文："])
        tint = green if "偏多" in direction else red if "偏空" in direction else amber
        return embed("📰 BTC 重要新聞 · " + direction,
                     f"**{zh}**\n\n**可能影響：**{impact}\n\n僅供市場背景參考，不能單獨作為進場訊號。",
                     tint,
                     [{"name": "事件", "value": value(["類型："]), "inline": True},
                      {"name": "原文", "value": source[:1024], "inline": False}])

    # Unknown future notifications still display, without dropping information.
    return embed("📡 BTC 雷射雷達", raw, blue)


def send_discord(msg):
    msg = str(msg).strip()
    print(msg)  # Preserve the full original notification in GitHub execution logs.
    if not WEBHOOK:
        print("缺少 DISCORD_WEBHOOK，僅輸出到 log")
        return
    payload = discord_card(msg)
    last = None
    for i in range(3):
        try:
            r = requests.post(WEBHOOK, json=payload, timeout=20)
            print("Discord HTTP:", r.status_code)
            r.raise_for_status()
            return
        except requests.RequestException as e:
            last = e
            if i < 2:
                time.sleep(2**i)
    print("DISCORD_DELIVERY_FAILED:", repr(last))


def setup_signature(setup):
    return setup["setup_key"]


def touch_setup_lifecycle(state, side, setup, price, status=None, note=None):
    now = now_utc()
    rows = state.setdefault("setup_lifecycle", [])
    row = next((x for x in rows if x.get("setup_key") == setup["setup_key"]), None)
    if row is None:
        row = {
            "setup_key": setup["setup_key"], "side": side, "type": setup["kind"],
            "label": setup["label"], "trigger": float(setup["trigger"]),
            "defense": float(setup["defense"]), "first_seen": now, "first_seen_iso": iso(now),
            "last_seen": now, "last_seen_iso": iso(now), "last_price": float(price),
            "status": status or "DETECTED", "last_note": note, "block_count": 0,
        }
        rows.append(row)
    else:
        row["last_seen"] = now; row["last_seen_iso"] = iso(now); row["last_price"] = float(price)
        if status:
            row["status"] = status
        if note:
            row["last_note"] = note
    state["setup_lifecycle"] = rows[-2000:]
    return row


def record_blocked(state, side, setup, reason, price, extra=None):
    """One evolving blocked record per setup instead of appending the same setup every run."""
    now = now_utc()
    row = next((x for x in reversed(state["blocked"]) if x.get("setup_key") == setup["setup_key"]), None)
    if row is None:
        row = {
            "key": setup["setup_key"], "setup_key": setup["setup_key"],
            "first_time": now, "first_time_iso": iso(now), "time": now, "time_iso": iso(now),
            "side": side, "type": setup["kind"], "engine_version": VERSION, "reason": reason,
            "trigger": setup["trigger"], "price": price, "block_count": 1,
        }
        state["blocked"].append(row)
    else:
        row["time"] = now; row["time_iso"] = iso(now); row["reason"] = reason; row["price"] = price
        row["engine_version"] = VERSION
        row["block_count"] = int(row.get("block_count", 1)) + 1
    if extra:
        row.update(extra)
    state["blocked"] = state["blocked"][-1000:]
    life = touch_setup_lifecycle(state, side, setup, price, "BLOCKED", reason)
    life["block_count"] = int(life.get("block_count", 0)) + 1


# These are candidate-evaluation outcomes, not changes to the trading rules.
FORMAL_REJECTION_LABELS = {
    "BREAKOUT_NOT_ACCEPTED": "突破尚未確認",
    "TRIGGER_RETEST_FAILED": "穿越觸發價後未能守住",
    "NO_RISK_PLAN": "無法建立有效風控",
    "LOW_VOLUME": "成交量不足",
    "RISK_TOO_LARGE": "止損距離過大",
    "OVEREXTENDED": "價格過度延伸",
    "CHASE_WAIT": "追價風險偏高，等待位置",
    "CONTEXT_CONFLICT": "市場情境衝突",
    "QUALITY_CALIBRATION": "訊號品質不符合要求",
    "ENTRY_TIMING_WEAK": "進場時機不足",
    "FEASIBILITY_WEAK": "預期發展空間不足",
    "STRONG_OBSTACLE_NEAR": "前方強支撐壓力距離不足",
    "POOR_ENTRY_GEOMETRY": "進場、止損與前方空間搭配不佳",
    "TREND_MISMATCH": "與一小時趨勢不符",
    "PULLBACK_FAR_FROM_EMA": "回踩距離均線太遠",
    "STALE_TRIGGER": "進場觸發已逾期",
}


def record_candidate_rejection(state, ev, price, vol):
    """Audit evaluated, triggered candidates without changing signal eligibility.

    A candidate that has not crossed its trigger is merely waiting, not blocked.
    Keep one rolling record per setup; record_blocked updates existing entries.
    """
    if ev.get("ready") or ev.get("reason") == "TRIGGER_NOT_CROSSED":
        return
    code = ev.get("reason")
    if code not in FORMAL_REJECTION_LABELS:
        return
    setup = ev["setup"]
    # Previously issued formal setups are not fresh rejection candidates.
    if setup["setup_key"] in state.get("formal_seen", []):
        return
    extra = _blocked_extra(ev, vol)
    extra["reason_code"] = code
    record_blocked(state, ev["side"], setup, FORMAL_REJECTION_LABELS[code], price, extra)


def create_prepare_validation(state, side, setup, price, a, trend, ema_pos, vol, reg, oi, cvd):
    pid = int(state["next_prepare_id"])
    state["next_prepare_id"] += 1
    row = {
        "id": pid,
        "setup_key": setup["setup_key"],
        "side": side,
        "trigger_type": setup["label"],
        "opened_at": now_utc(),
        "opened_iso": iso(now_utc()),
        "prepare_price": float(price),
        "trigger": float(setup["trigger"]),
        "atr_open": float(a),
        "trend_1h": trend["state"],
        "ema_position": ema_pos["label"],
        "ema_distance_atr": ema_pos["distance_atr"],
        "volume_ratio": vol["ratio"],
        "regime": reg["state"],
        "oi_dir": oi["dir"],
        "cvd_dir": cvd["dir"],
        "formal_promoted": False,
        "formal_trade_id": None,
        "formal_at": None,
        "formal_delay_min": None,
        "status": "WATCHING",
        "best_price": float(price),
        "worst_price": float(price),
        "mfe_atr": 0.0,
        "mae_atr": 0.0,
        "mfe_atr_30": None,
        "mae_atr_30": None,
        "mfe_atr_60": None,
        "mae_atr_60": None,
        "duration_min": 0,
    }
    state["prepare_active"].append(row)
    return row


def mark_prepare_promoted(state, setup_key, trade_id):
    now = now_utc()
    for p in state.get("prepare_active", []):
        if p.get("setup_key") == setup_key and not p.get("formal_promoted"):
            p["formal_promoted"] = True
            p["formal_trade_id"] = trade_id
            p["formal_at"] = now
            p["formal_delay_min"] = round((now - int(p["opened_at"])) / 60, 1)
            for life in state.get("setup_lifecycle", []):
                if life.get("setup_key") == setup_key:
                    life["status"] = "FORMAL"; life["last_note"] = f"建立模擬單 #{trade_id}"
            return


def update_prepare_validations(state, live_price, event_rows=None):
    remain = []
    now = now_utc()
    for p in state.get("prepare_active", []):
        entry = float(p["prepare_price"])
        a = float(p.get("atr_open") or 0.0)
        if a <= 0:
            remain.append(p)
            continue
        p_events = post_entry_event_bars(event_rows, p.get("opened_at", 0))
        wh, wl = window_high_low(p_events, live_price)
        if p["side"] == "LONG":
            p["best_price"] = max(float(p.get("best_price", entry)), wh)
            p["worst_price"] = min(float(p.get("worst_price", entry)), wl)
            p["mfe_atr"] = max(float(p.get("mfe_atr", 0.0)), (p["best_price"] - entry) / a)
            p["mae_atr"] = max(float(p.get("mae_atr", 0.0)), (entry - p["worst_price"]) / a)
        else:
            p["best_price"] = min(float(p.get("best_price", entry)), wl)
            p["worst_price"] = max(float(p.get("worst_price", entry)), wh)
            p["mfe_atr"] = max(float(p.get("mfe_atr", 0.0)), (entry - p["best_price"]) / a)
            p["mae_atr"] = max(float(p.get("mae_atr", 0.0)), (p["worst_price"] - entry) / a)
        elapsed = (now - int(p["opened_at"])) / 60
        p["duration_min"] = int(elapsed)
        if elapsed >= 30 and p.get("mfe_atr_30") is None:
            p["mfe_atr_30"] = round(float(p["mfe_atr"]), 4)
            p["mae_atr_30"] = round(float(p["mae_atr"]), 4)
        if elapsed >= PREPARE_VALIDATE_MIN:
            p["mfe_atr_60"] = round(float(p["mfe_atr"]), 4)
            p["mae_atr_60"] = round(float(p["mae_atr"]), 4)
            p["status"] = "PROMOTED" if p.get("formal_promoted") else "NO_FORMAL"
            p["closed_at"] = now
            p["closed_iso"] = iso(now)
            state["prepare_history"].append(p)
        else:
            remain.append(p)
    state["prepare_active"] = remain
    state["prepare_history"] = state.get("prepare_history", [])[-2000:]


def create_trade(state, side, setup, plan, ctx):
    tid = int(state["next_trade_id"])
    state["next_trade_id"] += 1
    t = {
        "id": tid, "setup_key": setup["setup_key"], "side": side, "trigger_type": setup["label"],
        "engine_version": VERSION, "engine_build": BUILD,
        "opened_at": now_utc(), "opened_iso": iso(now_utc()), "status": "OPEN",
        "entry": plan["entry"], "sl": plan["sl"], "tp1": plan["tp1"], "tp2": plan.get("tp2"),
        "risk": plan["risk"], "risk_atr": plan["risk_atr"], "trigger": setup["trigger"],
        "defense_source": plan.get("defense_source"), "defense_anchor": plan.get("defense_anchor"),
        "stop_buffer_atr": plan.get("stop_buffer_atr"), "position_size_factor": plan.get("position_size_factor"),
        "extension_atr": ctx["extension_atr"], "trend_1h": ctx["trend_1h"],
        "ema_position": ctx["ema_position"], "ema_distance_atr": ctx.get("ema_distance_atr"),
        "volume_ratio": ctx["volume_ratio"],
        "regime": ctx["regime"], "space_r": ctx["space_r"],
        "space_strength": ctx.get("space_strength"),
        "oi_dir": ctx["oi_dir"], "cvd_dir": ctx["cvd_dir"], "flow_quality": ctx["flow_quality"],
        "context_class": ctx.get("context_class"), "context_reason": ctx.get("context_reason"),
        "quality_state": ctx.get("quality_state"),
        "quality_decision": ctx.get("quality_decision"), "quality_reason": ctx.get("quality_reason"),
        "trigger_freshness": ctx.get("trigger_freshness"), "trigger_age_min": ctx.get("trigger_age_min"),
        "trigger_hold_atr": ctx.get("trigger_hold_atr"),
        "geometry_class": ctx.get("geometry_class"), "geometry_reason": ctx.get("geometry_reason"),
        "exit_profile": ctx.get("exit_profile"),
        "entry_timing": ctx.get("entry_timing"), "entry_timing_label": ctx.get("entry_timing_label"),
        "entry_timing_reason": ctx.get("entry_timing_reason"),
        "feasibility_class": ctx.get("feasibility_class"), "feasibility_label": ctx.get("feasibility_label"),
        "feasibility_reason": ctx.get("feasibility_reason"),
        "penetration_quality": ctx.get("penetration_quality"), "penetration_reason": ctx.get("penetration_reason"),
        "tp1_r": plan.get("tp1_r", 1.0), "tp2_r": plan.get("tp2_r"),
        "mfe_r": 0.0, "mae_r": 0.0, "tp1_hit": False, "tp2_hit": False,
        "best_price": plan["entry"], "worst_price": plan["entry"], "duration_min": 0,
    }
    state["trades"].append(t)
    return t

def update_trades(state, live_price, event_rows=None):
    remain = []
    bars = event_rows or [{"time": now_utc(), "high": live_price, "low": live_price, "close": live_price}]
    for t in state["trades"]:
        entry, risk = float(t["entry"]), float(t["risk"])
        closed = False
        trade_bars = post_entry_event_bars(bars, t.get("opened_at", 0)) or [{"time": now_utc(), "high": live_price, "low": live_price, "close": live_price}]
        for bar in trade_bars:
            hi, lo = float(bar["high"]), float(bar["low"])
            if t["side"] == "LONG":
                t["best_price"] = max(float(t.get("best_price", entry)), hi)
                t["worst_price"] = min(float(t.get("worst_price", entry)), lo)
                t["mfe_r"] = max(float(t.get("mfe_r", 0)), (t["best_price"]-entry)/risk)
                t["mae_r"] = max(float(t.get("mae_r", 0)), (entry-t["worst_price"])/risk)
                hit_tp1 = hi >= float(t["tp1"]); hit_tp2 = t.get("tp2") is not None and hi >= float(t["tp2"]); hit_sl = lo <= float(t["sl"])
            else:
                t["best_price"] = min(float(t.get("best_price", entry)), lo)
                t["worst_price"] = max(float(t.get("worst_price", entry)), hi)
                t["mfe_r"] = max(float(t.get("mfe_r", 0)), (entry-t["best_price"])/risk)
                t["mae_r"] = max(float(t.get("mae_r", 0)), (t["worst_price"]-entry)/risk)
                hit_tp1 = lo <= float(t["tp1"]); hit_tp2 = t.get("tp2") is not None and lo <= float(t["tp2"]); hit_sl = hi >= float(t["sl"])

            # OHLC cannot tell sequence when TP and SL are both inside the same 1m bar.
            # Mark it and use the conservative outcome instead of pretending precision.
            if hit_sl and (hit_tp1 or hit_tp2):
                t["intrabar_ambiguous"] = True
                hit_tp1 = hit_tp2 = False

            if hit_tp1 and not t.get("tp1_hit"):
                t["tp1_hit"] = True
                send_discord(f"🎯 模擬單 #{t['id']}｜TP1 達成\n{zh_side(t['side'])}｜Entry ${entry:,.0f}\nMFE：+{t['mfe_r']:.2f}R｜MAE：-{t['mae_r']:.2f}R")
                # If Space never supported a TP2, the published trade plan ends at TP1.
                if t.get("tp2") is None:
                    t["status"] = "TP1"; closed = True
            if not closed and hit_tp2 and not t.get("tp2_hit"):
                t["tp2_hit"] = True; t["status"] = "TP2"; closed = True
                send_discord(f"🏁 模擬單 #{t['id']}｜TP2 達成\n{zh_side(t['side'])}｜MFE：+{t['mfe_r']:.2f}R｜MAE：-{t['mae_r']:.2f}R")
            elif not closed and hit_sl:
                t["status"] = "TP1_THEN_SL" if t.get("tp1_hit") else "SL"; closed = True
                send_discord(f"❌ 模擬單 #{t['id']}｜停損\n{zh_side(t['side'])}｜結果：{t['status']}\nMFE：+{t['mfe_r']:.2f}R｜MAE：-{t['mae_r']:.2f}R")
            if closed:
                t["closed_at"] = int(bar.get("time", now_utc())); t["closed_iso"] = iso(t["closed_at"]); break

        t["duration_min"] = int((now_utc()-int(t["opened_at"]))/60)
        if t["status"] == "OPEN":
            remain.append(t)
        else:
            state["history"].append(t)
    state["trades"] = remain
    state["history"] = state["history"][-2000:]


def export_data(state):
    save_json(TRADES_JSON, {"open": state["trades"], "history": state["history"]})
    rows = state["history"] + state["trades"]
    fields = ["id","setup_key","side","trigger_type","engine_version","engine_build","opened_iso","closed_iso","status","entry","sl","tp1","tp2",
              "risk_atr","defense_source","defense_anchor","stop_buffer_atr","position_size_factor","trigger","extension_atr","trend_1h","ema_position","ema_distance_atr","volume_ratio","regime","space_r","space_strength",
              "oi_dir","cvd_dir","flow_quality","context_class","context_reason","quality_state","quality_decision","quality_reason",
              "trigger_freshness","trigger_age_min","trigger_hold_atr","geometry_class","geometry_reason",
              "entry_timing","entry_timing_label","entry_timing_reason","feasibility_class","feasibility_label","feasibility_reason",
              "penetration_quality","penetration_reason","exit_profile","tp1_r","tp2_r",
              "mfe_r","mae_r","duration_min","excluded_from_stats","legacy_rule_mismatch","intrabar_ambiguous"]
    with open(TRADES_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    save_json(BLOCKED_JSON, state["blocked"])
    bfields = ["first_time_iso","time_iso","setup_key","side","type","engine_version","reason","reason_code","trigger","price","block_count","volume_ratio","space_r","extension_atr","context_class","context_reason","trigger_freshness","trigger_age_min","geometry_class","quality_decision","quality_reason","entry_timing","entry_timing_reason","feasibility_class","feasibility_reason","penetration_quality","penetration_reason"]
    with open(BLOCKED_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=bfields, extrasaction="ignore")
        w.writeheader(); w.writerows(state["blocked"])

    prepare_rows = state.get("prepare_history", []) + state.get("prepare_active", [])
    save_json(PREPARE_JSON, {"active": state.get("prepare_active", []), "history": state.get("prepare_history", [])})
    pfields = [
        "id","setup_key","side","trigger_type","opened_iso","closed_iso","status",
        "prepare_price","trigger","atr_open","trend_1h","ema_position","ema_distance_atr",
        "volume_ratio","regime","oi_dir","cvd_dir","formal_promoted","formal_trade_id",
        "formal_delay_min","mfe_atr","mae_atr","mfe_atr_30","mae_atr_30",
        "mfe_atr_60","mae_atr_60","duration_min"
    ]
    with open(PREPARE_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=pfields, extrasaction="ignore")
        w.writeheader(); w.writerows(prepare_rows)

    setup_rows = state.get("setup_lifecycle", [])
    save_json(SETUPS_JSON, setup_rows)
    sfields = ["setup_key","side","type","label","trigger","defense","first_seen_iso","last_seen_iso","last_price","status","last_note","block_count"]
    with open(SETUPS_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=sfields, extrasaction="ignore")
        w.writeheader(); w.writerows(setup_rows)


def major_news_class(title):
    t = title.lower()
    groups = [
        (("fomc", "federal reserve", " fed ", "powell", "rate decision", "interest rate"), "聯準會／利率政策"),
        (("cpi", "inflation", "pce", "nonfarm", "payroll", "jobs report"), "美國重要經濟數據"),
        (("bitcoin etf", "spot bitcoin etf", "btc etf"), "比特幣 ETF"),
        (("hack", "hacked", "exploit", "withdrawal halt", "withdrawals suspended", "depeg"), "交易所／穩定幣系統性風險"),
        (("sec", "cftc", "regulation", "regulatory"), "重大監管消息"),
    ]
    for keys, label in groups:
        if any(k in t for k in keys):
            return label
    return None


def chinese_news_summary(title, category):
    # Fallback only: used if the online title translation request fails.
    if category == "聯準會／利率政策":
        return "聯準會或利率政策出現新消息，可能放大 BTC 短線波動。"
    if category == "美國重要經濟數據":
        return "美國通膨或就業等重要經濟數據出現新消息，可能影響風險資產波動。"
    if category == "比特幣 ETF":
        return "比特幣現貨 ETF 出現重要消息，可能影響 BTC 資金流與短線情緒。"
    if category == "交易所／穩定幣系統性風險":
        return "加密市場出現安全、提款或穩定幣風險消息，需注意流動性與波動。"
    if category == "重大監管消息":
        return "加密貨幣監管出現重要消息，可能提高 BTC 短線不確定性。"
    return "加密市場出現重要消息，請注意短線波動。"


def translate_news_title_zh(title, category):
    """Best-effort English -> Traditional Chinese title translation.

    Uses Google's public translate endpoint without an API key. If the
    service is unavailable, malformed, rate-limited, or returns an empty
    result, fall back to the deterministic Chinese category summary so a
    translation failure can never break the radar.
    """
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "zh-TW",
                "dt": "t",
                "q": title,
            },
            timeout=10,
            headers={"User-Agent": "Laser-Signal-3.1/1.0"},
        )
        r.raise_for_status()
        data = r.json()
        parts = data[0] if isinstance(data, list) and data else []
        translated = "".join(
            str(part[0]) for part in parts
            if isinstance(part, list) and part and part[0]
        ).strip()
        if translated and translated.lower() != title.lower():
            return translated, False
    except Exception as e:
        print("News translation error:", type(e).__name__, e)
    return chinese_news_summary(title, category), True


def news_direction(title, category):
    """Conservative headline-only assessment: no guessing from generic mentions."""
    t = re.sub(r"\s+", " ", title.lower()).strip()
    # Negation / hypothetical / speculative headlines cannot be read as settled events.
    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in (
        "may", "might", "could", "would", "if", "rumor", "rumour", "prediction",
        "predicts", "potential", "debate", "asks", "expects", "forecast", "eyes")):
        return None
    triggers = (
        (r"\b(?:spot bitcoin|bitcoin|btc) etf\b.*\b(?:inflows?|record inflows?)\b|\betf inflows?\b", "偏多", "ETF 資金流入，可能支持買盤", "ETF 流入"),
        (r"\b(?:spot bitcoin|bitcoin|btc) etf\b.*\boutflows?\b|\betf outflows?\b", "偏空", "ETF 資金流出，可能增加賣壓", "ETF 流出"),
        (r"\b(?:fed|federal reserve)\b.*\b(?:cuts?|cutting|lowered) (?:interest )?rates?\b|\brate cut (?:by|from|to)\b", "偏多", "降息通常改善風險偏好，但仍須觀察市場預期", "政策降息"),
        (r"\b(?:fed|federal reserve)\b.*\b(?:hikes?|raised|raises) (?:interest )?rates?\b|\brate hike (?:by|from|to)\b", "偏空", "升息可能壓抑風險資產", "政策升息"),
        (r"\b(?:exchange|bitcoin|btc|crypto|stablecoin|usdt|usdc)\b.*\b(?:hacked|hack|exploit|withdrawals suspended|withdrawal halt|depegged|depeg)\b", "偏空", "安全或流動性事件，短期風險可能升高", "市場安全風險"),
        (r"\b(?:sec|regulator|court)\b.*\b(?:approves?|approved)\b.*\b(?:spot bitcoin etf|bitcoin etf)\b", "偏多", "比特幣 ETF 核准可能改善市場預期", "ETF 核准"),
        (r"\b(?:sec|regulator|court)\b.*\b(?:rejects?|rejected|bans?|banned)\b.*\b(?:spot bitcoin etf|bitcoin etf)\b", "偏空", "ETF 遭拒或限制可能影響市場信心", "ETF 遭拒"),
    )
    hits = [(direction, impact, key) for pattern, direction, impact, key in triggers if re.search(pattern, t)]
    if len({x[0] for x in hits}) != 1 or not hits:
        return None
    direction, impact, key = hits[0]
    return direction, impact, key


def _rss_entries(url):
    r = requests.get(url, timeout=20, headers={"User-Agent": "Laser-Signal-4.1/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.findall(".//item")[:25]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        date = (item.findtext("pubDate") or "").strip()
        out.append((title, link, date))
    if not out:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for item in root.findall(".//a:entry", ns)[:25]:
            title = (item.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_el = item.find("a:link", ns)
            link = (link_el.get("href") if link_el is not None else "") or ""
            date = (item.findtext("a:published", default="", namespaces=ns) or "").strip()
            out.append((title, link.strip(), date))
    return out


def check_news(state):
    news_state = state.setdefault("news", {"seen": [], "last_notify": 0})
    seen_list = list(news_state.get("seen", []))
    seen = set(seen_list)
    recent_topics = news_state.setdefault("recent_topics", {})
    now = now_utc()
    recent_topics = {k: int(v) for k, v in recent_topics.items()
                     if isinstance(v, (int, float)) and now - int(v) < NEWS_SIMILAR_COOLDOWN}
    news_state["recent_topics"] = recent_topics
    items = []
    for url in FEEDS:
        try:
            for title, link, published in _rss_entries(url):
                if not title:
                    continue
                nid = hashlib.sha1((title + link).encode("utf-8", "ignore")).hexdigest()[:16]
                if nid in seen:
                    continue
                seen.add(nid)
                seen_list.append(nid)
                cat = major_news_class(" " + title + " ")
                if not cat:
                    continue
                try:
                    dt = parsedate_to_datetime(published) if published else None
                    if dt is None or dt.tzinfo is None:
                        continue  # No trustworthy publication timestamp; don't alert.
                    age = now - int(dt.timestamp())
                    if not 0 <= age <= NEWS_MAX_AGE:
                        continue
                except (TypeError, ValueError, OverflowError):
                    continue
                assessment = news_direction(title, cat)
                if not assessment:
                    continue  # Generic coverage or uncertain direction: silent.
                direction, impact, topic = assessment
                key = f"{topic}:{direction}"
                if key in recent_topics:
                    continue
                items.append((nid, title, link, cat, direction, impact, key, int(dt.timestamp())))
        except Exception as e:
            print("News feed error:", e)
    news_state["seen"] = seen_list[-500:]
    if not items:
        return
    if now - int(news_state.get("last_notify", 0)) < NEWS_NOTIFY_COOLDOWN:
        return
    # Send only the most recent materially directional event.
    _, title, link, cat, direction, impact, topic_key, _ = max(items, key=lambda x: x[-1])
    zh, fallback = translate_news_title_zh(title, cat)
    message = (f"📰 BTC 重大新聞提醒\n"
               f"類型：{cat}\n"
               f"方向：{direction}（依新聞標題初步判讀）\n"
               f"中文摘要：{zh}\n"
               f"原文：{title}\n"
               f"影響：{impact}\n"
               f"備註：非交易訊號")
    send_discord(message)
    news_state["last_notify"] = now
    recent_topics[topic_key] = now


def maybe_prepare(state, side, setup, price, trend, ema_pos, vol, reg, oi, cvd):
    sig = setup_signature(setup)
    # Prepare is optional. If this setup already became Formal, never send a late Prepare.
    if sig in state["formal_seen"]:
        return
    if sig in state["prepare_seen"]:
        return
    if side == "LONG":
        dist = (setup["trigger"] - price) / atr15_global
    else:
        dist = (price - setup["trigger"]) / atr15_global
    touch_setup_lifecycle(state, side, setup, price)
    if 0 <= dist <= PREPARE_DISTANCE_ATR:
        state["prepare_seen"].append(sig)
        state["prepare_seen"] = state["prepare_seen"][-500:]
        create_prepare_validation(state, side, setup, price, atr15_global, trend, ema_pos, vol, reg, oi, cvd)
        touch_setup_lifecycle(state, side, setup, price, "PREPARED", "進入準備距離")
        flow, icon = flow_quality(side, oi, cvd)
        send_discord(
            f"👀 BTC {zh_side(side)}準備\n"
            f"1H背景：{zh_dir(trend['state'])}\n"
            f"15M結構：{setup['label']}準備\n"
            f"觸發價：${setup['trigger']:,.0f}\n目前：${price:,.0f}\n"
            f"EMA位置：{ema_pos['label']}（{ema_pos['distance_atr']:.2f} ATR）\n"
            f"成交量：{vol['ratio']:.2f}×（{vol['label']}）\n"
            f"市場狀態：{'趨勢' if reg['state']=='TREND' else '震盪'}\n"
            f"資金流：{icon} {flow}｜OI {zh_dir(oi['dir'])} / CVD Proxy {zh_dir(cvd['dir'])}\n"
            f"狀態：等待突破 ${setup['trigger']:,.0f}"
        )




def entry_volume_floor(setup):
    """Lower participation is acceptable on a trend pullback; a breakout needs confirmation."""
    return VOL_PULLBACK_MIN if setup.get("kind") == "PULLBACK" else VOL_BREAKOUT_MIN


def structure_first_formal(side, setup, price, a, trend, ema_pos, vol, reg, oi, cvd,
                         ext, freshness, geometry, plan, space):
    """4.1 formal eligibility: trend, trigger, reasonable risk and clear forward path.

    OI/CVD and weaker context signals grade a trade but never override hard risk checks.
    """
    kind = setup.get("kind")
    trend_needed = "BULL" if side == "LONG" else "BEAR"
    if kind not in ("PULLBACK", "BREAKOUT") or trend.get("state") != trend_needed:
        return {"ready": False, "reason": "TREND_MISMATCH", "side": side, "setup": setup,
                "plan": plan, "space": space, "ext": ext, "freshness": freshness, "geometry": geometry}
    vr = float(vol.get("ratio") or 0)
    if vr < entry_volume_floor(setup):
        return {"ready": False, "reason": "LOW_VOLUME", "side": side, "setup": setup,
                "plan": plan, "space": space, "ext": ext, "freshness": freshness, "geometry": geometry}
    if kind == "PULLBACK" and float(ema_pos.get("distance_atr") or 999) > PULLBACK_EMA_DISTANCE_MAX_ATR:
        return {"ready": False, "reason": "PULLBACK_FAR_FROM_EMA", "side": side, "setup": setup,
                "plan": plan, "space": space, "ext": ext, "freshness": freshness, "geometry": geometry}
    if freshness.get("state") == "STALE":
        return {"ready": False, "reason": "STALE_TRIGGER", "side": side, "setup": setup,
                "plan": plan, "space": space, "ext": ext, "freshness": freshness, "geometry": geometry}
    if float(plan.get("risk_atr") or 999) > MAX_STOP_ATR:
        return {"ready": False, "reason": "RISK_TOO_LARGE", "side": side, "setup": setup,
                "plan": plan, "space": space, "ext": ext, "freshness": freshness, "geometry": geometry}
    # Preserve trade quality: do not buy/sell directly into a strong nearby obstacle.
    # Weak/normal structure is informational, not an automatic veto.
    strong_space_r = space.get("space_r")
    if strong_space_r is not None and float(strong_space_r) < 0.80:
        return {"ready": False, "reason": "STRONG_OBSTACLE_NEAR", "side": side, "setup": setup,
                "plan": plan, "space": space, "ext": ext, "freshness": freshness, "geometry": geometry}
    if geometry.get("class") == "POOR":
        return {"ready": False, "reason": "POOR_ENTRY_GEOMETRY", "side": side, "setup": setup,
                "plan": plan, "space": space, "ext": ext, "freshness": freshness, "geometry": geometry}
    if ext > EXT_WAIT_ATR:
        return {"ready": False, "reason": "OVEREXTENDED", "side": side, "setup": setup,
                "plan": plan, "space": space, "ext": ext, "freshness": freshness, "geometry": geometry}
    if EXT_WARN_ATR < ext <= EXT_WAIT_ATR and vr < 1.30:
        return {"ready": False, "reason": "CHASE_WAIT", "side": side, "setup": setup,
                "plan": plan, "space": space, "ext": ext, "freshness": freshness, "geometry": geometry}

    context = formal_context(side, setup, trend, reg, ema_pos, vol, oi, cvd,
                             ext, space, freshness, geometry)
    # Context is supplementary in the original trend-following plan.
    if context.get("class") == "CONFLICT":
        context = {**context, "class": "MIXED", "label": "🟡 混合",
                   "reason": "順勢主條件已滿足；輔助資訊提醒：" + context.get("reason", "")}
    calibration = {"decision": "CAUTION" if context["class"] == "MIXED" else "ACCEPT",
                   "label": "保守" if context["class"] == "MIXED" else "正常",
                   "reason": "4.1 結構與風險條件通過，資金流資訊僅作評分"}
    timing = {"allow": True, "class": "CORE", "label": "核心進場", "reason": "15M 結構觸發且未過度追價", "size_mult": 1.0}
    feasibility = {"decision": "CAUTION" if context["class"] == "MIXED" else "ACCEPT",
                   "class": "CORE", "label": "核心結構", "reason": "前方強結構已通過最低空間要求",
                   "penetration": None, "penetration_reason": None}
    if context["class"] == "MIXED":
        plan["position_size_factor"] = min(float(plan.get("position_size_factor") or 1.0), 0.85)
    exit_info = apply_exit_profile(plan, context, space, reg, vol, freshness, geometry)
    quality = "HIGH" if (context["class"] == "CONSISTENT" and freshness.get("state") == "FRESH"
                         and geometry.get("class") == "BALANCED") else (
                         "CAUTION" if context["class"] == "MIXED" else "STANDARD")
    return {"ready": True, "side": side, "setup": setup, "context": context, "plan": plan,
            "space": space, "ext": ext, "freshness": freshness, "geometry": geometry,
            "exit_info": exit_info, "quality_state": quality, "calibration": calibration,
            "entry_timing": timing, "feasibility": feasibility, "volume": vol}


def evaluate_formal_candidate(side, setup, price, rows15, a, trend, ema_pos, vol, reg, oi, cvd, event_rows=None):
    """Single source of truth for Laser Signal 4.1 formal eligibility."""
    ext = (price - setup["trigger"])/a if side == "LONG" else (setup["trigger"] - price)/a
    wh, wl = window_high_low(event_rows or [], price)
    if setup.get("kind") == "BREAKOUT":
        br = breakout_acceptance(side, setup, price, a, event_rows)
        setup["breakout_stage"] = br["stage"]
        if not br["wick_break"]:
            return {"ready": False, "reason": "TRIGGER_NOT_CROSSED", "side": side, "setup": setup, "ext": ext}
        if not br["accepted"]:
            return {"ready": False, "reason": "BREAKOUT_NOT_ACCEPTED", "side": side, "setup": setup, "ext": ext,
                    "breakout": br}
    else:
        trigger_level = setup["trigger"] + BREAK_BUFFER_ATR*a if side == "LONG" else setup["trigger"] - BREAK_BUFFER_ATR*a
        crossed = wh >= trigger_level if side == "LONG" else wl <= trigger_level
        if not crossed:
            return {"ready": False, "reason": "TRIGGER_NOT_CROSSED", "side": side, "setup": setup, "ext": ext}

    freshness = trigger_freshness(side, setup, price, a, event_rows)
    if side == "LONG" and price < setup["trigger"] - TRIGGER_RETEST_ATR*a:
        return {"ready": False, "reason": "TRIGGER_RETEST_FAILED", "side": side, "setup": setup,
                "ext": ext, "freshness": freshness}
    if side == "SHORT" and price > setup["trigger"] + TRIGGER_RETEST_ATR*a:
        return {"ready": False, "reason": "TRIGGER_RETEST_FAILED", "side": side, "setup": setup,
                "ext": ext, "freshness": freshness}

    plan = risk_plan(setup, side, price, a, ema_pos, reg)
    if not plan:
        return {"ready": False, "reason": "NO_RISK_PLAN", "side": side, "setup": setup,
                "ext": ext, "freshness": freshness}
    space = space_context(rows15, side, price, plan["risk"], setup["trigger_time"], a)
    geometry = trade_geometry(plan, space, ext)

    return structure_first_formal(side, setup, price, a, trend, ema_pos, vol, reg, oi, cvd,
                                ext, freshness, geometry, plan, space)


def preview_formal_candidate(side, setup, price, rows15, a, trend, ema_pos, vol, reg, oi, cvd, event_rows=None):
    return evaluate_formal_candidate(side, setup, price, rows15, a, trend, ema_pos, vol, reg, oi, cvd, event_rows)




def choose_arbitration_winner(candidates, trend):
    """Choose an eligible setup by actual 4.1 fields; avoid legacy phantom weights."""
    ready = [c for c in candidates if c and c.get("ready")]
    if not ready:
        return None
    rank = {"HIGH": 3, "STANDARD": 2, "CAUTION": 1}
    fresh = {"FRESH": 2, "AGING": 1, "STALE": 0}

    def key(c):
        plan = c.get("plan") or {}
        space = c.get("space") or {}
        front = space.get("space_r")
        return (
            rank.get(c.get("quality_state"), 0),
            fresh.get((c.get("freshness") or {}).get("state"), 0),
            1 if c.get("setup", {}).get("kind") == "PULLBACK" else 0,
            min(float(front), 3.0) if front is not None else 3.0,
            -abs(float(c.get("ext") or 0)),
            -float(plan.get("risk_atr") or 99),
            int(c.get("setup", {}).get("trigger_time") or 0),
        )

    return max(ready, key=key)

def active_valid_trade(state):
    """Only one live BTC thesis/position is allowed at a time."""
    for t in state.get("trades", []):
        if t.get("status") == "OPEN" and not t.get("excluded_from_stats"):
            return t
    return None


def _blocked_extra(ev, vol):
    calibration = ev.get("calibration") or {}
    return {
        "volume_ratio": vol.get("ratio"),
        "space_r": (ev.get("space") or {}).get("space_r"),
        "extension_atr": ev.get("ext"),
        "context_class": (ev.get("context") or {}).get("class", "UNASSESSED"),
        "context_reason": calibration.get("reason") or (ev.get("context") or {}).get("reason", ev.get("reason")),
        "trigger_freshness": (ev.get("freshness") or {}).get("state"),
        "trigger_age_min": (ev.get("freshness") or {}).get("age_min"),
        "geometry_class": (ev.get("geometry") or {}).get("class"),
        "quality_decision": calibration.get("decision"),
        "quality_reason": calibration.get("reason"),
        "entry_timing": (ev.get("entry_timing") or {}).get("class"),
        "entry_timing_reason": (ev.get("entry_timing") or {}).get("reason"),
        "feasibility_class": (ev.get("feasibility") or {}).get("class"),
        "feasibility_reason": (ev.get("feasibility") or {}).get("reason"),
        "penetration_quality": (ev.get("feasibility") or {}).get("penetration"),
        "penetration_reason": (ev.get("feasibility") or {}).get("penetration_reason"),
    }


def formal_check(state, side, setup, price, rows15, a, trend, ema_pos, vol, reg, oi, cvd, event_rows=None):
    sig = setup_signature(setup)
    if sig in state["formal_seen"]:
        return True

    ev = evaluate_formal_candidate(side, setup, price, rows15, a, trend, ema_pos, vol, reg, oi, cvd, event_rows)
    if not ev.get("ready"):
        reason_map = {
            "TRIGGER_NOT_CROSSED": None,
            "BREAKOUT_NOT_ACCEPTED": None,
            "TRIGGER_RETEST_FAILED": "Trigger穿越後未能守住",
            "NO_RISK_PLAN": "無法建立有效風控",
            "LOW_VOLUME": "成交量低於該型態門檻",
            "TREND_MISMATCH": "不符合1H趨勢方向或不是核心進場型態",
            "PULLBACK_FAR_FROM_EMA": "回踩位置距離EMA區域太遠",
            "STALE_TRIGGER": "進場觸發已過期",
            "RISK_TOO_LARGE": f"停損距離過大（{float((ev.get('plan') or {}).get('risk_atr') or 0):.2f} ATR）",
            "OVEREXTENDED": "價格過度延伸",
            "CHASE_WAIT": "偏追價，等待較好位置",
            "STRONG_OBSTACLE_NEAR": "前方強支撐壓力距離不足",
            "POOR_ENTRY_GEOMETRY": "進場、止損與前方空間搭配不佳",
            "CONTEXT_CONFLICT": f"情境衝突：{(ev.get('context') or {}).get('reason','整體條件不一致')}",
            "QUALITY_CALIBRATION": f"品質校準：{(ev.get('calibration') or {}).get('reason','多個普通弱點同時疊加')}",
            "ENTRY_TIMING_WEAK": f"進場時機：{(ev.get('entry_timing') or {}).get('reason','延伸進場缺乏足夠動能')}",
            "FEASIBILITY_WEAK": f"發展空間：{(ev.get('feasibility') or {}).get('reason','前方結構與市場力量不匹配')}",
        }
        reason = reason_map.get(ev.get("reason"))
        if reason:
            record_blocked(state, side, setup, reason, price, _blocked_extra(ev, vol))
        return False

    plan = ev["plan"]; space = ev["space"]; context = ev["context"]
    freshness = ev["freshness"]; geometry = ev["geometry"]; exit_info = ev["exit_info"]
    entry_timing = ev.get("entry_timing") or {"class": "NORMAL", "label": "正常時機", "reason": ""}
    feasibility = ev.get("feasibility") or {"class": "WORKABLE", "label": "可發展", "reason": ""}
    flow, icon = flow_quality(side, oi, cvd)
    ctx = {
        "extension_atr": ev["ext"], "trend_1h": trend["state"],
        "ema_position": ema_pos["label"], "ema_distance_atr": ema_pos.get("distance_atr"),
        "volume_ratio": vol["ratio"], "regime": reg["state"], "space_r": space["space_r"],
        "space_strength": space.get("nearest_strength"),
        "oi_dir": oi["dir"], "cvd_dir": cvd["dir"], "flow_quality": flow,
        "context_class": context["class"], "context_reason": context["reason"],
        "quality_state": ev.get("quality_state"),
        "quality_decision": (ev.get("calibration") or {}).get("decision"),
        "quality_reason": (ev.get("calibration") or {}).get("reason"),
        "trigger_freshness": freshness.get("state"), "trigger_age_min": freshness.get("age_min"),
        "trigger_hold_atr": freshness.get("hold_atr"),
        "geometry_class": geometry.get("class"), "geometry_reason": geometry.get("reason"),
        "exit_profile": exit_info.get("profile"),
        "entry_timing": entry_timing.get("class"), "entry_timing_label": entry_timing.get("label"),
        "entry_timing_reason": entry_timing.get("reason"),
        "feasibility_class": feasibility.get("class"), "feasibility_label": feasibility.get("label"),
        "feasibility_reason": feasibility.get("reason"),
        "penetration_quality": feasibility.get("penetration"), "penetration_reason": feasibility.get("penetration_reason"),
    }
    t = create_trade(state, side, setup, plan, ctx)
    touch_setup_lifecycle(state, side, setup, price, "FORMAL",
                          f"建立模擬單 #{t['id']}｜{context['label']}｜Trigger {freshness['label']}｜幾何{geometry['label']}")
    mark_prepare_promoted(state, setup["setup_key"], t["id"])
    state["formal_seen"].append(sig)
    state["formal_seen"] = state["formal_seen"][-500:]
    state["active_thesis"] = {
        "side": side, "setup_key": setup["setup_key"], "source": "FORMAL",
        "context_class": context["class"], "updated_at": now_utc(), "updated_iso": iso(now_utc())
    }

    if plan.get("tp2") is None:
        targets_text = f"止盈：1R（${plan['tp1']:,.0f}）"
    else:
        targets_text = (f"TP1：1R（${plan['tp1']:,.0f}）\n"
                        f"TP2：{float(plan.get('tp2_r') or 2.0):.2g}R（${plan['tp2']:,.0f}）")

    if space.get("nearest_target") is None:
        space_text = "開放"
    else:
        shown_r = space.get("nearest_r")
        rtxt = f"{shown_r:.2f}R" if shown_r is not None else "—"
        space_text = f"{rtxt}（{space['label']}）"
        if space.get("nearest_strength") == "STRONG" and shown_r is not None and shown_r < 1.0:
            space_text += " ⚠️ 前方強結構近"

    opposite = ((side == "LONG" and trend["state"] == "BEAR") or
                (side == "SHORT" and trend["state"] == "BULL"))
    bg_note = " ⚠️ 與1H背景反向" if opposite else ""
    age_txt = "—" if freshness.get("age_min") is None else f"{freshness['age_min']:.0f}分"
    send_discord(
        f"⚡ BTC 正式{zh_side(side)}訊號\n\n"
        f"進場：${plan['entry']:,.0f}\n停損：${plan['sl']:,.0f}\n{targets_text}\n"
        f"風險距離：{plan['risk_atr']:.2f} ATR（{risk_quality(plan['risk_atr'])}）\n"
        f"防守：{plan.get('defense_source','結構')}外＋{plan.get('stop_buffer_atr',0):.2f} ATR 緩衝｜倉位係數 {plan.get('position_size_factor',1.0):.2f}×\n\n"
        f"交易品質：{context['label']}｜Trigger {freshness['label']} {age_txt}｜幾何{geometry['label']}\n"
        f"1H背景：{zh_dir(trend['state'])}{bg_note}\n"
        f"15M觸發：{setup['label']}\n"
        f"EMA位置：{ema_pos['label']}（{ema_pos['distance_atr']:.2f} ATR）\n"
        f"成交量：{vol['ratio']:.2f}×（{vol['label']}）\n"
        f"前方空間：{space_text}\n"
        f"市場狀態：{'趨勢' if reg['state']=='TREND' else '震盪'}\n"
        f"資金流：{icon} {flow}｜OI {zh_dir(oi['dir'])} / CVD Proxy {zh_dir(cvd['dir'])}\n"
        f"進場時機：{entry_timing.get('label','正常時機')}｜{extension_quality(ev['ext'])}（{ev['ext']:.2f} ATR）\n"
        f"發展空間：{feasibility.get('label','可發展')}｜{feasibility.get('reason','')}\n"
        f"判斷：{context['reason']}\n"
        f"出場策略：{exit_info['profile']}\n\n"
        f"🧾 已建立模擬單 #{t['id']}"
    )
    return True


def preview_prepare_candidate(side, setup, price, rows15, a, trend, ema_pos, vol, reg, oi, cvd):
    """Prepare = approaching a valid trend-following structure, NOT a formal entry.

    Keep expensive/strict formal quality requirements for formal orders only.
    """
    dist = ((float(setup["trigger"]) - price) / a) if side == "LONG" else ((price - float(setup["trigger"])) / a)
    result = {"ready": False, "side": side, "setup": setup, "dist": dist}
    if trend.get("state") != ("BULL" if side == "LONG" else "BEAR"):
        return {**result, "reason": "PREPARE_TREND_MISMATCH"}
    if not (0 <= dist <= PREPARE_DISTANCE_ATR):
        return {**result, "reason": "PREPARE_DISTANCE"}
    if float(vol.get("ratio") or 0) < PREPARE_NOTIFY_VOL_MIN and dist > 0.08:
        return {**result, "reason": "PREPARE_LOW_PARTICIPATION"}
    plan = risk_plan(setup, side, price, a, ema_pos, reg)
    if not plan:
        return {**result, "reason": "PREPARE_NO_RISK_PLAN"}
    if float(plan.get("risk_atr") or 999) > MAX_STOP_ATR:
        return {**result, "reason": "PREPARE_RISK_TOO_LARGE"}
    space = space_context(rows15, side, price, plan["risk"], setup["trigger_time"], a)
    # Use forward space for a *ranking hint*, not a formal veto at prepare stage.
    return {**result, "ready": True, "space": space,
            "context": {"class": "CONSISTENT", "reason": "順勢結構接近觸發"},
            "geometry": {"class": "MANAGE"},
            "feasibility": {"class": "WORKABLE", "penetration": None}}


def choose_prepare_winner(candidates, trend, state):
    ready = [c for c in candidates if c and c.get("ready")]
    if not ready: return None
    cls_rank = {"CONSISTENT": 2, "MIXED": 1}
    geom_rank = {"BALANCED": 2, "MANAGE": 1, "POOR": 0}
    feas_rank = {"OPEN": 5, "POWER_THROUGH": 4, "WORKABLE": 4, "TIGHT": 2, "BLOCKED": 1}
    pen_rank = {"STRONG": 3, "ADEQUATE": 2, "WEAK": 1, None: 0}
    def key(c):
        feas = c.get("feasibility", {})
        return (
            feas_rank.get(feas.get("class"), 0),
            pen_rank.get(feas.get("penetration"), 0),
            cls_rank.get(c.get("context", {}).get("class"), 0),
            geom_rank.get(c.get("geometry", {}).get("class"), 0),
            -abs(float(c.get("dist") or 0.0)),
            int(c.get("setup", {}).get("trigger_time", 0)),
        )
    ordered = sorted(ready, key=key, reverse=True)
    winner = ordered[0]
    thesis = state.get("active_thesis") or {}
    old_side = thesis.get("side")
    old_age = (now_utc() - int(thesis.get("updated_at", 0) or 0))/60 if thesis.get("updated_at") else 999
    if old_side and old_side != winner["side"] and old_age < THESIS_HOLD_MIN:
        old = next((c for c in ready if c["side"] == old_side), None)
        if old is not None and key(winner) <= key(old): winner = old
    return winner

def manual_summary(state, price, trend, reg, oi, cvd):
    """Report the current v31 lineage only; legacy engines are intentionally ignored."""
    history = list(state.get("history", []))
    open_trades = list(state.get("trades", []))
    completed = len(history)
    open_n = len(open_trades)
    p_done = len(state.get("prepare_history", []))
    p_open = len(state.get("prepare_active", []))

    tp1_hits = sum(1 for x in history if x.get("tp1_hit"))
    sl_count = sum(1 for x in history if x.get("status") == "SL")
    quick_sl = sum(1 for x in history if x.get("status") == "SL" and int(x.get("duration_min") or 0) <= 30)
    if history:
        avg_mfe = sum(float(x.get("mfe_r") or 0.0) for x in history) / len(history)
        avg_mae = sum(float(x.get("mae_r") or 0.0) for x in history) / len(history)
        perf = (f"🎯 TP1達成：{tp1_hits}/{completed}｜SL：{sl_count}\n"
                f"📈 平均 MFE：+{avg_mfe:.2f}R｜平均 MAE：-{avg_mae:.2f}R\n"
                f"⚠️ 快速SL（≤30分）：{quick_sl}")
    else:
        perf = "🎯 雷射訊號 4.0 尚無完成樣本"

    msg = (
        f"📡 雷射訊號｜Laser Signal 4.0｜手動查詢\nBTC：${price:,.0f}\n"
        f"1H背景：{zh_dir(trend['state'])}\n市場狀態：{'趨勢' if reg['state']=='TREND' else '震盪'}\n"
        f"OI：{zh_dir(oi['dir'])}｜CVD Proxy：{zh_dir(cvd['dir'])}\n"
        f"👀 Prepare：完成 {p_done}｜追蹤中 {p_open}\n"
        f"⚡ 雷射訊號正式單：完成 {completed}｜進行中 {open_n}\n"
        f"{perf}"
    )
    send_discord(msg)


def main():
    global atr15_global
    state = load_state()
    rows15 = history_candles("15m", 240)
    rows1h = history_candles("1H", 160)
    live = ticker_price()
    current_bar = None
    try:
        current_bar = current_15m_candle()
    except Exception as e:
        print("Current 15m unavailable:", e)

    a = atr(rows15)
    if not a or a <= 0:
        raise RuntimeError("15M ATR unavailable")
    atr15_global = a
    trend = trend_background(rows1h)
    ema_pos = ema_position(rows15, live, a)
    vol = volume_context(rows15, current_bar)
    reg = regime(rows15, a)

    events = event_window(state)

    update_oi(state)
    oi = oi_context(state)
    cvd = cvd_proxy()

    update_trades(state, live, events)
    update_prepare_validations(state, live, events)
    try:
        check_news(state)
    except Exception as e:
        # A feed, translation, or Discord news outage must never stop trading detection.
        print("NEWS_WARNING: news delivery skipped; signal evaluation continues:", repr(e))

    if MANUAL_RUN:
        manual_summary(state, live, trend, reg, oi, cvd)
    else:
        setup_items = []
        previews = {}
        prepare_previews = {}
        for side in ("LONG", "SHORT"):
            for setup in collect_setups(rows15, a, side, live):
                skey = setup.get("setup_key")
                if not skey:
                    continue
                setup_items.append((side, setup))
                previews[skey] = preview_formal_candidate(
                    side, setup, live, rows15, a, trend, ema_pos, vol, reg, oi, cvd, events
                )
                prepare_previews[skey] = preview_prepare_candidate(
                    side, setup, live, rows15, a, trend, ema_pos, vol, reg, oi, cvd
                )

        active = active_valid_trade(state)
        # Audit rejections from ALL formal previews, not only the arbitration winner.
        # This is logging-only: it neither promotes nor suppresses a trade.
        if not active:
            for ev in previews.values():
                record_candidate_rejection(state, ev, live, vol)
        ready = []
        for ev in previews.values():
            if not ev.get("ready"):
                continue
            # While a Formal is open, the existing global arbitration already owns
            # duplicate suppression. The thesis guard is for re-entry after a trade closes.
            guard = {"allow": True, "reason": None} if active else duplicate_thesis_guard(state, ev, a)
            ev["duplicate_guard"] = guard
            if guard.get("allow"):
                ready.append(ev)
            else:
                record_blocked(
                    state, ev["side"], ev["setup"], guard.get("reason") or "同方向重複 thesis", live,
                    {**_blocked_extra(ev, vol), "context_reason": guard.get("reason")}
                )
        winner = None if active else choose_arbitration_winner(ready, trend)
        unresolved_tie = (not active and len(ready) >= 2 and winner is None)
        prepare_winner = None if active else choose_prepare_winner(prepare_previews.values(), trend, state)

        if active:
            state["active_thesis"] = {
                "side": active.get("side"), "setup_key": active.get("setup_key"), "source": "FORMAL",
                "context_class": active.get("context_class"), "updated_at": now_utc(), "updated_iso": iso(now_utc())
            }
        elif winner is not None:
            state["active_thesis"] = {
                "side": winner["side"], "setup_key": winner["setup"]["setup_key"], "source": "FORMAL_CANDIDATE",
                "context_class": winner.get("context", {}).get("class"), "updated_at": now_utc(), "updated_iso": iso(now_utc())
            }
        elif prepare_winner is not None:
            state["active_thesis"] = {
                "side": prepare_winner["side"], "setup_key": prepare_winner["setup"]["setup_key"], "source": "PREPARE",
                "context_class": prepare_winner.get("context", {}).get("class"), "updated_at": now_utc(), "updated_iso": iso(now_utc())
            }

        before_formal_count = len(state.get("trades", [])) + len(state.get("history", []))
        before_prepare_count = len(state.get("prepare_active", [])) + len(state.get("prepare_history", []))
        for side, setup in setup_items:
            skey = setup.get("setup_key")
            preview = previews.get(skey, {})

            if active:
                if preview.get("ready"):
                    relation = "同方向" if active.get("side") == side else "反向"
                    record_blocked(
                        state, side, setup, f"全局仲裁：已有{relation}正式單 #{active.get('id')} 進行中", live,
                        {**_blocked_extra(preview, vol),
                         "context_reason": "同一時間只保留一個有效BTC正式交易 thesis"}
                    )
                else:
                    touch_setup_lifecycle(state, side, setup, live, "DETECTED",
                                          f"已有正式單 #{active.get('id')} 進行中，暫不新增外部訊號")
                continue

            if unresolved_tie and preview.get("ready"):
                record_blocked(
                    state, side, setup, "全局仲裁：候選品質完全相同，等待市場自行選邊", live,
                    {**_blocked_extra(preview, vol),
                     "context_reason": "多個候選品質完全相同，暫不強行選擇"}
                )
                continue

            if winner is not None:
                winner_key = winner.get("setup", {}).get("setup_key")
                if skey != winner_key:
                    if preview.get("ready"):
                        record_blocked(
                            state, side, setup, f"全局仲裁：{winner.get('setup',{}).get('label', winner['side'])} thesis 品質優先", live,
                            {**_blocked_extra(preview, vol),
                             "context_reason": "同輪所有多空/型態候選共同競爭，只採用發展能力較完整的 thesis"}
                        )
                    else:
                        touch_setup_lifecycle(state, side, setup, live, "DETECTED",
                                              "本輪由更完整 thesis 主導，此候選僅內部追蹤")
                    continue
                became_formal = formal_check(
                    state, side, setup, live, rows15, a, trend, ema_pos, vol, reg, oi, cvd, events
                )
                if not became_formal and prepare_winner and skey == prepare_winner.get("setup", {}).get("setup_key"):
                    maybe_prepare(state, side, setup, live, trend, ema_pos, vol, reg, oi, cvd)
                continue

            # No Formal candidate: only the best current internal thesis may notify Prepare.
            if prepare_winner is not None and skey == prepare_winner.get("setup", {}).get("setup_key"):
                maybe_prepare(state, side, setup, live, trend, ema_pos, vol, reg, oi, cvd)
            else:
                touch_setup_lifecycle(state, side, setup, live, "DETECTED", "非本輪主要 thesis，僅內部追蹤")

        # A formally eligible candidate may be suppressed later (already-seen or
        # duplicate protection). Do not lose an unrelated valid Prepare opportunity.
        if (not active and prepare_winner is not None and
                len(state.get("trades", [])) + len(state.get("history", [])) == before_formal_count and
                len(state.get("prepare_active", [])) + len(state.get("prepare_history", [])) == before_prepare_count):
            maybe_prepare(state, prepare_winner["side"], prepare_winner["setup"], live,
                          trend, ema_pos, vol, reg, oi, cvd)

        # One summary per scheduled run; makes the missing-signal cause inspectable.
        from collections import Counter
        formal_reasons = Counter(ev.get("reason", "READY") if not ev.get("ready") else "READY" for ev in previews.values())
        prepare_reasons = Counter(ev.get("reason", "READY") if not ev.get("ready") else "READY" for ev in prepare_previews.values())
        print("SIGNAL_AUDIT", json.dumps({
            "setups": len(setup_items), "formal_ready": sum(bool(x.get("ready")) for x in previews.values()),
            "prepare_ready": sum(bool(x.get("ready")) for x in prepare_previews.values()),
            "formal_reasons": dict(formal_reasons), "prepare_reasons": dict(prepare_reasons),
            "formal_new": len(state.get("trades", [])) + len(state.get("history", [])) - before_formal_count,
            "prepare_new": len(state.get("prepare_active", [])) + len(state.get("prepare_history", [])) - before_prepare_count,
        }, ensure_ascii=False, sort_keys=True))

    export_data(state)
    save_json(STATE_FILE, state)
    valid_open = [x for x in state.get("trades", []) if not x.get("excluded_from_stats")]
    valid_history = [x for x in state.get("history", []) if not x.get("excluded_from_stats")]
    print(f"{BUILD}: BTC={live:.0f} 1H={trend['state']} Regime={reg['state']} Vol={vol['ratio']:.2f}x Open={len(valid_open)} Done={len(valid_history)}")


if __name__ == "__main__":
    main()
