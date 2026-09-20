#!/usr/bin/env python3
"""
OKX CVD data-source test for BTC-USDT-SWAP.
Independent test only:
- does NOT touch market_radar.py
- does NOT send Discord
- does NOT create Forward Test samples

It checks whether OKX public trade history can provide enough historical
trades to estimate 1H / 3H taker-buy vs taker-sell CVD.
"""

import time
import requests
from datetime import datetime, timezone

BASE = "https://www.okx.com"
INST_ID = "BTC-USDT-SWAP"
TARGET_HOURS = 3
MAX_PAGES = 120
LIMIT = 100

session = requests.Session()
session.headers.update({
    "User-Agent": "crypto-market-radar-cvd-test/1.0",
    "Accept": "application/json",
})

def fetch_page(after=None):
    params = {"instId": INST_ID, "limit": str(LIMIT)}
    if after is not None:
        params["after"] = str(after)

    url = f"{BASE}/api/v5/market/history-trades"
    r = session.get(url, params=params, timeout=20)

    print(f"HTTP {r.status_code} | {r.url}")
    r.raise_for_status()

    payload = r.json()
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX error: {payload}")

    return payload.get("data", [])

def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

def main():
    now_ms = int(time.time() * 1000)
    target_ms = now_ms - TARGET_HOURS * 3600 * 1000

    all_trades = []
    seen_trade_ids = set()
    after = None

    for page_no in range(1, MAX_PAGES + 1):
        rows = fetch_page(after)
        if not rows:
            print("No more rows.")
            break

        added = 0
        oldest_ts = None

        for row in rows:
            trade_id = str(row.get("tradeId", ""))
            ts = int(row["ts"])
            oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)

            if trade_id and trade_id in seen_trade_ids:
                continue
            if trade_id:
                seen_trade_ids.add(trade_id)

            all_trades.append(row)
            added += 1

        print(
            f"Page {page_no}: rows={len(rows)}, new={added}, "
            f"oldest={fmt_ts(oldest_ts) if oldest_ts else 'N/A'}"
        )

        if oldest_ts is not None and oldest_ts <= target_ms:
            print("Reached >= 3 hours of trade history.")
            break

        # OKX history-trades pagination: request older trades using the
        # oldest tradeId from the current batch.
        oldest_row = min(rows, key=lambda x: int(x["ts"]))
        next_after = oldest_row.get("tradeId")

        if not next_after or str(next_after) == str(after):
            print("Pagination stopped: no new 'after' cursor.")
            break

        after = next_after
        time.sleep(0.08)

    if not all_trades:
        raise RuntimeError("No trades returned from OKX.")

    all_trades.sort(key=lambda x: int(x["ts"]))
    newest_ms = max(int(x["ts"]) for x in all_trades)
    oldest_ms = min(int(x["ts"]) for x in all_trades)
    coverage_min = (newest_ms - oldest_ms) / 60000

    def calc(hours):
        cutoff = newest_ms - hours * 3600 * 1000
        rows = [x for x in all_trades if int(x["ts"]) >= cutoff]

        buy = 0.0
        sell = 0.0

        for x in rows:
            sz = float(x["sz"])
            side = str(x.get("side", "")).lower()
            if side == "buy":
                buy += sz
            elif side == "sell":
                sell += sz

        return {
            "count": len(rows),
            "buy": buy,
            "sell": sell,
            "delta": buy - sell,
        }

    one = calc(1)
    three = calc(3)

    print("\n" + "=" * 62)
    print("OKX BTC-USDT-SWAP CVD DATA TEST")
    print("=" * 62)
    print(f"Unique trades: {len(all_trades)}")
    print(f"Newest UTC: {fmt_ts(newest_ms)}")
    print(f"Oldest UTC: {fmt_ts(oldest_ms)}")
    print(f"Coverage: {coverage_min:.1f} minutes")
    print()
    print(
        f"1H | trades={one['count']} | buy={one['buy']:.4f} contracts | "
        f"sell={one['sell']:.4f} contracts | delta={one['delta']:+.4f}"
    )
    print(
        f"3H | trades={three['count']} | buy={three['buy']:.4f} contracts | "
        f"sell={three['sell']:.4f} contracts | delta={three['delta']:+.4f}"
    )
    print()

    sides = {str(x.get("side", "")).lower() for x in all_trades}
    print(f"Sides found: {sorted(sides)}")

    if coverage_min >= 175:
        print("RESULT: PASS - enough history for a direct ~3H CVD test.")
    elif coverage_min >= 55:
        print("RESULT: PARTIAL - enough for ~1H, but not a full 3H window.")
    else:
        print("RESULT: LIMITED - trade-history endpoint does not cover enough time.")
        print("If LIMITED, V6 should accumulate CVD snapshots incrementally instead")
        print("of pretending this response represents a complete 1H/3H CVD.")

if __name__ == "__main__":
    main()
