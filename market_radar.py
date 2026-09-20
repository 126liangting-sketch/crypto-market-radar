import os
import json
import time
import requests
import feedparser
import csv
import hashlib

BASE = "https://futures.kraken.com/api/charts/v1"
SYMBOL = "PI_XBTUSD"
WEBHOOK = os.getenv("DISCORD_WEBHOOK")
STATE = "probability_state.json"
CSV_FILE = "forward_test_v5.csv"
V5_JSON = "forward_test_v5.json"
VERSION = "V5_2_MECHANISM"
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
OI_FLAT_PCT = 0.0010               # +/-0.10% treated as flat
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


def analytics_fields(kind, keys):
    """Read one or more named arrays from Kraken Futures market analytics."""
    now = int(time.time())
    data = get(f"{BASE}/analytics/{SYMBOL}/{kind}",
               {"since": now - 18000, "to": now, "interval": 900})
    result = data.get("result", data)
    if not isinstance(result, dict):
        return []
    timestamps = result.get("timestamp", [])
    raw = result.get("data", {})
    if not isinstance(raw, dict):
        return []
    arrays = [raw.get(k, []) for k in keys]
    if not timestamps or any(not a for a in arrays):
        return []
    out = []
    for values in zip(timestamps, *arrays):
        try:
            out.append((int(values[0]), *[float(x) for x in values[1:]]))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def trade_volume_rows():
    # Kraken spot/index candles can legitimately report volume=0.
    # Use the futures analytics trade-volume feed (buyVolume + sellVolume) instead.
    return analytics_fields("trade-volume", ("buyVolume", "sellVolume"))


def cvd_fallback_from_trade_volume():
    """Build a cumulative delta series from buy/sell trade volume when Kraken CVD is flat/zero."""
    rows = trade_volume_rows()
    total = 0.0
    out = []
    for ts, buy, sell in rows:
        total += buy - sell
        out.append((ts, total))
    return out


def direction_details(kind):
    """Return (classification, metrics) using ~3h broad + ~1h recent movement.
    Small moves are intentionally FLAT. A clear recent reversal neutralizes the broad direction.
    """
    try:
        rows = analytics(kind)
        if kind == "cvd" and rows and all(abs(v) < 1e-12 for _, v in rows):
            fallback = cvd_fallback_from_trade_volume()
            if len(fallback) >= 5:
                rows = fallback
                print("CVD API 回傳全 0，改用 buyVolume-sellVolume 建立 CVD fallback")
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



VERSION = "V6_1_FLOW_FIX"
V5_JSON = "forward_test_v5.json"
V5_CSV = "forward_test_v5.csv"
V6_JSON = "forward_test_v6.json"
V6_CSV = "forward_test_v6.csv"
STATE = "probability_state.json"
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
NO_CHASE_ATR = 1.00
EMA_BAD_SLOPE = 0.15
MIN_STOP_ATR = 0.50
MAX_STOP_ATR = 1.25
STOP_BUFFER_ATR = 0.20
OI_FLAT_V6 = 0.0010  # 0.10%
NEWS_RED_SECONDS = 2 * 3600
NEWS_YELLOW_SECONDS = 8 * 3600


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f: state = json.load(f)
        except (json.JSONDecodeError, OSError): state = {}
    else: state = {}
    # Keep every V5.2 field untouched; V6 uses its own namespace.
    state.setdefault("states", {})
    state.setdefault("pending", [])
    state.setdefault("forward_tests", [])
    state.setdefault("forward_history_v5", [])
    state.setdefault("forward_stats", {})
    state.setdefault("v6", {})
    v = state["v6"]
    v.setdefault("last_processed_candle", None)
    v.setdefault("next_setup_id", 1)
    v.setdefault("next_test_id", 1)
    v.setdefault("active_setup", None)
    v.setdefault("breakout_watch", None)
    v.setdefault("history", [])
    v.setdefault("news", {"last_major_id": None, "last_major_time": 0})
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


def volume_ratio(rows=None):
    # Primary source: Kraken futures trade-volume analytics.
    # This avoids the PI_XBTUSD spot/index candle volume=0 issue.
    try:
        tv = trade_volume_rows()
        totals = [buy + sell for _, buy, sell in tv]
        if len(totals) >= VOL_LOOKBACK + 1:
            cur = totals[-1]
            prev = totals[-VOL_LOOKBACK-1:-1]
            avg = sum(prev) / len(prev) if prev else 0.0
            if avg > 0:
                return cur / avg
    except (requests.exceptions.RequestException, ValueError, RuntimeError) as e:
        print(f"trade-volume 暫時無法取得: {type(e).__name__}: {e}")

    # Fallback only if candle volume is actually populated.
    if rows and len(rows) >= VOL_LOOKBACK + 1:
        cur = float(rows[-1].get("volume", 0) or 0)
        prev = [float(x.get("volume", 0) or 0) for x in rows[-VOL_LOOKBACK-1:-1]]
        avg = sum(prev) / len(prev) if prev else 0.0
        if avg > 0:
            return cur / avg
    return 0.0


def flow_for_side(side, price_dir, oi_metrics, cvd_metrics):
    rp=float(oi_metrics.get("recent_pct",0) or 0)
    oi_dir="FLAT" if abs(rp)<OI_FLAT_V6 else "UP" if rp>0 else "DOWN"
    cd=float(cvd_metrics.get("recent_delta",0) or 0); span=abs(float(cvd_metrics.get("broad_span",0) or 0))
    rel=abs(cd)/span if span else 0.0
    cvd_dir="FLAT" if rel<0.10 else "UP" if cd>0 else "DOWN"
    cvd_strong=rel>=0.50
    wanted="UP" if side=="LONG" else "DOWN"
    cvd_support=cvd_dir==wanted
    cvd_oppose=cvd_dir not in ("FLAT",wanted) and cvd_strong
    # OI rising supports whichever way price is actually moving; falling OI is weak/non-confirming, not directional support.
    oi_support=(oi_dir=="UP" and price_dir==wanted)
    oi_oppose=(oi_dir=="UP" and price_dir not in ("FLAT",wanted))
    valid=(cvd_support or oi_support) and not cvd_oppose and not oi_oppose
    strong_both=cvd_support and oi_support
    return {"valid":valid,"strong_both":strong_both,"cvd_dir":cvd_dir,"cvd_strong":cvd_strong,"oi_dir":oi_dir,"oi_recent_pct":rp,"cvd_recent_delta":cd,"cvd_rel":rel}


def v6_quality_score(side, ctx, vol, flow, ext):
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
    if ctx["zone_distance_atr"]>NO_CHASE_ATR: return False,"離 EMA Zone > 1 ATR"
    if ext>NO_CHASE_ATR: return False,"突破延伸 > 1 ATR"
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


def news_status_v6(state):
    now=int(time.time()); major=[]; any_titles=False
    words=["sec","fed","fomc","rate","hack","exploit","etf","lawsuit","ban","regulation","approval"]
    for url in FEEDS:
        try:
            feed=feedparser.parse(url)
            for x in feed.entries[:10]:
                title=x.get("title",""); any_titles |= bool(title)
                if any(w in title.lower() for w in words): major.append(title.strip())
        except Exception: pass
    ns=state["v6"]["news"]
    if major:
        major_id=hashlib.sha256("|".join(sorted(major)).encode("utf-8")).hexdigest()
        if major_id!=ns.get("last_major_id"):
            ns["last_major_id"]=major_id; ns["last_major_time"]=now
            return "🔴 高風險", True, major[0]
    age=now-int(ns.get("last_major_time",0) or 0)
    if ns.get("last_major_time") and age<=NEWS_RED_SECONDS: return "🔴 高風險",False,None
    if ns.get("last_major_time") and age<=NEWS_YELLOW_SECONDS: return "🟡 注意",False,None
    return ("⚪ 正常" if any_titles else "⚪ 暫無新聞資料"),False,None


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


def update_all_forward(state, rows15):
    latest=int(rows15[-1]["time"]); keep=[]
    for test in state.get("forward_tests",[]):
        entry_t=int(test["entry_time"]); entry=float(test["entry_price"]); side=test["side"]
        test.setdefault("results",{}); test.setdefault("max_high",entry); test.setdefault("min_low",entry)
        for r in rows15:
            t=int(r["time"])
            if entry_t<t<=latest:
                test["max_high"]=max(float(test["max_high"]),float(r["high"])); test["min_low"]=min(float(test["min_low"]),float(r["low"]))
                if str(test.get("version","" )).startswith("V6"):
                    for ev in hit_levels(test,r):
                        key=ev+"_notified"
                        if not test.get(key) and test.get("notify_setup"):
                            icon={"TP1":"🎯","TP2":"🏁","SL":"❌","AMBIGUOUS":"⚠️"}[ev]
                            label={"TP1":"TP1 達成","TP2":"TP2 達成","SL":"SL 觸發","AMBIGUOUS":"TP/SL 同根K・順序不明"}[ev]
                            send_discord(f"{icon} 訊號 #{test.get('setup_id','?')}｜BTC {'做多' if side=='LONG' else '做空'}｜{label}")
                            test[key]=True
                        if ev in ("TP2","SL","AMBIGUOUS"):
                            a=state["v6"].get("active_setup")
                            if a and a.get("test_id")==test.get("id"): state["v6"]["active_setup"]=None
        for label,secs in FORWARD_HORIZONS.items():
            if label in test["results"] or latest<entry_t+secs: continue
            row=next((r for r in rows15 if int(r["time"])>=entry_t+secs),None)
            if row:
                raw=float(row["close"])/entry-1; signed=raw if side=="LONG" else -raw
                test["results"][label]={"price":float(row["close"]),"return":signed,"correct":signed>0}
        if "2h" in test["results"]:
            mfe=(float(test["max_high"])/entry-1) if side=="LONG" else (entry/float(test["min_low"])-1)
            mae=(entry/float(test["min_low"])-1) if side=="LONG" else (float(test["max_high"])/entry-1)
            test["mfe"]=max(0,mfe); test["mae"]=max(0,mae)
            if str(test.get("version","")).startswith("V6"):
                if not any(x.get("id")==test.get("id") for x in state["v6"]["history"]): state["v6"]["history"].append(test)
            elif str(test.get("version","")).startswith("V5"):
                # Preserve old V5 data; do not reset or convert it to V6.
                if not any(x.get("id")==test.get("id") for x in state.get("forward_history_v5",[])):
                    f=test.get("features",{}); om=f.get("oi_metrics",{}); cm=f.get("cvd_metrics",{})
                    state["forward_history_v5"].append({"version":test.get("version"),"id":test.get("id"),"setup_id":test.get("setup_id"),"side":side,"score":test.get("score"),"entry_time":entry_t,"entry_price":entry,"tp1":test.get("tp1"),"tp2":test.get("tp2"),"sl":test.get("sl"),"risk_pct":test.get("risk_pct"),"ret_15m":test["results"].get("15m",{}).get("return"),"ret_30m":test["results"].get("30m",{}).get("return"),"ret_1h":test["results"].get("1h",{}).get("return"),"ret_2h":test["results"].get("2h",{}).get("return"),"mfe":max(0,mfe),"mae":max(0,mae),"first_outcome":test.get("first_outcome"),"outcome_time":test.get("outcome_time"),"ema_1h":f.get("ema_1h"),"ema_15m":f.get("ema_15m"),"zone_ok":f.get("zone_ok"),"zone_distance_atr":f.get("zone_distance_atr"),"ema34_slope_atr":f.get("ema34_slope_atr"),"ema50_slope_atr":f.get("ema50_slope_atr"),"ema_gap_atr":f.get("ema_gap_atr"),"price_dir":f.get("price_dir"),"oi":f.get("oi"),"oi_broad_pct":om.get("broad_pct"),"oi_recent_pct":om.get("recent_pct"),"cvd":f.get("cvd"),"cvd_broad_delta":cm.get("broad_delta"),"cvd_recent_delta":cm.get("recent_delta"),"atr_pct":f.get("atr_pct"),"news":f.get("news")})
        else: keep.append(test)
    state["forward_tests"]=keep


def export_data(state):
    # V5 export remains intact and separate.
    with open(V5_JSON,"w",encoding="utf-8") as f: json.dump({"version":"V5_PRESERVED","completed":state.get("forward_history_v5",[]),"pending":[x for x in state.get("forward_tests",[]) if str(x.get("version","")).startswith("V5")]},f,ensure_ascii=False,indent=2)
    with open(V6_JSON,"w",encoding="utf-8") as f: json.dump({"version":VERSION,"completed":state["v6"]["history"],"pending":[x for x in state.get("forward_tests",[]) if str(x.get("version","")).startswith("V6")]},f,ensure_ascii=False,indent=2)
    fields=["version","id","setup_id","trigger_type","side","quality_score","entry_time","entry_price","tp1","tp2","sl","risk_atr","swing_level","breakout_distance_atr","volume_ratio","ema_1h","ema_15m","zone_distance_atr","cvd_dir","oi_dir","oi_recent_pct","cvd_recent_delta","news","ret_15m","ret_30m","ret_1h","ret_2h","mfe","mae","terminal_outcome"]
    with open(V6_CSV,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for x in state["v6"]["history"]:
            feat=x.get("features",{}); row={k:x.get(k,feat.get(k,"")) for k in fields}
            for h in FORWARD_HORIZONS: row["ret_"+h]=x.get("results",{}).get(h,{}).get("return")
            w.writerow(row)


def zh_side(side): return "做多" if side=="LONG" else "做空"
def em(v): return {"BULL":"🟢 多頭","BEAR":"🔴 空頭","NEUTRAL":"⚪ 中性"}.get(v,"⚪ 中性")
def fd(v): return {"UP":"🔺 上升","DOWN":"🔻 下降","FLAT":"⚪ 持平"}.get(v,"⚪ 持平")


def create_signal(state, side, trigger_type, level, ext, ctx, flow, vol, risk, close, closed_time, news):
    v=state["v6"]; sid=v["next_setup_id"]; v["next_setup_id"]+=1; tid=v["next_test_id"]; v["next_test_id"]+=1
    q=v6_quality_score(side,ctx,vol,flow,ext)
    test={"version":VERSION,"id":tid,"setup_id":sid,"trigger_type":trigger_type,"side":side,"quality_score":q,"score":q,"entry_time":closed_time,"entry_price":close,"tp1":risk["tp1"],"tp2":risk["tp2"],"sl":risk["sl"],"risk_atr":risk["risk_atr"],"risk_pct":risk["risk"]/close,"swing_level":level,"defense_level":level,"breakout_distance_atr":ext,"volume_ratio":vol,"results":{},"max_high":close,"min_low":close,"notify_setup":True,"features":{"ema_1h":ctx["ema_1h"],"ema_15m":ctx["ema_15m"],"zone_distance_atr":ctx["zone_distance_atr"],"ema34_slope_atr":ctx["s34"],"ema50_slope_atr":ctx["s50"],"cvd_dir":flow["cvd_dir"],"oi_dir":flow["oi_dir"],"oi_recent_pct":flow["oi_recent_pct"],"cvd_recent_delta":flow["cvd_recent_delta"],"volume_ratio":vol,"breakout_distance_atr":ext,"news":news}}
    state["forward_tests"].append(test)
    v["active_setup"]={"id":sid,"test_id":tid,"side":side,"trigger_type":trigger_type,"defense_level":level,"enhanced":False,"start_time":closed_time}
    warning=""
    wanted="BULL" if side=="LONG" else "BEAR"
    if ctx["ema_1h"]!=wanted: warning="\n⚠️ 逆 1H 趨勢・偏激進"
    typ="BREAKOUT 突破" if trigger_type=="BREAKOUT" else "RETEST 回踩"
    send_discord(f"⚡ 新訊號 #{sid}｜BTC {zh_side(side)}\n\n💰 進場 ${close:,.0f}\n🎯 TP1 ${risk['tp1']:,.0f}｜TP2 ${risk['tp2']:,.0f}\n🛑 SL ${risk['sl']:,.0f}\n⚖️ 風險距離 {risk['risk_atr']:.2f} ATR\n\n📍 {typ}\n1H：{em(ctx['ema_1h'])}\n15M：{em(ctx['ema_15m'])}\nVolume：{'🔥 ' if vol>=VOL_STRONG else ''}{vol:.2f}×\nCVD：{fd(flow['cvd_dir'])}\nOI：{fd(flow['oi_dir'])}\n品質 Score：{q}/8{warning}\n📰 {news}")


def main():
    print("=== BTC Market Radar V6.1 FLOW FIX ===")
    state=load_state(); rows15=closed_rows("15m",240); rows1h=closed_rows("1h",120)
    if len(rows15)<80 or len(rows1h)<60: raise RuntimeError("K線資料不足")
    closed=rows15[-1]; prev=rows15[-2]; ct=int(closed["time"]); close=float(closed["close"]); prev_close=float(prev["close"])
    a=atr(rows15,14)
    oi,om=direction_details("open-interest"); cvd,cm=direction_details("cvd")
    update_all_forward(state,rows15)
    ctx=ema_context(rows15,rows1h,a); vol=volume_ratio(rows15); highs,lows=pivots(rows15)
    news,new_major,headline=news_status_v6(state)
    if new_major and not MANUAL_RUN: send_discord(f"🔴 BTC 市場風險提醒\n\n📰 偵測到新的高影響事件\n{headline[:140]}\n⚠️ 短線波動風險提高\nℹ️ 不直接改變交易方向")

    price_dir="UP" if close>prev_close else "DOWN" if close<prev_close else "FLAT"
    long_flow=flow_for_side("LONG",price_dir,om,cm); short_flow=flow_for_side("SHORT",price_dir,om,cm)
    v=state["v6"]

    if MANUAL_RUN:
        active=v.get("active_setup"); at=f"#{active['id']} {zh_side(active['side'])}" if active else "無"
        sh=highs[-1][1] if highs else None; sl=lows[-1][1] if lows else None
        send_discord(f"📊 BTC V6 市場現況｜手動查詢\n\n💰 BTC：${close:,.0f}\n1H：{em(ctx['ema_1h'])}\n15M：{em(ctx['ema_15m'])}\nEMA Zone 距離：{ctx['zone_distance_atr']:.2f} ATR\nVolume：{vol:.2f}×\nOI：{fd(om.get('recent_dir','FLAT'))} ({float(om.get('recent_pct',0) or 0):+.2%})\nCVD：{fd(cm.get('recent_dir','FLAT'))}\nATR：{a/close:.2%}\n\n最近 Swing High：${sh:,.0f}\n最近 Swing Low：${sl:,.0f}\n📡 目前訊號：{at}\n🧪 V6 實測完成：{len(v['history'])}｜進行中：{sum(str(x.get('version','')).startswith('V6') for x in state['forward_tests'])}\n📰 {news}\n\nℹ️ 手動查詢不建立新實測樣本")
        export_data(state); save_state(state); return

    if v.get("last_processed_candle")!=ct:
        active=v.get("active_setup")
        # Structural invalidation and meaningful enhancement only.
        if active:
            invalid=(close<float(active["defense_level"])) if active["side"]=="LONG" else (close>float(active["defense_level"]))
            if invalid:
                send_discord(f"⚪ 訊號 #{active['id']} 失效｜BTC {zh_side(active['side'])}\n\n📍 15M 收盤破壞原結構\nℹ️ 結構失效，不代表 SL 已觸發")
                v["active_setup"]=None; active=None
            elif not active.get("enhanced"):
                side=active["side"]; fl=long_flow if side=="LONG" else short_flow; wanted="BULL" if side=="LONG" else "BEAR"
                evidence=sum([ctx["ema_1h"]==wanted,ctx["ema_15m"]==wanted,fl["strong_both"],vol>=VOL_STRONG])
                if evidence>=2:
                    send_discord(f"🔥 訊號 #{active['id']} 增強｜BTC {zh_side(side)}\n\n📍 原結構持續守住\nVolume：{vol:.2f}×\nCVD：{fd(fl['cvd_dir'])}\nOI：{fd(fl['oi_dir'])}\n1H：{em(ctx['ema_1h'])}")
                    active["enhanced"]=True

        # Detect fresh 2-2 swing breakout. Require previous close not already beyond the same level.
        last_high=highs[-1] if highs else None; last_low=lows[-1] if lows else None
        candidates=[]
        if last_high:
            level=last_high[1]; ext=(close-level)/a
            if prev_close<=level and close>=level+BREAK_ATR*a: candidates.append(("LONG",level,ext,long_flow))
        if last_low:
            level=last_low[1]; ext=(level-close)/a
            if prev_close>=level and close<=level-BREAK_ATR*a: candidates.append(("SHORT",level,ext,short_flow))

        for side,level,ext,fl in candidates:
            ok,reason=eligibility(side,close,ctx,a,vol,fl,ext); risk=structure_risk(side,close,a,highs,lows)
            # Arm first retest even when direct breakout is too extended / otherwise not tradable.
            v["breakout_watch"]={"side":side,"level":level,"break_time":ct,"bars":0,"used":False}
            current=v.get("active_setup")
            reversal=current and current["side"]!=side
            if ok and risk:
                if reversal:
                    send_discord(f"🔄 市場結構反轉｜BTC {zh_side(current['side'])} → {zh_side(side)}")
                    v["active_setup"]=None
                if v.get("active_setup") is None: create_signal(state,side,"BREAKOUT",level,ext,ctx,fl,vol,risk,close,ct,news)
            else: print(f"V6 breakout armed {side}, no direct alert: {reason if not ok else 'risk/RR not suitable'}")

        # First valid retest only, starting after the breakout candle.
        watch=v.get("breakout_watch")
        if watch and ct>int(watch["break_time"]) and not watch.get("used"):
            watch["bars"]=int(watch.get("bars",0))+1; side=watch["side"]; level=float(watch["level"])
            touched=(float(closed["low"])<=level+RETEST_TOL_ATR*a and close>=level) if side=="LONG" else (float(closed["high"])>=level-RETEST_TOL_ATR*a and close<=level)
            if touched:
                fl=long_flow if side=="LONG" else short_flow; ext=abs(close-level)/a
                ok,reason=eligibility(side,close,ctx,a,vol,fl,ext); risk=structure_risk(side,close,a,highs,lows)
                watch["used"]=True
                if ok and risk and v.get("active_setup") is None: create_signal(state,side,"RETEST",level,ext,ctx,fl,vol,risk,close,ct,news)
                else: print(f"V6 retest consumed, no alert: {reason if not ok else 'risk/RR or active setup'}")
            if watch["bars"]>=RETEST_MAX_BARS: v["breakout_watch"]=None
        v["last_processed_candle"]=ct

    export_data(state); save_state(state)
    print(f"Radar V6: BTC={close:.0f} 1H={ctx['ema_1h']} 15M={ctx['ema_15m']} Vol={vol:.2f}x V6 completed={len(v['history'])}")


if __name__ == "__main__": main()
