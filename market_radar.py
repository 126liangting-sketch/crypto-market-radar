import os
import json
import time
import requests


# ============================================================
# 基本設定
# ============================================================

API_BASE = (
    "https://futures.kraken.com/"
    "api/charts/v1/spot/PI_XBTUSD"
)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

STATE_FILE = "signal_state.json"

MIN_CANDLES = 80

LOOKBACK = 8

SNR_LOOKBACK = 40

SNR_TOLERANCE = 0.003


# ============================================================
# 取得 K 線
# ============================================================

def get_candles(resolution):

    print(f"\n取得 {resolution} K 線...")

    if resolution == "1h":

        interval_seconds = 60 * 60
        required_candles = 80

    elif resolution == "15m":

        interval_seconds = 15 * 60
        required_candles = 80

    else:

        raise Exception(
            f"不支援的 timeframe：{resolution}"
        )

    now = int(time.time())

    from_time = now - (
        interval_seconds
        * required_candles
        * 2
    )

    to_time = now

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
    # 處理 Kraken 不同回傳格式
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

    elif isinstance(data, list):

        candles = data

    else:

        raise Exception(
            f"未知 API 格式："
            f"{type(data).__name__}"
        )

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
    # 統一格式
    # --------------------------------------------------------

    normalized = []

    for candle in candles:

        # Dictionary 格式

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

            except (
                KeyError,
                TypeError,
                ValueError
            ):

                continue

        # List 格式

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
    # 排序
    # --------------------------------------------------------

    normalized.sort(
        key=lambda x: x["time"]
    )

    # --------------------------------------------------------
    # 移除重複 K 線
    # --------------------------------------------------------

    unique = {}

    for candle in normalized:

        unique[
            candle["time"]
        ] = candle

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

    if len(candles) < MIN_CANDLES:

        raise Exception(
            f"{resolution} K 線不足："
            f"{len(candles)} 根"
        )

    return candles


# ============================================================
# EMA
# ============================================================

def calculate_ema(
    candles,
    period
):

    closes = [
        candle["close"]
        for candle in candles
    ]

    multiplier = (
        2 / (period + 1)
    )

    ema = closes[0]

    for price in closes[1:]:

        ema = (
            (price - ema)
            * multiplier
            + ema
        )

    return ema


# ============================================================
# EMA Pullback
# ============================================================

def check_ema_pullback(
    candles
):

    if len(candles) < 10:

        return False, False

    recent = candles[-5:]

    ema34 = calculate_ema(
        candles,
        34
    )

    ema50 = calculate_ema(
        candles,
        50
    )

    zone_high = max(
        ema34,
        ema50
    )

    zone_low = min(
        ema34,
        ema50
    )

    bullish = False
    bearish = False

    for candle in recent:

        candle_high = candle["high"]
        candle_low = candle["low"]
        candle_close = candle["close"]

        touched_zone = (
            candle_low
            <= zone_high
            and
            candle_high
            >= zone_low
        )

        if touched_zone:

            if candle_close > zone_high:

                bullish = True

            if candle_close < zone_low:

                bearish = True

    return bullish, bearish


# ============================================================
# Breakout
# ============================================================

def check_breakout(
    candles
):

    if len(candles) < LOOKBACK + 2:

        return False, False

    # 使用「前一根之前」的區間
    # 避免把目前 K 線自己算進最高/最低

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

    bullish_breakout = (
        current["close"]
        > previous_high
    )

    bearish_breakout = (
        current["close"]
        < previous_low
    )

    return (
        bullish_breakout,
        bearish_breakout
    )


# ============================================================
# Structure
# ============================================================

def check_structure(
    candles
):

    if len(candles) < 5:

        return False, False

    c1 = candles[-4]
    c2 = candles[-3]
    c3 = candles[-2]
    c4 = candles[-1]

    bullish = (
        c2["low"] > c1["low"]
        and
        c4["high"] > c3["high"]
    )

    bearish = (
        c2["high"] < c1["high"]
        and
        c4["low"] < c3["low"]
    )

    return bullish, bearish


# ============================================================
# SNR
# ============================================================

def calculate_snr(
    candles,
    lookback=SNR_LOOKBACK,
    tolerance=SNR_TOLERANCE
):

    if len(candles) < lookback + 2:

        return {
            "support": 0,
            "resistance": 0,
            "near_support": False,
            "near_resistance": False,
            "breakout_up": False,
            "breakout_down": False
        }

    # --------------------------------------------------------
    # 不使用目前 K 線找 SNR
    # --------------------------------------------------------

    history = candles[
        -(lookback + 1):-1
    ]

    current = candles[-1]

    current_price = current["close"]

    support = min(
        candle["low"]
        for candle in history
    )

    resistance = max(
        candle["high"]
        for candle in history
    )

    # --------------------------------------------------------
    # 距離
    # --------------------------------------------------------

    support_distance = (
        abs(
            current_price
            - support
        )
        / current_price
    )

    resistance_distance = (
        abs(
            current_price
            - resistance
        )
        / current_price
    )

    near_support = (
        support_distance
        <= tolerance
    )

    near_resistance = (
        resistance_distance
        <= tolerance
    )

    # --------------------------------------------------------
    # 突破
    # -----------------

breakout_up = (

        current_price

        > resistance

    )

    breakout_down = (

        current_price

        < support

    )

    return {

        "support": support,

        "resistance": resistance,

        "near_support": near_support,

        "near_resistance": near_resistance,

        "breakout_up": breakout_up,

        "breakout_down": breakout_down

    }

# ============================================================

# Discord

# ============================================================

def send_discord(

    message

):

    if not DISCORD_WEBHOOK:

        print(

            "找不到 DISCORD_WEBHOOK"

        )

        return False

    payload = {

        "content": message

    }

    response = requests.post(

        DISCORD_WEBHOOK,

        json=payload,

        timeout=20

    )

    print(

        "Discord Status:",

        response.status_code

    )

    if response.status_code in [

        200,

        204

    ]:

        print(

            "Discord 發送成功"

        )

        return True

    print(

        "Discord 發送失敗：",

        response.text

    )

    return False

# ============================================================

# State

# ============================================================

def load_state():

    if not os.path.exists(

        STATE_FILE

    ):

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

def save_state(

    signal

):

    state = {

        "last_signal": signal

    }

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

# ============================================================

# 主分析

# ============================================================

def main():

    print(

        "\n"

        "========================================"

    )

    print(

        "       CRYPTO MARKET RADAR"

    )

    print(

        "========================================"

    )

    # --------------------------------------------------------

    # 取得資料

    # --------------------------------------------------------

    candles_1h = get_candles(

        "1h"

    )

    candles_15m = get_candles(

        "15m"

    )

    # --------------------------------------------------------

    # 目前價格

    # --------------------------------------------------------

    current_price = (

        candles_15m[-1]["close"]

    )

    # --------------------------------------------------------

    # 1H EMA

    # --------------------------------------------------------

    ema34_1h = calculate_ema(

        candles_1h,

        34

    )

    ema50_1h = calculate_ema(

        candles_1h,

        50

    )

    h1_bull = (

        ema34_1h

        > ema50_1h

    )

    h1_bear = (

        ema34_1h

        < ema50_1h

    )

    # --------------------------------------------------------

    # 15M EMA

    # --------------------------------------------------------

    ema34_15m = calculate_ema(

        candles_15m,

        34

    )

    ema50_15m = calculate_ema(

        candles_15m,

        50

    )

    # --------------------------------------------------------

    # Pullback

    # --------------------------------------------------------

    bullish_pullback, bearish_pullback = (

        check_ema_pullback(

            candles_15m

        )

    )

    # --------------------------------------------------------

    # Breakout

    # --------------------------------------------------------

    bullish_breakout, bearish_breakout = (

        check_breakout(

            candles_15m

        )

    )

    # --------------------------------------------------------

    # Structure

    # --------------------------------------------------------

    bullish_structure, bearish_structure = (

        check_structure(

            candles_15m

        )

    )

    # --------------------------------------------------------

    # SNR

    # --------------------------------------------------------

    snr = calculate_snr(

        candles_15m

    )

    # ========================================================

    # SCORE

    # ========================================================

    long_score = 0

    short_score = 0

    # --------------------------------------------------------

    # 1H Trend

    # --------------------------------------------------------

    if h1_bull:

        long_score += 2

    if h1_bear:

        short_score += 2

    # --------------------------------------------------------

    # EMA Pullback

    # --------------------------------------------------------

    if bullish_pullback:

        long_score += 1

    if bearish_pullback:

        short_score += 1

    # --------------------------------------------------------

    # Breakout

    # --------------------------------------------------------

    if bullish_breakout:

        long_score += 2

    if bearish_breakout:

        short_score += 2

    # --------------------------------------------------------

    # Structure

    # --------------------------------------------------------

    if bullish_structure:

        long_score += 1

    if bearish_structure:

        short_score += 1

    # --------------------------------------------------------

    # SNR

    # --------------------------------------------------------

    if snr["near_support"]:

        long_score += 1

    if snr["near_resistance"]:

        short_score += 1

    if snr["breakout_up"]:

        long_score += 2

    if snr["breakout_down"]:

        short_score += 2

    # ========================================================

    # TRIGGER

    # ========================================================

    long_trigger = (

        bullish_pullback

        or

        bullish_breakout

        or

        bullish_structure

        or

        snr["near_support"]

        or

        snr["breakout_up"]

    )

    short_trigger = (

        bearish_pullback

        or

        bearish_breakout

        or

        bearish_structure

        or

        snr["near_resistance"]

        or

        snr["breakout_down"]

    )

    # --------------------------------------------------------

    # 最終訊號

    # --------------------------------------------------------

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

    print("\n")

    print(

        "========== MARKET DATA =========="

    )

    print(

        f"Price       : {current_price:.2f}"

    )

    print(

        f"1H EMA34    : {ema34_1h:.2f}"

    )

    print(

        f"1H EMA50    : {ema50_1h:.2f}"

    )

    print(

        f"15M EMA34   : {ema34_15m:.2f}"

    )

    print(

        f"15M EMA50   : {ema50_15m:.2f}"

    )

    print(

        "================================="

    )

    print("\n")

    # --------------------------------------------------------

    # Trend

    # --------------------------------------------------------

    print(

        "========== 1H TREND =========="

    )

    if h1_bull:

        print(

            "1H Trend : BULL"

        )

    elif h1_bear:

        print(

            "1H Trend : BEAR"

        )

    else:

        print(

            "1H Trend : NEUTRAL"

        )

    print(

        "=============================="

    )

    # --------------------------------------------------------

    # Trigger

    # --------------------------------------------------------

    print("\n")

    print(

        "========== TRIGGER DEBUG =========="

    )

    print(

        "Bullish Pullback :",

        bullish_pullback

    )

    print(

        "Bearish Pullback :",

        bearish_pullback

    )

    print(

        "Bullish Breakout :",

        bullish_breakout

    )

    print(

        "Bearish Breakout :",

        bearish_breakout

    )

    print(

        "Bullish Structure:",

        bullish_structure

    )

    print(

        "Bearish Structure:",

        bearish_structure

    )

    print(

        "Long Trigger     :",

        long_trigger

    )

    print(

        "Short Trigger    :",

        short_trigger

    )

    print(

        "==================================="

    )

    # --------------------------------------------------------

    # SNR

    # --------------------------------------------------------

    print("\n")

    print(

        "========== SNR =========="

    )

    print(

        f"Support    : "

        f"{snr['support']:.2f}"

    )

    print(

        f"Resistance : "

        f"{snr['resistance']:.2f}"

    )

    print(

        "Near Support    :",

        snr["near_support"]

    )

    print(

        "Near Resistance :",

        snr["near_resistance"]

    )

    print(

        "Breakout Up     :",

        snr["breakout_up"]

    )

    print(

        "Breakout Down   :",

        snr["breakout_down"]

    )

    print(

        "=========================="

    )

    # --------------------------------------------------------

    # Score

    # --------------------------------------------------------

    print("\n")

    print(

        "========== SCORE =========="

    )

    print(

        "LONG Score :",

        long_score

    )

  print(

        "SHORT Score:",

        short_score

    )

    print(

        "Signal     :",

        current_signal

    )

    print(

        "==========================="

    )

    # ========================================================

    # Discord 狀態控制

    # ========================================================

    state = load_state()

    last_signal = state.get(

        "last_signal",

        "NONE"

    )

    print("\n")

    print(

        "上一個訊號：",

        last_signal

    )

    print(

        "目前訊號：",

        current_signal

    )

    # --------------------------------------------------------

    # 新訊號

    # --------------------------------------------------------

    if (

        current_signal != "NONE"

        and

        current_signal != last_signal

    ):

        if current_signal == "LONG":

            direction = "🟢 LONG"

            trend_text = "BULL"

        else:

            direction = "🔴 SHORT"

            trend_text = "BEAR"

        message = (

            "🚨 **CRYPTO MARKET RADAR**\n\n"

            f"方向：{direction}\n"

            f"價格：{current_price:.2f}\n"

            f"1H 趨勢：{trend_text}\n\n"

            f"LONG Score：{long_score}\n"

            f"SHORT Score：{short_score}\n\n"

            "【15M】\n"

            f"EMA Pullback："

            f"{'✅' if "

            "bullish_pullback "

            "else '❌'}\n"

            f"Breakout："

            f"{'✅' if "

            "bullish_breakout "

            "else '❌'}\n"

            f"Structure："

            f"{'✅' if "

            "bullish_structure "

            "else '❌'}\n\n"

            "【SNR】\n"

            f"Support："

            f"{snr['support']:.2f}\n"

            f"Resistance："

            f"{snr['resistance']:.2f}\n"

            f"Near Support："

            f"{'✅' if "

            "snr['near_support'] "

            "else '❌'}\n"

            f"Near Resistance："

            f"{'✅' if "

            "snr['near_resistance'] "

            "else '❌'}"

        )

        success = send_discord(

            message

        )

        if success:

            save_state(

                current_signal

            )

    elif current_signal == "NONE":

        if last_signal != "NONE":

            save_state(

                "NONE"

            )

        print(

            "沒有新訊號"

        )

    else:

        print(

            "沒有新訊號"

        )

    print(

        "\n========================================"

    )

    print(

        "Market Radar 執行完成"

    )

    print(

        "========================================"

    )

# ============================================================

# 執行

# ============================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print(

            "\n❌ 程式發生錯誤："

        )

        print(

            str(e)

        )

        raise
