import os
import requests

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

API_URL = "https://futures.kraken.com/api/charts/v1/spot/PI_XBTUSD"


def get_candles(resolution, count=100):
    url = f"{API_URL}/{resolution}"

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

    if len(candles) < 50:
        raise Exception(
            f"{resolution} K線不足，需要至少50根，目前只有{len(candles)}根"
        )

    return candles


def calculate_ema(prices, length):
    multiplier = 2 / (length + 1)

    ema = sum(prices[:length]) / length

    for price in prices[length:]:
        ema = (price - ema) * multiplier + ema

    return ema


def get_analysis(resolution):
    candles = get_candles(resolution)

    closes = [
        float(candle["close"])
        for candle in candles
    ]

    current_price = closes[-1]

    ema34 = calculate_ema(closes, 34)
    ema50 = calculate_ema(closes, 50)

    return {
        "price": current_price,
        "ema34": ema34,
        "ema50": ema50,
        "candles": candles,
        "closes": closes
    }


def send_discord(message):
    response = requests.post(
        DISCORD_WEBHOOK,
        json={"content": message},
        timeout=20
    )

    response.raise_for_status()


def main():

    print("開始分析 BTC...")

    # =========================
    # 1H
    # =========================

    h1 = get_analysis("1h")

    h1_bull = h1["ema34"] > h1["ema50"]
    h1_bear = h1["ema34"] < h1["ema50"]

    # =========================
    # 15M
    # =========================

    m15 = get_analysis("15m")

    closes = m15["closes"]
    ema34 = m15["ema34"]
    ema50 = m15["ema50"]

    current_price = closes[-1]

    # =========================
    # 判斷最近的15M價格
    # 是否曾經進入 EMA34~EMA50區域
    # =========================

    recent_closes = closes[-6:]

    ema_zone_top = max(ema34, ema50)
    ema_zone_bottom = min(ema34, ema50)

    touched_zone = any(
        ema_zone_bottom <= price <= ema_zone_top
        for price in recent_closes
    )

    # =========================
    # 目前價格是否重新站回 EMA34
    # =========================

    bullish_reclaim = (
        current_price > ema34
    )

    bearish_reclaim = (
        current_price < ema34
    )

    # =========================
    # 最終訊號
    # =========================

    long_signal = (
        h1_bull
        and touched_zone
        and bullish_reclaim
    )

    short_signal = (
        h1_bear
        and touched_zone
        and bearish_reclaim
    )

    # =========================
    # Discord訊息
    # =========================

    message = (
        "🚨 **Crypto Market Radar**\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "₿ BTC / USD\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"💰 目前價格：`${current_price:,.2f}`\n\n"

        "📊 **1H 趨勢**\n"
        f"EMA34：`${h1['ema34']:,.2f}`\n"
        f"EMA50：`${h1['ema50']:,.2f}`\n"
        f"方向：**{'🟢 多頭' if h1_bull else '🔴 空頭'}**\n\n"

        "📊 **15M**\n"
        f"EMA34：`${ema34:,.2f}`\n"
        f"EMA50：`${ema50:,.2f}`\n"
        f"EMA區域：{'✅ 有回踩' if touched_zone else '❌ 尚未回踩'}\n\n"
    )

    # =========================
    # 訊號
    # =========================

    if long_signal:

        message += (
            "━━━━━━━━━━━━━━━━━━\n"
            "🟢 **BTC 多頭回踩訊號**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "1H：多頭\n"
            "15M：回踩 EMA 區域\n"
            "15M：重新站回 EMA34\n\n"
            "🚨 **LONG SETUP**"
        )

    elif short_signal:

        message += (
            "━━━━━━━━━━━━━━━━━━\n"
            "🔴 **BTC 空頭回踩訊號**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "1H：空頭\n"
            "15M：回踩 EMA 區域\n"
            "15M：重新跌破 EMA34\n\n"
            "🚨 **SHORT SETUP**"
        )

    else:

        message += (
            "━━━━━━━━━━━━━━━━━━\n"
            "⚪ **目前沒有交易訊號**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "等待：\n"
            "1H 趨勢 + 15M EMA回踩 + 重新站回"
        )

    send_discord(message)

    print("Discord通知成功")


if __name__ == "__main__":
    main()
