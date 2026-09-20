BTC Market Radar V6 OKX 正式版

主資料源：OKX BTC-USDT-SWAP
- 15M / 1H K線：OKX
- Price / EMA34/50 / EMA Zone / Swing / ATR：OKX
- Volume：OKX K線成交量
- OI：OKX 公開 OI 快照，逐次累積 1H / 3H 歷史
- CVD：目前明確標記無資料，不用假的 CVD 影響訊號
- V5.2 forward_test_v5.json / csv：不覆寫
- V6 forward_test_v6.json / csv：獨立輸出

第一次上線：
OI 需要實際累積。約 1 小時後才有 1H 變化，約 3 小時後才有 3H 背景。
這是刻意設計，避免用單一快照偽造歷史。

安裝：
1. ZIP 內 market_radar.py 直接取代 GitHub 正式 market_radar.py。
2. workflow 改回：python market_radar.py
3. market_data_source_test.py 可刪除。
4. JSON / CSV / Secrets 不要刪。
5. 手動 Run workflow 一次確認 Discord。
