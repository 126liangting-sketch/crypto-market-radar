import os
import json
import time
import requests
import feedparser
import csv

BASE = "https://futures.kraken.com/api/charts/v1"
SYMBOL = "PI_XBTUSD"
WEBHOOK = os.getenv("DISCORD_WEBHOOK")
STATE = "probability_state.json"
CSV_FILE = "forward_test_v5.csv"
V5_JSON = "forward_test_v5.json"
VERSION = "V5_BASELINE"
TP_SL_ATR_MULT = 1.0
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
CVD_FLAT_REL = 0.10               # <=2% of recent CVD range treated as flat

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


def candles(resolution, count=200):
    sec = {"15m": 900, "1h": 3600}[resolution]
    now = int(time.time())
    data = get(f"{BASE}/spot/{SYMBOL}/{resolution}",
               {"from": now - count * sec, "to": now, "count": count})
    rows = sorted(data.get("candles", []), key=lambda x: int(x["time"]))
    if not rows:
        raise RuntimeError(f"Kraken {resolution} K線沒有資料")
    return rows


def closed_rows(resolution, count=200):
    rows = candles(resolution, count)
    return rows[:-1] if len(rows) > 1 else rows


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


def analytics(kind):
    now = int(time.time())
    data = get(f"{BASE}/analytics/{SYMBOL}/{kind}",
               {"since": now - 18000, "to": now, "interval": 900})
    result = data.get("result", data)
    if not isinstance(result, dict): return []
    timestamps, raw = result.get("timestamp", []), result.get("data", [])
    key = "openInterest" if kind == "open-interest" else "cvd"
    values = raw.get(key, []) if isinstance(raw, dict) else raw
    if not values: values = result.get(key, [])
    out = []
    for ts, value in zip(timestamps, values):
        try:
            if isinstance(value, dict): value = value.get(key, value.get("value"))
            elif isinstance(value, list):
                nums = [float(x) for x in value if _number(x)]
                value = nums[-1] if nums else None
            out.append((int(ts), float(value)))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def direction_details(kind):
    """Return (classification, metrics) using ~3h broad + ~1h recent movement.
    Small moves are intentionally FLAT. A clear recent reversal neutralizes the broad direction.
    """
    try:
        rows = analytics(kind)
    except (requests.exceptions.RequestException, ValueError, RuntimeError) as e:
        print(f"{kind} 暫時無法取得: {type(e).__name__}: {e}")
        return "UNAVAILABLE", {}
    if len(rows) < 5:
        return "UNAVAILABLE", {}

    window = rows[-ANALYTICS_BARS:] if len(rows) >= ANALYTICS_BARS else rows
    recent = window[-ANALYTICS_CONFIRM_BARS:] if len(window) >= ANALYTICS_CONFIRM_BARS else window
    start_v, end_v = window[0][1], window[-1][1]
    recent_start, recent_end = recent[0][1], recent[-1][1]
    delta = end_v - start_v
    recent_delta = recent_end - recent_start
    metrics = {"broad_start": start_v, "broad_end": end_v, "broad_delta": delta,
               "recent_start": recent_start, "recent_end": recent_end, "recent_delta": recent_delta}

    if kind == "open-interest":
        pct = 0.0 if start_v == 0 else delta / abs(start_v)
        recent_pct = 0.0 if recent_start == 0 else recent_delta / abs(recent_start)
        broad = "FLAT" if abs(pct) <= OI_FLAT_PCT else ("UP" if pct > 0 else "DOWN")
        recent_dir = "FLAT" if abs(recent_pct) <= OI_FLAT_PCT else ("UP" if recent_pct > 0 else "DOWN")
        metrics.update({"broad_pct": pct, "recent_pct": recent_pct, "broad_dir": broad, "recent_dir": recent_dir})
        print(f"OI ~3h/12 bars: {start_v:.6g} -> {end_v:.6g} ({pct:+.3%}); recent ~1h: {recent_pct:+.3%}")
    else:
        vals = [v for _, v in window]
        span = max(vals) - min(vals) if vals else 0.0
        tol = span * CVD_FLAT_REL
        broad = "FLAT" if abs(delta) <= tol else ("UP" if delta > 0 else "DOWN")
        rvals = [v for _, v in recent]
        rspan = max(rvals) - min(rvals) if rvals else 0.0
        rtol = rspan * CVD_FLAT_REL
        recent_dir = "FLAT" if abs(recent_delta) <= rtol else ("UP" if recent_delta > 0 else "DOWN")
        metrics.update({"broad_span": span, "recent_span": rspan, "broad_dir": broad, "recent_dir": recent_dir})
        print(f"CVD ~3h/12 bars: {start_v:.6g} -> {end_v:.6g} (delta {delta:+.6g}); recent ~1h delta {recent_delta:+.6g}")

    final = broad
    if broad in ("UP", "DOWN") and recent_dir in ("UP", "DOWN") and broad != recent_dir:
        final = "FLAT"
        print(f"{kind}: broad={broad}, recent={recent_dir} -> FLAT (recent reversal)")
    metrics["final"] = final
    return final, metrics


def direction(kind):
    return direction_details(kind)[0]


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f: state = json.load(f)
        except (json.JSONDecodeError, OSError): state = {}
    else: state = {}
    # Preserve all old V3 fields/data, only add V4 fields when missing.
    state.setdefault("states", {})
    state.setdefault("pending", [])
    state.setdefault("last_closed_candle", None)
    state.setdefault("last_signal_key", None)
    state.setdefault("signal_armed", True)
    state.setdefault("forward_tests", [])
    state.setdefault("forward_stats", {})
    state.setdefault("last_forward_candle", None)
    state.setdefault("last_notification", {})
    state.setdefault("next_test_id", 1)
    state.setdefault("forward_history_v5", [])
    return state


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)


def stats_for(state, key):
    return state["states"].setdefault(key, {"LONG": 0, "SHORT": 0, "NEUTRAL": 0})


def finish_legacy_pending(state, latest_time, latest_price):
    keep = []
    for sample in state["pending"]:
        if latest_time < int(sample["target_time"]):
            keep.append(sample); continue
        change = latest_price / sample["price"] - 1
        result = "LONG" if change >= MOVE else "SHORT" if change <= -MOVE else "NEUTRAL"
        stats_for(state, sample["state"])[result] += 1
    state["pending"] = keep


def score_market(rows15, rows1h, oi, cvd):
    state1h, e34_1h, e50_1h = ema_state_from_rows(rows1h)
    state15, e34_15, e50_15 = ema_state_from_rows(rows15)
    last, prev = rows15[-1], rows15[-2]
    close, prev_close = float(last["close"]), float(prev["close"])
    high, low = float(last["high"]), float(last["low"])
    price_dir = "UP" if close > prev_close else "DOWN" if close < prev_close else "FLAT"
    a = atr(rows15, 14)
    atr_pct = (a / close) if a else 0.0
    zone_low, zone_high = sorted([e34_15, e50_15]) if e34_15 and e50_15 else (None, None)
    pad = (a or 0) * ZONE_ATR_MULT
    touches_zone = zone_low is not None and low <= zone_high + pad and high >= zone_low - pad
    atr_ok = ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT

    # Research-only features: stored, not used to change V4.5 trigger weights.
    if zone_low is None or not a:
        zone_distance_atr = None
    elif zone_low <= close <= zone_high:
        zone_distance_atr = 0.0
    else:
        nearest = zone_low if close < zone_low else zone_high
        zone_distance_atr = abs(close - nearest) / a

    closes15 = [float(x["close"]) for x in rows15]
    e34s = ema_series(closes15, 34); e50s = ema_series(closes15, 50)
    def slope_atr(series, lookback=4):
        vals = [x for x in series if x is not None]
        if len(vals) <= lookback or not a: return None
        return (vals[-1] - vals[-1-lookback]) / a
    ema34_slope_atr = slope_atr(e34s)
    ema50_slope_atr = slope_atr(e50s)
    ema_gap_atr = abs(e34_15 - e50_15) / a if a and e34_15 is not None and e50_15 is not None else None

    def calc(side):
        s, reasons = 0, []
        wanted = "BULL" if side == "LONG" else "BEAR"
        pdir = "UP" if side == "LONG" else "DOWN"
        if state1h == wanted: s += 2; reasons.append("1H +2")
        if state15 == wanted: s += 1; reasons.append("15M +1")
        zone_confirm = touches_zone and ((side == "LONG" and close >= zone_high) or (side == "SHORT" and close <= zone_low))
        if zone_confirm: s += 2; reasons.append("EMA Zone +2")
        if price_dir == pdir and cvd == pdir: s += 1; reasons.append("Price+CVD +1")
        if price_dir == pdir and oi == "UP": s += 1; reasons.append("Price+OI +1")
        if atr_ok: s += 1; reasons.append("ATR +1")
        return s, reasons, zone_confirm

    long_score, long_reasons, long_zone = calc("LONG")
    short_score, short_reasons, short_zone = calc("SHORT")
    if long_score > short_score:
        side, score, reasons, zone_ok = "LONG", long_score, long_reasons, long_zone
    elif short_score > long_score:
        side, score, reasons, zone_ok = "SHORT", short_score, short_reasons, short_zone
    else:
        side, score, reasons, zone_ok = "NONE", long_score, [], False
    return {
        "side": side, "score": score, "reasons": reasons,
        "ema_1h": state1h, "ema_15m": state15, "price_dir": price_dir,
        "oi": oi, "cvd": cvd, "atr_pct": atr_pct, "atr_value": a,
        "zone_ok": zone_ok, "zone_distance_atr": zone_distance_atr,
        "ema34_slope_atr": ema34_slope_atr, "ema50_slope_atr": ema50_slope_atr,
        "ema_gap_atr": ema_gap_atr,
        "long_score": long_score, "short_score": short_score,
    }


def forward_bucket(state, side, score):
    key = f"{side}|{score}"
    bucket = state["forward_stats"].setdefault(key, {})
    for h in FORWARD_HORIZONS:
        bucket.setdefault(h, {"total": 0, "correct": 0, "sum_return": 0.0})
    bucket.setdefault("completed_tests", 0)
    bucket.setdefault("sum_mfe", 0.0)
    bucket.setdefault("sum_mae", 0.0)
    bucket.setdefault("target_0_5", 0)
    bucket.setdefault("target_1_0", 0)
    return bucket


def _hit_levels(test, row):
    """Track TP/SL from closed 15m OHLC. If TP and SL occur inside the same candle,
    ordering is unknowable from OHLC, so mark AMBIGUOUS instead of inventing a result.
    """
    if test.get("first_outcome"):
        return
    side = test["side"]
    high, low = float(row["high"]), float(row["low"])
    tp1, tp2, sl = float(test["tp1"]), float(test["tp2"]), float(test["sl"])
    if side == "LONG":
        hit_sl, hit_tp1, hit_tp2 = low <= sl, high >= tp1, high >= tp2
    else:
        hit_sl, hit_tp1, hit_tp2 = high >= sl, low <= tp1, low <= tp2
    if hit_sl and hit_tp1:
        test["first_outcome"] = "AMBIGUOUS"
    elif hit_sl:
        test["first_outcome"] = "SL"
    elif hit_tp2:
        test["first_outcome"] = "TP2"
    elif hit_tp1:
        test["first_outcome"] = "TP1"
    if test.get("first_outcome"):
        test["outcome_time"] = int(row["time"])


def export_v5_csv(state):
    # Dedicated V5 research export. probability_state.json remains the runtime state.
    payload = {
        "version": VERSION,
        "completed": state.get("forward_history_v5", []),
        "pending": [x for x in state.get("forward_tests", []) if x.get("version") == VERSION],
    }
    with open(V5_JSON, "w", encoding="utf-8") as jf:
        json.dump(payload, jf, ensure_ascii=False, indent=2)

    fields = [
        "version","id","side","score","entry_time","entry_price","tp1","tp2","sl","risk_pct",
        "ema_1h","ema_15m","zone_ok","zone_distance_atr","ema34_slope_atr","ema50_slope_atr","ema_gap_atr",
        "price_dir","oi","oi_broad_pct","oi_recent_pct","cvd","cvd_broad_delta","cvd_recent_delta","atr_pct","news",
        "ret_15m","ret_30m","ret_1h","ret_2h","mfe","mae","first_outcome","outcome_time"
    ]
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in state.get("forward_history_v5", []):
            w.writerow({k: x.get(k, "") for k in fields})


def update_forward_tests(state, rows15):
    by_time = {int(r["time"]): r for r in rows15}
    latest_time = int(rows15[-1]["time"])
    keep, completed_msgs = [], []
    for test in state["forward_tests"]:
        entry_t, entry = int(test["entry_time"]), float(test["entry_price"])
        side, score = test["side"], int(test["score"])
        test.setdefault("results", {})
        test.setdefault("max_high", entry); test.setdefault("min_low", entry)
        for r in rows15:
            t = int(r["time"])
            if entry_t < t <= latest_time:
                test["max_high"] = max(test["max_high"], float(r["high"]))
                test["min_low"] = min(test["min_low"], float(r["low"]))
                if test.get("version") == VERSION and all(k in test for k in ("tp1","tp2","sl")):
                    _hit_levels(test, r)
        bucket = forward_bucket(state, side, score)
        for label, seconds in FORWARD_HORIZONS.items():
            if label in test["results"] or latest_time < entry_t + seconds: continue
            row = by_time.get(entry_t + seconds)
            if row is None: continue
            px = float(row["close"])
            raw_ret = px / entry - 1
            signed = raw_ret if side == "LONG" else -raw_ret
            correct = signed > 0
            test["results"][label] = {"price": px, "return": signed, "correct": correct}
            st = bucket[label]; st["total"] += 1; st["correct"] += int(correct); st["sum_return"] += signed
        if "2h" in test["results"]:
            mfe = (test["max_high"] / entry - 1) if side == "LONG" else (entry / test["min_low"] - 1)
            mae = (entry / test["min_low"] - 1) if side == "LONG" else (test["max_high"] / entry - 1)
            bucket["completed_tests"] += 1; bucket["sum_mfe"] += max(0.0, mfe); bucket["sum_mae"] += max(0.0, mae)
            bucket["target_0_5"] += int(mfe >= 0.005); bucket["target_1_0"] += int(mfe >= 0.01)
            if test.get("version") == VERSION:
                f = test.get("features", {})
                oi_m, cvd_m = f.get("oi_metrics", {}), f.get("cvd_metrics", {})
                row = {
                    "version": VERSION, "id": test["id"], "side": side, "score": score,
                    "entry_time": entry_t, "entry_price": entry, "tp1": test.get("tp1"), "tp2": test.get("tp2"), "sl": test.get("sl"),
                    "risk_pct": test.get("risk_pct"), "ema_1h": f.get("ema_1h"), "ema_15m": f.get("ema_15m"),
                    "zone_ok": f.get("zone_ok"), "zone_distance_atr": f.get("zone_distance_atr"),
                    "ema34_slope_atr": f.get("ema34_slope_atr"), "ema50_slope_atr": f.get("ema50_slope_atr"), "ema_gap_atr": f.get("ema_gap_atr"),
                    "price_dir": f.get("price_dir"), "oi": f.get("oi"), "oi_broad_pct": oi_m.get("broad_pct"), "oi_recent_pct": oi_m.get("recent_pct"),
                    "cvd": f.get("cvd"), "cvd_broad_delta": cvd_m.get("broad_delta"), "cvd_recent_delta": cvd_m.get("recent_delta"),
                    "atr_pct": f.get("atr_pct"), "news": f.get("news"),
                    "ret_15m": test["results"].get("15m",{}).get("return"), "ret_30m": test["results"].get("30m",{}).get("return"),
                    "ret_1h": test["results"].get("1h",{}).get("return"), "ret_2h": test["results"].get("2h",{}).get("return"),
                    "mfe": max(0.0,mfe), "mae": max(0.0,mae), "first_outcome": test.get("first_outcome","NONE"), "outcome_time": test.get("outcome_time")
                }
                if not any(x.get("id") == row["id"] for x in state["forward_history_v5"]):
                    state["forward_history_v5"].append(row)
            completed_msgs.append((test, mfe, mae))
        else:
            keep.append(test)
    state["forward_tests"] = keep
    export_v5_csv(state)
    return completed_msgs


def empirical_1h(state, side, score):
    b = forward_bucket(state, side, score)["1h"]
    if b["total"] == 0: return None, 0
    return b["correct"] / b["total"], b["total"]


def level_from_rate(rate):
    if rate >= 0.90: return "🚨 極強"
    if rate >= 0.80: return "🔥 強"
    if rate >= 0.70: return "🟢 偏強"
    if rate >= 0.60: return "🟡 注意"
    return "⚪ 未達通知門檻"


def score_level(score):
    return {5: "🟡 注意", 6: "🟢 偏強", 7: "🔥 強", 8: "🚨 極強"}.get(score, "⚪")


def news_status():
    titles = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            titles.extend(x.get("title", "") for x in feed.entries[:10])
        except Exception: pass
    if not titles: return "⚪ 無重大消息"
    words = ["sec", "fed", "fomc", "rate", "hack", "etf", "lawsuit", "ban", "regulation", "approval"]
    return "🔴 高影響消息" if any(w in " ".join(titles).lower() for w in words) else "🟡 有新消息"


def send_discord(message):
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


def emoji_ema(v): return {"BULL":"🟢 多頭","BEAR":"🔴 空頭","NEUTRAL":"⚪ 中性"}.get(v,"⚪ 中性")
def emoji_direction(v): return {"UP":"🔺 上升","DOWN":"🔻 下降","FLAT":"⚪ 持平","UNAVAILABLE":"⚪ 暫時無資料"}.get(v,"⚪ 暫時無資料")


def should_notify(state, side, score, rate, n, closed_time):
    # Once a bucket has enough forward samples, real 1h hit rate must be >=60%.
    if n >= MIN_FORWARD_SAMPLES and (rate is None or rate < FORWARD_NOTIFY_THRESHOLD):
        return False
    key = f"{side}|{score}"
    last = state["last_notification"].get(key)
    if last and closed_time - int(last) < NOTIFY_COOLDOWN:
        return False
    state["last_notification"][key] = closed_time
    return True


def main():
    state = load_state()
    rows15 = closed_rows("15m", 220)
    rows1h = closed_rows("1h", 120)
    if len(rows15) < 60 or len(rows1h) < 60:
        raise RuntimeError("K線資料不足")
    closed = rows15[-1]
    closed_time, closed_price = int(closed["time"]), float(closed["close"])

    oi, oi_metrics = direction_details("open-interest")
    cvd, cvd_metrics = direction_details("cvd")
    finish_legacy_pending(state, closed_time, closed_price)
    completed = update_forward_tests(state, rows15)

    # Keep V3 exact-state statistics alive when analytics are available.
    ema1, _, _ = ema_state_from_rows(rows1h)
    ema15, _, _ = ema_state_from_rows(rows15)
    analytics_ok = oi != "UNAVAILABLE" and cvd != "UNAVAILABLE"
    if (not MANUAL_RUN) and analytics_ok and state["last_closed_candle"] != closed_time:
        market_state = f"{ema1}|{ema15}|{oi}|{cvd}"
        state["pending"].append({"candle_time": closed_time, "target_time": closed_time + PREDICT_SECONDS,
                                 "price": closed_price, "state": market_state})
        state["last_closed_candle"] = closed_time

    if analytics_ok:
        sig = score_market(rows15, rows1h, oi, cvd)
        side, score = sig["side"], sig["score"]
        # workflow_dispatch is a pure manual status query: it never creates a new signal/test.
        if MANUAL_RUN:
            rate, n = empirical_1h(state, side, score) if side in ("LONG", "SHORT") else (None, 0)
            if score >= MIN_SCORE:
                status = f"已達 {MIN_SCORE}/8 訊號門檻"
            else:
                status = f"觀察中・距離 {MIN_SCORE}/8 還差 {MIN_SCORE - score} 分"
            empirical = "累積中" if n < MIN_FORWARD_SAMPLES or rate is None else f"{rate:.1%}（1H樣本 {n}）"
            news = news_status()
            message = (
                f"📊 BTC 市場現況｜手動查詢\n\n"
                f"💰 BTC：${closed_price:,.0f}\n"
                f"目前方向：{'🟢 LONG' if side == 'LONG' else '🔴 SHORT' if side == 'SHORT' else '⚪ 無明確方向'}\n"
                f"LONG：{sig['long_score']} / 8\n"
                f"SHORT：{sig['short_score']} / 8\n\n"
                f"1H：{emoji_ema(sig['ema_1h'])}\n"
                f"15M：{emoji_ema(sig['ema_15m'])}\n"
                f"EMA Zone：{'✅ 符合' if sig['zone_ok'] else '➖ 未符合'}\n"
                f"OI：{emoji_direction(oi)}\n"
                f"CVD：{emoji_direction(cvd)}\n"
                f"ATR：{sig['atr_pct']:.2%}\n\n"
                f"⚪ 狀態：{status}\n"
                f"📊 Forward實測：{empirical}\n"
                f"🧪 Forward進行中：{len(state['forward_tests'])}\n"
                f"📰 消息：{news}\n\n"
                f"ℹ️ 手動查詢不建立新 Forward Test"
            )
            send_discord(message)
        # Every qualifying scheduled closed 15m candle becomes a forward-test sample, even if Discord is suppressed.
        elif state["last_forward_candle"] != closed_time and side in ("LONG", "SHORT") and score >= MIN_SCORE:
            test_id = state["next_test_id"]
            state["next_test_id"] += 1
            # V5 keeps the V4.5 trigger/score unchanged. TP/SL are research targets only.
            risk = max(float(sig.get("atr_value") or 0.0) * TP_SL_ATR_MULT, closed_price * 0.002)
            if side == "LONG":
                sl, tp1, tp2 = closed_price - risk, closed_price + risk, closed_price + 2 * risk
            else:
                sl, tp1, tp2 = closed_price + risk, closed_price - risk, closed_price - 2 * risk
            news = news_status()
            state["forward_tests"].append({
                "version": VERSION,
                "id": test_id, "entry_time": closed_time, "entry_price": closed_price,
                "side": side, "score": score, "results": {},
                "max_high": closed_price, "min_low": closed_price,
                "tp1": tp1, "tp2": tp2, "sl": sl, "risk_pct": risk / closed_price,
                "first_outcome": None, "outcome_time": None,
                "features": {
                    "ema_1h": sig["ema_1h"], "ema_15m": sig["ema_15m"],
                    "zone_ok": sig["zone_ok"], "zone_distance_atr": sig["zone_distance_atr"],
                    "ema34_slope_atr": sig["ema34_slope_atr"], "ema50_slope_atr": sig["ema50_slope_atr"], "ema_gap_atr": sig["ema_gap_atr"],
                    "price_dir": sig["price_dir"], "oi": oi, "cvd": cvd, "atr_pct": sig["atr_pct"],
                    "oi_metrics": oi_metrics, "cvd_metrics": cvd_metrics, "news": news,
                }
            })
            state["last_forward_candle"] = closed_time

            rate, n = empirical_1h(state, side, score)
            if should_notify(state, side, score, rate, n, closed_time):
                if n < MIN_FORWARD_SAMPLES:
                    confidence = f"累積中（1H 已驗證 {n}/{MIN_FORWARD_SAMPLES}）"
                    level = score_level(score) + "・驗證中"
                else:
                    confidence = f"{rate:.1%}（1H樣本 {n}）"
                    level = level_from_rate(rate)
                message = (
                    f"{'🟢' if side == 'LONG' else '🔴'} BTC {side}｜{score}/8｜Forward #{test_id}\n\n"
                    f"💰 Entry ${closed_price:,.0f}\n"
                    f"🎯 TP1 ${tp1:,.0f}｜TP2 ${tp2:,.0f}\n"
                    f"🛑 SL ${sl:,.0f}\n\n"
                    f"1H {'🟢' if sig['ema_1h']=='BULL' else '🔴' if sig['ema_1h']=='BEAR' else '⚪'}｜"
                    f"15M {'🟢' if sig['ema_15m']=='BULL' else '🔴' if sig['ema_15m']=='BEAR' else '⚪'}｜"
                    f"Zone {'🟢' if sig['zone_ok'] else '➖'}\n"
                    f"CVD {emoji_direction(cvd).split()[0]}｜OI {emoji_direction(oi).split()[0]}｜ATR {sig['atr_pct']:.2%}\n"
                    f"📰 {'🔴' if '高影響' in news else '🟡' if '新消息' in news else '⚪'}"
                )
                send_discord(message)
        elif state["last_forward_candle"] != closed_time:
            # Mark candle processed even when no test is created; avoids duplicate work on 5m workflow runs.
            state["last_forward_candle"] = closed_time
        print(f"Radar V5: LONG={sig['long_score']}/8 SHORT={sig['short_score']}/8 chosen={side} {score}/8")
    else:
        print(f"Radar PARTIAL: OI={oi}, CVD={cvd}; 本期不建立 Score/Forward 樣本")

    save_state(state)
    print("Forward pending:", len(state["forward_tests"]), "Legacy pending:", len(state["pending"]))
    if completed:
        print("Completed forward tests:", ", ".join(str(x[0]["id"]) for x in completed))


if __name__ == "__main__":
    main()
