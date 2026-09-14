import os
import requests


# Discord Webhook
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

# CoinGecko API
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"


def get_market_data():
    response = requests.get(
        COINGECKO_URL,
        params={
            "vs_currency": "usd",
            "days": "2",
            "interval": "hourly"
        },
        timeout=20
    )

    response.raise_for_status()

    return response.json()


def calculate_ema(prices, length):
    multiplier = 2 / (length + 1)

    ema = prices[0]

    for price in prices[1:]:
        ema = (price - ema) * multiplier + ema

    return ema


def send_discord(message):
    response = requests.post(
        DISCORD_WEBHOOK,
        json={"content": message},
        timeout=20
    )

    response.raise_for_status()


def main():
    data = get_market_data()

    prices = [
        item[1]
        for item in data["prices"]
    ]

    if len(prices) < 50:
        raise Exception("取得的價格資料不足")

    current_price = prices[-1]

    ema34 = calculate_ema(prices, 34)
    ema50 = calculate_ema(prices, 50)

    if ema34 > ema50:
        trend = "🟢 多頭"
    elif ema34 < ema50:
        trend = "🔴 空頭"
    else:
        trend = "⚪ 震盪"

    message = (
        "🚨 **Crypto Market Radar**\n\n"
        f"幣種：BTC/USDT\n"
        f"目前價格：${current_price:,.2f}\n\n"
        f"EMA34：${ema34:,.2f}\n"
        f"EMA50：${ema50:,.2f}\n\n"
        f"1H 趨勢：{trend}\n\n"
        "✅ 市場監控測試成功"
    )

    send_discord(message)


if __name__ == "__main__":
    main()
