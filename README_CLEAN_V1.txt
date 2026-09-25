BTC MARKET RADAR — CLEAN V1

核心原則
1. 1H EMA34/50 只當市場背景，不硬擋反向 15M 訊號。
2. 15M 結構是唯一核心 Trigger，只留：回踩再啟動、有效突破。
3. EMA20/34/50 只看位置，不是必要條件。
4. Volume 是主要硬確認：低於 0.70x 才硬擋。
5. Space 至少 1R；前方空間不足不出正式單。
6. Regime 分趨勢/震盪，但目前只標記，不硬擋。
7. OI + CVD Proxy 只做資金流品質描述，不單獨封鎖訊號。
8. 防追價採分級：<=0.30 正常；0.30~0.50 稍延伸；0.50~0.75 需強量+足夠空間；>0.75 放棄。
9. SL 以結構防守點 + 0.15 ATR 緩衝；最小風險 0.50 ATR。
10. TP1=1R；TP2=2R，但如果前方已知結構空間不足 2R，不硬塞 TP2。
11. Prepare 只提醒，不建立模擬單；Formal 才建立 Paper Trade。
12. 正式單記錄 MFE/MAE、持有時間、背景/結構/量能/資金流等資料。
13. 新聞只通知重大宏觀、BTC ETF、監管、交易所安全/穩定幣系統風險；Discord 顯示原文+中文摘要。

新資料檔
- radar_state_clean_v1.json
- paper_trades_clean_v1.json/.csv
- blocked_setups_clean_v1.json/.csv

舊 V7 檔案保留作歷史，不再由新程式寫入。

重要
- CVD 仍是 OKX 1m K線 CLV 推算的 CVD Proxy，不是真正逐筆成交 CVD。
- 模擬單不會送出真實交易訂單。
- 第一批門檻是研究起點；要用累積 Paper Trade 的 MFE/MAE 和結果再驗證，不要憑感覺一直加 Filter。
