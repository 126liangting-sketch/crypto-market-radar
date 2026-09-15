import os
import json
import time
import requests
import feedparser

BASE = "https://futures.kraken.com/api/charts/v1"
SYMBOL = "PI_XBTUSD"
WEBHOOK = os.getenv("DISCORD_WEBHOOK")
STATE = "probability_state.json"

MIN_SAMPLES = 100
PREDICT_SECONDS = 3600
MOVE = 0.01
THRESHOLD = 0.65
HIGH = 0.75
STRONG = 0.85

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]


def get(url, params=None):
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def candles(resolution, count=200):
    sec = {"15m": 900, "1h": 3600}[resolution]
    now = int(time.time())
    data = get(
        f"{BASE}/spot/{SYMBOL}/{resolution}",
        {"from": now - count * sec, "to": now, "count": count},
    )
    rows = sorted(data.get("candles", []), key=lambda x: int(x["time"]))
    if not rows:
        raise RuntimeError(f"Kraken {resolution} K線沒有資料")
    return rows


def closed_15m_candle():
    rows = candles("15m", 120)
    # 最後一根通常仍在形成中，因此使用倒數第二根作為已收盤K線。
    return rows[-2]


def ema(values, length):
    if len(values) < length:
        return None
    k = 2 / (length + 1)
    value = sum(values[:length]) / length
    for x in values[length:]:
        value = x * k + value * (1 - k)
    return value


def ema_state(resolution):
    rows = candles(resolution, 100)
    closes = [float(x["close"]) for x in rows]
    fast = ema(closes, 34)
    slow = ema(closes, 50)
    if fast is None or slow is None:
        return "NEUTRAL"
    if fast > slow:
        return "BULL"
    if fast < slow:
        return "BEAR"
    return "NEUTRAL"


def _number(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def analytics(kind):
    now = int(time.time())
    data = get(
        f"{BASE}/analytics/{SYMBOL}/{kind}",
        {"since": now - 7200, "to": now, "interval": 900},
    )
    result = data.get("result", data)
    if not isinstance(result, dict):
        return []

    timestamps = result.get("timestamp", [])
    raw = result.get("data", [])
    key = "openInterest" if kind == "open-interest" else "cvd"

    if isinstance(raw, dict):
        values = raw.get(key, [])
    else:
        values = raw
    if not values:
        values = result.get(key, [])

    out = []
    for ts, value in zip(timestamps, values):
        try:
            if isinstance(value, dict):
                value = value.get(key, value.get("value"))
            elif isinstance(value, list):
                nums = [float(x) for x in value if _number(x)]
                value = nums[-1] if nums else None
            value = float(value)
            out.append((int(ts), value))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def direction(kind):
    rows = analytics(kind)
    if len(rows) < 2:
        return "FLAT"
    previous = rows[-2][1]
    current = rows[-1][1]
    if current > previous:
        return "UP"
    if current < previous:
        return "DOWN"
    return "FLAT"


def load_state():
    if not os.path.exists(STATE):
        return {
            "states": {},
            "pending": [],
            "last_closed_candle": None,
            "last_signal_key": None,
            "signal_armed": True,
        }
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def stats_for(state, key):
    return state["states"].setdefault(
        key, {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}
    )


def finish_pending(state, latest_closed_time, latest_closed_price):
    """Only finalize a sample after its full 1-hour observation window has closed."""
    keep = []
    for sample in state["pending"]:
        target_time = int(sample["target_time"])
        if latest_closed_time < target_time:
            keep.append(sample)
            continue

        change = latest_closed_price / sample["price"] - 1
        if change >= MOVE:
            result = "LONG"
        elif change <= -MOVE:
            result = "SHORT"
        else:
            result = "NEUTRAL"

        stats_for(state, sample["state"])[result] += 1

    state["pending"] = keep


def news_status():
    titles = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            titles.extend(x.get("title", "") for x in feed.entries[:10])
        except Exception:
            pass

    if not titles:
        return "⚪ 無重大消息"

    high_impact = [
        "sec", "fed", "fomc", "rate", "hack", "etf",
        "lawsuit", "ban", "regulation", "approval",
    ]
    joined = " ".join(titles).lower()
    if any(word in joined for word in high_impact):
        return "🔴 高影響消息"
    return "🟡 有新消息"


def send_discord(message):
    if not WEBHOOK:
        print("缺少 DISCORD_WEBHOOK")
        return

    r = requests.post(
        WEBHOOK,
        json={"content": message},
        timeout=20
    )

    print("Discord HTTP:", r.status_code)
    r.raise_for_status()


def emoji_ema(value):
    return {"BULL": "🟢 多頭", "BEAR": "🔴 空頭", "NEUTRAL": "⚪ 中性"}.get(
        value, "⚪ 中性"
    )


def emoji_direction(value):
    return {"UP": "🔺 上升", "DOWN": "🔻 下降", "FLAT": "⚪ 持平"}.get(
        value, "⚪ 持平"
    )


def main():
    state = load_state()

    closed = closed_15m_candle()
    closed_time = int(closed["time"])
    closed_price = float(closed["close"])

    ema_1h = ema_state("1h")
    ema_15m = ema_state("15m")
    oi = direction("open-interest")
    cvd = direction("cvd")
    market_state = f"{ema_1h}|{ema_15m}|{oi}|{cvd}"

    finish_pending(state, closed_time, closed_price)

    if state["last_closed_candle"] != closed_time:
        state["pending"].append(
            {
                "candle_time": closed_time,
                "target_time": closed_time + PREDICT_SECONDS,
                "price": closed_price,
                "state": market_state,
            }
        )
        state["last_closed_candle"] = closed_time

    current_stats = stats_for(state, market_state)
    total = sum(current_stats.values())

    if total >= MIN_SAMPLES:
        probabilities = {
            name: current_stats[name] / total
            for name in ("LONG", "SHORT", "NEUTRAL")
        }

        best = max(probabilities, key=probabilities.get)
        probability = probabilities[best]

        if best == "NEUTRAL" or probability < THRESHOLD:
            state["signal_armed"] = True

        elif state["signal_armed"]:
            signal_key = f"{market_state}|{best}"

            if signal_key != state["last_signal_key"]:
                icon = (
                    "🚨"
                    if probability >= STRONG
                    else "🔥"
                    if probability >= HIGH
                    else "🟢"
                )

                action = "做多" if best == "LONG" else "做空"

                message = (
                    "🚨 BTC 訊號\n\n"
                    f"{icon} {action}：{probability:.1%}\n\n"
                    f"1H：{emoji_ema(ema_1h)}\n"
                    f"15M：{emoji_ema(ema_15m)}\n"
                    f"OI：{emoji_direction(oi)}\n"
                    f"CVD：{emoji_direction(cvd)}\n\n"
                    f"📊 歷史樣本：{total}\n"
                    f"📰 消息：{news_status()}"
                )

                send_discord(message)

                state["last_signal_key"] = signal_key
                state["signal_armed"] = False

    save_state(state)

    print(
        "Radar OK:",
        market_state,
        "Samples:",
        total,
        "Pending:",
        len(state["pending"]),
    )

if __name__ == "__main__":
    main()
