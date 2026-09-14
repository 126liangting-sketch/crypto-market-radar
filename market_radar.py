import os
import requests

# 取得 Discord Webhook
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

# Binance 行情 API
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"


def get_btc_price():
    response = requests.get(
        BINANCE_URL,
        params={"symbol": "BTCUSDT"},
        timeout=10
    )

    response.raise_for_status()

    data = response.json()

    return float(data["price"])


def send_discord(message):
    data = {
        "content": message
    }

    response = requests.post(
        DISCORD_WEBHOOK,
        json=data,
        timeout=10
    )

    response.raise_for_status()


def main():
    price = get_btc_price()

    message = (
        "🚨 **Crypto Market Radar**\n\n"
        "幣種：BTC/USDT\n"
        f"目前價格：${price:,.2f}\n\n"
        "✅ 市場監控測試成功"
    )

    send_discord(message)


if __name__ == "__main__":
    main()
