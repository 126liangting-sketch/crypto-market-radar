import os
import json
import requests
# ============================================================
# 基本設定
# ============================================================
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
API_URL = "https://futures.kraken.com/api/charts/v1/spot/PI_XBTUSD"
STATE_FILE = "signal_state.json"
EMA_FAST = 34
EMA_SLOW = 50
LOOKBACK = 8
# ============================================================
# Discord
# ============================================================
def send_discord(message):
    response = requests.post(
        DISCORD_WEBHOOK,
        json={"content": message},
        timeout=20
    )
    if response.status_code not in [200, 204]:
        raise Exception(
            f"Discord 發送失敗：{response.status_code} {response.text}"
        )
# ============================================================
# 狀態
# ============================================================
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_signal": "NONE"}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_signal": "NONE"}
def save_state(signal):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"last_signal": signal},
            f,
            ensure_ascii=False,
            indent=2
        )
# ============================================================
# Kraken K線
# ============================================================
def get_candles(resolution, count=120):
    url = f"{API_URL}/{resolution}"
    response = requests.get(
        url,
        params={"count": count},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()
    if "candles" not in data:
        raise Exception(
            f"Kraken API 回傳異常：{data}"
        )
    candles = data["candles"]
    if len(candles) < 60:
        raise Exception(
            f"{resolution} K線不足，目前只有 {len(candles)} 根"
        )
    return candles
# ============================================================
# EMA
# ============================================================
def calculate_ema(prices, length):
    if len(prices) < length:
        raise Exception(
            f"價格資料不足，無法計算 EMA{length}"
        )
    multiplier = 2 / (length + 1)
    ema = sum(prices[:length]) / length
    for price in prices[length:]:
        ema = (price - ema) * multiplier + ema
    return ema
# ============================================================
# 取得市場分析
# ============================================================
def get_analysis(resolution):
    candles = get_candles(resolution)
    closes = [
        float(candle["close"])
        for candle in candles
    ]
    current_price = closes[-1]
    ema34 = calculate_ema(
        closes,
        EMA_FAST
    )
    ema50 = calculate_ema(
        closes,
        EMA_SLOW
    )
    return {
        "price": current_price,
        "ema34": ema34,
        "ema50": ema50,
        "candles": candles,
        "closes": closes
    }
# ============================================================
# EMA Pullback / Reaction
# ============================================================
def check_ema_pullback(candles, ema34, ema50):
    zone_top = max(
        ema34,
        ema50
    )
    zone_bottom = min(
        ema34,
        ema50
    )
    recent = candles[-LOOKBACK:]
    bullish_reaction = False
    bearish_reaction = False
    for candle in recent:
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        touched_zone = (
            low <= zone_top
            and high >= zone_bottom
        )
        if not touched_zone:
            continue
        # 回踩 EMA 區後重新站回 EMA34
        if close > ema34:
            bullish_reaction = True
        # 反彈到 EMA 區後跌回 EMA34 下方
        if close < ema34:
            bearish_reaction = True
    return (
        bullish_reaction,
        bearish_reaction
    )
# ============================================================
# Breakout
# ============================================================
def check_breakout(candles):
    if len(candles) < LOOKBACK + 1:
        return False, False
    current = candles[-1]
    current_close = float(
        current["close"]
    )
    previous = candles[
        -(LOOKBACK + 1):-1
    ]
    recent_high = max(
        float(candle["high"])
        for candle in previous
    )
    recent_low = min(
        float(candle["low"])
        for candle in previous
    )
    bullish_breakout = (
        current_close > recent_high
    )
    bearish_breakout = (
        current_close < recent_low
    )
    return (
        bullish_breakout,
        bearish_breakout
    )
# ============================================================
# Structure
# ============================================================
def check_structure(candles):
    if len(candles) < 5:
        return False, False
    c1 = candles[-4]
    c3 = candles[-2]
    c4 = candles[-1]
    h1 = float(c1["high"])
    h3 = float(c3["high"])
    h4 = float(c4["high"])
    l1 = float(c1["low"])
    l3 = float(c3["low"])
    l4 = float(c4["low"])
    # Higher Low + Higher High
    bullish_structure = (
        l3 > l1
        and
        h4 > h3
    )
    # Lower High + Lower Low
    bearish_structure = (
        h3 < h1
        and
        l4 < l3
    )
    return (
        bullish_structure,
        bearish_structure
    )
# ============================================================
# 主程式
# ============================================================
def main():
    print("===================================")
    print("Crypto Market Radar")
    print("===================================")
    # --------------------------------------------------------
    # 取得 1H
    # --------------------------------------------------------
    h1 = get_analysis("1h")
    h1_price = h1["price"]
    h1_ema34 = h1["ema34"]
    h1_ema50 = h1["ema50"]
    h1_bull = h1_ema34 > h1_ema50
    h1_bear = h1_ema34 < h1_ema50
    # --------------------------------------------------------
    # 取得 15M
    # --------------------------------------------------------
    m15 = get_analysis("15m")
    price = m15["price"]
    ema34 = m15["ema34"]
    ema50 = m15["ema50"]
    candles = m15["candles"]
    # --------------------------------------------------------
    # 15M 條件
    # --------------------------------------------------------
    bullish_pullback, bearish_pullback = check_ema_pullback(
        candles,
        ema34,
        ema50
    )
    bullish_breakout, bearish_breakout = check_breakout(
        candles
    )
    bullish_structure, bearish_structure = check_structure(
        candles
    )
    # --------------------------------------------------------
    # 分數
    # --------------------------------------------------------
    long_score = 0
    short_score = 0
    long_reasons = []
    short_reasons = []
    # --------------------------------------------------------
    # 1H 趨勢
    # --------------------------------------------------------
    if h1_bull:
        long_score += 2
        long_reasons.append(
            "1H EMA34 > EMA50，多頭趨勢"
        )
    if h1_bear:
        short_score += 2
        short_reasons.append(
            "1H EMA34 < EMA50，空頭趨勢"
        )
    # --------------------------------------------------------
    # EMA Pullback
    # --------------------------------------------------------
    if h1_bull and bullish_pullback:
        long_score += 1
        long_reasons.append(
            "15M 回踩 EMA34/50 區後出現多頭反應"
        )
    if h1_bear and bearish_pullback:
        short_score += 1
        short_reasons.append(
            "15M 反彈 EMA34/50 區後出現空頭反應"
        )
    # --------------------------------------------------------
    # Breakout
    # --------------------------------------------------------
    if h1_bull and bullish_breakout:
        long_score += 2
        long_reasons.append(
            f"15M 突破近 {LOOKBACK} 根高點"
        )
    if h1_bear and bearish_breakout:
        short_score += 2
        short_reasons.append(
            f"15M 跌破近 {LOOKBACK} 根低點"
        )
    # --------------------------------------------------------
    # Structure
    # --------------------------------------------------------
    if h1_bull and bullish_structure:
        long_score += 1
        long_reasons.append(
            "15M 結構出現 Higher Low / Higher High"
        )
    if h1_bear and bearish_structure:
        short_score += 1
        short_reasons.append(
            "15M 結構出現 Lower High / Lower Low"
        )
    # --------------------------------------------------------
    # 價格位置
    # --------------------------------------------------------
    if h1_bull and price > ema34:
        long_score += 1
        long_reasons.append(
            "15M 價格位於 EMA34 上方"
        )
    if h1_bear and price < ema34:
        short_score += 1
        short_reasons.append(
            "15M 價格位於 EMA34 下方"
        )
    # --------------------------------------------------------
    # 強制要求：一定要有 15M 實際觸發條件
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
    # 輸出分析
    # ----------------------------------------
    print("")

    print("========== MARKET ==========")

    print(f"BTC Price : {price:.2f}")

    print(

        f"1H EMA34  : {h1_ema34:.2f}"

    )

    print(

        f"1H EMA50  : {h1_ema50:.2f}"

    )

    print(

        f"15M EMA34 : {ema34:.2f}"

    )

    print(

        f"15M EMA50 : {ema50:.2f}"

    )

    print("")

    print(

        f"1H Trend  : "

        f"{'BULL' if h1_bull else 'BEAR'}"

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

    print("")

    # --------------------------------------------------------

    # 狀態

    # --------------------------------------------------------

    state = load_state()

    last_signal = state.get(

        "last_signal",

        "NONE"

    )

    print(

        f"上一個訊號：{last_signal}"

    )

    print(

        f"目前訊號：{current_signal}"

    )

    # --------------------------------------------------------

    # NONE → NONE

    # 不通知

    # --------------------------------------------------------

    if (

        current_signal == "NONE"

        and

        last_signal == "NONE"

    ):

        print(

            "沒有新訊號"

        )

        return

    # --------------------------------------------------------

    # LONG → LONG

    # SHORT → SHORT

    # 不重複通知

    # --------------------------------------------------------

    if current_signal == last_signal:

        print(

            "訊號沒有變化，不重複通知"

        )

        return

    # --------------------------------------------------------

    # LONG / SHORT → NONE

    # 只更新狀態，不發 Discord

    # --------------------------------------------------------

    if current_signal == "NONE":

        save_state("NONE")

        print(

            "訊號失效，狀態重置為 NONE"

        )

        return

    # --------------------------------------------------------

    # 新 LONG

    # --------------------------------------------------------

    if current_signal == "LONG":

        message = (

            f"📊 **LONG SETUP — BTC**\n"

            f"價格：`{price:.2f}`\n"

            f"分數：`{long_score}`\n\n"

            f"多頭條件達標：\n"

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

    # 新 SHORT

    # --------------------------------------------------------

    if current_signal == "SHORT":

        message = (

            f"📊 **SHORT SETUP — BTC**\n"

            f"價格：`{price:.2f}`\n"

            f"分數：`{short_score}`\n\n"

            f"空頭條件達標：\n"

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
