# Crypto Market Radar

GitHub Actions → Kraken Futures → 統計模型 → Discord

- 每 5 分鐘執行
- 每 15 分鐘建立樣本
- 觀察下一小時
- 1H / 15M EMA34/50
- OI / CVD
- +1% = LONG、-1% = SHORT、其餘 NEUTRAL
- 100 個完成樣本後才正式計算機率
- ≥65% 才通知 Discord
- 消息面只作警告，不決定方向
