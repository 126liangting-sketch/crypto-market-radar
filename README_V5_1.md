# Crypto Market Radar V5.1 — Mechanism Update

V5.1 keeps the V5 core strategy unchanged: same 8-point Score and the same >=5/8 qualifying threshold.

## What changed
- Discord uses a Setup lifecycle instead of repeating near-identical signals every 15m.
- NEW SETUP: first qualifying LONG/SHORT >=5/8.
- Same-direction 5/8 or 6/8 continuation: silent on Discord, but still stored as Forward research observations.
- Meaningful enhancement: only 7/8+ triggers one Setup enhancement alert.
- Direction reversal creates one new Setup alert (not a duplicate pair of messages).
- Two consecutive closed 15m bars below 5/8 invalidate the active Setup.
- TP1 / TP2 / SL / ambiguous same-candle outcome can notify Discord for the notified Setup.
- Manual Run Workflow remains status-only and does not create a Forward sample.

## TP / SL
Risk distance = max(1.5 x ATR, 0.40% of entry price).
- SL = 1R
- TP1 = 1R
- TP2 = 2R

The 0.40% floor prevents very low ATR from producing an ultra-tight stop. Raw ATR, MFE and MAE remain stored for later analysis.

## Research data
Every qualifying closed 15m candle can still be stored as a Forward observation. `setup_id` groups observations belonging to the same market Setup, reducing the risk of treating repeated nearby entries as independent opportunities during later analysis.

Existing V5 history is preserved. New observations are tagged `V5_1_MECHANISM`.
