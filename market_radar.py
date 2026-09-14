import os
import json
import time
import requests

API_BASE = "https://futures.kraken.com/api/charts/v1/spot/PI_XBTUSD"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
STATE_FILE = "signal_state.json"

MIN_CANDLES = 80
LOOKBACK = 8
SNR_LOOKBACK = 40
SNR_TOLERANCE = 0.003


def get_candles(resolution):
    print(f"\n取得 {resolution} K 線...")

    if resolution == "1h":
        interval_seconds = 3600
    elif resolution == "15m":
        interval_seconds = 900
    else:
        raise ValueError(f"不支援的 timeframe：{resolution}")

    now = int(time.time())
    from_time = now - interval_seconds * MIN_CANDLES * 2

    url = f"{API_BASE}/{resolution}"
    params = {"from": from_time, "to": now}

    response = requests.get(url, params=params, timeout=20)

    print("API Status:", response.status_code)

    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict):
        candles = data.get("candles", data.get("data"))

        if candles is None:
            raise RuntimeError(
                f"Kraken 回傳格式無法辨識：{list(data.keys())}"
            )

    elif isinstance(data, list):
        candles = data

    else:
        raise RuntimeError(
            f"未知 API 格式：{type(data).__name__}"
        )

    normalized = []

    for c in candles:

        try:

            if isinstance(c, dict):

                normalized.append({
                    "time": float(c["time"]),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": float(c.get("volume", 0)),
                })

            elif isinstance(c, list) and len(c) >= 5:

                normalized.append({
                    "time": float(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]) if len(c) > 5 else 0,
                })

        except (
            KeyError,
            TypeError,
            ValueError
        ):
            continue

    unique = {
        c["time"]: c
        for c in normalized
    }

    candles = sorted(
        unique.values(),
        key=lambda x: x["time"]
    )

    print(
        f"{resolution} 有效 K 線："
        f"{len(candles)} 根"
    )

    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"{resolution} K 線不足："
            f"{len(candles)} 根"
        )

    return candles


def calculate_ema(candles, period):

    closes = [
        c["close"]
        for c in candles
    ]

    multiplier = 2 / (period + 1)

    ema = closes[0]

    for price in closes[1:]:

        ema = (
            (price - ema)
            * multiplier
            + ema
        )

    return ema


def check_ema_pullback(candles):

    if len(candles) < 10:
        return False, False

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

    for candle in candles[-5:]:

        touched = (
            candle["low"] <= zone_high
            and
            candle["high"] >= zone_low
        )

        if touched:

            if candle["close"] > zone_high:
                bullish = True

            if candle["close"] < zone_low:
                bearish = True

    return bullish, bearish


def check_breakout(candles):

    if len(candles) < LOOKBACK + 2:
        return False, False

    previous = candles[
        -(LOOKBACK + 1):-1
    ]

    current = candles[-1]

    previous_high = max(
        c["high"]
        for c in previous
    )

    previous_low = min(
        c["low"]
        for c in previous
    )

    bullish = (
        current["close"]
        > previous_high
    )

    bearish = (
        current["close"]
        < previous_low
    )

    return bullish, bearish


def check_structure(candles):

    if len(candles) < 5:
        return False, False

    c1, c2, c3, c4 = candles[-4:]

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


def calculate_snr(candles):

    if len(candles) < SNR_LOOKBACK + 2:

        return {
            "support": 0,
            "resistance": 0,
            "near_support": False,
            "near_resistance": False,
            "breakout_up": False,
            "breakout_down": False,
        }

    history = candles[
        -(SNR_LOOKBACK + 1):-1
    ]

    current_price = candles[-1]["close"]

    support = min(
        c["low"]
        for c in history
    )

    resistance = max(
        c["high"]
        for c in history
    )

    support_distance = (
        abs(current_price - support)
        / current_price
    )

    resistance_distance = (
        abs(current_price - resistance)
        / current_price
    )

    return {
        "support": support,
        "resistance": resistance,

        "near_support":
            support_distance <= SNR_TOLERANCE,

        "near_resistance":
            resistance_distance <= SNR_TOLERANCE,

        "breakout_up":
            current_price > resistance,

        "breakout_down":
            current_price < support,
    }


def send_discord(message):

    if not DISCORD_WEBHOOK:

        print(
            "找不到 DISCORD_WEBHOOK"
        )

        return False

    response = requests.post(
        DISCORD_WEBHOOK,
        json={
            "content": message
        },
        timeout=20
    )

    print(
        "Discord Status:",
        response.status_code
    )

    if response.status_code in (
        200,
        204
    ):

        print(
            "Discord 發送成功"
        )

        return True

    print(
        "Discord 發送失敗：",
        response.text
    )

    return False


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


def main():

    print(
        "========================================"
    )

    print(
        "       CRYPTO MARKET RADAR"
    )

    print(
        "========================================"
    )

    candles_1h = get_candles(
        "1h"
    )

    candles_15m = get_candles(
        "15m"
    )

    current_price = (
        candles_15m[-1]["close"]
    )

    # 1H EMA

    ema34_1h = calculate_ema(
        candles_1h,
        34
    )

    ema50_1h = calculate_ema(
        candles_1h,
        50
    )

    h1_bull = (
        ema34_1h > ema50_1h
    )

    h1_bear = (
        ema34_1h < ema50_1h
    )

    # 15M EMA

    ema34_15m = calculate_ema(
        candles_15m,
        34
    )

    ema50_15m = calculate_ema(
        candles_15m,
        50
    )

    # 技術條件

    bullish_pullback, bearish_pullback = (
        check_ema_pullback(
            candles_15m
        )
    )

    bullish_breakout, bearish_breakout = (
        check_breakout(
            candles_15m
        )
    )

    bullish_structure, bearish_structure = (
        check_structure(
            candles_15m
        )
    )

    # SNR

    snr = calculate_snr(
        candles_15m
    )

    # ========================================================
    # SCORE
    # ========================================================

    long_score = 0
    short_score = 0

    if h1_bull:
        long_score += 2

    if h1_bear:
        short_score += 2

    if bullish_pullback:
        long_score += 1

    if bearish_pullback:
        short_score += 1

    if bullish_breakout:
        long_score += 2

    if bearish_breakout:
        short_score += 2

    if bullish_structure:
        long_score += 1

    if bearish_structure:
        short_score += 1

    # SNR 加分

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

    # ========================================================
    # 最終訊號
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

    print(
        "\n========== MARKET DATA =========="
    )

    print(
        f"Price     : {current_price:.2f}"
    )

print(

        f"1H EMA34  : {ema34_1h:.2f}"

    )

    print(

        f"1H EMA50  : {ema50_1h:.2f}"

    )

    print(

        f"15M EMA34 : {ema34_15m:.2f}"

    )

    print(

        f"15M EMA50 : {ema50_15m:.2f}"

    )

    print(

        "================================="

    )

    print(

        "\n========== TRIGGER DEBUG =========="

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

    # ========================================================

    # SNR DEBUG

    # ========================================================

    print(

        "\n========== SNR =========="

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

    # ========================================================

    # SCORE DEBUG

    # ========================================================

    print(

        "\n========== SCORE =========="

    )

    if h1_bull:

        trend = "BULL"

    elif h1_bear:

        trend = "BEAR"

    else:

        trend = "NEUTRAL"

    print(

        "1H Trend  :",

        trend

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

    # Discord 狀態

    # ========================================================

    state = load_state()

    last_signal = state.get(

        "last_signal",

        "NONE"

    )

    print(

        "\n上一個訊號：",

        last_signal

    )

    print(

        "目前訊號：",

        current_signal

    )

    # ========================================================

    # 新訊號

    # ========================================================

    if (

        current_signal != "NONE"

        and

        current_signal != last_signal

    ):

        if current_signal == "LONG":

            direction = "🟢 LONG"

            pullback_ok = (

                bullish_pullback

            )

            breakout_ok = (

                bullish_breakout

            )

            structure_ok = (

                bullish_structure

            )

        else:

            direction = "🔴 SHORT"

            pullback_ok = (

                bearish_pullback

            )

            breakout_ok = (

                bearish_breakout

            )

            structure_ok = (

                bearish_structure

            )

        message = (

            "🚨 **CRYPTO MARKET RADAR**\n\n"

            f"方向：{direction}\n"

            f"價格：{current_price:.2f}\n"

            f"1H 趨勢：{trend}\n\n"

            f"LONG Score：{long_score}\n"

            f"SHORT Score：{short_score}\n\n"

            "【15M】\n"

            f"EMA Pullback："

            f"{'✅' if pullback_ok else '❌'}\n"

            f"Breakout："

            f"{'✅' if breakout_ok else '❌'}\n"

            f"Structure："

            f"{'✅' if structure_ok else '❌'}\n\n"

            "【SNR】\n"

            f"Support："

            f"{snr['support']:.2f}\n"

            f"Resistance："

            f"{snr['resistance']:.2f}\n"

            f"Near Support："

            f"{'✅' if snr['near_support'] else '❌'}\n"

            f"Near Resistance："

            f"{'✅' if snr['near_resistance'] else '❌'}"

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

        "\nMarket Radar 執行完成"

    )

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
