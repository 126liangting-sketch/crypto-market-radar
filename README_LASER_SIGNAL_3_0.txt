雷射訊號｜Laser Signal 3.0
==================

目的
- 把先前 Clean V1 / Formal Engine 2.0 / Quality 2.1 的有效模組整合成單一主線。
- 雷射訊號 3.0 使用全新的 state / CSV / JSON，樣本從 0 開始。
- 舊的 *_clean_v1.* 檔案暫時只作歷史備份；雷射訊號 3.0 不讀、不寫、不統計它們。

雷射訊號 3.0 主流程
1H 背景
→ 15M Opportunity（回踩再啟動 / 有效突破）
→ Trigger Freshness
→ Context / Quality Calibration
→ Global Arbitration / Active Thesis
→ Trade Geometry
→ Dynamic Exit
→ Paper Trade / 1m event tracking
→ 雷射訊號 3.0 專屬績效統計

新資料檔
- radar_state_v3.json
- paper_trades_v3.json / paper_trades_v3.csv
- blocked_setups_v3.json / blocked_setups_v3.csv
- prepare_validation_v3.json / prepare_validation_v3.csv
- setup_lifecycle_v3.json / setup_lifecycle_v3.csv

GitHub Actions
- 每 5 分鐘執行。
- git push 遇到 GitHub 暫時性錯誤時最多重試 3 次（10 秒、20 秒）。

Discord 原則
- 只保留有價值的 Prepare、Formal、TP/SL、重大新聞與真正系統錯誤。
- 小型內部狀態改變不額外洗版。

上線確認
手動 Run 後，log 最後應看到：
LASER_SIGNAL_CORE_3_0_INTEGRATED

第一次上線後，手動查詢中的 雷射訊號 3.0 正式單與 Prepare 樣本應從 0 開始。
