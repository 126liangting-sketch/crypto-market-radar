# Crypto Market Radar V5 Baseline

V5 is based directly on V4.5. The automatic trigger is intentionally unchanged: the selected LONG/SHORT side must reach **5/8 or higher** on the existing V4.5 score. The score is a setup summary, not a probability or win rate.

## V5 additions
- Discord signal message simplified.
- Each signal shows Entry, ATR-adaptive TP1 (1R), TP2 (2R), and SL (1R). These are Forward-Test research levels, not trade instructions.
- OI flat band widened to +/-0.25%; CVD flat tolerance widened to 10% of the analysis-window range to reduce tiny moves being labeled directional.
- Keeps ~3h broad + ~1h recent reversal check for OI/CVD.
- Stores research-only features without changing trigger score: EMA34/50 slopes, EMA gap, Zone distance in ATR, raw OI/CVD broad/recent changes, ATR and news state.
- Continues 15m / 30m / 1h / 2h Forward Test plus MFE/MAE.
- Exports `forward_test_v5.json` and `forward_test_v5.csv` for later analysis.
- V4.x Forward samples remain in runtime state but are never inserted into the V5 dataset.
- If TP and SL are both touched inside the same 15m candle, result is `AMBIGUOUS` because OHLC cannot reveal intrabar ordering.

## Files that should be committed by GitHub Actions
`probability_state.json`, `forward_test_v5.json`, and `forward_test_v5.csv`.

The included workflow is a reference/replacement workflow using the existing `DISCORD_WEBHOOK` secret.
