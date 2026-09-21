BTC Market Radar — V6.1 Structure / OKX

核心變更：
1. 保留原本 BREAKOUT / RETEST / OI / CVD Proxy / Volume / ATR / News / Forward Test。
2. 新增 15M 結構引擎：
   - 多頭：HH → HL
   - 空頭：LL → LH
3. 結構形成且回到合理區域時，先發：
   👀 結構準備（不建立 Forward Test，不代表直接進場）
4. HL/LH 之後重新突破短期 3 根 K 的 micro structure 時，才發：
   ⚡ HH/HL・LL/LH 延續訊號
   這筆會建立 Forward Test。
5. 正式 Swing Breakout 之後可作為 🔥 確認，不會重複建立第二筆交易。
6. 保留防追價：Early signal 距 EMA Zone > 0.75 ATR 不直接進場；正式 Breakout 仍維持 >1 ATR 不追。
7. 手動 Run 只查詢，不建立新樣本。
8. 不重置 V5 / V6 既有歷史資料。

重要：
- 這版的目標是把「前面已經形成 HH→HL / LL→LH 的回踩延續」提早抓出來，
  而不是只等最後的大突破。
- 結構判定仍使用 confirmed 2-2 swing，所以不是預知最低/最高點；HL/LH 需右側 2 根 15M K 確認。
- CVD Proxy 仍是 OKX 1m candle-based proxy，不是真逐筆 CVD。

GitHub 使用：
- 用壓縮檔內 market_radar.py 覆蓋原本 market_radar.py。
- Workflow 保持：python market_radar.py
- 不要刪 probability_state.json、forward_test_v5/v6 JSON/CSV、Secrets。

V6.1 Structure Alert patch
- 正式 Swing Breakout 若已成立，但因 EMA 距離 / 延伸 / Flow / Volume / SL-RR 等條件不適合直接進場：
  Discord 仍會發「👀 爆發預警」。
- 爆發預警只通知，不建立 Entry、不建立 Forward Test。
- 同時維持 breakout_watch，等待第一個有效 RETEST。
- 若先前已有 CONTINUATION 訊號，後續正式 Swing Breakout 會發「🔥 突破確認」；
  即使當下已延伸過遠，也只作為原訊號確認，不會建立第二筆交易。
