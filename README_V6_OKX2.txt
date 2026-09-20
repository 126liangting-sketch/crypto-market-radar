BTC Market Radar V6 OKX v2 FORMAL

Changes from V6_OKX_1_FORMAL:
- Keeps OKX BTC-USDT-SWAP for price, EMA, structure, ATR, volume and OI.
- Keeps OI snapshot accumulation in probability_state.json.
- Replaces unavailable CVD with a clearly labelled "CVD Proxy" based on OKX 1-minute candles.
- CVD Proxy uses CLV-weighted BTC volume over 1H and 3H. It is NOT true trade-by-trade taker CVD.
- Does not use the misleading OKX history-trades 1H/3H calculation (live test covered only ~0.3 min).
- Keeps V6 lifecycle / Forward Test and does not rewrite V5 JSON/CSV.
- Manual workflow run still does not create a new Forward Test sample.

Deployment:
1. Replace only market_radar.py with this file.
2. Workflow command must be: python market_radar.py
3. Keep probability_state.json, all forward_test JSON/CSV files, and DISCORD_WEBHOOK secret.
4. okx_cvd_test.py may be deleted.
