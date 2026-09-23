import os
import json
import time
import requests
import feedparser
import csv
import hashlib
import re
from datetime import datetime, timezone

OKX_BASE = "https://www.okx.com"
OKX_SYMBOL = "BTC-USDT-SWAP"
WEBHOOK = os.getenv("DISCORD_WEBHOOK")
STATE = "radar_state_v7.json"
VERSION = "V7_OKX_FORMAL"
TP_SL_ATR_MULT = 1.5
MIN_RISK_PCT = 0.004          # minimum 0.40% stop distance; avoids ultra-tight stops in low ATR
SETUP_INVALID_BARS = 3        # three consecutive closed 15m bars below 5/8 ends the setup
SETUP_ENHANCE_SCORE = 7       # only a meaningful upgrade (7/8+) gets another Discord alert
MANUAL_RUN = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

# Legacy V3 statistics are kept for backward compatibility.
MIN_SAMPLES = 100
PREDICT_SECONDS = 3600
MOVE = 0.01

# V4 score + forward test
MIN_SCORE = 5
FORWARD_HORIZONS = {"15m": 900, "30m": 1800, "1h": 3600, "2h": 7200}
MIN_FORWARD_SAMPLES = 20       # before this, show "累積中" and use Score for attention alerts
FORWARD_NOTIFY_THRESHOLD = 0.60
NOTIFY_COOLDOWN = 3600         # same direction/score: at most one Discord alert per hour
ATR_MIN_PCT = 0.002            # 0.20%
ATR_MAX_PCT = 0.025            # 2.50%
ZONE_ATR_MULT = 0.35
ANALYTICS_BARS = 12                 # 12 x 15m = about 3 hours
ANALYTICS_CONFIRM_BARS = 4          # latest ~1 hour confirms no clear reversal
OI_FLAT_PCT = 0.0025               # +/-0.10% treated as flat
CVD_FLAT_REL = 0.05               # <=5% of recent CVD range treated as flat

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]


def get(url, params=None, retries=3):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=20,
                             headers={"User-Agent": "Crypto-Market-Radar/5.0"})
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            last_error = e
            print(f"API 請求失敗 ({attempt}/{retries}): {type(e).__name__}: {e}")
            if attempt < retries:
                wait = 2 ** (attempt - 1)
                print(f"{wait} 秒後重試...")
                time.sleep(wait)
    raise last_error


def okx_public(path, params=None):
    data = get(f"{OKX_BASE}{path}", params)
    if not isinstance(data, dict) or str(data.get("code", "0")) != "0":
        raise RuntimeError(f"OKX API error: {data}")
    return data.get("data", [])


def candles(resolution, count=200):
    """Return OKX BTC-USDT-SWAP candles in the old radar row format.
    OKX candle: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
    volCcy is BTC volume for BTC-USDT-SWAP and is used for Volume ratio.
    """
    bar = {"1m": "1m", "15m": "15m", "1h": "1H"}[resolution]
    # /history-candles supports historical closed candles. 100 per request is a safe page size.
    rows, after = [], None
    while len(rows) < count:
        params = {"instId": OKX_SYMBOL, "bar": bar, "limit": min(100, count - len(rows))}
        if after:
            params["after"] = after
        batch = okx_public("/api/v5/market/history-candles", params)
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1][0]
        if len(batch) < int(params["limit"]):
            break
        time.sleep(0.05)

    out = []
    seen = set()
    for x in rows:
        if len(x) < 6:
            continue
        ts = int(x[0]) // 1000
        if ts in seen:
            continue
        seen.add(ts)
        # Prefer base-currency volume (BTC). Fall back to contract volume.
        vol = x[6] if len(x) > 6 and _number(x[6]) else x[5]
        out.append({
            "time": ts,
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(vol),
            "confirm": str(x[8]) if len(x) > 8 else "1",
        })
    out.sort(key=lambda r: r["time"])
    if not out:
        raise RuntimeError(f"OKX {resolution} K線沒有資料")
    return out[-count:]


def closed_rows(resolution, count=200):
    rows = candles(resolution, count)
    # history-candles is normally closed data; still respect confirm when present.
    confirmed = [r for r in rows if str(r.get("confirm", "1")) == "1"]
    return confirmed if confirmed else rows


def okx_oi_snapshot():
    rows = okx_public("/api/v5/public/open-interest",
                      {"instType": "SWAP", "instId": OKX_SYMBOL})
    if not rows:
        return None
    r = rows[0]
    # oiCcy is BTC-equivalent OI, which is easier to interpret across contract-size changes.
    value = r.get("oiCcy") or r.get("oi")
    return {"time": int(r.get("ts", int(time.time()*1000))) // 1000,
            "value": float(value)}


def update_oi_history(state):
    """Persist OKX OI snapshots so the radar can calculate real 1h/3h changes.
    This does not touch V5 data.
    """
    v = state["v7"]
    hist = v.setdefault("oi_history", [])
    try:
        snap = okx_oi_snapshot()
    except Exception as e:
        print(f"OKX OI 暫時無法取得: {type(e).__name__}: {e}")
        snap = None
    if snap:
        # One snapshot per workflow run; replace near-duplicate timestamps.
        if not hist or abs(int(hist[-1]["time"]) - snap["time"]) > 60:
            hist.append(snap)
        else:
            hist[-1] = snap
    cutoff = int(time.time()) - 7 * 86400
    hist[:] = [x for x in hist if int(x.get("time", 0)) >= cutoff][-1000:]
    return hist


def _nearest_old(hist, target_time):
    eligible = [x for x in hist if int(x["time"]) <= target_time]
    return eligible[-1] if eligible else None


def oi_details_from_state(state):
    hist = state["v7"].get("oi_history", [])
    if not hist:
        return "UNAVAILABLE", {"available": False}
    cur = hist[-1]
    now_t, now_v = int(cur["time"]), float(cur["value"])
    one = _nearest_old(hist, now_t - 3600)
    three = _nearest_old(hist, now_t - 10800)
    m = {"available": True, "current": now_v}
    if one:
        rp = 0.0 if float(one["value"]) == 0 else (now_v / float(one["value"]) - 1)
        m["recent_pct"] = rp
        m["recent_dir"] = "FLAT" if abs(rp) < OI_FLAT_V7 else ("UP" if rp > 0 else "DOWN")
    else:
        m["recent_pct"] = None
        m["recent_dir"] = "UNAVAILABLE"
    if three:
        bp = 0.0 if float(three["value"]) == 0 else (now_v / float(three["value"]) - 1)
        m["broad_pct"] = bp
        m["broad_dir"] = "FLAT" if abs(bp) < OI_FLAT_V7 else ("UP" if bp > 0 else "DOWN")
    else:
        m["broad_pct"] = None
        m["broad_dir"] = "UNAVAILABLE"
    print(f"OKX OI: {now_v:.6g} BTC | 1H={m.get('recent_pct')} | 3H={m.get('broad_pct')}")
    return m.get("recent_dir", "UNAVAILABLE"), m


def cvd_details_okx():
    """Return a transparent candle-based CVD *proxy* from OKX 1m candles.

    OKX public REST trade history covered only seconds during live testing, so it
    cannot honestly reconstruct a complete 1h/3h true taker CVD from a scheduled
    GitHub Action. Instead we use 1-minute BTC-USDT-SWAP candles and estimate
    directional volume with Close Location Value (CLV):

        delta_proxy = volume * (2*close - high - low) / (high - low)

    This is NOT true trade-by-trade CVD. Discord labels it "CVD Proxy".
    It is useful as a consistent flow-confirmation feature without pretending
    unavailable public trade history is complete.
    """
    try:
        rows = closed_rows("1m", 220)
    except Exception as e:
        print(f"OKX CVD Proxy 暫時無法取得: {type(e).__name__}: {e}")
        return "UNAVAILABLE", {"available": False, "recent_dir": "UNAVAILABLE",
                               "recent_delta": 0.0, "broad_delta": 0.0,
                               "broad_span": 0.0, "source": "OKX_1M_CLV_PROXY"}

    deltas = []
    for r in rows:
        h, l, c = float(r["high"]), float(r["low"]), float(r["close"])
        v = float(r.get("volume", 0) or 0)
        if h <= l or v <= 0:
            d = 0.0
        else:
            clv = max(-1.0, min(1.0, (2.0*c - h - l) / (h - l)))
            d = v * clv
        deltas.append(d)

    if len(deltas) < 180:
        return "UNAVAILABLE", {"available": False, "recent_dir": "UNAVAILABLE",
                               "recent_delta": 0.0, "broad_delta": 0.0,
                               "broad_span": 0.0, "source": "OKX_1M_CLV_PROXY"}

    recent = deltas[-60:]
    broad = deltas[-180:]
    recent_delta = sum(recent)
    broad_delta = sum(broad)
    broad_span = sum(abs(x) for x in broad)
    rel = abs(recent_delta) / broad_span if broad_span else 0.0
    recent_dir = "FLAT" if rel < CVD_FLAT_REL else ("UP" if recent_delta > 0 else "DOWN")
    broad_rel = abs(broad_delta) / broad_span if broad_span else 0.0
    broad_dir = "FLAT" if broad_rel < CVD_FLAT_REL else ("UP" if broad_delta > 0 else "DOWN")
    m = {"available": True, "recent_dir": recent_dir, "broad_dir": broad_dir,
         "recent_delta": recent_delta, "broad_delta": broad_delta,
         "broad_span": broad_span, "recent_rel": rel,
         "source": "OKX_1M_CLV_PROXY"}
    print(f"OKX CVD Proxy: 1H={recent_delta:+.4f} BTC ({recent_dir}) | 3H={broad_delta:+.4f} BTC ({broad_dir})")
    return recent_dir, m

def ema_series(values, length):
    if len(values) < length:
        return []
    k = 2 / (length + 1)
    seed = sum(values[:length]) / length
    out = [None] * (length - 1) + [seed]
    value = seed
    for x in values[length:]:
        value = x * k + value * (1 - k)
        out.append(value)
    return out


def ema(values, length):
    s = ema_series(values, length)
    return s[-1] if s else None


def ema_state_from_rows(rows):
    closes = [float(x["close"]) for x in rows]
    fast, slow = ema(closes, 34), ema(closes, 50)
    if fast is None or slow is None:
        return "NEUTRAL", fast, slow
    return ("BULL" if fast > slow else "BEAR" if fast < slow else "NEUTRAL"), fast, slow


def true_range(row, prev_close):
    high, low = float(row["high"]), float(row["low"])
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(rows, length=14):
    if len(rows) < length + 1:
        return None
    trs = [true_range(rows[i], float(rows[i - 1]["close"])) for i in range(1, len(rows))]
    return sum(trs[-length:]) / length


def _number(value):
    try:
        float(value); return True
    except (TypeError, ValueError):
        return False


def direction_details(kind, state=None):
    if kind == "open-interest" and state is not None:
        return oi_details_from_state(state)
    if kind == "cvd":
        return cvd_details_okx()
    return "UNAVAILABLE", {"available": False}


def direction(kind, state=None):
    return direction_details(kind, state)[0]


def send_discord(message):
    separator = "━━━━━━━━━━━━━━━━━━"
    message = str(message).strip()
    if not message.startswith(separator):
        message = f"{separator}\n{message}\n{separator}"
    if not WEBHOOK:
        print("缺少 DISCORD_WEBHOOK"); return
    last = None
    for attempt in range(3):
        try:
            r = requests.post(WEBHOOK, json={"content": message}, timeout=20)
            print("Discord HTTP:", r.status_code)
            r.raise_for_status(); return
        except requests.exceptions.RequestException as e:
            last = e
            if attempt < 2: time.sleep(2 ** attempt)
    raise last



VERSION = "V7_OKX_FORMAL"
V7_JSON = "paper_trades_v7.json"
V7_CSV = "paper_trades_v7.csv"
PREP_JSON = "prepare_trades_v7.json"
PREP_CSV = "prepare_trades_v7.csv"
BLOCKED_JSON = "blocked_reasons_v7.json"
BLOCKED_CSV = "blocked_reasons_v7.csv"
STATE = "radar_state_v7.json"
MANUAL_RUN = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
FORWARD_HORIZONS = {"15m": 900, "30m": 1800, "1h": 3600, "2h": 7200}
SWING_LEFT = SWING_RIGHT = 2
BREAK_ATR = 0.10
RETEST_TOL_ATR = 0.25
RETEST_MAX_BARS = 8
VOL_LOOKBACK = 20
VOL_MIN = 0.70
VOL_NORMAL = 0.90
VOL_STRONG = 1.50
EXTENDED_ATR = 0.60
NO_CHASE_ATR = 1.50
EMA_BAD_SLOPE = 0.15
MIN_STOP_ATR = 0.50
MAX_STOP_ATR = 1.25
STOP_BUFFER_ATR = 0.20
OI_FLAT_V7 = 0.0010  # 0.10%
NEWS_RED_SECONDS = 2 * 3600
NEWS_YELLOW_SECONDS = 8 * 3600
NEWS_NOTIFY_COOLDOWN = 3 * 3600

# V7 Structure Engine — HH/HL and LL/LH continuation logic.
STRUCT_MIN_ATR = 0.05
STRUCT_READY_ZONE_ATR = 0.90
EARLY_NO_CHASE_ATR = 1.00
MICRO_BREAK_ATR = 0.05
MICRO_LOOKBACK = 3
EARLY_MIN_ROOM_R = 0.50


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}
    else:
        state = {}

    state.setdefault("paper_trades", [])
    state.setdefault("prepare_trades", [])
    state.setdefault("v7", {})
    v = state["v7"]
    v.setdefault("last_processed_candle", None)
    v.setdefault("next_setup_id", 1)
    v.setdefault("next_test_id", 1)
    v.setdefault("next_prepare_id", 1)
    v.setdefault("active_setup", None)
    v.setdefault("breakout_watch", None)
    v.setdefault("history", [])
    v.setdefault("prepare_history", [])
    v.setdefault("news", {"last_major_id": None, "last_major_time": 0, "seen_major_ids": []})
    v["news"].setdefault("seen_major_ids", [])
    v.setdefault("oi_history", [])
    v.setdefault("structure_ready_key", None)
    v.setdefault("structure_ready_side", None)
    v.setdefault("structure_ready_time", None)
    v.setdefault("structure_early_key", None)
    v.setdefault("prepare_setup_ids", {})
    v.setdefault("blocked_events", [])
    v.setdefault("blocked_seen_keys", [])
    return state

def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)


def pivots(rows):
    highs, lows = [], []
    for i in range(SWING_LEFT, len(rows)-SWING_RIGHT):
        h=float(rows[i]["high"]); l=float(rows[i]["low"])
        if all(h > float(rows[j]["high"]) for j in range(i-SWING_LEFT,i)) and all(h > float(rows[j]["high"]) for j in range(i+1,i+1+SWING_RIGHT)):
            highs.append((int(rows[i]["time"]), h))
        if all(l < float(rows[j]["low"]) for j in range(i-SWING_LEFT,i)) and all(l < float(rows[j]["low"]) for j in range(i+1,i+1+SWING_RIGHT)):
            lows.append((int(rows[i]["time"]), l))
    return highs, lows


def ema_context(rows15, rows1h, a):
    c15=[float(x["close"]) for x in rows15]; c1=[float(x["close"]) for x in rows1h]
    e34s=ema_series(c15,34); e50s=ema_series(c15,50)
    e34,e50=e34s[-1],e50s[-1]
    e341,e501=ema(c1,34),ema(c1,50)
    def slope(series, n=4):
        vals=[x for x in series if x is not None]
        return (vals[-1]-vals[-1-n])/a if a and len(vals)>n else 0.0
    s34=slope(e34s); s50=slope(e50s)
    state15="BULL" if e34>e50 else "BEAR" if e34<e50 else "NEUTRAL"
    state1="BULL" if e341>e501 else "BEAR" if e341<e501 else "NEUTRAL"
    zl,zh=sorted((e34,e50)); close=c15[-1]
    dist=0.0 if zl<=close<=zh else (zl-close)/a if close<zl else (close-zh)/a
    return {"ema_1h":state1,"ema_15m":state15,"e34":e34,"e50":e50,"zone_low":zl,"zone_high":zh,"s34":s34,"s50":s50,"zone_distance_atr":dist}


def volume_ratio(rows):
    if len(rows)<VOL_LOOKBACK+1: return 0.0
    cur=float(rows[-1].get("volume",0) or 0)
    prev=[float(x.get("volume",0) or 0) for x in rows[-VOL_LOOKBACK-1:-1]]
    avg=sum(prev)/len(prev) if prev else 0
    return cur/avg if avg else 0.0


def flow_for_side(side, price_dir, oi_metrics, cvd_metrics):
    rp = oi_metrics.get("recent_pct")
    oi_available = rp is not None
    if oi_available:
        rp = float(rp)
        oi_dir = "FLAT" if abs(rp) < OI_FLAT_V7 else ("UP" if rp > 0 else "DOWN")
    else:
        rp = 0.0
        oi_dir = "UNAVAILABLE"

    cvd_available = bool(cvd_metrics.get("available"))
    cd = float(cvd_metrics.get("recent_delta", 0) or 0)
    span = abs(float(cvd_metrics.get("broad_span", 0) or 0))
    rel = abs(cd) / span if span else 0.0
    cvd_dir = ("FLAT" if rel < CVD_FLAT_REL else "UP" if cd > 0 else "DOWN") if cvd_available else "UNAVAILABLE"
    cvd_strong = cvd_available and rel >= 0.50

    wanted = "UP" if side == "LONG" else "DOWN"
    cvd_support = cvd_available and cvd_dir == wanted
    cvd_oppose = cvd_available and cvd_dir not in ("FLAT", wanted) and cvd_strong

    # OI rising confirms fresh positioning in the actual price direction.
    oi_support = oi_available and oi_dir == "UP" and price_dir == wanted
    oi_oppose = oi_available and oi_dir == "UP" and price_dir not in ("FLAT", wanted)

    valid = (cvd_support or oi_support) and not cvd_oppose and not oi_oppose
    strong_both = cvd_support and oi_support
    return {"valid": valid, "strong_both": strong_both,
            "opposed": bool(cvd_oppose or oi_oppose),
            "cvd_available": cvd_available, "oi_available": oi_available,
            "cvd_dir": cvd_dir, "cvd_strong": cvd_strong,
            "oi_dir": oi_dir, "oi_recent_pct": rp if oi_available else None,
            "cvd_recent_delta": cd, "cvd_rel": rel}

def classify_structure(rows, highs, lows, a):
    """Detect sequential continuation structure from confirmed 2-2 pivots."""
    if not a or a <= 0:
        return {"side": None, "label": "NONE"}
    candidates = []
    if len(highs) >= 2 and len(lows) >= 2:
        h1, h2 = highs[-2], highs[-1]
        if h2[1] >= h1[1] + STRUCT_MIN_ATR * a:
            before = [x for x in lows if x[0] < h2[0]]
            after = [x for x in lows if x[0] > h2[0]]
            if before and after:
                prior_low, hl = before[-1], after[-1]
                if hl[1] >= prior_low[1] + STRUCT_MIN_ATR * a:
                    candidates.append({"side":"LONG","label":"HH→HL","impulse":h2,"pullback":hl,"prior_defense":prior_low,"key":f"LONG:{h2[0]}:{hl[0]}","ready_time":hl[0]})
    if len(lows) >= 2 and len(highs) >= 2:
        l1, l2 = lows[-2], lows[-1]
        if l2[1] <= l1[1] - STRUCT_MIN_ATR * a:
            before = [x for x in highs if x[0] < l2[0]]
            after = [x for x in highs if x[0] > l2[0]]
            if before and after:
                prior_high, lh = before[-1], after[-1]
                if lh[1] <= prior_high[1] - STRUCT_MIN_ATR * a:
                    candidates.append({"side":"SHORT","label":"LL→LH","impulse":l2,"pullback":lh,"prior_defense":prior_high,"key":f"SHORT:{l2[0]}:{lh[0]}","ready_time":lh[0]})
    return max(candidates, key=lambda x: x["ready_time"]) if candidates else {"side":None,"label":"NONE"}


def micro_resumption(rows, structure, a):
    side = structure.get("side")
    t = structure.get("ready_time")
    if not side or t is None or len(rows) < MICRO_LOOKBACK + 2:
        return None
    current, previous = rows[-1], rows[-2]
    prior = [r for r in rows[:-1] if int(r["time"]) > int(t)]
    if len(prior) < 2:
        return None
    prior = prior[-MICRO_LOOKBACK:]
    close, prev_close = float(current["close"]), float(previous["close"])
    if side == "LONG":
        level = max(float(r["high"]) for r in prior)
        fired = prev_close <= level and close >= level + MICRO_BREAK_ATR * a
        ext = (close - level) / a
    else:
        level = min(float(r["low"]) for r in prior)
        fired = prev_close >= level and close <= level - MICRO_BREAK_ATR * a
        ext = (level - close) / a
    return {"fired": fired, "level": level, "ext": ext}


def early_eligibility(side, close, ctx, vol, flow):
    if side == "LONG":
        ema_ok = close >= ctx["zone_low"] and ctx["s34"] >= -EMA_BAD_SLOPE
        trend_ok = ctx["ema_15m"] != "BEAR"
    else:
        ema_ok = close <= ctx["zone_high"] and ctx["s34"] <= EMA_BAD_SLOPE
        trend_ok = ctx["ema_15m"] != "BULL"
    if not trend_ok or not ema_ok:
        return False, "15M EMA 明顯反向"
    if ctx["zone_distance_atr"] > EARLY_NO_CHASE_ATR:
        return False, f"離 EMA Zone > {EARLY_NO_CHASE_ATR:.2f} ATR"
    if vol < VOL_MIN:
        return False, "成交量 < 0.7×"
    if flow.get("opposed"):
        return False, "資金流明顯反向"
    if vol < VOL_NORMAL and not flow.get("valid"):
        return False, "成交量偏低且資金流未支持"
    return True, "OK"


def continuation_risk(side, entry, a, structure):
    if not structure or not a:
        return None
    defense = float(structure["pullback"][1])
    impulse = float(structure["impulse"][1])
    if side == "LONG":
        raw_sl = defense - STOP_BUFFER_ATR * a
        risk = entry - raw_sl
        if risk <= 0 or risk > MAX_STOP_ATR * a:
            return None
        risk = max(risk, MIN_STOP_ATR * a); sl = entry - risk
        room = (impulse - entry) / risk
        if room < EARLY_MIN_ROOM_R:
            return None
        tp1, tp2 = entry + risk, entry + 2*risk
    else:
        raw_sl = defense + STOP_BUFFER_ATR * a
        risk = raw_sl - entry
        if risk <= 0 or risk > MAX_STOP_ATR * a:
            return None
        risk = max(risk, MIN_STOP_ATR * a); sl = entry + risk
        room = (entry - impulse) / risk
        if room < EARLY_MIN_ROOM_R:
            return None
        tp1, tp2 = entry - risk, entry - 2*risk
    return {"sl":sl,"tp1":tp1,"tp2":tp2,"risk":risk,"risk_atr":risk/a,"room_r":room,"defense_level":defense}


def v7_quality_score(side, ctx, vol, flow, ext):
    score=0
    if ctx["ema_1h"] == ("BULL" if side=="LONG" else "BEAR"): score+=2
    if ctx["ema_15m"] == ("BULL" if side=="LONG" else "BEAR"): score+=1
    if ctx["zone_distance_atr"]<=0.30: score+=1
    if vol>=VOL_NORMAL: score+=1
    if vol>=VOL_STRONG: score+=1
    if flow["valid"]: score+=1
    if flow["strong_both"]: score+=1
    return min(score,8)


def eligibility(side, close, ctx, a, vol, flow, ext):
    if side=="LONG":
        ema_ok=close>=ctx["zone_low"] and ctx["s34"]>=-EMA_BAD_SLOPE
    else:
        ema_ok=close<=ctx["zone_high"] and ctx["s34"]<=EMA_BAD_SLOPE
    if ctx["zone_distance_atr"]>NO_CHASE_ATR: return False,f"離 EMA Zone > {NO_CHASE_ATR:.2f} ATR"
    if ext>NO_CHASE_ATR: return False,f"突破延伸 > {NO_CHASE_ATR:.2f} ATR"
    if not ema_ok: return False,"15M EMA 明顯反向"
    if vol<VOL_MIN: return False,"成交量 < 0.7×"
    if vol<VOL_NORMAL and not flow["strong_both"]: return False,"成交量偏低且資金流不足"
    if not flow["valid"]: return False,"CVD/OI 未提供有效支持"
    return True,"OK"


def structure_risk(side, entry, a, highs, lows):
    if side=="LONG":
        candidates=[p for _,p in lows if p<entry]
        if not candidates: return None
        raw_sl=candidates[-1]-STOP_BUFFER_ATR*a
        risk=entry-raw_sl
        if risk>MAX_STOP_ATR*a: return None
        risk=max(risk,MIN_STOP_ATR*a); sl=entry-risk
        obstacles=[p for _,p in highs if p>entry]
        obstacle=min(obstacles) if obstacles else None
    else:
        candidates=[p for _,p in highs if p>entry]
        if not candidates: return None
        raw_sl=candidates[-1]+STOP_BUFFER_ATR*a
        risk=raw_sl-entry
        if risk>MAX_STOP_ATR*a: return None
        risk=max(risk,MIN_STOP_ATR*a); sl=entry+risk
        obstacles=[p for _,p in lows if p<entry]
        obstacle=max(obstacles) if obstacles else None
    room=(abs(obstacle-entry)/risk) if obstacle is not None and risk else None
    if room is not None and room<1.0: return None
    tp1=entry+risk if side=="LONG" else entry-risk
    tp2=entry+2*risk if side=="LONG" else entry-2*risk
    return {"sl":sl,"tp1":tp1,"tp2":tp2,"risk":risk,"risk_atr":risk/a,"room_r":room}


def news_status_v7(state):
    now = int(time.time())
    critical = []
    any_titles = False

    # Keep standalone Discord alerts rare: macro, BTC ETF, systemic exchange/security,
    # stablecoin depeg, or direct Bitcoin regulatory shocks.
    macro = [
        "fomc", "federal reserve", "fed rate", "interest rate decision",
        "cpi", "consumer price index", "pce", "nonfarm", "non-farm", "jobs report"
    ]
    btc_direct = [
        "bitcoin etf", "btc etf", "spot bitcoin etf",
        "bitcoin ban", "bitcoin regulation", "bitcoin reserve"
    ]
    systemic = [
        "exchange hack", "exchange hacked", "withdrawals suspended",
        "suspend withdrawals", "security breach", "exploit",
        "stablecoin depeg", "usdt depeg", "usdc depeg"
    ]

    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for x in feed.entries[:12]:
                title = (x.get("title", "") or "").strip()
                if not title:
                    continue
                any_titles = True
                low = title.lower()
                if any(k in low for k in macro + btc_direct + systemic):
                    critical.append(title)
        except Exception:
            pass

    ns = state["v7"]["news"]
    ns.setdefault("seen_major_ids", [])
    seen = set(ns.get("seen_major_ids", []))
    last_time = int(ns.get("last_major_time", 0) or 0)

    new_headline = None
    new_id = None
    for title in critical:
        normalized = re.sub(r"\s+", " ", title.lower()).strip()
        event_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if event_id not in seen:
            new_headline = title
            new_id = event_id
            break

    if new_headline:
        seen.add(new_id)
        ns["seen_major_ids"] = list(seen)[-100:]
        ns["last_major_id"] = new_id
        # Standalone news Discord alert has a cooldown to avoid RSS spam.
        if now - last_time >= NEWS_NOTIFY_COOLDOWN:
            ns["last_major_time"] = now
            return "🔴 高風險", True, new_headline

    age = now - int(ns.get("last_major_time", 0) or 0)
    if ns.get("last_major_time") and age <= NEWS_RED_SECONDS:
        return "🔴 高風險", False, None
    if ns.get("last_major_time") and age <= NEWS_YELLOW_SECONDS:
        return "🟡 注意", False, None
    return ("⚪ 正常" if any_titles else "⚪ 暫無新聞資料"), False, None

def hit_levels(test,row):
    if test.get("terminal_outcome") in ("SL","TP2","AMBIGUOUS"): return []
    hi,lo=float(row["high"]),float(row["low"]); side=test["side"]
    sl,tp1,tp2=map(float,(test["sl"],test["tp1"],test["tp2"]))
    hs=(lo<=sl) if side=="LONG" else (hi>=sl); h1=(hi>=tp1) if side=="LONG" else (lo<=tp1); h2=(hi>=tp2) if side=="LONG" else (lo<=tp2)
    if not test.get("tp1_hit") and hs and h1:
        test["terminal_outcome"]="AMBIGUOUS"; test["outcome_time"]=int(row["time"]); return ["AMBIGUOUS"]
    if hs:
        test["terminal_outcome"]="SL"; test["outcome_time"]=int(row["time"]); return ["SL"]
    ev=[]
    if h1 and not test.get("tp1_hit"): test["tp1_hit"]=True; test["tp1_time"]=int(row["time"]); ev.append("TP1")
    if h2 and not test.get("tp2_hit"): test["tp2_hit"]=True; test["terminal_outcome"]="TP2"; test["outcome_time"]=int(row["time"]); ev.append("TP2")
    return ev


def update_paper_trades(state, rows15):
    latest = int(rows15[-1]["time"])
    still_open = []

    for trade in state.get("paper_trades", []):
        entry_t = int(trade["entry_time"])
        entry = float(trade["entry_price"])
        side = trade["side"]
        trade.setdefault("results", {})
        trade.setdefault("max_high", entry)
        trade.setdefault("min_low", entry)
        trade.setdefault("events", [])

        for r in rows15:
            t = int(r["time"])
            if not (entry_t < t <= latest):
                continue

            trade["max_high"] = max(float(trade["max_high"]), float(r["high"]))
            trade["min_low"] = min(float(trade["min_low"]), float(r["low"]))

            for ev in hit_levels(trade, r):
                if ev not in trade["events"]:
                    trade["events"].append(ev)

                key = ev + "_notified"
                if not trade.get(key):
                    icon = {"TP1":"🎯","TP2":"🏁","SL":"❌","AMBIGUOUS":"⚠️"}[ev]
                    label = {
                        "TP1":"TP1 達成",
                        "TP2":"TP2 達成",
                        "SL":"SL 觸發",
                        "AMBIGUOUS":"TP/SL 同根K・順序不明"
                    }[ev]
                    send_discord(
                        f"{icon} Paper Trade #{trade.get('setup_id','?')}｜BTC "
                        f"{'做多' if side=='LONG' else '做空'}｜{label}\n\n"
                        f"Entry ${entry:,.0f}｜TP1 ${float(trade['tp1']):,.0f}｜"
                        f"TP2 ${float(trade['tp2']):,.0f}｜SL ${float(trade['sl']):,.0f}"
                    )
                    trade[key] = True

                if ev in ("TP2", "SL", "AMBIGUOUS"):
                    a = state["v7"].get("active_setup")
                    if a and a.get("test_id") == trade.get("id"):
                        state["v7"]["active_setup"] = None

        for label, secs in FORWARD_HORIZONS.items():
            if label in trade["results"] or latest < entry_t + secs:
                continue
            row = next((r for r in rows15 if int(r["time"]) >= entry_t + secs), None)
            if row:
                raw = float(row["close"]) / entry - 1
                signed = raw if side == "LONG" else -raw
                trade["results"][label] = {
                    "price": float(row["close"]),
                    "return": signed,
                    "correct": signed > 0
                }

        terminal = trade.get("terminal_outcome")
        if terminal in ("TP2", "SL", "AMBIGUOUS"):
            mfe = (float(trade["max_high"]) / entry - 1) if side == "LONG" else (entry / float(trade["min_low"]) - 1)
            mae = (entry / float(trade["min_low"]) - 1) if side == "LONG" else (float(trade["max_high"]) / entry - 1)
            trade["mfe"] = max(0, mfe)
            trade["mae"] = max(0, mae)
            trade["closed_time"] = trade.get("outcome_time")
            trade["duration_min"] = max(0, (int(trade["closed_time"]) - entry_t) // 60) if trade.get("closed_time") else None

            if trade.get("tp1_hit") and terminal == "SL":
                trade["final_result"] = "TP1_THEN_SL"
            else:
                trade["final_result"] = terminal

            if not any(x.get("id") == trade.get("id") for x in state["v7"]["history"]):
                state["v7"]["history"].append(trade)
        else:
            still_open.append(trade)

    state["paper_trades"] = still_open

def blocked_reason_code(reason):
    mapping = {
        "離 EMA Zone > 1.50 ATR": ("EMA_DISTANCE", "離 EMA Zone 過遠"),
        "離 EMA Zone > 1.00 ATR": ("EMA_DISTANCE", "離 EMA Zone 過遠"),
        "突破延伸 > 1.50 ATR": ("BREAKOUT_EXTENSION", "突破延伸過遠"),
        "15M EMA 明顯反向": ("EMA_OPPOSE", "15M EMA 明顯反向"),
        "成交量 < 0.7×": ("VOLUME_LOW", "成交量過低"),
        "成交量偏低且資金流不足": ("LOW_VOLUME_FLOW", "成交量偏低＋資金流不足"),
        "CVD/OI 未提供有效支持": ("FLOW_NO_SUPPORT", "CVD/OI 未提供有效支持"),
        "資金流明顯反向": ("FLOW_OPPOSE", "資金流明顯反向"),
        "成交量偏低且資金流未支持": ("LOW_VOLUME_FLOW", "成交量偏低＋資金流不足"),
        "SL / RR 結構不適合": ("RISK_RR", "SL / RR 結構不適合"),
        "risk/room not suitable": ("RISK_RR", "SL / RR / 空間不適合"),
        "SL / RR / 空間不適合": ("RISK_RR", "SL / RR / 空間不適合"),
        "risk/RR or active setup": ("RISK_OR_ACTIVE", "SL / RR 不適合或已有進行中訊號"),
        "active setup": ("ACTIVE_SETUP", "已有進行中訊號"),
    }
    return mapping.get(str(reason), ("OTHER", str(reason)))


def record_blocked(state, candle_time, side, trigger_type, reason, price, vol, ctx, flow, ext=None):
    """Record one unique blocked trade opportunity per closed candle."""
    v = state["v7"]
    code, label = blocked_reason_code(reason)
    key = f"{int(candle_time)}|{side}|{trigger_type}|{code}"
    seen = v.setdefault("blocked_seen_keys", [])
    if key in seen:
        return False

    event = {
        "candle_time": int(candle_time),
        "time_utc": datetime.fromtimestamp(int(candle_time), tz=timezone.utc).isoformat(),
        "side": side,
        "trigger_type": trigger_type,
        "reason_code": code,
        "reason": label,
        "price": float(price),
        "volume_ratio": float(vol),
        "zone_distance_atr": float(ctx.get("zone_distance_atr", 0) or 0),
        "extension_atr": None if ext is None else float(ext),
        "ema_1h": ctx.get("ema_1h"),
        "ema_15m": ctx.get("ema_15m"),
        "oi_dir": flow.get("oi_dir"),
        "cvd_dir": flow.get("cvd_dir"),
        "oi_recent_pct": flow.get("oi_recent_pct"),
        "cvd_recent_delta": flow.get("cvd_recent_delta"),
    }
    v.setdefault("blocked_events", []).append(event)
    v["blocked_events"] = v["blocked_events"][-500:]
    seen.append(key)
    v["blocked_seen_keys"] = seen[-1200:]
    return True


def export_blocked_data(state):
    events = list(state["v7"].get("blocked_events", []))
    summary = {}
    for e in events:
        code = e.get("reason_code", "OTHER")
        item = summary.setdefault(code, {
            "reason": e.get("reason", code),
            "count": 0,
            "LONG": 0,
            "SHORT": 0,
        })
        item["count"] += 1
        if e.get("side") in ("LONG", "SHORT"):
            item[e["side"]] += 1

    ordered = dict(sorted(summary.items(), key=lambda kv: (-kv[1]["count"], kv[0])))
    with open(BLOCKED_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "version": VERSION,
            "total_blocked": len(events),
            "summary": ordered,
            "recent": events[-100:],
        }, f, ensure_ascii=False, indent=2)

    fields = [
        "candle_time", "time_utc", "side", "trigger_type",
        "reason_code", "reason", "price", "volume_ratio",
        "zone_distance_atr", "extension_atr",
        "ema_1h", "ema_15m", "oi_dir", "cvd_dir",
        "oi_recent_pct", "cvd_recent_delta",
    ]
    with open(BLOCKED_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for e in events:
            w.writerow({k: e.get(k, "") for k in fields})



def export_prepare_data(state):
    completed = state["v7"].get("prepare_history", [])
    open_trades = state.get("prepare_trades", [])

    with open(PREP_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "version": VERSION,
            "completed": completed,
            "open": open_trades,
        }, f, ensure_ascii=False, indent=2)

    fields = [
        "version","id","setup_id","track","side","structure_label",
        "entry_time","entry_price","tp1","tp2","sl","risk_atr",
        "ema_1h","ema_15m","zone_distance_atr","volume_ratio",
        "oi_dir","cvd_dir","oi_recent_pct","cvd_recent_delta",
        "anchor_price","defense_price",
        "ret_15m","ret_30m","ret_1h","ret_2h",
        "mfe","mae","tp1_hit","terminal_outcome","final_result",
        "duration_min","entry_reason"
    ]

    with open(PREP_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in completed + open_trades:
            feat = x.get("features", {})
            row = {k: x.get(k, feat.get(k, "")) for k in fields}
            for h in FORWARD_HORIZONS:
                row["ret_" + h] = x.get("results", {}).get(h, {}).get("return")
            w.writerow(row)


def export_data(state):
    completed = state["v7"].get("history", [])
    open_trades = state.get("paper_trades", [])

    with open(V7_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "version": VERSION,
            "completed": completed,
            "open": open_trades,
        }, f, ensure_ascii=False, indent=2)

    fields = [
        "version","id","setup_id","trigger_type","side","quality_score",
        "entry_time","entry_price","tp1","tp2","sl","risk_atr",
        "swing_level","breakout_distance_atr","volume_ratio",
        "ema_1h","ema_15m","zone_distance_atr","cvd_dir","oi_dir",
        "oi_recent_pct","cvd_recent_delta","news",
        "ret_15m","ret_30m","ret_1h","ret_2h",
        "mfe","mae","tp1_hit","terminal_outcome","final_result",
        "duration_min","entry_reason"
    ]

    with open(V7_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in completed + open_trades:
            feat = x.get("features", {})
            row = {k: x.get(k, feat.get(k, "")) for k in fields}
            for h in FORWARD_HORIZONS:
                row["ret_" + h] = x.get("results", {}).get(h, {}).get("return")
            w.writerow(row)

    export_blocked_data(state)
    export_prepare_data(state)

def zh_side(side): return "做多" if side=="LONG" else "做空"
def em(v): return {"BULL":"🟢 多頭","BEAR":"🔴 空頭","NEUTRAL":"⚪ 中性"}.get(v,"⚪ 中性")
def fd(v): return {"UP":"🔺 上升","DOWN":"🔻 下降","FLAT":"⚪ 持平","UNAVAILABLE":"⚠️ 累積中/無資料"}.get(v,"⚠️ 無資料")



def build_prepare_plan(side, ctx, structure, price):
    """
    Preview-only trade plan for a structure-ready alert.
    It does NOT create a formal Paper Trade.
    """
    atr = float(ctx["atr"])
    defense = float(structure["pullback"][1])
    entry = float(price)

    if side == "LONG":
        sl_raw = defense - STOP_BUFFER_ATR * atr
        risk = entry - sl_raw
    else:
        sl_raw = defense + STOP_BUFFER_ATR * atr
        risk = sl_raw - entry

    min_risk = MIN_STOP_ATR * atr
    max_risk = MAX_STOP_ATR * atr
    risk = max(risk, min_risk)
    too_wide = risk > max_risk

    if side == "LONG":
        sl = entry - risk
        tp1 = entry + risk
        tp2 = entry + 2.0 * risk
    else:
        sl = entry + risk
        tp1 = entry - risk
        tp2 = entry - 2.0 * risk

    return {
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "risk_atr": risk / atr if atr else None,
        "too_wide": too_wide,
    }


def create_prepare_trade(state, setup_id, side, label, plan, ctx, flow, vol, price, candle_time, structure):
    key = f"{setup_id}|{side}|{int(candle_time)}|PREP"
    existing = state.get("prepare_trades", [])
    if any(x.get("dedupe_key") == key for x in existing):
        return

    v = state["v7"]
    pid = int(v.get("next_prepare_id", 1))
    v["next_prepare_id"] = pid + 1
    trade = {
        "version": VERSION,
        "id": pid,
        "setup_id": setup_id,
        "dedupe_key": key,
        "track": "PREPARE",
        "side": side,
        "structure_label": label,
        "entry_time": int(candle_time),
        "entry_price": float(plan["entry"]),
        "sl": float(plan["sl"]),
        "tp1": float(plan["tp1"]),
        "tp2": float(plan["tp2"]),
        "risk_atr": plan.get("risk_atr"),
        "terminal_outcome": None,
        "tp1_hit": False,
        "events": [],
        "results": {},
        "max_high": float(plan["entry"]),
        "min_low": float(plan["entry"]),
        "features": {
            "ema_1h": ctx.get("ema_1h"),
            "ema_15m": ctx.get("ema_15m"),
            "zone_distance_atr": ctx.get("zone_distance_atr"),
            "volume_ratio": float(vol),
            "oi_dir": flow.get("oi_dir"),
            "cvd_dir": flow.get("cvd_dir"),
            "oi_recent_pct": flow.get("oi_recent_pct"),
            "cvd_recent_delta": flow.get("cvd_recent_delta"),
            "anchor_price": structure.get("anchor_price"),
            "defense_price": structure.get("defense_price"),
        },
        "entry_reason": f"STRUCTURE_PREP | {label}",
    }
    state.setdefault("prepare_trades", []).append(trade)


def update_prepare_trades(state, rows15):
    latest = int(rows15[-1]["time"])
    still_open = []
    history = state["v7"].setdefault("prepare_history", [])

    for trade in state.get("prepare_trades", []):
        entry_t = int(trade["entry_time"])
        entry = float(trade["entry_price"])
        side = trade["side"]

        for r in rows15:
            t = int(r["time"])
            if not (entry_t < t <= latest):
                continue

            trade["max_high"] = max(float(trade.get("max_high", entry)), float(r["high"]))
            trade["min_low"] = min(float(trade.get("min_low", entry)), float(r["low"]))

            for ev in hit_levels(trade, r):
                if ev not in trade["events"]:
                    trade["events"].append(ev)
                if ev in ("TP2", "SL", "AMBIGUOUS"):
                    trade["terminal_outcome"] = ev
                    trade["outcome_time"] = int(r["time"])

        for label, secs in FORWARD_HORIZONS.items():
            if label in trade["results"] or latest < entry_t + secs:
                continue
            row = next((r for r in rows15 if int(r["time"]) >= entry_t + secs), None)
            if row:
                raw = float(row["close"]) / entry - 1
                signed = raw if side == "LONG" else -raw
                trade["results"][label] = {
                    "price": float(row["close"]),
                    "return": signed,
                    "correct": signed > 0
                }

        if trade.get("terminal_outcome") in ("TP2", "SL", "AMBIGUOUS"):
            mfe = (float(trade["max_high"]) / entry - 1) if side == "LONG" else (entry / float(trade["min_low"]) - 1)
            mae = (entry / float(trade["min_low"]) - 1) if side == "LONG" else (float(trade["max_high"]) / entry - 1)
            trade["mfe"] = max(0, mfe)
            trade["mae"] = max(0, mae)
            trade["closed_time"] = trade.get("outcome_time")
            trade["duration_min"] = max(0, (int(trade["closed_time"]) - entry_t)//60) if trade.get("closed_time") else None
            if trade.get("tp1_hit") and trade["terminal_outcome"] == "SL":
                trade["final_result"] = "TP1_THEN_SL"
            else:
                trade["final_result"] = trade["terminal_outcome"]

            if not any(x.get("dedupe_key") == trade.get("dedupe_key") for x in history):
                history.append(trade)
        else:
            still_open.append(trade)

    state["prepare_trades"] = still_open


def create_signal(state, side, trigger_type, level, ext, ctx, flow, vol, risk, close, closed_time, news, defense_level=None, setup_id=None):
    v=state["v7"]
    if setup_id is None:
        sid=v["next_setup_id"]
        v["next_setup_id"]+=1
    else:
        sid=int(setup_id)
        if v["next_setup_id"] <= sid:
            v["next_setup_id"] = sid + 1
    tid=v["next_test_id"]
    v["next_test_id"]+=1
    q=v7_quality_score(side,ctx,vol,flow,ext)
    test={"version":VERSION,"id":tid,"setup_id":sid,"trigger_type":trigger_type,"side":side,"quality_score":q,"score":q,"entry_time":closed_time,"entry_price":close,"tp1":risk["tp1"],"tp2":risk["tp2"],"sl":risk["sl"],"risk_atr":risk["risk_atr"],"risk_pct":risk["risk"]/close,"swing_level":level,"defense_level":float(defense_level if defense_level is not None else level),"breakout_distance_atr":ext,"volume_ratio":vol,"results":{},"max_high":close,"min_low":close,"notify_setup":True,"features":{"ema_1h":ctx["ema_1h"],"ema_15m":ctx["ema_15m"],"zone_distance_atr":ctx["zone_distance_atr"],"ema34_slope_atr":ctx["s34"],"ema50_slope_atr":ctx["s50"],"cvd_dir":flow["cvd_dir"],"oi_dir":flow["oi_dir"],"oi_recent_pct":flow["oi_recent_pct"],"cvd_recent_delta":flow["cvd_recent_delta"],"volume_ratio":vol,"breakout_distance_atr":ext,"news":news}}
    test["entry_reason"] = (
        f"{trigger_type} | 1H={ctx['ema_1h']} | 15M={ctx['ema_15m']} | "
        f"Volume={vol:.2f}x | OI={flow['oi_dir']} | CVD_PROXY={flow['cvd_dir']} | "
        f"Zone={ctx['zone_distance_atr']:.2f}ATR"
    )
    state["paper_trades"].append(test)
    v["active_setup"]={"id":sid,"test_id":tid,"side":side,"trigger_type":trigger_type,"defense_level":float(defense_level if defense_level is not None else level),"enhanced":False,"start_time":closed_time}
    warning=""
    wanted="BULL" if side=="LONG" else "BEAR"
    if ctx["ema_1h"]!=wanted: warning="\n⚠️ 逆 1H 趨勢・偏激進"
    typ={"BREAKOUT":"BREAKOUT 突破","RETEST":"RETEST 回踩","CONTINUATION":"HH/HL・LL/LH 延續"}.get(trigger_type, trigger_type)
    send_discord(f"⚡ V7 新訊號 #{sid}｜BTC {zh_side(side)}\n\n💰 進場 ${close:,.0f}\n🎯 TP1 ${risk['tp1']:,.0f}｜TP2 ${risk['tp2']:,.0f}\n🛑 SL ${risk['sl']:,.0f}\n⚖️ 風險距離 {risk['risk_atr']:.2f} ATR\n\n📍 {typ}\n1H：{em(ctx['ema_1h'])}\n15M：{em(ctx['ema_15m'])}\nVolume：{'🔥 ' if vol>=VOL_STRONG else ''}{vol:.2f}×\nCVD Proxy：{fd(flow['cvd_dir'])}\nOI：{fd(flow['oi_dir'])}\n品質 Score：{q}/8{warning}\n📰 {news}")


def main():
    state=load_state(); rows15=closed_rows("15m",240); rows1h=closed_rows("1h",120)
    if len(rows15)<80 or len(rows1h)<60:
        raise RuntimeError("K線資料不足")

    closed=rows15[-1]; prev=rows15[-2]
    ct=int(closed["time"]); close=float(closed["close"]); prev_close=float(prev["close"])
    a=atr(rows15,14)

    update_oi_history(state)
    oi,om=direction_details("open-interest", state)
    cvd,cm=direction_details("cvd", state)

    # Update existing simulated trades before evaluating a new closed candle.
    update_paper_trades(state,rows15)
    update_prepare_trades(state,rows15)

    ctx=ema_context(rows15,rows1h,a)
    vol=volume_ratio(rows15)
    highs,lows=pivots(rows15)
    structure=classify_structure(rows15, highs, lows, a)

    news,new_major,headline=news_status_v7(state)
    if new_major and not MANUAL_RUN:
        send_discord(
            f"🔴 BTC 市場風險提醒\n\n"
            f"📰 偵測到新的高影響事件\n{headline[:140]}\n"
            f"⚠️ 短線波動風險提高\n"
            f"ℹ️ 不直接改變交易方向"
        )

    price_dir="UP" if close>prev_close else "DOWN" if close<prev_close else "FLAT"
    long_flow=flow_for_side("LONG",price_dir,om,cm)
    short_flow=flow_for_side("SHORT",price_dir,om,cm)
    v=state["v7"]

    if MANUAL_RUN:
        active=v.get("active_setup")
        at=f"#{active['id']} {zh_side(active['side'])}" if active else "無"
        sh=highs[-1][1] if highs else None
        sl=lows[-1][1] if lows else None
        sh_text=f"${sh:,.0f}" if sh is not None else "N/A"
        sl_text=f"${sl:,.0f}" if sl is not None else "N/A"

        send_discord(
            f"📊 BTC V7 OKX 市場現況｜手動查詢\n\n"
            f"💰 BTC：${close:,.0f}\n"
            f"1H：{em(ctx['ema_1h'])}\n"
            f"15M：{em(ctx['ema_15m'])}\n"
            f"EMA Zone 距離：{ctx['zone_distance_atr']:.2f} ATR\n"
            f"Volume：{vol:.2f}×\n"
            f"OI：{fd(om.get('recent_dir','UNAVAILABLE'))}"
            f"{(' (' + format(float(om['recent_pct']), '+.2%') + ')') if om.get('recent_pct') is not None else ''}\n"
            f"CVD Proxy：{fd(cm.get('recent_dir','UNAVAILABLE'))}\n"
            f"ATR：{a/close:.2%}\n\n"
            f"最近 Swing High：{sh_text}\n"
            f"最近 Swing Low：{sl_text}\n"
            f"🧱 15M 結構："
            f"{('🟢 ' + structure['label']) if structure.get('side')=='LONG' else ('🔴 ' + structure['label']) if structure.get('side')=='SHORT' else '⚪ 尚未形成 HH→HL / LL→LH'}\n"
            f"📡 目前訊號：{at}\n"
            f"👀 準備單 完成：{len(v.get('prepare_history',[]))}｜進行中：{len(state.get('prepare_trades',[]))}\n"
            f"⚡ 正式單 完成：{len(v.get('history',[]))}｜進行中：{len(state.get('paper_trades',[]))}\n"
            f"📰 {news}\n\n"
            f"ℹ️ 手動查詢不建立任何新模擬單"
        )
        export_data(state); save_state(state); return

    # Only process signal-generation logic once per newly closed 15m candle.
    if v.get("last_processed_candle")!=ct:
        active=v.get("active_setup")

        # Structural invalidation / strengthening for an existing formal setup.
        if active:
            invalid=(close<float(active["defense_level"])) if active["side"]=="LONG" else (close>float(active["defense_level"]))
            if invalid:
                send_discord(
                    f"⚪ 訊號 #{active['id']} 失效｜BTC {zh_side(active['side'])}\n\n"
                    f"📍 15M 收盤破壞原結構\n"
                    f"ℹ️ 結構失效，不代表 SL 已觸發"
                )
                v["active_setup"]=None
                active=None
            elif not active.get("enhanced"):
                side=active["side"]
                fl=long_flow if side=="LONG" else short_flow
                wanted="BULL" if side=="LONG" else "BEAR"
                evidence=sum([
                    ctx["ema_1h"]==wanted,
                    ctx["ema_15m"]==wanted,
                    fl["strong_both"],
                    vol>=VOL_STRONG
                ])
                if evidence>=2:
                    send_discord(
                        f"🔥 訊號 #{active['id']} 增強｜BTC {zh_side(side)}\n\n"
                        f"📍 原結構持續守住\n"
                        f"Volume：{vol:.2f}×\n"
                        f"CVD Proxy：{fd(fl['cvd_dir'])}\n"
                        f"OI：{fd(fl['oi_dir'])}\n"
                        f"1H：{em(ctx['ema_1h'])}"
                    )
                    active["enhanced"]=True

        # 1) Structure Prepare: HH→HL / LL→LH.
        if structure.get("side"):
            side=structure["side"]
            label=structure["label"]
            wanted="BULL" if side=="LONG" else "BEAR"
            fl=long_flow if side=="LONG" else short_flow
            ready_key=structure["key"]
            zone_near=ctx["zone_distance_atr"]<=STRUCT_READY_ZONE_ATR
            ema_not_opposite=ctx["ema_15m"]==wanted

            if ready_key != v.get("structure_ready_key") and zone_near and ema_not_opposite and not fl.get("opposed"):
                v["structure_ready_key"]=ready_key
                v["structure_ready_side"]=side
                v["structure_ready_time"]=ct

                # Reserve one setup_id for this structure so PREPARE and FORMAL can be compared.
                prep_map=v.setdefault("prepare_setup_ids", {})
                if ready_key not in prep_map:
                    prep_map[ready_key]=int(v["next_setup_id"])
                    v["next_setup_id"]+=1
                setup_id=prep_map[ready_key]

                plan=build_prepare_plan(side,ctx,structure,close)
                create_prepare_trade(
                    state, setup_id, side, label, plan, ctx, fl, vol,
                    close, ct, structure
                )
                impulse=float(structure["impulse"][1])
                pull=float(structure["pullback"][1])
                plan_note=(
                    "⚠️ 預設 SL 距離偏寬，等待正式觸發重新計算"
                    if plan["too_wide"]
                    else "✅ 預設風險距離在允許範圍"
                )
                send_discord(
                    f"👀 BTC {zh_side(side)}結構準備｜{label}\n\n"
                    f"💰 BTC：${close:,.0f}\n"
                    f"📍 前段結構：${impulse:,.0f}\n"
                    f"🛡️ 回踩防守：${pull:,.0f}\n"
                    f"EMA Zone 距離：{ctx['zone_distance_atr']:.2f} ATR\n"
                    f"Volume：{vol:.2f}×\n"
                    f"OI：{fd(fl['oi_dir'])}\n"
                    f"CVD Proxy：{fd(fl['cvd_dir'])}\n\n"
                    f"📝 預設交易計畫（準備模擬單 #{setup_id}）\n"
                    f"Entry：${plan['entry']:,.0f}\n"
                    f"SL：${plan['sl']:,.0f}\n"
                    f"TP1：${plan['tp1']:,.0f}\n"
                    f"TP2：${plan['tp2']:,.0f}\n"
                    f"Risk：約 {plan['risk_atr']:.2f} ATR\n"
                    f"{plan_note}\n\n"
                    f"ℹ️ 這筆會記入 Prepare Track；只有 ⚡ 正式訊號才算正式單"
                )

            # 2) Early continuation trigger from the same prepared structure.
            micro=micro_resumption(rows15,structure,a)
            if micro and micro.get("fired") and ready_key != v.get("structure_early_key"):
                ok,reason=early_eligibility(side,close,ctx,vol,fl)
                risk=continuation_risk(side,close,a,structure)
                if ok and risk:
                    current=v.get("active_setup")
                    reversal=current and current["side"]!=side
                    if reversal:
                        send_discord(f"🔄 市場結構反轉｜BTC {zh_side(current['side'])} → {zh_side(side)}")
                        v["active_setup"]=None
                    if v.get("active_setup") is None:
                        v["structure_early_key"]=ready_key
                        sid=v.setdefault("prepare_setup_ids",{}).get(ready_key)
                        create_signal(
                            state,side,"CONTINUATION",
                            float(micro["level"]),float(micro["ext"]),
                            ctx,fl,vol,risk,close,ct,news,
                            defense_level=float(structure["pullback"][1]),
                            setup_id=sid
                        )
                else:
                    block_reason=reason if not ok else "SL / RR / 空間不適合"
                    print(f"V7 structure continuation {side} blocked: {block_reason}")
                    record_blocked(
                        state,ct,side,"CONTINUATION",block_reason,
                        close,vol,ctx,fl,float(micro.get("ext",0) or 0)
                    )

        # 3) Fresh confirmed 2-2 Swing Breakout.
        last_high=highs[-1] if highs else None
        last_low=lows[-1] if lows else None
        candidates=[]

        if last_high:
            level=last_high[1]
            ext=(close-level)/a
            if prev_close<=level and close>=level+BREAK_ATR*a:
                candidates.append(("LONG",level,ext,long_flow))
        if last_low:
            level=last_low[1]
            ext=(level-close)/a
            if prev_close>=level and close<=level-BREAK_ATR*a:
                candidates.append(("SHORT",level,ext,short_flow))

        for side,level,ext,fl in candidates:
            ok,reason=eligibility(side,close,ctx,a,vol,fl,ext)
            risk=structure_risk(side,close,a,highs,lows)

            # Always arm the first retest, even when direct breakout is not tradable.
            v["breakout_watch"]={
                "side":side,"level":level,"break_time":ct,
                "bars":0,"used":False
            }

            current=v.get("active_setup")
            reversal=current and current["side"]!=side
            same_continuation=(
                current
                and current.get("side")==side
                and current.get("trigger_type")=="CONTINUATION"
                and not current.get("enhanced")
            )

            if same_continuation:
                confirm_note=(
                    "✅ 位置仍符合正式突破條件"
                    if ok and risk
                    else f"⚠️ 已突破，但不追加進場：{reason if not ok else 'SL / RR 結構不適合'}"
                )
                send_discord(
                    f"🔥 訊號 #{current['id']} 突破確認｜BTC {zh_side(side)}\n\n"
                    f"📍 HH/HL・LL/LH 延續後，正式 Swing Breakout 已確認\n"
                    f"💰 BTC：${close:,.0f}\n"
                    f"突破位：${level:,.0f}\n"
                    f"Volume：{'🔥 ' if vol>=VOL_STRONG else ''}{vol:.2f}×\n"
                    f"EMA Zone 距離：{ctx['zone_distance_atr']:.2f} ATR\n"
                    f"CVD Proxy：{fd(fl['cvd_dir'])}\n"
                    f"OI：{fd(fl['oi_dir'])}\n"
                    f"{confirm_note}\n"
                    f"ℹ️ 這是原正式單的確認，不建立第二筆正式單"
                )
                current["enhanced"]=True

            elif ok and risk:
                if reversal:
                    send_discord(f"🔄 市場結構反轉｜BTC {zh_side(current['side'])} → {zh_side(side)}")
                    v["active_setup"]=None
                if v.get("active_setup") is None:
                    create_signal(
                        state,side,"BREAKOUT",level,ext,
                        ctx,fl,vol,risk,close,ct,news
                    )
            else:
                block_reason=reason if not ok else "SL / RR 結構不適合"
                print(f"V7 breakout blocked: {block_reason}")
                record_blocked(
                    state,ct,side,"BREAKOUT",block_reason,
                    close,vol,ctx,fl,ext
                )
                # No standalone explosion-warning Discord in V7.
                # It stays in blocked stats and waits for first valid retest.

        # 4) First valid Retest after breakout.
        watch=v.get("breakout_watch")
        if watch and ct>int(watch["break_time"]) and not watch.get("used"):
            watch["bars"]=int(watch.get("bars",0))+1
            side=watch["side"]
            level=float(watch["level"])
            touched=(
                float(closed["low"])<=level+RETEST_TOL_ATR*a and close>=level
                if side=="LONG"
                else float(closed["high"])>=level-RETEST_TOL_ATR*a and close<=level
            )
            if touched:
                fl=long_flow if side=="LONG" else short_flow
                ext=abs(close-level)/a
                ok,reason=eligibility(side,close,ctx,a,vol,fl,ext)
                risk=structure_risk(side,close,a,highs,lows)
                watch["used"]=True
                if ok and risk and v.get("active_setup") is None:
                    create_signal(
                        state,side,"RETEST",level,ext,
                        ctx,fl,vol,risk,close,ct,news
                    )
                else:
                    if not ok:
                        block_reason=reason
                    elif not risk:
                        block_reason="SL / RR 結構不適合"
                    else:
                        block_reason="active setup"
                    print(f"V7 retest consumed, no alert: {block_reason}")
                    record_blocked(
                        state,ct,side,"RETEST",block_reason,
                        close,vol,ctx,fl,ext
                    )
            if watch["bars"]>=RETEST_MAX_BARS:
                v["breakout_watch"]=None

        v["last_processed_candle"]=ct

    export_data(state)
    save_state(state)
    print(
        f"Radar V7: BTC={close:.0f} "
        f"1H={ctx['ema_1h']} 15M={ctx['ema_15m']} "
        f"Structure={structure.get('label')} Vol={vol:.2f}x "
        f"PrepareOpen={len(state.get('prepare_trades',[]))} "
        f"FormalOpen={len(state.get('paper_trades',[]))} "
        f"FormalCompleted={len(v.get('history',[]))}"
    )


if __name__ == "__main__": main()
