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
        raise Exception(f"Kraken API 回傳格式異常：{data}")

    candles = data["candles"]

    if len(candles) < 50:
        raise Exception(
            f"{resolution} K線不足，需要至少50根，目前只有{len(candles)}根"
        )

    return candles


def calculate_ema(prices, length):
    if len(prices) < length:
        raise Exception(
            f"價格資料不足，需要至少 {length} 根，目前只有 {len(prices)} 根"
        )

    multiplier = 2 / (length + 1)

    ema = sum(prices[:length]) / length

    for price in prices[length:]:
        ema = (price - ema) * multiplier + ema

    return ema


def analyze_timeframe(resolution, name):
    candles = get_candles(resolution)

    # 使用已取得的收盤價
    closes = [
        float(candle["close"])
        for candle in candles
    ]

    current_price = closes[-1]

    ema34 = calculate_ema(closes, 34)
    ema50 = calculate_ema(closes, 50)

    if ema34 > ema50:
        trend = "🟢 多頭"
    elif ema34 < ema50:
        trend = "🔴 空頭"
    else:
        trend = "⚪ 震盪"

    return {
        "name": name,
        "price": current_price,
        "ema34": ema34,
        "ema50": ema50,
        "trend": trend
    }


def send_discord(message):
    response = requests.post(
        DISCORD_WEBHOOK,
        json={"content": message},
        timeout=20
    )

    response.raise_for_status()


def main():

    print("開始取得 BTC 多週期資料...")

    # 1H
    h1 = analyze_timeframe("1h", "1H")

    # 15M
    m15 = analyze_timeframe("15m", "15M")

    # 判斷多週期方向
    if "多頭" in h1["trend"] and "多頭" in m15["trend"]:
        alignment = "🟢 多週期多頭一致"

    elif "空頭" in h1["trend"] and "空頭" in m15["trend"]:
        alignment = "🔴 多週期空頭一致"

    else:
        alignment = "🟡 多週期方向分歧"

    message = (
        "🚨 **Crypto Market Radar**\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "₿ BTC / USD — Kraken\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"💰 最新價格：`${m15['price']:,.2f}`\n\n"

        "📊 **1H 趨勢**\n"
        f"EMA34：`${h1['ema34']:,.2f}`\n"
        f"EMA50：`${h1['ema50']:,.2f}`\n"
        f"方向：**{h1['trend']}**\n\n"

        "📊 **15M 趨勢**\n"
        f"EMA34：`${m15['ema34']:,.2f}`\n"
        f"EMA50：`${m15['ema50']:,.2f}`\n"
        f"方向：**{m15['trend']}**\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **多週期判斷：{alignment}**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "✅ 1H K線取得成功\n"
        "✅ 15M K線取得成功\n"
        "✅ EMA34 / EMA50 計算成功"
    )

    print(message)

    send_discord(message)

    print("Discord 通知成功")


if __name__ == "__main__":
    main()
