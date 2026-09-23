BTC MARKET RADAR V7 — CLEAN FORMAL VERSION

V7 goals
- Keep: OKX BTC-USDT-SWAP, 1H/15M EMA, HH/HL + LL/LH structure, BREAKOUT, RETEST,
  Volume, OI, CVD Proxy, ATR, Discord alerts and blocked-reason statistics.
- Formal trade signal now automatically creates a Paper Trade.
- Paper Trade records Entry / TP1 / TP2 / SL, trigger, reason, market context,
  15m/30m/1h/2h returns, MFE/MAE, duration and final result.
- Discord still sends the actual formal signal and later TP1 / TP2 / SL result alerts.
- Breakout no-chase threshold: 1.50 ATR.
- Continuation no-chase threshold: 1.00 ATR.
- CVD Proxy FLAT threshold: 5%.
- News alerts are reduced to major macro / direct BTC / systemic exchange-security events.

V7 files
- market_radar.py
- radar_state_v7.json
- paper_trades_v7.json
- paper_trades_v7.csv
- blocked_reasons_v7.json
- blocked_reasons_v7.csv
- btc_market_radar_v7.yml

CLEAN MIGRATION
1. Upload/replace market_radar.py.
2. Replace the existing GitHub workflow content with btc_market_radar_v7.yml
   (or copy its full contents into your existing .github/workflows/*.yml file).
3. Keep DISCORD_WEBHOOK secret.
4. Run workflow once.
5. After V7 has a green run and the five V7 data files appear, old V5/V6 data files
   can be deleted from the repo if you no longer want historical records:
   probability_state.json
   forward_test_v5.json / .csv
   forward_test_v6.json / .csv
   blocked_reasons.json / .csv
6. Do NOT delete the V7 files listed above.

Important
- V7 starts a fresh state. OI 1H/3H history must accumulate again after migration.
- CVD Proxy is still a candle-based proxy, not true trade-by-trade CVD.
- Paper Trade is simulated tracking only; it does not place a real exchange order.


V7.2 Prepare Plan
- Discord notification flow is simplified to:
  1) 👀 Structure Prepare
  2) ⚡ Formal Signal + Paper Trade
  3) TP1 / TP2 / SL result
- Removed standalone Swing Breakout explosion alerts from Discord.
  Blocked breakouts are still recorded in blocked_reasons_v7 and RETEST watch remains active.
- Structure Prepare now includes a preview trade plan:
  Entry / SL / TP1 / TP2 / approximate ATR risk.
- The preview plan is NOT an open trade and does NOT create a Paper Trade.
- When a formal signal triggers, Entry/SL/TP are recalculated from the actual trigger context.


V7 Dual Track
- Product name remains simply V7.
- 👀 Structure Prepare creates a PREPARE simulated trade in prepare_trades_v7.*
  using the preview Entry / SL / TP1 / TP2 shown in Discord.
- ⚡ Formal Signal creates the FORMAL Paper Trade in paper_trades_v7.*
- Both carry setup_id so the early-vs-confirmed entry can be compared.
- Prepare trades are research-only; they do not change formal signal logic.
- Prepare trades are tracked for TP1 / TP2 / SL, MFE / MAE, 15m / 30m / 1h / 2h outcomes.
- Discord stays clean: no extra TP/SL spam for prepare trades; TP/SL notifications remain for formal trades.
