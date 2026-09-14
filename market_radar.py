import os
import requests


# =========================
# Discord
# =========================

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]


# =========================
# CoinGecko API
# =========================

API_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc"


# =========================
# 取得 K 線
# =========================

def get_ohlc(days):
    response = requests.get(
        API_URL,
        params={
            "vs_currency": "usd",
            "days": days
        },
        timeout=20
    )

    response.raise_for_status()

    return response.json()


# =========================
# EMA
# =========================

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


# =========================
# Discord 通知
# =========================

def send_discord(message):

    response = requests.post(
        DISCORD_WEBHOOK,
        json={
            "content": message
        },
        timeout=20
    )

    response.raise_for_status()


# =========================
# 主程式
# =========================

def main():

    # CoinGecko OHLC
    data = get_ohlc(30)

    if not data:
        raise Exception("沒有取得 K 線資料")

    # CoinGecko 回傳：
    # [時間, 開盤, 最高, 最低, 收盤]

    closes = [
        candle[4]
        for candle in data
    ]

    if len(closes) < 50:
        raise Exception(
            f"K線資料不足，需要至少50根，目前只有{len(closes)}根"
        )

    current_price = closes[-1]

    ema34 = calculate_ema(closes, 34)
    ema50 = calculate_ema(closes, 50)

    # 趨勢判斷

    if ema34 > ema50:
        trend = "🟢 多頭"
    elif ema34 < ema50:
        trend = "🔴 空頭"
    else:
        trend = "⚪ 震盪"


    # Discord 訊息

    message = (
        "🚨 **Crypto Market Radar**\n\n"

        "━━━━━━━━━━━━━━\n"
        "₿ BTC / USD\n"
        "━━━━━━━━━━━━━━\n\n"

        f"目前價格：`${current_price:,.2f}`\n\n"

        f"EMA34：`${ema34:,.2f}`\n"
        f"EMA50：`${ema50:,.2f}`\n\n"

        f"趨勢：**{trend}**\n\n"

        "✅ K線資料取得成功\n"
        "✅ EMA計算成功\n"
        "✅ Discord通知成功"
    )

    send_discord(message)


# =========================
# 執行
# =========================

if __name__ == "__main__":
    main()
