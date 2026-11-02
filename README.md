# foodsec - 糧食安全儀表板（A 路線，第一階段）

目的：用公開資料做台灣糧食供應的監測與預警，先從「批發市場價格異常」開始，因為它是唯一有免金鑰即時 API 的資料源。
純 Python 標準庫，不需安裝套件；輸出單一 HTML 檔，瀏覽器直接開，Power BI 也可以直接接 SQLite。

## 檔案

| 檔 | 用途 |
|---|---|
| `fetch_prices.py` | 抓農業部批發市場每日行情到 `data/foodsec.db`（PRICE_DAILY、FETCH_LOG） |
| `build_dashboard.py` | 從資料庫算指標，輸出 `docs/index.html`（自包含，資料內嵌）與 `docs/data.json` |
| `watch_crops.txt` | 重點監測作物名單與種植參數 |
| `markets.txt` | 批發市場清單 |
| `input/` | 手動匯入的年報與進口統計 CSV 範本（第二階段） |
| `docs/` | 儀表板產出，進版控，GitHub Pages 從這裡發佈 |
| `data/` | SQLite 與抓取記錄，不進版控 |

## 使用

```powershell
cd 7_Other\AgriAI\foodsec
py -3.14 -I fetch_prices.py --days 400      # 第一次抓一年多，約 40 分鐘；之後 --days 7 增量
py -3.14 -I build_dashboard.py              # 產 docs\index.html，瀏覽器直接開
```

## 放上 GitHub Pages

這個資料夾自己是一個公開 repo（Pages 免費方案只給公開 repo），不含 App 與商業評估。

1. GitHub Desktop：File → Add local repository → 選這個 foodsec 資料夾 → 提示不是 repo 時按 create a repository here → 名稱 foodsec、Git ignore 選 None（已有）、License 選 MIT → Create repository。
2. 左下 Summary 填「foodsec 糧食供應監測儀表板初版」→ Commit to main。
3. 上方 Publish repository → 取消勾選 Keep this code private → Publish。
4. 瀏覽器開 github.com 的這個 repo → Settings → Pages → Build and deployment 的 Source 選 Deploy from a branch → Branch 選 main、資料夾選 /docs → Save。
5. 等一到三分鐘，網址是 https://你的帳號.github.io/foodsec/ ，Settings → Pages 頁面上方會顯示。

之後更新：本機跑 fetch 與 build → GitHub Desktop 看到 docs/index.html 與 data.json 變更 → Commit → Push origin，幾分鐘後 Pages 自動換新。

## 自動更新（GitHub Actions）

`.github/workflows/update.yml` 每天台灣時間 20:00 在 GitHub 雲端跑：抓近 10 天增量、產頁面、由 github-actions 機器人把 docs 推回 main，Pages 自動換新。資料庫用 Actions 快取保留，快取失效就全抓一次。

- 第一次：commit 這個檔推上去後，到 repo 的 Actions 頁 → 左側 update-dashboard → 右側 Run workflow 手動跑一次，看綠勾。第一次沒有快取會全抓，約 40 分鐘。
- 之後每天自動跑；Actions 頁可看每次紀錄，紅叉點進去看哪一步失敗。
- 排程時間改 cron 那行，格式是 UTC，台灣時間減 8 小時。
- 本機就不必再手動跑；真要手動，本機跑完 commit 推上去也可以，兩邊不衝突，機器人下次會在你的版本上接著更新。

## 儀表板視角

左側按鈕切換，同一份資料不同呈現，網址加 #buy、#farm、#media、#gov、#grow 可直接開到該視角。

| 視角 | 給誰 | 看什麼 |
|---|---|---|
| 家庭與餐飲採購 | 買菜的人 | 本週偏貴、正常、偏便宜三欄，加全表（預設依 vs 30 天排序） |
| 農民產銷 | 種的人 | 供應警示、量縮與量增最多的作物、去年同期，全表預設依量變化排序 |
| 媒體 | 寫新聞的人 | 依變動幅度排序的可引用句子，附資料日期 |
| 政府與農會 | 調節供需的人 | 監測作物整體交易量對近 30 天與去年同期、供應吃緊與供過於求清單、第二階段資料佔位 |
| 我要種什麼 | 自己種的人 | 露地表（含適種月份與現在合不合適）與家用水耕表，附預計採收日與去年採收期價格 |

買菜等級：近 7 天對近 30 天高 15% 以上偏貴、低 15% 以上偏便宜，z 分數 ±1 也歸類；資料未滿 30 天不分類。
表格每欄可點標題排序。

`watch_crops.txt` 格式：`主名|顯示名|群組|家用水耕天數|露地天數|露地適種月份`，天數空白就不進對應的種植表；月份寫 全年、9-3（跨年）或 3-5,9-11。

## 抓取規則與已知限制

- 非會員每次請求只回第一頁 1000 筆，所以腳本按「市場 × 2 天」一個請求，滿 1000 筆自動拆成單日重抓；單日仍滿會在 FETCH_LOG 記 WARN。
- 市場清單 `markets.txt` 前 15 個是實際看到的，後面是候選；跑完看 FETCH_LOG 哪些永遠 0 筆就刪掉。
- 作物名稱格式「主名-品種」，儀表板依主名合併品種、排除名稱含「進口」者。重點作物在 `watch_crops.txt`。
- Python 3.13 起預設嚴格憑證檢查，農業部憑證鏈缺 Subject Key Identifier，腳本只關掉那個旗標，CA 與主機名驗證照常。
- 「休市」列會略過。

## 資料源

| 資料 | 來源 | 取得方式 | 狀態 |
|---|---|---|---|
| 批發市場每日行情 | 農業部資料開放平台 AgriProductsTransType | 免金鑰 API，民國年日期參數 | 已接 |
| 糧食供需年報（自給率） | 農業部統計 | PDF，手動轉 CSV 放 `input/` | 第二階段 |
| 農產貿易統計（黃豆小麥玉米進口） | 農產貿易統計查詢系統 | 網頁查詢手動匯出 | 第二階段 |
| 氣象觀測 | 氣象署開放平台 | 需授權碼，放環境變數 `CWA_API_KEY` 自己申請 | 第二階段 |
| 公糧庫存 | 無開放資料 | 人工輸入警示欄 | 不做 |
