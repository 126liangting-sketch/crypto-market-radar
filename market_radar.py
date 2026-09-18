import os
import json
import time
import requests
import feedparser

BASE = "https://futures.kraken.com/api/charts/v1"
SYMBOL = "PI_XBTUSD"
WEBHOOK = os.getenv("DISCORD_WEBHOOK")
STATE = "probability_state.json"
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
OI_FLAT_PCT = 0.001               # +/-0.10% treated as flat
CVD_FLAT_REL = 0.02               # <=2% of recent CVD range treated as flat

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]


def get(url, params=None, retries=3):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=20,
                             headers={"User-Agent": "Crypto-Market-Radar/4.4"})
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


def direction(kind):
    """Classify OI/CVD from ~12 x 15m points (about 3h), with the latest
    ~4 points used as a reversal check. If the broad move and the recent move
    clearly disagree, return FLAT instead of forcing an UP/DOWN score.
    """
    try:
        rows = analytics(kind)
    except (requests.exceptions.RequestException, ValueError, RuntimeError) as e:
        print(f"{kind} 暫時無法取得: {type(e).__name__}: {e}")
        return "UNAVAILABLE"
    if len(rows) < 5:
        return "UNAVAILABLE"

    window = rows[-ANALYTICS_BARS:] if len(rows) >= ANALYTICS_BARS else rows
    recent = window[-ANALYTICS_CONFIRM_BARS:] if len(window) >= ANALYTICS_CONFIRM_BARS else window
    start_v, end_v = window[0][1], window[-1][1]
    recent_start, recent_end = recent[0][1], recent[-1][1]
    delta = end_v - start_v
    recent_delta = recent_end - recent_start

    if kind == "open-interest":
        if start_v == 0:
            broad = "FLAT" if delta == 0 else ("UP" if delta > 0 else "DOWN")
        else:
            pct = delta / abs(start_v)
            broad = "FLAT" if abs(pct) <= OI_FLAT_PCT else ("UP" if pct > 0 else "DOWN")
        if recent_start == 0:
            recent_dir = "FLAT" if recent_delta == 0 else ("UP" if recent_delta > 0 else "DOWN")
            recent_pct = 0.0
        else:
            recent_pct = recent_delta / abs(recent_start)
            recent_dir = "FLAT" if abs(recent_pct) <= OI_FLAT_PCT else ("UP" if recent_pct > 0 else "DOWN")
        pct = 0.0 if start_v == 0 else delta / abs(start_v)
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
        print(f"CVD ~3h/12 bars: {start_v:.6g} -> {end_v:.6g} (delta {delta:+.6g}); recent ~1h delta {recent_delta:+.6g}")

    # Broad trend is primary. A clear opposite move in the latest ~1h means
    # momentum is reversing, so withhold the point by returning FLAT.
    if broad in ("UP", "DOWN") and recent_dir in ("UP", "DOWN") and broad != recent_dir:
        print(f"{kind}: broad={broad}, recent={recent_dir} -> FLAT (recent reversal)")
        return "FLAT"
    return broad


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

    def calc(side):
        s, reasons = 0, []
        wanted = "BULL" if side == "LONG" else "BEAR"
        pdir = "UP" if side == "LONG" else "DOWN"
        if state1h == wanted: s += 2; reasons.append("1H +2")
        if state15 == wanted: s += 1; reasons.append("15M +1")
        # Pullback/position: candle touches EMA34/50 zone and closes on the trend side.
        zone_confirm = touches_zone and ((side == "LONG" and close >= zone_high) or (side == "SHORT" and close <= zone_low))
        if zone_confirm: s += 2; reasons.append("EMA Zone +2")
        # CVD confirms the latest 15m price direction.
        if price_dir == pdir and cvd == pdir: s += 1; reasons.append("Price+CVD +1")
        # Rising OI with directional price expansion = new positioning supports the move.
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
        "oi": oi, "cvd": cvd, "atr_pct": atr_pct, "zone_ok": zone_ok,
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


def update_forward_tests(state, rows15):
    by_time = {int(r["time"]): r for r in rows15}
    latest_time = int(rows15[-1]["time"])
    keep = []
    completed_msgs = []
    for test in state["forward_tests"]:
        entry_t, entry = int(test["entry_time"]), float(test["entry_price"])
        side, score = test["side"], int(test["score"])
        test.setdefault("results", {})
        test.setdefault("max_high", entry)
        test.setdefault("min_low", entry)
        # Update excursion using all closed candles after entry.
        for r in rows15:
            t = int(r["time"])
            if entry_t < t <= latest_time:
                test["max_high"] = max(test["max_high"], float(r["high"]))
                test["min_low"] = min(test["min_low"], float(r["low"]))
        bucket = forward_bucket(state, side, score)
        for label, seconds in FORWARD_HORIZONS.items():
            if label in test["results"] or latest_time < entry_t + seconds: continue
            # Closed 15m timestamps line up with all configured horizons.
            target_t = entry_t + seconds
            row = by_time.get(target_t)
            if row is None: continue
            px = float(row["close"])
            raw_ret = px / entry - 1
            signed = raw_ret if side == "LONG" else -raw_ret
            correct = signed > 0
            test["results"][label] = {"price": px, "return": signed, "correct": correct}
            st = bucket[label]
            st["total"] += 1
            st["correct"] += int(correct)
            st["sum_return"] += signed
        if "2h" in test["results"]:
            mfe = (test["max_high"] / entry - 1) if side == "LONG" else (entry / test["min_low"] - 1)
            mae = (entry / test["min_low"] - 1) if side == "LONG" else (test["max_high"] / entry - 1)
            bucket["completed_tests"] += 1
            bucket["sum_mfe"] += max(0.0, mfe)
            bucket["sum_mae"] += max(0.0, mae)
            bucket["target_0_5"] += int(mfe >= 0.005)
            bucket["target_1_0"] += int(mfe >= 0.01)
            completed_msgs.append((test, mfe, mae))
        else:
            keep.append(test)
    state["forward_tests"] = keep
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

    oi, cvd = direction("open-interest"), direction("cvd")
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
            state["forward_tests"].append({
                "id": test_id, "entry_time": closed_time, "entry_price": closed_price,
                "side": side, "score": score, "results": {},
                "max_high": closed_price, "min_low": closed_price,
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
                    f"{level} BTC {side} 訊號\n\n"
                    f"⚡ Score：{score} / 8\n"
                    f"📊 Forward實測：{confidence}\n"
                    f"💰 BTC：${closed_price:,.0f}\n\n"
                    f"1H：{emoji_ema(sig['ema_1h'])}\n"
                    f"15M：{emoji_ema(sig['ema_15m'])}\n"
                    f"EMA Zone：{'✅' if sig['zone_ok'] else '➖'}\n"
                    f"OI：{emoji_direction(oi)}\n"
                    f"CVD：{emoji_direction(cvd)}\n"
                    f"ATR：{sig['atr_pct']:.2%}\n\n"
                    f"🧪 Forward Test #{test_id}\n"
                    f"⏳ 15M / 30M / 1H / 2H 自動驗證\n"
                    f"📰 消息：{news_status()}"
                )
                send_discord(message)
        elif state["last_forward_candle"] != closed_time:
            # Mark candle processed even when no test is created; avoids duplicate work on 5m workflow runs.
            state["last_forward_candle"] = closed_time
        print(f"Radar V4.4: LONG={sig['long_score']}/8 SHORT={sig['short_score']}/8 chosen={side} {score}/8")
    else:
        print(f"Radar PARTIAL: OI={oi}, CVD={cvd}; 本期不建立 Score/Forward 樣本")

    save_state(state)
    print("Forward pending:", len(state["forward_tests"]), "Legacy pending:", len(state["pending"]))
    if completed:
        print("Completed forward tests:", ", ".join(str(x[0]["id"]) for x in completed))


if __name__ == "__main__":
    main()
