#!/usr/bin/env python3
"""
BTC Market Radar - Data Source Connectivity Test
Pure diagnostics:
- does NOT send Discord messages
- does NOT modify Forward Test / JSON / CSV
- does NOT create trading signals
Run this once in GitHub Actions and paste the complete output back into ChatGPT.
"""

import json
import time
import urllib.parse
import urllib.request
import urllib.error

UA = "Mozilla/5.0 (compatible; BTC-Market-Radar-Source-Test/1.0)"

def fetch(name, url, timeout=15):
    print(f"\n=== {name} ===")
    print("URL:", url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            print("HTTP:", r.status)
            print("content-type:", r.headers.get("content-type"))
            print("bytes:", len(raw))
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception as e:
                print("JSON decode failed:", type(e).__name__, str(e))
                print("body head:", raw[:300])
                return None
            inspect_obj(obj)
            return obj
    except urllib.error.HTTPError as e:
        body = e.read(500)
        print("HTTP ERROR:", e.code, e.reason)
        print("body head:", body.decode("utf-8", errors="replace"))
    except Exception as e:
        print("ERROR:", type(e).__name__, str(e))
    return None

def inspect_obj(obj):
    print("top-level type:", type(obj).__name__)
    if isinstance(obj, dict):
        print("top-level keys:", list(obj.keys())[:20])
        # Common result containers
        for key in ("result", "data"):
            if key in obj:
                val = obj[key]
                print(f"{key} type:", type(val).__name__)
                if isinstance(val, dict):
                    print(f"{key} keys:", list(val.keys())[:20])
                    for k, v in list(val.items())[:8]:
                        if isinstance(v, list):
                            print(f"{key}[{k}] len={len(v)} tail={v[-3:] if v else []}")
                        elif not isinstance(v, (dict, list)):
                            print(f"{key}[{k}]={v}")
                elif isinstance(val, list):
                    print(f"{key} len:", len(val))
                    print(f"{key} tail:", val[-3:] if val else [])
        if "errors" in obj:
            print("errors:", obj["errors"])
    elif isinstance(obj, list):
        print("len:", len(obj))
        print("tail:", obj[-3:] if obj else [])

print("=== BTC MARKET RADAR DATA SOURCE TEST ===")
print("No Discord / no state writes / no Forward samples.")

# 1) Kraken Futures analytics currently used by the radar
base = "https://futures.kraken.com/api/charts/v1/spot/PI_XBTUSD"
for analytics in ("trade-volume", "open-interest", "cvd"):
    fetch(
        f"Kraken Futures {analytics}",
        f"{base}/{analytics}?resolution=15m"
    )

# 2) Coinbase Exchange BTC-USD candles: candle format includes volume.
# granularity=900 = 15 minutes.
fetch(
    "Coinbase Exchange BTC-USD 15m candles (Volume candidate)",
    "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=900"
)

# 3) OKX public candles (Volume candidate)
fetch(
    "OKX BTC-USDT-SWAP 15m candles (Volume candidate)",
    "https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=15m&limit=20"
)

# 4) OKX public open interest current snapshot
fetch(
    "OKX BTC-USDT-SWAP Open Interest",
    "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP"
)

# 5) Bybit: included only to confirm the 403 in the same run
fetch(
    "Bybit BTCUSDT 15m candles",
    "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=15&limit=20"
)
fetch(
    "Bybit BTCUSDT Open Interest",
    "https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=15min&limit=20"
)

print("\n=== TEST FINISHED ===")
print("Paste everything from BTC MARKET RADAR DATA SOURCE TEST through TEST FINISHED.")
