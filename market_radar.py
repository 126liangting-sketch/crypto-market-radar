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

VERSION = "BTC_RADAR_CLEAN_V1"
STATE_FILE = "radar_state_clean_v1.json"
TRADES_JSON = "paper_trades_clean_v1.json"
TRADES_CSV = "paper_trades_clean_v1.csv"
BLOCKED_JSON = "blocked_setups_clean_v1.json"
BLOCKED_CSV = "blocked_setups_clean_v1.csv"

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
    e34, e50 = e34s[-1], e50s[-1]
    sep = abs(e34-e50)/a if a else 0.0
    recent = rows15[-12:]
    crossings = 0
    last_side = None
    for r in recent:
        c = float(r["close"])
        side = 1 if c > max(e34, e50) else -1 if c < min(e34, e50) else 0
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
    highs, lows = significant_pivots(rows15, a, 0.20)
    if side == "LONG":
        # Find a prior low -> impulse high -> pullback low -> local trigger high after pullback.
        for pl in reversed(lows[-8:]):
            after_highs = [h for h in highs if h[0] > pl[0]]
            if not after_highs:
                continue
            ih = after_highs[0]
            if ih[1] - pl[1] < IMPULSE_MIN_ATR * a:
                continue
            after_lows = [l for l in lows if l[0] > ih[0]]
            if not after_lows:
                continue
            pb = after_lows[-1]
            if ih[1] - pb[1] < PULLBACK_MIN_ATR * a:
                continue
            if pb[1] <= pl[1]:
                continue
            trig_highs = [h for h in highs if h[0] > pb[0]]
            if not trig_highs:
                continue
            tr = trig_highs[-1]
            if tr[1] <= pb[1]:
                continue
            return {"side": side, "kind": "PULLBACK", "label": "回踩再啟動",
                    "trigger": tr[1], "trigger_time": tr[0], "defense": pb[1], "defense_time": pb[0],
                    "impulse_start": pl[1], "impulse_end": ih[1],
                    "setup_key": f"PB:L:{pl[0]}:{ih[0]}:{pb[0]}:{tr[0]}"}
    else:
        for ph in reversed(highs[-8:]):
            after_lows = [l for l in lows if l[0] > ph[0]]
            if not after_lows:
                continue
            il = after_lows[0]
            if ph[1] - il[1] < IMPULSE_MIN_ATR * a:
                continue
            after_highs = [h for h in highs if h[0] > il[0]]
            if not after_highs:
                continue
            pb = after_highs[-1]
            if pb[1] - il[1] < PULLBACK_MIN_ATR * a:
                continue
            if pb[1] >= ph[1]:
                continue
            trig_lows = [l for l in lows if l[0] > pb[0]]
            if not trig_lows:
                continue
            tr = trig_lows[-1]
            if tr[1] >= pb[1]:
                continue
            return {"side": side, "kind": "PULLBACK", "label": "回踩再啟動",
                    "trigger": tr[1], "trigger_time": tr[0], "defense": pb[1], "defense_time": pb[0],
                    "impulse_start": ph[1], "impulse_end": il[1],
                    "setup_key": f"PB:S:{ph[0]}:{il[0]}:{pb[0]}:{tr[0]}"}
    return None


def detect_breakout_setup(rows15, a, side):
    highs, lows = significant_pivots(rows15, a, 0.35)
    if side == "LONG" and highs:
        h = highs[-1]
        # Require the swing to have meaningful range to the last low before it.
        prior_lows = [l for l in lows if l[0] < h[0]]
        if prior_lows and h[1] - prior_lows[-1][1] >= IMPULSE_MIN_ATR * a:
            return {"side": side, "kind": "BREAKOUT", "label": "有效突破",
                    "trigger": h[1], "trigger_time": h[0], "defense": prior_lows[-1][1],
                    "defense_time": prior_lows[-1][0], "setup_key": f"BO:L:{h[0]}"}
    if side == "SHORT" and lows:
        l = lows[-1]
        prior_highs = [h for h in highs if h[0] < l[0]]
        if prior_highs and prior_highs[-1][1] - l[1] >= IMPULSE_MIN_ATR * a:
            return {"side": side, "kind": "BREAKOUT", "label": "有效突破",
                    "trigger": l[1], "trigger_time": l[0], "defense": prior_highs[-1][1],
                    "defense_time": prior_highs[-1][0], "setup_key": f"BO:S:{l[0]}"}
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


def space_context(rows15, side, entry, risk, trigger_time):
    highs, lows = local_pivots(rows15, 2, 2)
    if side == "LONG":
        levels = sorted({p for t, p, _ in highs if p > entry and t != trigger_time})
        target = levels[0] if levels else None
        space_r = ((target-entry)/risk) if target else None
    else:
        levels = sorted({p for t, p, _ in lows if p < entry and t != trigger_time}, reverse=True)
        target = levels[0] if levels else None
        space_r = ((entry-target)/risk) if target else None
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
    s.setdefault("oi_history", [])
    s.setdefault("news", {"seen": [], "last_notify": 0})
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


def record_blocked(state, side, setup, reason, price, extra=None):
    key = f"{setup['setup_key']}:{reason}"
    if any(x.get("key") == key for x in state["blocked"][-200:]):
        return
    row = {"key": key, "time": now_utc(), "time_iso": iso(now_utc()), "side": side,
           "type": setup["kind"], "reason": reason, "trigger": setup["trigger"], "price": price}
    if extra:
        row.update(extra)
    state["blocked"].append(row)
    state["blocked"] = state["blocked"][-1000:]


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


def update_trades(state, live_price):
    remain = []
    for t in state["trades"]:
        entry, risk = float(t["entry"]), float(t["risk"])
        if t["side"] == "LONG":
            t["best_price"] = max(float(t.get("best_price", entry)), live_price)
            t["worst_price"] = min(float(t.get("worst_price", entry)), live_price)
            t["mfe_r"] = max(float(t.get("mfe_r", 0)), (t["best_price"]-entry)/risk)
            t["mae_r"] = max(float(t.get("mae_r", 0)), (entry-t["worst_price"])/risk)
            hit_tp1 = live_price >= float(t["tp1"])
            hit_tp2 = t.get("tp2") is not None and live_price >= float(t["tp2"])
            hit_sl = live_price <= float(t["sl"])
        else:
            t["best_price"] = min(float(t.get("best_price", entry)), live_price)
            t["worst_price"] = max(float(t.get("worst_price", entry)), live_price)
            t["mfe_r"] = max(float(t.get("mfe_r", 0)), (entry-t["best_price"])/risk)
            t["mae_r"] = max(float(t.get("mae_r", 0)), (t["worst_price"]-entry)/risk)
            hit_tp1 = live_price <= float(t["tp1"])
            hit_tp2 = t.get("tp2") is not None and live_price <= float(t["tp2"])
            hit_sl = live_price >= float(t["sl"])

        t["duration_min"] = int((now_utc()-int(t["opened_at"]))/60)
        if hit_tp1 and not t.get("tp1_hit"):
            t["tp1_hit"] = True
            send_discord(f"🎯 模擬單 #{t['id']}｜TP1 達成\n{zh_side(t['side'])}｜Entry ${entry:,.0f}\nMFE：+{t['mfe_r']:.2f}R｜MAE：-{t['mae_r']:.2f}R")
        if hit_tp2 and not t.get("tp2_hit"):
            t["tp2_hit"] = True
            t["status"] = "TP2"
            t["closed_at"] = now_utc(); t["closed_iso"] = iso(now_utc())
            send_discord(f"🏁 模擬單 #{t['id']}｜TP2 達成\n{zh_side(t['side'])}｜MFE：+{t['mfe_r']:.2f}R｜MAE：-{t['mae_r']:.2f}R")
        elif hit_sl:
            t["status"] = "TP1_THEN_SL" if t.get("tp1_hit") else "SL"
            t["closed_at"] = now_utc(); t["closed_iso"] = iso(now_utc())
            send_discord(f"❌ 模擬單 #{t['id']}｜停損\n{zh_side(t['side'])}｜結果：{t['status']}\nMFE：+{t['mfe_r']:.2f}R｜MAE：-{t['mae_r']:.2f}R")
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
              "oi_dir","cvd_dir","flow_quality","mfe_r","mae_r","duration_min"]
    with open(TRADES_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    save_json(BLOCKED_JSON, state["blocked"])
    bfields = ["time_iso","side","type","reason","trigger","price","volume_ratio","space_r","extension_atr"]
    with open(BLOCKED_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=bfields, extrasaction="ignore")
        w.writeheader(); w.writerows(state["blocked"])


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
    if 0 <= dist <= PREPARE_DISTANCE_ATR:
        state["prepare_seen"].append(sig)
        state["prepare_seen"] = state["prepare_seen"][-500:]
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


def formal_check(state, side, setup, price, rows15, a, trend, ema_pos, vol, reg, oi, cvd):
    sig = setup_signature(setup)
    if sig in state["formal_seen"]:
        return
    ext = setup["extension_atr"]
    buffer_ok = price >= setup["trigger"] + BREAK_BUFFER_ATR*a if side == "LONG" else price <= setup["trigger"] - BREAK_BUFFER_ATR*a
    if not buffer_ok:
        return

    plan = risk_plan(setup, side, price, a)
    if not plan:
        return
    space = space_context(rows15, side, price, plan["risk"], setup["trigger_time"])
    reasons = []
    if vol["ratio"] < VOL_HARD_MIN:
        reasons.append("成交量過低")
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
    state["formal_seen"].append(sig)
    state["formal_seen"] = state["formal_seen"][-500:]

    tp2_text = f"${plan['tp2']:,.0f}" if plan.get("tp2") is not None else "—（前方空間不足 2R）"
    space_text = "開放" if space["space_r"] is None else f"{space['space_r']:.2f}R（{space['label']}）"
    opposite = ((side == "LONG" and trend["state"] == "BEAR") or (side == "SHORT" and trend["state"] == "BULL"))
    bg_note = " ⚠️ 與1H背景反向" if opposite else ""
    send_discord(
        f"⚡ BTC 正式{zh_side(side)}訊號\n\n"
        f"進場：${plan['entry']:,.0f}\n停損：${plan['sl']:,.0f}\n止盈1：${plan['tp1']:,.0f}\n止盈2：{tp2_text}\n"
        f"風險距離：{plan['risk_atr']:.2f} ATR\n\n"
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
    completed = len(state.get("history", []))
    open_n = len(state.get("trades", []))
    last = state.get("history", [])[-20:]
    wins = sum(1 for x in last if x.get("status") in ("TP2", "TP1_THEN_SL") and x.get("tp1_hit"))
    send_discord(
        f"📊 BTC Radar Clean V1｜手動查詢\nBTC：${price:,.0f}\n"
        f"1H背景：{zh_dir(trend['state'])}\n市場狀態：{'趨勢' if reg['state']=='TREND' else '震盪'}\n"
        f"OI：{zh_dir(oi['dir'])}｜CVD Proxy：{zh_dir(cvd['dir'])}\n"
        f"⚡ 正式單 完成：{completed}｜進行中：{open_n}\n"
        f"最近20筆曾到TP1：{wins}/{len(last)}" if last else
        f"📊 BTC Radar Clean V1｜手動查詢\nBTC：${price:,.0f}\n1H背景：{zh_dir(trend['state'])}\n"
        f"市場狀態：{'趨勢' if reg['state']=='TREND' else '震盪'}\nOI：{zh_dir(oi['dir'])}｜CVD Proxy：{zh_dir(cvd['dir'])}\n"
        f"⚡ 正式單 完成：{completed}｜進行中：{open_n}\n目前尚無完成樣本"
    )


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

    update_oi(state)
    oi = oi_context(state)
    cvd = cvd_proxy()

    update_trades(state, live)
    check_news(state)

    if MANUAL_RUN:
        manual_summary(state, live, trend, reg, oi, cvd)
    else:
        for side in ("LONG", "SHORT"):
            setup = choose_setup(rows15, a, side, live)
            if not setup:
                continue
            maybe_prepare(state, side, setup, live, trend, ema_pos, vol, reg, oi, cvd)
            formal_check(state, side, setup, live, rows15, a, trend, ema_pos, vol, reg, oi, cvd)

    export_data(state)
    save_json(STATE_FILE, state)
    print(f"{VERSION}: BTC={live:.0f} 1H={trend['state']} Regime={reg['state']} Vol={vol['ratio']:.2f}x Open={len(state['trades'])} Done={len(state['history'])}")


if __name__ == "__main__":
    main()
