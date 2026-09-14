import os
import json
import time
import requests


# ============================================================
# SETTINGS
# ============================================================

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

API_BASE = "https://futures.kraken.com/api/charts/v1/spot/PI_XBTUSD"

STATE_FILE = "signal_state.json"

MIN_CANDLES = 80
LOOKBACK = 8


# ============================================================
# DISCORD
# ============================================================

def send_discord(message):

    response = requests.post(
        DISCORD_WEBHOOK,
        json={
            "content": message
        },
        timeout=20
    )

    print(f"Discord response status: {response.status_code}")

    if response.status_code not in [200, 204]:

        print(
            "Discord response:",
            response.text[:500]
        )

        raise Exception(
            f"Discord 發送失敗："
            f"{response.status_code}"
        )


# ============================================================
# SIGNAL STATE
# ============================================================

def load_state():

    if not os.path.exists(STATE_FILE):
        return {
            "last_signal": "NONE"
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return {
            "last_signal": "NONE"
        }


def save_state(signal):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            {
                "last_signal": signal
            },
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# GET KLINES
# ============================================================

def get_candles(resolution):

    print(f"\n取得 {resolution} K 線...")

    # --------------------------------------------------------
    # 每個 timeframe 要抓多少歷史時間
    # --------------------------------------------------------

    if resolution == "1h":

        # 抓 120 小時
        interval_seconds = 60 * 60
        required_candles = 80

    elif resolution == "15m":

        # 抓 30 小時
        interval_seconds = 15 * 60
        required_candles = 80

    else:

        raise Exception(
            f"不支援的 timeframe：{resolution}"
        )

    now = int(time.time())

    from_time = now - (
        interval_seconds * required_candles * 2
    )

    to_time = now

    # --------------------------------------------------------
    # Kraken Charts API
    # --------------------------------------------------------

    url = (
        f"{API_BASE}/{resolution}"
    )

    params = {
        "from": from_time,
        "to": to_time
    }

    print(f"API URL: {url}")
    print(f"From: {from_time}")
    print(f"To  : {to_time}")

    response = requests.get(
        url,
        params=params,
        timeout=20
    )

    print(
        f"API Status: "
        f"{response.status_code}"
    )

    response.raise_for_status()

    data = response.json()

    print(
        f"API Response Type: "
        f"{type(data).__name__}"
    )

    # --------------------------------------------------------
    # Kraken 有些版本會回傳 dict
    # --------------------------------------------------------

    if isinstance(data, dict):

        if "candles" in data:

            candles = data["candles"]

        elif "data" in data:

            candles = data["data"]

        else:

            print(
                "API Dictionary Keys:",
                list(data.keys())
            )

            raise Exception(
                "Kraken 回傳 Dictionary，"
                "但找不到 candles/data"
            )

    # --------------------------------------------------------
    # 有些 endpoint 會直接回傳 list
    # --------------------------------------------------------

    elif isinstance(data, list):

        candles = data

    else:

        raise Exception(
            f"未知 API 格式："
            f"{type(data).__name__}"
        )

    # --------------------------------------------------------
    # 顯示第一根 K 線，方便確認格式
    # --------------------------------------------------------

    if len(candles) > 0:

        print(
            "第一根 K 線：",
            candles[0]
        )

    print(
        f"{resolution} 原始 K 線數量："
        f"{len(candles)}"
    )

    # --------------------------------------------------------
    # 標準化 K 線格式
    # --------------------------------------------------------

    normalized = []

    for candle in candles:

        # --------------------------------------------
        # 格式 1：
        # {
        #   time,
        #   open,
        #   high,
        #   low,
        #   close,
        #   volume
        # }
        # --------------------------------------------

        if isinstance(candle, dict):

            try:

                normalized.append(
                    {
                        "time": float(
                            candle["time"]
                        ),

                        "open": float(
                            candle["open"]
                        ),

                        "high": float(
                            candle["high"]
                        ),

                        "low": float(
                            candle["low"]
                        ),

                        "close": float(
                            candle["close"]
                        ),

                        "volume": float(
                            candle.get(
                                "volume",
                                0
                            )
                        )
                    }
                )

            except KeyError:

                continue

        # --------------------------------------------
        # 格式 2：
        # [timestamp, open, high, low, close, volume]
        # --------------------------------------------

        elif isinstance(candle, list):

            if len(candle) < 5:
                continue

            try:

                normalized.append(
                    {
                        "time": float(
                            candle[0]
                        ),

                        "open": float(
                            candle[1]
                        ),

                        "high": float(
                            candle[2]
                        ),

                        "low": float(
                            candle[3]
                        ),

                        "close": float(
                            candle[4]
                        ),

                        "volume": (
                            float(candle[5])
                            if len(candle) > 5
                            else 0
                        )
                    }
                )

            except (
                TypeError,
                ValueError
            ):

                continue

    # --------------------------------------------------------
    # 時間排序
    # --------------------------------------------------------

    normalized.sort(
        key=lambda x: x["time"]
    )

    # --------------------------------------------------------
    # 去除重複
    # --------------------------------------------------------

    unique = {}

    for candle in normalized:

        unique[candle["time"]] = candle

    candles = list(
        unique.values()
    )

    candles.sort(
        key=lambda x: x["time"]
    )

    print(
        f"{resolution} 有效 K 線："
        f"{len(candles)} 根"
    )

    # --------------------------------------------------------
    # K 線數量檢查
    # --------------------------------------------------------

    if len(candles) < MIN_CANDLES:

        raise Exception(
            f"{resolution} K 線仍然不足："
            f"{len(candles)} 根"
        )

    return candles


# ============================================================
# EMA
# ============================================================

def calculate_ema(values, length):

    if len(values) < length:

        return None

    multiplier = 2 / (
        length + 1
    )

    ema = values[0]

    for price in values[1:]:

        ema = (
            price - ema
        ) * multiplier + ema

    return ema


# ============================================================
# GET ANALYSIS
# ============================================================

def get_analysis(candles):

    closes = [
        candle["close"]
        for candle in candles
    ]

    ema34 = calculate_ema(
        closes,
        34
    )

    ema50 = calculate_ema(
        closes,
        50
    )

    latest = candles[-1]

    return {

        "price": latest["close"],

        "high": latest["high"],

        "low": latest["low"],

        "ema34": ema34,

        "ema50": ema50,

        "candles": candles

    }


# ============================================================
# EMA PULLBACK
# ============================================================

def check_ema_pullback(
    analysis,
    direction
):

    candles = analysis["candles"]

    ema34 = analysis["ema34"]
    ema50 = analysis["ema50"]

    if ema34 is None or ema50 is None:

        return False

    zone_high = max(
        ema34,
        ema50
    )

    zone_low = min(
        ema34,
        ema50
    )

    recent = candles[-4:]

    for candle in recent:

        candle_high = candle["high"]
        candle_low = candle["low"]

        touched_zone = (
            candle_high >= zone_low
            and
            candle_low <= zone_high
        )

        if not touched_zone:
            continue

        if direction == "LONG":

            if candle["close"] >= zone_low:

                return True

        elif direction == "SHORT":

            if candle["close"] <= zone_high:

               return True

    return False

# ============================================================

# BREAKOUT

# ============================================================

def check_breakout(

    candles,

    direction

):

    if len(candles) < LOOKBACK + 2:

        return False

    previous = candles[

        -(LOOKBACK + 1):-1

    ]

    current = candles[-1]

    previous_high = max(

        candle["high"]

        for candle in previous

    )

    previous_low = min(

        candle["low"]

        for candle in previous

    )

    if direction == "LONG":

        return (

            current["close"]

            > previous_high

        )

    if direction == "SHORT":

        return (

            current["close"]

            < previous_low

        )

    return False

# ============================================================

# STRUCTURE

# ============================================================

def check_structure(

    candles,

    direction

):

    if len(candles) < 5:

        return False

    c1 = candles[-5]

    c2 = candles[-4]

    c3 = candles[-3]

    c4 = candles[-2]

    c5 = candles[-1]

    if direction == "LONG":

        higher_low = (

            c3["low"]

            > c1["low"]

        )

        higher_high = (

            c5["high"]

            > c3["high"]

        )

        return (

            higher_low

            and

            higher_high

        )

    if direction == "SHORT":

        lower_high = (

            c3["high"]

            < c1["high"]

        )

        lower_low = (

            c5["low"]

            < c3["low"]

        )

        return (

            lower_high

            and

            lower_low

        )

    return False

# ============================================================

# MAIN

# ============================================================

def main():

    print(

        "========================================"

    )

    print(

        "        BTC MARKET RADAR"

    )

    print(

        "========================================"

    )

    # ========================================================

    # 1H

    # ========================================================

    h1_candles = get_candles(

        "1h"

    )

    h1 = get_analysis(

        h1_candles

    )

    h1_bull = (

        h1["ema34"]

        >

        h1["ema50"]

    )

    h1_bear = (

        h1["ema34"]

        <

        h1["ema50"]

    )

    # ========================================================

    # 15M

    # ========================================================

    print()

    m15_candles = get_candles(

        "15m"

    )

    m15 = get_analysis(

        m15_candles

    )

    # ========================================================

    # CONDITIONS

    # ========================================================

    bullish_pullback = (

        check_ema_pullback(

            m15,

            "LONG"

        )

    )

    bearish_pullback = (

        check_ema_pullback(

            m15,

            "SHORT"

        )

    )

    bullish_breakout = (

        check_breakout(

            m15_candles,

            "LONG"

        )

    )

    bearish_breakout = (

        check_breakout(

            m15_candles,

            "SHORT"

        )

    )

    bullish_structure = (

        check_structure(

            m15_candles,

            "LONG"

        )

    )

    bearish_structure = (

        check_structure(

            m15_candles,

            "SHORT"

        )

    )

    # ========================================================

    # SCORE

    # ========================================================

    long_score = 0

    short_score = 0

    # 1H trend

    if h1_bull:

        long_score += 2

    if h1_bear:

        short_score += 2

    # EMA pullback

    if bullish_pullback:

        long_score += 1

    if bearish_pullback:

        short_score += 1

    # Breakout

    if bullish_breakout:

        long_score += 2

    if bearish_breakout:

        short_score += 2

    # Structure

    if bullish_structure:

        long_score += 1

    if bearish_structure:

        short_score += 1

    # ========================================================

    # TRIGGER

    # ========================================================

    long_trigger = (

        bullish_pullback

        or

        bullish_breakout

        or

        bullish_structure

    )

    short_trigger = (

        bearish_pullback

        or

        bearish_breakout

        or

        bearish_structure

    )

    # ========================================================

    # SIGNAL

    # ========================================================

    current_signal = "NONE"

    if (

        h1_bull

        and

        long_trigger

        and

        long_score >= 3

    ):

        current_signal = "LONG"

    elif (

        h1_bear

        and

        short_trigger

        and

        short_score >= 3

    ):

        current_signal = "SHORT"

    # ========================================================

    # DEBUG

    # ========================================================

    print()

    print(

        "========== MARKET DATA =========="

    )

    print(

        f"Price       : "

        f"{m15['price']:.2f}"

    )

    print(

        f"1H EMA34    : "

        f"{h1['ema34']:.2f}"

    )

    print(

        f"1H EMA50    : "

        f"{h1['ema50']:.2f}"

    )

    print(

        f"15M EMA34   : "

        f"{m15['ema34']:.2f}"

    )

    print(

        f"15M EMA50   : "

        f"{m15['ema50']:.2f}"

    )

    print()

    print(

        f"1H Trend    : "

        f"{'BULL' if h1_bull else 'BEAR'}"

    )

    print(

        f"LONG Score  : "

        f"{long_score}"

    )

    print(

        f"SHORT Score : "

        f"{short_score}"

    )

    print()

    print(

        "========== TRIGGER DEBUG =========="

    )

    print(

        f"Bullish Pullback : "

        f"{bullish_pullback}"

    )

    print(

        f"Bullish Breakout : "

        f"{bullish_breakout}"

    )

    print(

        f"Bullish Structure: "

        f"{bullish_structure}"

    )

    print(

        f"Long Trigger     : "

        f"{long_trigger}"

    )

    print()

    print(

        f"Bearish Pullback : "

        f"{bearish_pullback}"

    )

    print(

        f"Bearish Breakout : "

        f"{bearish_breakout}"

    )

    print(

        f"Bearish Structure: "

        f"{bearish_structure}"

    )

    print(

        f"Short Trigger    : "

        f"{short_trigger}"

    )

    print(

        "==================================="

    )

    print()

    print(

        f"目前訊號："

        f"{current_signal}"

    )

    # ========================================================

    # STATE

    # ========================================================

    state = load_state()

    previous_signal = state.get(

        "last_signal",

        "NONE"

    )

    print(

        f"上一個訊號："

        f"{previous_signal}"

    )

    # ========================================================

    # DISCORD

    # ========================================================

    if (

        current_signal != "NONE"

        and

        current_signal != previous_signal

    ):

        message = (

            "🚨 **BTC MARKET RADAR**\n\n"

            f"訊號：**{current_signal}**\n"

            f"價格：`{m15['price']:.2f}`\n\n"

            f"1H Trend："

            f"`{'BULL' if h1_bull else 'BEAR'}`\n"

            f"LONG Score："

            f"`{long_score}`\n"

            f"SHORT Score："

            f"`{short_score}`\n\n"

            f"EMA Pullback："

            f"`{'LONG' if bullish_pullback else 'SHORT' if bearish_pullback else 'NONE'}`\n"

            f"Breakout："

            f"`{'LONG' if bullish_breakout else 'SHORT' if bearish_breakout else 'NONE'}`\n"

            f"Structure："

            f"`{'LONG' if bullish_structure else 'SHORT' if bearish_structure else 'NONE'}`"

        )

        print()

        print(

            "發送 Discord 訊號..."

        )

        send_discord(

            message

        )

        print(

            "Discord 發送成功"

        )

    else:

        print(

            "沒有新訊號"

        )

    # ========================================================

    # SAVE STATE

    # ========================================================

    save_state(

        current_signal

    )

    print()

    print(

        "狀態已儲存"

    )

    print(

        "========================================"

    )

# ============================================================

# RUN

# ============================================================

if __name__ == "__main__":

    main()
