# Crypto Market Radar V4.1

V4.1 adds a manual Discord market-status query without changing the automatic signal rules.

## Manual query
GitHub → Actions → Crypto Market Radar → Run workflow.
Because GitHub sets `GITHUB_EVENT_NAME=workflow_dispatch` automatically, no workflow YAML change is required.

A manual run sends the current BTC status to Discord even below 5/8. It does **not** create a new Forward Test, does not consume the current 15m candle, and does not create a new legacy sample. Existing pending tests may still be settled normally.

## Automatic schedule
Scheduled runs behave like V4: score >= 5/8 can create Forward Tests and alerts; forward-test statistics later calibrate notification confidence.

Keep your existing `probability_state.json`; do not replace or delete it.
