# Crypto Market Radar V4.5

- OI/CVD: uses the latest 12 x 15-minute analytics points (~3 hours) as the broad trend.
- Latest 4 points (~1 hour) are used as a reversal check. If they clearly oppose the broad trend, OI/CVD is treated as FLAT for scoring.
- Keeps V4.4 news risk alerts (news does not affect the 8-point score).
- Keeps >=5/8 alerts, manual status, Forward Test, 15m/30m/1h/2h validation, MFE/MAE, API retry protection, and legacy state preservation.
- Do not delete probability_state.json when upgrading.
