import os
import json
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

# =========================================================
# 基本設定
# =========================================================

BASE = "https://futures.kraken.com/api/charts/v1"
SYMBOL = "PI_XBTUSD"

WEBHOOK = os.getenv("DISCORD_WEBHOOK")

STATE_FILE = "probability_state.json"

# 至少累積多少「完成樣本」後才正式顯示機率
MIN_SAMPLES = 100

# 達到多少機率才發 Discord
SIGNAL_THRESHOLD = 0.65

# 高機率區間
HIGH_THRESHOLD = 0.75
STRONG_THRESHOLD = 0.85

# 預測時間：1 小時
PREDICT_SECONDS = 3600

# 每個樣本觀察的漲跌幅
MOVE_THRESHOLD = 0.01


# =========================================================
# HTTP
# =========================================================

def get_json(url, params=None):

    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Crypto-Market-Radar/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url):

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Crypto-Market-Radar/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read().decode("utf-8")


# =========================================================
# Kraken K線
# =========================================================

def get_candles(resolution, count=100):

    url = f"{BASE}/spot/{SYMBOL}/{resolution}"

    now = int(time.time())

    data = get_json(
        url,
        {
            "from": now - 60 * 60 * 24,
            "to": now,
            "count": count
        }
    )

    candles = data.get("candles", [])

    result = []

    for c in candles:

        try:

            result.append({
                "time": int(c["time"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume", 0))
            })

        except Exception:
            continue

    result.sort(key=lambda x: x["time"])

    return result


# =========================================================
# EMA
# =========================================================

def ema(values, length):

    if len(values) < length:
        return None

    multiplier = 2 / (length + 1)

    value = sum(values[:length]) / length

    for price in values[length:]:

        value = (
            price - value
        ) * multiplier + value

    return value


# =========================================================
# EMA 趨勢
# =========================================================

def get_ema_direction(candles):

    if len(candles) < 60:
        return "FLAT"

    # 排除尚未完成的 K 線
    closed = candles[:-1]

    closes = [
        x["close"]
        for x in closed
    ]

    ema34 = ema(closes, 34)
    ema50 = ema(closes, 50)

    if ema34 is None or ema50 is None:
        return "FLAT"

    if ema34 > ema50:
        return "BULL"

    if ema34 < ema50:
        return "BEAR"

    return "FLAT"


# =========================================================
# Kraken Analytics
# =========================================================

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

    if not isinstance(result, dict):
        return []

    timestamps = result.get("timestamp", [])
    raw_data = result.get("data", [])

    values = []

    # -----------------------------------------------------
    # data 是 dict
    # -----------------------------------------------------

    if isinstance(raw_data, dict):

        if kind == "open-interest":
            values = raw_data.get(
                "openInterest",
                []
            )

        elif kind == "cvd":
            values = raw_data.get(
                "cvd",
                []
            )

    # -----------------------------------------------------
    # data 本身就是 list
    # -----------------------------------------------------

    elif isinstance(raw_data, list):

        values = raw_data

    # -----------------------------------------------------
    # 有些 API 可能直接把數值放在 result 裡
    # -----------------------------------------------------

    if not values:

        if kind == "open-interest":
            values = result.get(
                "openInterest",
                []
            )

        elif kind == "cvd":
            values = result.get(
                "cvd",
                []
            )

    output = []

    # -----------------------------------------------------
    # timestamps + values 對齊
    # -----------------------------------------------------

    if timestamps and values:

        for timestamp, value in zip(
            timestamps,
            values
        ):

            try:

                # 如果 value 是 list
                if isinstance(value, list):

                    numeric = None

                    for x in reversed(value):

                        try:
                            numeric = float(x)
                            break
                        except Exception:
                            continue

                    if numeric is None:
                        continue

                    value = numeric

                # 如果 value 是 dict
                elif isinstance(value, dict):

                    if kind == "open-interest":

                        value = value.get(
                            "openInterest",
                            value.get("value")
                        )

                    else:

                        value = value.get(
                            "cvd",
                            value.get("value")
                        )

                    value = float(value)

                else:

                    value = float(value)

                output.append(
                    (
                        int(timestamp),
                        value
                    )
                )

            except Exception:
                continue

    output.sort(
        key=lambda x: x[0]
    )

    return output


# =========================================================
# OI / CVD 方向
# =========================================================

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


# =========================================================
# 市場狀態
# =========================================================

def get_market_state():

    candles_1h = get_candles(
        "1h",
        100
    )

    candles_15m = get_candles(
        "15m",
        100
    )

    if len(candles_1h) < 60:
        raise Exception("1H K線資料不足")

    if len(candles_15m) < 60:
        raise Exception("15M K線資料不足")

    ema_1h = get_ema_direction(
        candles_1h
    )

    ema_15m = get_ema_direction(
        candles_15m
    )

    oi = get_direction(
        "open-interest"
    )

    cvd = get_direction(
        "cvd"
    )

    # 最新已完成 15M K線
    closed_15m = candles_15m[:-1]

    latest = closed_15m[-1]

    sample_time = (
        latest["time"] + 900
    )

    price = latest["close"]

    state_key = (
        f"{ema_1h}|"
        f"{ema_15m}|"
        f"{oi}|"
        f"{cvd}"
    )

    return {

        "sample_time": sample_time,

        "price": price,

        "ema_1h": ema_1h,

        "ema_15m": ema_15m,

        "oi": oi,

        "cvd": cvd,

        "state_key": state_key
    }


# =========================================================
# State
# =========================================================

def load_state():

    if not os.path.exists(
        STATE_FILE
    ):

        return {

            "pending": [],

            "stats": {},

            "total_completed": 0,

            "last_signal_key": "",

            "signal_armed": True
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        state.setdefault(
            "pending",
            []
        )

        state.setdefault(
            "stats",
            {}
        )

        state.setdefault(
            "total_completed",
            0
        )

        state.setdefault(
            "last_signal_key",
            ""
        )

        state.setdefault(
            "signal_armed",
            True
        )

        return state

    except Exception:

        return {

            "pending": [],

            "stats": {},

            "total_completed": 0,

            "last_signal_key": "",

            "signal_armed": True
        }


def save_state(state):

    temp = STATE_FILE + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        temp,
        STATE_FILE
    )


# =========================================================
# 建立統計樣本
# =========================================================

def add_sample(state, market):

    sample_time = market[
        "sample_time"
    ]

    # 同一根 15M K線不能重複紀錄
    for item in state["pending"]:

        if item["sample_time"] == sample_time:
            return False

    state["pending"].append({

        "sample_time":
            sample_time,

        "price":
            market["price"],

        "state_key":

            market["state_key"],

        "ema_1h":

            market["ema_1h"],

        "ema_15m":

            market["ema_15m"],

        "oi":

            market["oi"],

        "cvd":

            market["cvd"]

    })

    return True

# =========================================================

# 完成 1 小時樣本

# =========================================================

def resolve_samples(

    state,

    current_price,

    current_time

):

    remaining = []

    changed = False

    for sample in state["pending"]:

        target_time = (

            sample["sample_time"]

            + PREDICT_SECONDS

        )

        # 還沒滿 1 小時

        if current_time < target_time:

            remaining.append(

                sample

            )

            continue

        start_price = sample[

            "price"

        ]

        change = (

            current_price - start_price

        ) / start_price

        # -------------------------------------------------

        # 結果

        # -------------------------------------------------

        if change >= MOVE_THRESHOLD:

            outcome = "LONG"

        elif change <= -MOVE_THRESHOLD:

            outcome = "SHORT"

        else:

            outcome = "NEUTRAL"

        key = sample[

            "state_key"

        ]

        if key not in state["stats"]:

            state["stats"][key] = {

                "LONG": 0,

                "SHORT": 0,

                "NEUTRAL": 0

            }

        state["stats"][key][

            outcome

        ] += 1

        state["total_completed"] += 1

        changed = True

    state["pending"] = remaining

    return changed

# =========================================================

# 計算目前狀態的機率

# =========================================================

def get_probability(

    state,

    state_key

):

    stats = state[

        "stats"

    ].get(

        state_key

    )

    if not stats:

        return None

    total = (

        stats["LONG"]

        + stats["SHORT"]

        + stats["NEUTRAL"]

    )

    if total <= 0:

        return None

    return {

        "LONG":

            stats["LONG"] / total,

        "SHORT":

            stats["SHORT"] / total,

        "NEUTRAL":

            stats["NEUTRAL"] / total,

        "samples":

            total

    }

# =========================================================

# RSS 消息

# =========================================================

RSS_FEEDS = [

    "https://www.coindesk.com/arc/outboundfeeds/rss/",

    "https://cointelegraph.com/rss"

]

HIGH_KEYWORDS = [

    "sec",

    "federal reserve",

    "fed",

    "interest rate",

    "rate cut",

    "rate hike",

    "bitcoin etf",

    "ethereum etf",

    "crypto regulation",

    "regulation",

    "lawsuit",

    "hack",

    "exploit",

    "bankruptcy",

    "ban",

    "approval"

]

def get_news():

    articles = []

    for feed_url in RSS_FEEDS:

        try:

            text = get_text(

                feed_url

            )

            root = ET.fromstring(

                text

            )

            for item in root.findall(

                ".//item"

            )[:10]:

                title = item.findtext(

                    "title",

                    ""

                )

                if title:

                    articles.append(

                        title.strip()

                    )

        except Exception:

            continue

    if not articles:

        return (

            "⚪ 無重大消息",

            ""

        )

    high = []

    for title in articles:

        lower = title.lower()

        if any(

            keyword in lower

            for keyword in HIGH_KEYWORDS

        ):

            high.append(

                title

            )

    if high:

        return (

            "🔴 高影響消息",

            high[0]

        )

    return (

        "🟡 有新消息",

        articles[0]

    )

# =========================================================

# Discord

# =========================================================

def send_discord(message):

    if not WEBHOOK:

        print(

            "沒有設定 DISCORD_WEBHOOK"

        )

        return

    payload = json.dumps({

        "content":

            message

    }).encode(

        "utf-8"

    )

    req = urllib.request.Request(

        WEBHOOK,

        data=payload,

        headers={

            "Content-Type":

                "application/json"

        },

        method="POST"

    )

    try:

        urllib.request.urlopen(

            req,

            timeout=20

        )

        print(

            "Discord 已發送"

        )

    except Exception as e:

        print(

            "Discord 發送失敗:",

            e

        )

# =========================================================

# 中文轉換

# =========================================================

def trend_text(value):

    if value == "BULL":

        return "🟢 多頭"

    if value == "BEAR":

        return "🔴 空頭"

    return "⚪ 持平"

def direction_text(value):

    if value == "UP":

        return "🔺 上升"

    if value == "DOWN":

        return "🔻 下降"

    return "⚪ 持平"

# =========================================================

# 建立 Discord 訊息

# =========================================================

def build_signal_message(

    market,

    probability,

    news_level,

    news_title

):

    long_p = probability[

        "LONG"

    ]

    short_p = probability[

        "SHORT"

    ]

    neutral_p = probability[

        "NEUTRAL"

    ]

    samples = probability[

        "samples"

    ]

    max_probability = max(

        long_p,

        short_p,

        neutral_p

    )

    if max_probability >= STRONG_THRESHOLD:

        level = "🚨 強訊號"

    elif max_probability >= HIGH_THRESHOLD:

        level = "🔥 高機率訊號"

    else:

        level = "🟢 一般訊號"

    if long_p == max_probability:

        direction = "做多"

        emoji = "🟢"

        probability_text = (

            f"{long_p:.1%}"

        )

    elif short_p == max_probability:

        direction = "做空"

        emoji = "🔴"

        probability_text = (

            f"{short_p:.1%}"

        )

    else:

        direction = "盤整"

        emoji = "⚪"

        probability_text = (

            f"{neutral_p:.1%}"

        )

    message = f"""

{level}

BTC 市場雷達

{emoji} {direction}機率：{probability_text}

━━━━━━━━━━━━

📊 趨勢

1H EMA：{trend_text(market["ema_1h"])}

15M EMA：{trend_text(market["ema_15m"])}

📈 市場數據

OI：{direction_text(market["oi"])}

CVD：{direction_text(market["cvd"])}

📰 消息面

{news_level}

"""

    if news_title:

        message += (

            f"\n{news_title}\n"

        )

    message += f"""

━━━━━━━━━━━━

📚 此狀態歷史樣本：{samples} 筆

📊 已完成統計：{market["total_completed"]} 筆

⚠️ 歷史統計機率，不代表未來必然發生。

"""

    return message.strip()

# =========================================================

# 主程式

# =========================================================

def main():

    print(

        "========== Crypto Market Radar =========="

    )

    state = load_state()

    market = get_market_state()

    now = int(time.time())

    # -----------------------------------------------------

    # 先取得目前價格

    # -----------------------------------------------------

    current_price = market[

        "price"

    ]

    # -----------------------------------------------------

    # 先結算已經滿 1 小時的樣本

    # -----------------------------------------------------

    changed = resolve_samples(

        state,

        current_price,

        now

    )

    # -----------------------------------------------------

    # 再建立新的 15M 樣本

    # -----------------------------------------------------

    added = add_sample(

        state,

        market

    )

    # -----------------------------------------------------

    # 取得目前市場狀態的統計機率

    # -----------------------------------------------------

    probability = get_probability(

        state,

        market["state_key"]

    )

    # -----------------------------------------------------

    # 如果還沒有完成 100 筆

    # -----------------------------------------------------

    if (

        state["total_completed"]

        < MIN_SAMPLES

    ):

        print(

            f"資料累積中："

            f"{state['total_completed']}"

            f"/{MIN_SAMPLES}"

        )

        save_state(

            state

        )

        return

    # -----------------------------------------------------

    # 沒有這個市場狀態的統計

    # -----------------------------------------------------

    if probability is None:

        print(

            "目前市場狀態沒有足夠歷史資料"

        )

        save_state(

            state

        )

        return

    long_p = probability[

        "LONG"

    ]

    short_p = probability[

        "SHORT"

    ]

    neutral_p = probability[

        "NEUTRAL"

    ]

    max_probability = max(

        long_p,

        short_p,

        neutral_p

    )

    # -----------------------------------------------------

    # 印出目前結果

    # -----------------------------------------------------

    print(

        f"目前狀態："

        f"{market['state_key']}"

    )

    print(

        f"做多：{long_p:.1%}"

    )

    print(

        f"做空：{short_p:.1%}"

    )

    print(

        f"盤整：{neutral_p:.1%}"

    )

    print(

        f"樣本：{probability['samples']}"

    )

    # -----------------------------------------------------

    # 低於 65%

    # -----------------------------------------------------

    if max_probability < SIGNAL_THRESHOLD:

        # 重新武裝

        state[

            "signal_armed"

        ] = True

        state[

            "last_signal_key"

        ] = ""

        save_state(

            state

        )

        print(

            "低於 65%，不發送訊號"

        )

        return

    # -----------------------------------------------------

    # 判斷目前方向

    # -------------------------------

if long_p == max_probability:

        direction = "LONG"

    elif short_p == max_probability:

        direction = "SHORT"

    else:

        direction = "NEUTRAL"

    # -----------------------------------------------------

    # 盤整不當成做單訊號

    # -----------------------------------------------------

    if direction == "NEUTRAL":

        state[

            "signal_armed"

        ] = True

        state[

            "last_signal_key"

        ] = ""

        save_state(

            state

        )

        print(

            "最高機率為盤整，不發送做單訊號"

        )

        return

    # -----------------------------------------------------

    # 建立訊號 ID

    #

    # 同一個市場狀態 + 同方向

    # 不重複通知

    # -----------------------------------------------------

    signal_key = (

        f"{market['state_key']}"

        f"|{direction}"

    )

    if (

        signal_key

        == state["last_signal_key"]

        and not state["signal_armed"]

    ):

        print(

            "相同訊號已經發送過，不重複通知"

        )

        save_state(

            state

        )

        return

    # -----------------------------------------------------

    # News

    # -----------------------------------------------------

    news_level, news_title = (

        get_news()

    )

    market[

        "total_completed"

    ] = state[

        "total_completed"

    ]

    # -----------------------------------------------------

    # 發送

    # -----------------------------------------------------

    message = build_signal_message(

        market,

        probability,

        news_level,

        news_title

    )

    send_discord(

        message

    )

    # -----------------------------------------------------

    # 鎖定訊號

    # -----------------------------------------------------

    state[

        "last_signal_key"

    ] = signal_key

    state[

        "signal_armed"

    ] = False

    save_state(

        state

    )

    print(

        "訊號已發送"

    )

# =========================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print(

            "程式錯誤：",

            repr(e)

        )

        raise
