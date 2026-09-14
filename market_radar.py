import os
import json
import requests
# =========================================================
# 基本設定
# =========================================================
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
API_URL = "https://futures.kraken.com/api/charts/v1/spot/PI_XBTUSD"
STATE_FILE = "signal_state.json"
EMA_FAST = 34
EMA_SLOW = 50
LOOKBACK = 8
# =========================================================
# 訊號狀態
# =========================================================
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_signal": "NONE"}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_signal": "NONE"}
def save_state(signal):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_signal": signal}, f)
# =========================================================
# 取得 K 線
# =========================================================
def get_candles(resolution, count=120):
    url = f"{API_URL}/{resolution}"
    print(f"取得 {resolution} K線...")
    response = requests.get(
        url,
        params={"count": count},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()
    if "candles" not in data:
        raise Exception(f"Kraken API 回傳異常：{data}")
    candles = data["candles"]
    if len(candles) < 60:
        raise Exception(
            f"{resolution} K線不足，目前只有 {len(candles)} 根"
        )
    print(
        f"{resolution} K線取得成功："
        f"{len(candles)} 根"
    )
    return candles
# =========================================================
# EMA
# =========================================================
def calculate_ema(prices, length):
    multiplier = 2 / (length + 1)
    ema = sum(prices[:length]) / length
    for price in prices[length:]:
        ema = (price - ema) * multiplier + ema
    return ema
# =========================================================
# 市場分析
# =========================================================
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
# =========================================================
# EMA 回踩
# =========================================================
def check_ema_pullback(candles, ema34, ema50):
    zone_top = max(ema34, ema50)
    zone_bottom = min(ema34, ema50)
    recent = candles[-LOOKBACK:]
    for candle in recent:
        high = float(candle["high"])
        low = float(candle["low"])
        touched = (
            low <= zone_top
            and high >= zone_bottom
        )
        if touched:
            return True
    return False
# =========================================================
# 突破近期高低點
# =========================================================
def check_breakout(candles):
    if len(candles) < LOOKBACK + 1:
        return False, False
    current = candles[-1]
    current_close = float(current["close"])
    previous = candles[-(LOOKBACK + 1):-1]
    recent_high = max(
        float(candle["high"])
        for candle in previous
    )
    recent_low = min(
        float(candle["low"])
        for candle in previous
    )
    bullish_breakout = current_close > recent_high
    bearish_breakout = current_close < recent_low
    return bullish_breakout, bearish_breakout
# =========================================================
# 結構延續
# =========================================================
def check_structure(candles):
    if len(candles) < 5:
        return False, False
    c1 = candles[-4]
    c2 = candles[-3]
    c3 = candles[-2]
    c4 = candles[-1]
    h1 = float(c1["high"])
    h3 = float(c3["high"])
    h4 = float(c4["high"])
    l1 = float(c1["low"])
    l3 = float(c3["low"])
    l4 = float(c4["low"])
    # 多頭：
    # 低點抬高 + 最新 K 線突破前一個高點
    bullish_structure = (
        l3 > l1
        and h4 > h3
    )
    # 空頭：
    # 高點降低 + 最新 K 線跌破前一個低點
    bearish_structure = (
        h3 < h1
        and l4 < l3
    )
    return bullish_structure, bearish_structure
# =========================================================
# Discord
# =========================================================
def send_discord(message):
    response = requests.post(
        DISCORD_WEBHOOK,
        json={"content": message},
        timeout=20
    )
    response.raise_for_status()
    print("Discord 通知成功")
# =========================================================
# 主程式
# =========================================================
def main():
    print("================================")
    print("開始分析 BTC 市場")
    print("================================")
    state = load_state()
    last_signal = state.get(
        "last_signal",
        "NONE"
    )
    # =====================================================
    # 1H 趨勢
    # =====================================================
    h1 = get_analysis("1h")
    h1_ema34 = h1["ema34"]
    h1_ema50 = h1["ema50"]
    h1_bull = h1_ema34 > h1_ema50
    h1_bear = h1_ema34 < h1_ema50
    # =====================================================
    # 15M
    # =====================================================
    m15 = get_analysis("15m")
    candles = m15["candles"]
    current_price = m15["price"]
    ema34 = m15["ema34"]
    ema50 = m15["ema50"]
    # =====================================================
    # 三種訊號來源
    # =====================================================
    ema_pullback = check_ema_pullback(
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
    # =====================================================
    # 最終訊號
    # =====================================================
    long_signal = (
        h1_bull
        and (
            ema_pullback
            or bullish_breakout
            or bullish_structure
        )
        and current_price > ema34
    )
    short_signal = (
        h1_bear
        and (
            ema_pullback
            or bearish_breakout
            or bearish_structure
        )
        and current_price < ema34
    )
    if long_signal:
        current_signal = "LONG"
    elif short_signal:
        current_signal = "SHORT"
    else:
        current_signal = "NONE"
    # =====================================================
    # 顯示分析
    # =====================================================
    print("")
    print("========== 分析結果 ==========")
    print(f"目前價格：{current_price:,.2f}")
    print(f"1H EMA34：{h1_ema34:,.2f}")
    print(f"1H EMA50：{h1_ema50:,.2f}")
    print(f"15M EMA34：{ema34:,.2f}")
    print(f"15M EMA50：{ema50:,.2f}")
    print("")
    print(
        f"EMA 回踩："
        f"{'✅' if ema_pullback else '❌'}"
    )
    print(
        f"多頭突破："
        f"{'✅' if bullish_breakout else '❌'}"
    )
    print(
        f"空頭突破："
        f"{'✅' if bearish_breakout else '❌'}"
    )
    print(
        f"多頭結構："
        f"{'✅' if bullish_structure else '❌'}"
    )
    print(
        f"空頭結構："
        f"{'✅' if bearish_structure else '❌'}"
    )
    print("")
    print(f"目前訊號：{current_signal}")
    print(f"上一次訊號：{last_signal}")
    print("==============================")
    # =====================================================
    # Discord 通知
    # =====================================================
    if current_signal != last_signal:
        if current_signal == "LONG":
            message = (
                "🟢 **BTC 多頭訊號**\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💰 價格：`${current_price:,.2f}`\n\n"
                "📊 **1H 趨勢**\n"
                f"EMA34：`${h1_ema34:,.2f}`\n"
                f"EMA50：`${h1_ema50:,.2f}`\n"
                "方向：🟢 多頭\n\n"
                "📊 **15M 確認**\n"
                f"EMA 回踩："
                f"{'✅' if ema_pullback else '❌'}\n"
                f"突破："
                f"{'✅' if bullish_breakout else '❌'}\n"
                f"結構延續："
                f"{'✅' if bullish_structure else '❌'}\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🚨 **LONG SETUP**\n"
                "━━━━━━━━━━━━━━━━━━"
            )
            send_discord(message)
        elif current_signal == "SHORT":
            message = (
                "🔴 **BTC 空頭訊號**\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💰 價格：`${current_price:,.2f}`\n\n"
                "📊 **1H 趨勢**\n"
                f"EMA34：`${h1_ema34:,.2f}`\n"
                f"EMA50：`${h1_ema50:,.2f}`\n"
                "方向：🔴 空頭\n\n"
                "📊 **15M 確認**\n"
                f"EMA 回踩："
                f"{'✅' if ema_pullback else '❌'}\n"
                f"跌破："
                f"{'✅' if bearish_breakout else '❌'}\n"
                f"結構延續："
                f"{'✅' if bearish_structure else '❌'}\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🚨 **SHORT SETUP**\n"
                "━━━━━━━━━━━━━━━━━━"
            )
            send_discord(message)
        elif current_signal == "NONE":
            print(
                "訊號已失效，等待下一次訊號"
            )
        save_state(current_signal)
    else:
        print(
            "訊號沒有變化，不發送 Discord"
        )
    print("================================")
    print("分析完成")
    print("================================")
if __name__ == "__main__":
    main()
