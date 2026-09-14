import os
import json
import requests
# ============================================================
# 基本設定
# ============================================================
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
API_URL = "https://futures.kraken.com/api/charts/v1/spot/PI_XBTUSD"
LOOKBACK = 8
STATE_FILE = "signal_state.json"
# ============================================================
# Discord
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
        print(f"Discord response: {response.text[:500]}")
        raise Exception(
            f"Discord 發送失敗："
            f"{response.status_code} "
            f"{response.text[:500]}"
        )
# ============================================================
# 狀態
# ============================================================
def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "last_signal": "NONE"
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "last_signal": "NONE"
        }
def save_state(signal):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "last_signal": signal
            },
            f,
            ensure_ascii=False
        )
# ============================================================
# 取得 K 線
# ============================================================
def get_candles(resolution):
    url = f"{API_URL}?period={resolution}"

    response = requests.get(
        url,
        timeout=20
    )

    print(f"API URL: {url}")
    print(f"API Status: {response.status_code}")

    response.raise_for_status()

    data = response.json()

    print(f"API Response Type: {type(data).__name__}")

    # Kraken 目前回傳 List
    if isinstance(data, list):

        candles = data

    # 如果 API 回傳 Dictionary，兼容處理
    elif isinstance(data, dict):

        if "candles" in data:
            candles = data["candles"]

        elif "data" in data:
            candles = data["data"]

        else:
            print("API Dictionary Keys:")
            print(list(data.keys()))

            raise Exception(
                "API 回傳 Dictionary，但找不到 candles/data"
            )

    else:

        raise Exception(
            f"API 回傳未知格式：{type(data).__name__}"
        )

    if len(candles) < 60:

        raise Exception(
            f"{resolution} K 線數量不足：{len(candles)}"
        )

    print(
        f"{resolution} K 線取得成功："
        f"{len(candles)} 根"
    )

    return candles
# ============================================================
# EMA
# ============================================================
def calculate_ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = (
            (price - ema) * multiplier
            + ema
        )
    return ema
# ============================================================
# 取得 K 線欄位
# ============================================================
def candle_value(candle, key):
    return float(candle[key])
# ============================================================
# EMA 分析
# ============================================================
def get_analysis(candles):
    closes = [
        candle_value(c, "close")
        for c in candles
    ]
    ema34 = calculate_ema(
        closes,
        34
    )
    ema50 = calculate_ema(
        closes,
        50
    )
    price = closes[-1]
    return {
        "price": price,
        "ema34": ema34,
        "ema50": ema50
    }
# ============================================================
# EMA 回踩
# ============================================================
def check_ema_pullback(candles):
    if len(candles) < LOOKBACK + 50:
        return False
    closes = [
        candle_value(c, "close")
        for c in candles
    ]
    ema34 = calculate_ema(
        closes,
        34
    )
    ema50 = calculate_ema(
        closes,
        50
    )
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
    recent = candles[-LOOKBACK:]
    touched_zone = False
    for candle in recent:
        high = candle_value(
            candle,
            "high"
        )
        low = candle_value(
            candle,
            "low"
        )
        if (
            low <= zone_high
            and
            high >= zone_low
        ):
            touched_zone = True
            break
    if not touched_zone:
        return False
    last_close = candle_value(
        candles[-1],
        "close"
    )
    previous_close = candle_value(
        candles[-2],
        "close"
    )
    bullish_reaction = (
        last_close > zone_high
        and
        last_close > previous_close
    )
    bearish_reaction = (
        last_close < zone_low
        and
        last_close < previous_close
    )
    return {
        "bullish": bullish_reaction,
        "bearish": bearish_reaction
    }
# ============================================================
# 突破
# ============================================================
def check_breakout(candles):
    if len(candles) < LOOKBACK + 2:
        return {
            "bullish": False,
            "bearish": False
        }
    recent = candles[-LOOKBACK-1:-1]
    previous_high = max(
        candle_value(c, "high")
        for c in recent
    )
    previous_low = min(
        candle_value(c, "low")
        for c in recent
    )
    last_close = candle_value(
        candles[-1],
        "close"
    )
    bullish = last_close > previous_high
    bearish = last_close < previous_low
    return {
        "bullish": bullish,
        "bearish": bearish
    }
# ============================================================
# 結構
# ============================================================
def check_structure(candles):
    if len(candles) < 6:
        return {
            "bullish": False,
            "bearish": False
        }
    c1 = candles[-5]
    c2 = candles[-4]
    c3 = candles[-3]
    c4 = candles[-2]
    h1 = candle_value(c1, "high")
    h3 = candle_value(c3, "high")
    h4 = candle_value(c4, "high")
    l1 = candle_value(c1, "low")
    l3 = candle_value(c3, "low")
    l4 = candle_value(c4, "low")
    bullish = (
        l3 > l1
        and
        h4 > h3
    )
    bearish = (
        h3 < h1
        and
        l4 < l3
    )
    return {
        "bullish": bullish,
        "bearish": bearish
    }
# ============================================================
# 主程式
# ============================================================
def main():
    print("")
    print("========================================")
    print("        BTC MARKET RADAR")
    print("========================================")
    # --------------------------------------------------------
    # 取得 1H / 15m
    # --------------------------------------------------------
    print("取得 1H K 線...")
    h1_candles = get_candles("1h")
    print("取得 15m K 線...")
    m15_candles = get_candles("15m")
    # --------------------------------------------------------
    # 1H 分析
    # --------------------------------------------------------
    h1 = get_analysis(
        h1_candles
    )
    h1_price = h1["price"]
    h1_ema34 = h1["ema34"]
    h1_ema50 = h1["ema50"]
    h1_bull = h1_ema34 > h1_ema50
    h1_bear = h1_ema34 < h1_ema50
    if h1_bull:
        trend = "BULL"
    elif h1_bear:
        trend = "BEAR"
    else:
        trend = "NEUTRAL"
    # --------------------------------------------------------
    # 15m 分析
    # --------------------------------------------------------
    m15 = get_analysis(
        m15_candles
    )
    price = m15["price"]
    ema34 = m15["ema34"]
    ema50 = m15["ema50"]
    # --------------------------------------------------------
    # 條件
    # --------------------------------------------------------
    pullback_result = check_ema_pullback(
        m15_candles
    )
    bullish_pullback = pullback_result["bullish"]
    bearish_pullback = pullback_result["bearish"]
    breakout_result = check_breakout(
        m15_candles
    )
    bullish_breakout = breakout_result["bullish"]
    bearish_breakout = breakout_result["bearish"]
    structure_result = check_structure(
        m15_candles
    )
    bullish_structure = structure_result["bullish"]
    bearish_structure = structure_result["bearish"]
    # --------------------------------------------------------
    # Score
    # --------------------------------------------------------
    long_score = 0
    short_score = 0
    long_reasons = []
    short_reasons = []
    # 1H Trend
    if h1_bull:
        long_score += 2
        long_reasons.append(
            "1H EMA34 > EMA50"
        )
    if h1_bear:
        short_score += 2
        short_reasons.append(
            "1H EMA34 < EMA50"
        )
    # EMA Pullback
    if bullish_pullback:
        long_score += 1
        long_reasons.append(
            "15m EMA Zone 回踩後反應"
        )
    if bearish_pullback:
        short_score += 1
        short_reasons.append(
            "15m EMA Zone 回踩後反應"
        )
    # Breakout
    if bullish_breakout:
        long_score += 2
        long_reasons.append(
            "15m 向上突破"
        )
    if bearish_breakout:
        short_score += 2
        short_reasons.append(
            "15m 向下突破"
        )
    # Structure
    if bullish_structure:
        long_score += 1
        long_reasons.append(
            "15m 多頭結構"
        )
    if bearish_structure:
        short_score += 1
        short_reasons.append(
            "15m 空頭結構"
        )
    # --------------------------------------------------------
    # Trigger
    # --------------------------------------------------------
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
    # --------------------------------------------------------
    # Debug
    # --------------------------------------------------------
    print("")
    print("========== TRIGGER DEBUG ==========")
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

    # --------------------------------------------------------

    # 顯示結果

    # --------------------------------------------------------

    print("")

    print("============================")

    print(

        f"1H Trend  : {trend}"

    )

    print(

        f"LONG Score : {long_score}"

    )

    print(

        f"SHORT Score: {short_score}"

    )

    print(

        f"Signal     : {current_signal}"

    )

    print("============================")

    # --------------------------------------------------------

    # 狀態

    # --------------------------------------------------------

    state = load_state()

    last_signal = state.get(

        "last_signal",

        "NONE"

    )

    print("")

    print(

        f"上一個訊號：{last_signal}"

    )

    print(

        f"目前訊號：{current_signal}"

    )

    # --------------------------------------------------------

    # 沒有變化

    # --------------------------------------------------------

    if current_signal == last_signal:

        print(

            "沒有新訊號"

        )

        return

    # --------------------------------------------------------

    # 訊號失效

    # --------------------------------------------------------

    if current_signal == "NONE":

        save_state("NONE")

        print(

            "訊號失效，"

            "狀態重置為 NONE"

        )

        return

    # --------------------------------------------------------

    # LONG

    # --------------------------------------------------------

    if current_signal == "LONG":

        message = (

            "📊 **LONG SETUP — BTC**\n"

            f"價格：`{price:.2f}`\n"

            f"1H 趨勢：`BULL`\n"

            f"分數：`{long_score}`\n\n"

            "多頭條件達標：\n"

            +

            "\n".join(

                f"• {reason}"

                for reason in long_reasons

            )

        )

        send_discord(message)

        save_state("LONG")

        print(

            "🚀 LONG 訊號已發送 Discord"

        )

        return

    # --------------------------------------------------------

    # SHORT

    # --------------------------------------------------------

    if current_signal == "SHORT":

        message = (

            "📊 **SHORT SETUP — BTC**\n"

            f"價格：`{price:.2f}`\n"

            f"1H 趨勢：`BEAR`\n"

            f"分數：`{short_score}`\n\n"

            "空頭條件達標：\n"

            +

            "\n".join(

                f"• {reason}"

                for reason in short_reasons

            )

        )

        send_discord(message)

        save_state("SHORT")

        print(

            "🔻 SHORT 訊號已發送 Discord"

        )

        return

# ============================================================

# 執行

# ============================================================

if __name__ == "__main__":

    main()
