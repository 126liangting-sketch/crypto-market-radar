import os
import csv
import json
import math
import time
import hashlib
from datetime import datetime, timezone

import requests
import xml.etree.ElementTree as ET

OKX_BASE = "https://www.okx.com"
OKX_SYMBOL = "BTC-USDT-SWAP"
WEBHOOK = os.getenv("DISCORD_WEBHOOK")
MANUAL_RUN = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

VERSION = "BTC_RADAR_CLEAN_V1_ENGINE"
STATE_FILE = "radar_state_clean_v1.json"
TRADES_JSON = "paper_trades_clean_v1.json"
TRADES_CSV = "paper_trades_clean_v1.csv"
BLOCKED_JSON = "blocked_setups_clean_v1.json"
BLOCKED_CSV = "blocked_setups_clean_v1.csv"
PREPARE_JSON = "prepare_validation_clean_v1.json"
PREPARE_CSV = "prepare_validation_clean_v1.csv"
SETUPS_JSON = "setup_lifecycle_clean_v1.json"
SETUPS_CSV = "setup_lifecycle_clean_v1.csv"

# ---- Clean V1 rules agreed in chat ----
EMA_FAST = 34
EMA_SLOW = 50
VOL_LOOKBACK = 20
VOL_HARD_MIN = 0.70
PULLBACK_MIN_ATR = 0.25
IMPULSE_MIN_ATR = 0.60
BREAK_BUFFER_ATR = 0.05
PREPARE_DISTANCE_ATR = 0.25
EXT_NORMAL_ATR = 0.30
EXT_WARN_ATR = 0.50
EXT_WAIT_ATR = 0.75
SPACE_MIN_R = 1.00
SPACE_GOOD_R = 1.50
STOP_BUFFER_ATR = 0.15
MIN_STOP_ATR = 0.50
RISK_WARN_ATR = 1.50
RISK_HIGH_ATR = 2.00
MAX_STOP_ATR = 2.50
PREPARE_VALIDATE_MIN = 60
TRIGGER_RETEST_ATR = 0.10
EVENT_LOOKBACK_MIN = 12
OI_FLAT_PCT = 0.0010
CVD_FLAT_REL = 0.05
NEWS_NOTIFY_COOLDOWN = 3 * 3600

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
                             headers={"User-Agent": "BTC-Radar-Clean-V1/1.0"})
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


def significant_pivots(rows, a, min_move_atr=0.35):
    """1-1 pivots internally, then remove tiny alternating noise. No Major/Micro product concept."""
    highs, lows = local_pivots(rows, 1, 1)
    pts = [(t, p, i, "H") for t, p, i in highs] + [(t, p, i, "L") for t, p, i in lows]
    pts.sort(key=lambda x: x[0])
    if not pts:
        return [], []

    compressed = []
    for pt in pts:
        if not compressed:
            compressed.append(pt)
            continue
        prev = compressed[-1]
        if pt[3] == prev[3]:
            better = (pt[1] > prev[1]) if pt[3] == "H" else (pt[1] < prev[1])
            if better:
                compressed[-1] = pt
            continue
        if abs(pt[1] - prev[1]) >= min_move_atr * a:
            compressed.append(pt)
    highs2 = [(t, p, i) for t, p, i, k in compressed if k == "H"]
    lows2 = [(t, p, i) for t, p, i, k in compressed if k == "L"]
    return highs2, lows2


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


def detect_breakout_setup(rows15, a, side):
    # Trigger uses the cleaned/significant pivot, but the stop-defense point should use
    # the nearest local pivot before that trigger. Using the previous significant pivot
    # can be several ATR away and creates unrealistic stops.
    highs, lows = significant_pivots(rows15, a, 0.35)
    local_highs, local_lows = local_pivots(rows15, 1, 1)
    if side == "LONG" and highs:
        h = highs[-1]
        prior_sig_lows = [l for l in lows if l[0] < h[0]]
        if prior_sig_lows and h[1] - prior_sig_lows[-1][1] >= IMPULSE_MIN_ATR * a:
            prior_local_lows = [l for l in local_lows if l[0] < h[0]]
            defense = prior_local_lows[-1] if prior_local_lows else prior_sig_lows[-1]
            return {"side": side, "kind": "BREAKOUT", "label": "有效突破",
                    "trigger": h[1], "trigger_time": h[0], "defense": defense[1],
                    "defense_time": defense[0], "setup_key": f"BO:L:{h[0]}"}
    if side == "SHORT" and lows:
        l = lows[-1]
        prior_sig_highs = [h for h in highs if h[0] < l[0]]
        if prior_sig_highs and prior_sig_highs[-1][1] - l[1] >= IMPULSE_MIN_ATR * a:
            prior_local_highs = [h for h in local_highs if h[0] < l[0]]
            defense = prior_local_highs[-1] if prior_local_highs else prior_sig_highs[-1]
            return {"side": side, "kind": "BREAKOUT", "label": "有效突破",
                    "trigger": l[1], "trigger_time": l[0], "defense": defense[1],
                    "defense_time": defense[0], "setup_key": f"BO:S:{l[0]}"}
    return None


def choose_setup(rows15, a, side, live_price):
    candidates = []
    for s in (detect_pullback_setup(rows15, a, side), detect_breakout_setup(rows15, a, side)):
        if not s:
            continue
        # Don't reuse stale structures far behind price unless price is still near/through trigger.
        if side == "LONG":
            d = (live_price - s["trigger"]) / a
        else:
            d = (s["trigger"] - live_price) / a
        s = dict(s)
        s["extension_atr"] = d
        candidates.append(s)
    if not candidates:
        return None
    # Prefer pullback continuation when it is actionable; otherwise freshest trigger.
    actionable = [x for x in candidates if x["extension_atr"] >= -PREPARE_DISTANCE_ATR]
    pool = actionable or candidates
    pool.sort(key=lambda x: (x["kind"] == "PULLBACK", x["trigger_time"]), reverse=True)
    return pool[0]


def extension_quality(ext):
    if ext <= EXT_NORMAL_ATR:
        return "正常"
    if ext <= EXT_WARN_ATR:
        return "稍微延伸"
    if ext <= EXT_WAIT_ATR:
        return "偏追價"
    return "過度延伸"


def risk_quality(risk_atr):
    if risk_atr <= RISK_WARN_ATR:
        return "正常"
    if risk_atr <= RISK_HIGH_ATR:
        return "偏大"
    if risk_atr <= MAX_STOP_ATR:
        return "高風險"
    return "過大"


def risk_plan(setup, side, entry, a):
    if side == "LONG":
        raw_sl = float(setup["defense"]) - STOP_BUFFER_ATR * a
        min_sl = entry - MIN_STOP_ATR * a
        sl = min(raw_sl, min_sl)
        risk = entry - sl
        tp1, tp2 = entry + risk, entry + 2*risk
    else:
        raw_sl = float(setup["defense"]) + STOP_BUFFER_ATR * a
        min_sl = entry + MIN_STOP_ATR * a
        sl = max(raw_sl, min_sl)
        risk = sl - entry
        tp1, tp2 = entry - risk, entry - 2*risk
    if risk <= 0:
        return None
    return {"entry": entry, "sl": sl, "risk": risk, "risk_atr": risk/a,
            "tp1": tp1, "tp2": tp2}


def _pivot_is_effective(rows15, pivot, side, a, same_side_pivots):
    """Return True only for a structure level with a real reaction or repeated tests.

    This deliberately does NOT change the Space >= 1R hard rule. It only prevents
    tiny local wiggles from being treated as the next meaningful obstacle.
    """
    t, price, idx = pivot
    if a <= 0:
        return True

    # A close level can still be important when price clearly reacted from it.
    left = rows15[max(0, idx-4):idx]
    right = rows15[idx+1:min(len(rows15), idx+5)]
    reaction = 0.0
    if left and right:
        if side == "LONG":  # testing a prior swing high as resistance
            before = price - min(float(x["low"]) for x in left)
            after = price - min(float(x["low"]) for x in right)
        else:               # testing a prior swing low as support
            before = max(float(x["high"]) for x in left) - price
            after = max(float(x["high"]) for x in right) - price
        reaction = min(before, after) / a

    # Repeated pivots in roughly the same price zone also make the level meaningful.
    zone = 0.15 * a
    touches = sum(1 for tt, pp, ii in same_side_pivots
                  if tt != t and abs(float(pp) - float(price)) <= zone)

    return reaction >= 0.30 or touches >= 1


def space_context(rows15, side, entry, risk, trigger_time, a):
    highs, lows = local_pivots(rows15, 2, 2)
    pivots = highs if side == "LONG" else lows

    effective = []
    for pivot in pivots:
        t, p, _ = pivot
        if t == trigger_time:
            continue
        if side == "LONG" and p <= entry:
            continue
        if side == "SHORT" and p >= entry:
            continue
        if _pivot_is_effective(rows15, pivot, side, a, pivots):
            effective.append(float(p))

    if side == "LONG":
        target = min(effective) if effective else None
        space_r = ((target-entry)/risk) if target is not None else None
    else:
        target = max(effective) if effective else None
        space_r = ((entry-target)/risk) if target is not None else None

    if space_r is None:
        label = "開放"
    elif space_r < 1.0:
        label = "不足"
    elif space_r < 1.5:
        label = "普通"
    elif space_r < 2.0:
        label = "良好"
    else:
        label = "很好"
    return {"target": target, "space_r": space_r, "label": label}


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
    s.setdefault("version", VERSION)
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
    # Historical trades created before the risk guard remain visible, but they must not
    # contaminate performance statistics for the current ruleset.
    for t in s.get("trades", []) + s.get("history", []):
        if float(t.get("risk_atr") or 0.0) > MAX_STOP_ATR:
            t["excluded_from_stats"] = True
            t["legacy_rule_mismatch"] = True
    return s


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def send_discord(msg):
    msg = str(msg).strip()
    print(msg)
    if not WEBHOOK:
        print("缺少 DISCORD_WEBHOOK，僅輸出到 log")
        return
    sep = "━━━━━━━━━━━━━━━━━━"
    body = f"{sep}\n{msg}\n{sep}"
    last = None
    for i in range(3):
        try:
            r = requests.post(WEBHOOK, json={"content": body}, timeout=20)
            print("Discord HTTP:", r.status_code)
            r.raise_for_status()
            return
        except requests.RequestException as e:
            last = e
            if i < 2:
                time.sleep(2**i)
    raise last


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
            "side": side, "type": setup["kind"], "reason": reason,
            "trigger": setup["trigger"], "price": price, "block_count": 1,
        }
        state["blocked"].append(row)
    else:
        row["time"] = now; row["time_iso"] = iso(now); row["reason"] = reason; row["price"] = price
        row["block_count"] = int(row.get("block_count", 1)) + 1
    if extra:
        row.update(extra)
    state["blocked"] = state["blocked"][-1000:]
    life = touch_setup_lifecycle(state, side, setup, price, "BLOCKED", reason)
    life["block_count"] = int(life.get("block_count", 0)) + 1


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
        p_events = [r for r in (event_rows or []) if int(r.get("time", 0)) >= int(p.get("opened_at", 0)) - 60]
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
        "opened_at": now_utc(), "opened_iso": iso(now_utc()), "status": "OPEN",
        "entry": plan["entry"], "sl": plan["sl"], "tp1": plan["tp1"], "tp2": plan.get("tp2"),
        "risk": plan["risk"], "risk_atr": plan["risk_atr"], "trigger": setup["trigger"],
        "extension_atr": ctx["extension_atr"], "trend_1h": ctx["trend_1h"],
        "ema_position": ctx["ema_position"], "volume_ratio": ctx["volume_ratio"],
        "regime": ctx["regime"], "space_r": ctx["space_r"],
        "oi_dir": ctx["oi_dir"], "cvd_dir": ctx["cvd_dir"], "flow_quality": ctx["flow_quality"],
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
        trade_bars = [r for r in bars if int(r.get("time", 0)) >= int(t.get("opened_at", 0)) - 60] or [{"time": now_utc(), "high": live_price, "low": live_price, "close": live_price}]
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
    fields = ["id","setup_key","side","trigger_type","opened_iso","closed_iso","status","entry","sl","tp1","tp2",
              "risk_atr","trigger","extension_atr","trend_1h","ema_position","volume_ratio","regime","space_r",
              "oi_dir","cvd_dir","flow_quality","mfe_r","mae_r","duration_min","excluded_from_stats","legacy_rule_mismatch","intrabar_ambiguous"]
    with open(TRADES_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    save_json(BLOCKED_JSON, state["blocked"])
    bfields = ["first_time_iso","time_iso","setup_key","side","type","reason","trigger","price","block_count","volume_ratio","space_r","extension_atr"]
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
            headers={"User-Agent": "BTC-Radar-Clean-V1/1.0"},
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


def _rss_entries(url):
    r = requests.get(url, timeout=20, headers={"User-Agent": "BTC-Radar-Clean-V1/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.findall(".//item")[:15]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        out.append((title, link))
    if not out:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for item in root.findall(".//a:entry", ns)[:15]:
            title = (item.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_el = item.find("a:link", ns)
            link = (link_el.get("href") if link_el is not None else "") or ""
            out.append((title, link.strip()))
    return out


def check_news(state):
    news_state = state.setdefault("news", {"seen": [], "last_notify": 0})
    seen = set(news_state.get("seen", []))
    items = []
    for url in FEEDS:
        try:
            for title, link in _rss_entries(url):
                cat = major_news_class(" " + title + " ")
                if not title or not cat:
                    continue
                nid = hashlib.sha1((title+link).encode("utf-8", "ignore")).hexdigest()[:16]
                if nid in seen:
                    continue
                items.append((nid, title, link, cat))
        except Exception as e:
            print("News feed error:", e)
    if items:
        nid, title, link, cat = items[0]
        seen.add(nid)
        news_state["seen"] = list(seen)[-300:]
        if now_utc() - int(news_state.get("last_notify", 0)) >= NEWS_NOTIFY_COOLDOWN:
            news_state["last_notify"] = now_utc()
            zh, used_fallback = translate_news_title_zh(title, cat)
            zh_label = "中文摘要" if used_fallback else "中文"
            send_discord(
                f"📰 BTC 重大新聞提醒\n"
                f"類型：🔴 {cat}\n"
                f"原文：{title}\n"
                f"{zh_label}：{zh}\n"
                f"可能影響：短線波動可能放大\n"
                f"備註：僅做風險提醒，不改變多空訊號"
            )


def maybe_prepare(state, side, setup, price, trend, ema_pos, vol, reg, oi, cvd):
    sig = setup_signature(setup)
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


def formal_check(state, side, setup, price, rows15, a, trend, ema_pos, vol, reg, oi, cvd, event_rows=None):
    sig = setup_signature(setup)
    if sig in state["formal_seen"]:
        return
    ext = (price - setup["trigger"])/a if side == "LONG" else (setup["trigger"] - price)/a
    wh, wl = window_high_low(event_rows or [], price)
    trigger_level = setup["trigger"] + BREAK_BUFFER_ATR*a if side == "LONG" else setup["trigger"] - BREAK_BUFFER_ATR*a
    crossed = wh >= trigger_level if side == "LONG" else wl <= trigger_level
    if not crossed:
        return
    touch_setup_lifecycle(state, side, setup, price, "TRIGGERED", "1分鐘區間已穿越觸發價")

    # If the move crossed between GitHub runs and then made a small retest, keep the
    # opportunity alive. A deep failure back through the trigger is not treated as a fill.
    if side == "LONG" and price < setup["trigger"] - TRIGGER_RETEST_ATR*a:
        return
    if side == "SHORT" and price > setup["trigger"] + TRIGGER_RETEST_ATR*a:
        return

    plan = risk_plan(setup, side, price, a)
    if not plan:
        return
    space = space_context(rows15, side, price, plan["risk"], setup["trigger_time"], a)
    reasons = []
    if vol["ratio"] < VOL_HARD_MIN:
        reasons.append("成交量過低")
    if plan["risk_atr"] > MAX_STOP_ATR:
        reasons.append(f"停損距離過大（{plan['risk_atr']:.2f} ATR）")
    if space["space_r"] is not None and space["space_r"] < SPACE_MIN_R:
        reasons.append("前方空間不足")
    if ext > EXT_WAIT_ATR:
        reasons.append("價格過度延伸")

    # 0.50~0.75 ATR is a quality gate, not a blanket ban: allow strong participation + room.
    if EXT_WARN_ATR < ext <= EXT_WAIT_ATR:
        flow, _ = flow_quality(side, oi, cvd)
        if not (vol["ratio"] >= 1.30 and (space["space_r"] is None or space["space_r"] >= SPACE_GOOD_R)):
            reasons.append("偏追價，等待較好位置")

    if reasons:
        record_blocked(state, side, setup, " / ".join(reasons), price,
                       {"volume_ratio": vol["ratio"], "space_r": space["space_r"], "extension_atr": ext})
        return

    # If there is a known structure before 2R, don't invent a fake 2R TP2.
    if space["space_r"] is not None and space["space_r"] < 2.0:
        plan["tp2"] = None

    flow, icon = flow_quality(side, oi, cvd)
    ctx = {"extension_atr": ext, "trend_1h": trend["state"], "ema_position": ema_pos["label"],
           "volume_ratio": vol["ratio"], "regime": reg["state"], "space_r": space["space_r"],
           "oi_dir": oi["dir"], "cvd_dir": cvd["dir"], "flow_quality": flow}
    t = create_trade(state, side, setup, plan, ctx)
    touch_setup_lifecycle(state, side, setup, price, "FORMAL", f"建立模擬單 #{t["id"]}")
    mark_prepare_promoted(state, setup["setup_key"], t["id"])
    state["formal_seen"].append(sig)
    state["formal_seen"] = state["formal_seen"][-500:]

    tp2_text = f"${plan['tp2']:,.0f}" if plan.get("tp2") is not None else "—（前方空間不足 2R）"
    space_text = "開放" if space["space_r"] is None else f"{space['space_r']:.2f}R（{space['label']}）"
    opposite = ((side == "LONG" and trend["state"] == "BEAR") or (side == "SHORT" and trend["state"] == "BULL"))
    bg_note = " ⚠️ 與1H背景反向" if opposite else ""
    send_discord(
        f"⚡ BTC 正式{zh_side(side)}訊號\n\n"
        f"進場：${plan['entry']:,.0f}\n停損：${plan['sl']:,.0f}\n止盈1：${plan['tp1']:,.0f}\n止盈2：{tp2_text}\n"
        f"風險距離：{plan['risk_atr']:.2f} ATR（{risk_quality(plan['risk_atr'])}）\n\n"
        f"1H背景：{zh_dir(trend['state'])}{bg_note}\n"
        f"15M觸發：{setup['label']}\n"
        f"EMA位置：{ema_pos['label']}（{ema_pos['distance_atr']:.2f} ATR）\n"
        f"成交量：{vol['ratio']:.2f}×（{vol['label']}）\n"
        f"前方空間：{space_text}\n"
        f"市場狀態：{'趨勢' if reg['state']=='TREND' else '震盪'}\n"
        f"資金流：{icon} {flow}｜OI {zh_dir(oi['dir'])} / CVD Proxy {zh_dir(cvd['dir'])}\n"
        f"進場位置：{extension_quality(ext)}（{ext:.2f} ATR）\n\n"
        f"🧾 已建立模擬單 #{t['id']}"
    )


atr15_global = 1.0


def manual_summary(state, price, trend, reg, oi, cvd):
    valid_history = [x for x in state.get("history", []) if not x.get("excluded_from_stats")]
    valid_open = [x for x in state.get("trades", []) if not x.get("excluded_from_stats")]
    completed = len(valid_history)
    open_n = len(valid_open)
    p_done = len(state.get("prepare_history", []))
    p_open = len(state.get("prepare_active", []))
    last = valid_history[-20:]
    wins = sum(1 for x in last if x.get("status") in ("TP1", "TP2", "TP1_THEN_SL") and x.get("tp1_hit"))
    excluded = len(state.get("history", [])) + len(state.get("trades", [])) - completed - open_n
    base = (
        f"📊 BTC Radar Clean V1｜手動查詢\nBTC：${price:,.0f}\n"
        f"1H背景：{zh_dir(trend['state'])}\n市場狀態：{'趨勢' if reg['state']=='TREND' else '震盪'}\n"
        f"OI：{zh_dir(oi['dir'])}｜CVD Proxy：{zh_dir(cvd['dir'])}\n"
        f"👀 準備驗證 完成：{p_done}｜追蹤中：{p_open}\n"
        f"⚡ 正式單 完成：{completed}｜進行中：{open_n}\n"
    )
    if excluded:
        base += f"舊規則排除樣本：{excluded}\n"
    if last:
        base += f"最近20筆曾到TP1：{wins}/{len(last)}"
    else:
        base += "目前尚無完成樣本"
    send_discord(base)


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
    check_news(state)

    if MANUAL_RUN:
        manual_summary(state, live, trend, reg, oi, cvd)
    else:
        for side in ("LONG", "SHORT"):
            setup = choose_setup(rows15, a, side, live)
            if not setup:
                continue
            maybe_prepare(state, side, setup, live, trend, ema_pos, vol, reg, oi, cvd)
            formal_check(state, side, setup, live, rows15, a, trend, ema_pos, vol, reg, oi, cvd, events)

    export_data(state)
    save_json(STATE_FILE, state)
    print(f"{VERSION}: BTC={live:.0f} 1H={trend['state']} Regime={reg['state']} Vol={vol['ratio']:.2f}x Open={len(state['trades'])} Done={len(state['history'])}")


if __name__ == "__main__":
    main()
