import os
import json
import time
import requests
import xml.etree.ElementTree as ET

# =========================
# 基本設定
# =========================

SYMBOL = "PI_XBTUSD"
BASE = "https://futures.kraken.com/api/charts/v1"

STATE_FILE = "probability_state.json"
WEBHOOK = os.getenv("DISCORD_WEBHOOK")

CANDLE_COUNT = 120

# 預測未來 1 小時
LOOKAHEAD_SEC = 3600

# 至少累積 100 個已完成樣本後才顯示正式機率
MIN_SAMPLES = 100

# 未來 1 小時
# 上漲 >= 1% = LONG
# 下跌 >= 1% = SHORT
# 其餘 = NEUTRAL
TARGET_MOVE = 0.01

RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]


# =========================
# API
# =========================

def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


# =========================
# K線
# =========================

def get_candles(resolution):
    url = f"{BASE}/trade/{SYMBOL}/{resolution}"

    data = get_json(
        url,
        {
            "count": CANDLE_COUNT
        }
    )

    rows = data.get("candles", [])

    candles = []

    for x in rows:
        candles.append({
            "time": int(x["time"]) / 1000,
            "open": float(x["open"]),
            "high": float(x["high"]),
            "low": float(x["low"]),
            "close": float(x["close"])
        })

    candles.sort(key=lambda x: x["time"])

    return candles


# =========================
# EMA
# =========================

def calculate_ema(values, length):

    k = 2 / (length + 1)

    ema = values[0]

    for value in values[1:]:
        ema = value * k + ema * (1 - k)

    return ema


# =========================
# OI / CVD
# =========================

def get_analytics(kind):

    url = f"{BASE}/analytics/{SYMBOL}/{kind}"

    now = int(time.time())

    data = get_json(
        url,
        {
            "since": now - 7200,
            "to": now,
            "interval": 900
        }
    )

    result = data.get("result", data)

    timestamps = result.get("timestamp", [])
    values = result.get("data", {})

    if kind == "open-interest":
        values = values.get("openInterest", [])

    elif kind == "cvd":
        values = values.get("cvd", [])

    result_list = []

    for timestamp, value in zip(timestamps, values):

        try:
            result_list.append(
                (
                    int(timestamp),
                    float(value)
                )
            )

        except:
            pass

    return result_list


def get_direction(kind):

    data = get_analytics(kind)

    if len(data) < 2:
        return "FLAT"

    previous = data[-2][1]
    current = data[-1][1]

    if current > previous:
        return "UP"

    if current < previous:
        return "DOWN"

    return "FLAT"


# =========================
# 市場狀態
# =========================

def get_market_state():

    candles_1h = get_candles("1h")
    candles_15m = get_candles("15m")

    if len(candles_1h) < 60:
        raise RuntimeError("1H K線不足")

    if len(candles_15m) < 60:
        raise RuntimeError("15M K線不足")

    # 不使用尚未收完的 K
    close_1h = [
        x["close"]
        for x in candles_1h[:-1]
    ]

    close_15m = [
        x["close"]
        for x in candles_15m[:-1]
    ]

    ema34_1h = calculate_ema(close_1h, 34)
    ema50_1h = calculate_ema(close_1h, 50)

    ema34_15m = calculate_ema(close_15m, 34)
    ema50_15m = calculate_ema(close_15m, 50)

    ema_1h = (
        "BULL"
        if ema34_1h > ema50_1h
        else "BEAR"
    )

    ema_15m = (
        "BULL"
        if ema34_15m > ema50_15m
        else "BEAR"
    )

    oi = get_direction("open-interest")
    cvd = get_direction("cvd")

    price = candles_15m[-1]["close"]

    # 市場狀態組合
    key = "|".join([
        ema_1h,
        ema_15m,
        oi,
        cvd
    ])

    return {
        "price": price,
        "ema1h": ema_1h,
        "ema15": ema_15m,
        "oi": oi,
        "cvd": cvd,
        "key": key
    }


# =========================
# 狀態保存
# =========================

def load_state():

    if not os.path.exists(STATE_FILE):

        return {
            "pending": [],
            "stats": {}
        }

    with open(
        STATE_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def save_state(state):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


# =========================
# 結算過去樣本
# =========================

def resolve_pending(
    state,
    current_price,
    now
):

    changed = False

    remaining = []

    for sample in state["pending"]:

        age = now - sample["time"]

        # 還沒滿 1 小時
        if age < LOOKAHEAD_SEC:

            remaining.append(sample)

            continue

        move = (
            current_price / sample["price"]
        ) - 1

        if move >= TARGET_MOVE:

            result = "LONG"

        elif move <= -TARGET_MOVE:

            result = "SHORT"

        else:

            result = "NEUTRAL"

        key = sample["key"]

        if key not in state["stats"]:

            state["stats"][key] = {
                "LONG": 0,
                "SHORT": 0,
                "NEUTRAL": 0
            }

        state["stats"][key][result] += 1

        changed = True

    state["pending"] = remaining

    return changed


# =========================
# 計算機率
# =========================

def calculate_probability(
    state,
    key
):

    stats = state["stats"].get(
        key,
        {
            "LONG": 0,
            "SHORT": 0,
            "NEUTRAL": 0
        }
    )

    total = sum(stats.values())

    if total == 0:

        return (
            stats,
            0,
            {
                "LONG": 0,
                "SHORT": 0,
                "NEUTRAL": 0
            }
        )

    probability = {
        "LONG": round(
            stats["LONG"] / total * 100,
            1
        ),

        "SHORT": round(
            stats["SHORT"] / total * 100,
            1
        ),

        "NEUTRAL": round(
            stats["NEUTRAL"] / total * 100,
            1
        )
    }

    return stats, total, probability


# =========================
# 消息面
# =========================

def get_news():

    high_keywords = [
        "hack",
        "exploit",
        "sec",
        "lawsuit",
        "war",
        "tariff",
        "emergency",
        "etf"
    ]

    medium_keywords = [
        "fed",
        "fomc",
        "cpi",
        "rate",
        "inflation",
        "regulation",
        "liquidation"
    ]

    titles = []

    for feed in RSS_FEEDS:

        try:

            r = requests.get(
                feed,
                timeout=10
            )

            r.raise_for_status()

            root = ET.fromstring(r.text)

            for item in root.findall(".//item")[:10]:

                title = item.findtext("title")

                if title:

                    titles.append(
                        title.strip()
                    )

        except:

            continue

    high = [
        title
        for title in titles
        if any(
            word in title.lower()
            for word in high_keywords
        )
    ]

    medium = [
        title
        for title in titles
        if any(
            word in title.lower()
            for word in medium_keywords
        )
    ]

    if high:

        return (
            "🔴 高影響消息",
            high[0]
        )

    if medium:

        return (
            "🟡 重要消息",
            medium[0]
        )

    return (
        "⚪ 無重大消息",
        ""
    )


# =========================
# Discord
# =========================

def send_discord(message):

    if not WEBHOOK:

        print(message)

        return

    requests.post(
        WEBHOOK,
        json={
            "content": message
        },
        timeout=15
    )


# =========================
# 主程式
# =========================

def main():

    now = int(time.time())

    state = load_state()

    market = get_market_state()

    # 結算一小時前的樣本
    changed = resolve_pending(
        state,
        market["price"],
        now
    )

    # 建立新的預測樣本
    state["pending"].append({

        "time": now,

        "price": market["price"],

        "key": market["key"]
    })

    stats, total, probability = (
        calculate_probability(
            state,
            market["key"]
        )
    )

    news, headline = get_news()

    # =========================
    # 顯示結果
    # =========================

    if total >= MIN_SAMPLES:

        if (
            probability["LONG"]
            >= probability["SHORT"]
            and probability["LONG"]
            >= probability["NEUTRAL"]
        ):

            bias = "🟢 LONG"

        elif (
            probability["SHORT"]
            >= probability["LONG"]
            and probability["SHORT"]
            >= probability["NEUTRAL"]
        ):

            bias = "🔴 SHORT"

        else:

            bias = "⚪ NEUTRAL"

    else:

        bias = (
            f"⏳ 資料累積中 "
            f"({total}/{MIN_SAMPLES})"
        )

    message = f"""
**BTC MARKET RADAR**

**{bias}**

🟢 LONG：{probability["LONG"]}%
🔴 SHORT：{probability["SHORT"]}%
⚪ NEUTRAL：{probability["NEUTRAL"]}%

1H EMA：{market["ema1h"]}
15M EMA：{market["ema15"]}

OI：{market["oi"]}
CVD：{market["cvd"]}

消息面：{news}
{headline}

━━━━━━━━━━━━
統計樣本：{total}
"""

    # 不要每5分鐘瘋狂洗 Discord
    # 只有完成新樣本時才通知
    if changed:

        send_discord(message)

    else:

        print(message)

    save_state(state)


if __name__ == "__main__":
    main()
