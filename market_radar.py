import os
import json
import requests

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

API_URL = "https://futures.kraken.com/api/charts/v1/spot/PI_XBTUSD"
STATE_FILE = "signal_state.json"


# =========================
# 讀取 / 儲存訊號狀態
# =========================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "last_signal": "NONE"
        }

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {
            "last_signal": "NONE"
        }


def save_state(signal):
    with open(STATE_FILE, "w") as f:
        json.dump({
            "last_signal": signal
        }, f)


# =========================
# 取得K線
# =========================

def get_candles(resolution, count=100):

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

    if len(candles) < 50:
        raise Exception(
            f"{resolution} K線不足，需要至少50根，目前只有{len(candles)}根"
        )

    print(f"{resolution} K線取得成功：{len(candles)} 根")

    return candles


# =========================
# EMA
# =========================

def calculate_ema(prices, length):

    multiplier = 2 / (length + 1)

    ema = sum(prices[:length]) / length

    for price in prices[length:]:
        ema = (price - ema) * multiplier + ema

    return ema


# =========================
# 市場分析
# =========================

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


# =========================
# Discord
# =========================

def send_discord(message):

    response = requests.post(
        DISCORD_WEBHOOK,
        json={"content": message},
        timeout=20
    )

    response.raise_for_status()

    print("Discord通知成功")


# =========================
# 主程式
# =========================

def main():

    print("================================")
    print("開始分析 BTC 市場")
    print("================================")

    state = load_state()

    last_signal = state.get("last_signal", "NONE")

    # -------------------------
    # 1H
    # -------------------------

    h1 = get_analysis("1h")

    h1_bull = h1["ema34"] > h1["ema50"]
    h1_bear = h1["ema34"] < h1["ema50"]

    # -------------------------
    # 15M
    # -------------------------

    m15 = get_analysis("15m")

    closes = m15["closes"]

    ema34 = m15["ema34"]
    ema50 = m15["ema50"]

    current_price = closes[-1]

    # EMA區域
    ema_zone_top = max(ema34, ema50)
    ema_zone_bottom = min(ema34, ema50)

    # 最近6根K棒
    recent_closes = closes[-6:]

    # 是否曾進入EMA區域
    touched_zone = any(
        ema_zone_bottom <= price <= ema_zone_top
        for price in recent_closes
    )

    # -------------------------
    # 重新站回 / 跌破 EMA34
    # -------------------------

    bullish_reclaim = current_price > ema34

    bearish_reclaim = current_price < ema34

    # -------------------------
    # 訊號
    # -------------------------

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

    # -------------------------
    # 決定目前狀態
    # -------------------------

    if long_signal:

        current_signal = "LONG"

    elif short_signal:

        current_signal = "SHORT"

    else:

        current_signal = "NONE"

    print(f"目前訊號：{current_signal}")
    print(f"上一次訊號：{last_signal}")

    # =========================
    # 訊號變化才通知
    # =========================

    if current_signal != last_signal:

        # -------------------------
        # LONG
        # -------------------------

        if current_signal == "LONG":

            message = (
                "🟢 **BTC 多頭回踩訊號**\n\n"

                "━━━━━━━━━━━━━━━━━━\n"

                f"💰 價格：`${current_price:,.2f}`\n\n"

                "📊 **1H 趨勢**\n"
                f"EMA34：`${h1['ema34']:,.2f}`\n"
                f"EMA50：`${h1['ema50']:,.2f}`\n"
                "方向：🟢 多頭\n\n"

                "📊 **15M**\n"
                f"EMA34：`${ema34:,.2f}`\n"
                f"EMA50：`${ema50:,.2f}`\n"
                "回踩 EMA 區域：✅\n"
                "重新站回 EMA34：✅\n\n"

                "━━━━━━━━━━━━━━━━━━\n"
                "🚨 **LONG SETUP**\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            send_discord(message)

        # -------------------------
        # SHORT
        # -------------------------

        elif current_signal == "SHORT":

            message = (
                "🔴 **BTC 空頭回踩訊號**\n\n"

                "━━━━━━━━━━━━━━━━━━\n"

                f"💰 價格：`${current_price:,.2f}`\n\n"

                "📊 **1H 趨勢**\n"
                f"EMA34：`${h1['ema34']:,.2f}`\n"
                f"EMA50：`${h1['ema50']:,.2f}`\n"
                "方向：🔴 空頭\n\n"

                "📊 **15M**\n"
                f"EMA34：`${ema34:,.2f}`\n"
                f"EMA50：`${ema50:,.2f}`\n"
                "回踩 EMA 區域：✅\n"
                "重新跌破 EMA34：✅\n\n"

                "━━━━━━━━━━━━━━━━━━\n"
                "🚨 **SHORT SETUP**\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            send_discord(message)

        # -------------------------
        # 訊號消失
        # -------------------------

        elif current_signal == "NONE":

            print("訊號已失效，等待下一次訊號")

        # 儲存新的狀態
        save_state(current_signal)

    else:

        print("訊號沒有變化，不發送 Discord")

    print("================================")
    print("分析完成")
    print("================================")


if __name__ == "__main__":
    main()
