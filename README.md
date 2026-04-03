# 收據 LineBot（LINE 拍照上傳，自動寫入 Google Sheets）

這個專案可讓你把收據照片傳給 LINE Bot，系統會自動：

1. 下載 LINE 圖片訊息
2. 用 OpenAI 視覺模型辨識收據 / 發票內容
3. 抽出日期、店家、憑證編號、總計、品項等欄位
4. 寫入 Google Sheets
5. 自動維護 `登錄者ID -> 登錄者` 對照表

目前的 prompt 已經擴充為優先支援：

- 韓國收據
- 台灣一般收據
- 台灣電子發票 / 發票

## Google Sheet 結構

程式會自動建立或修正兩個工作表：

- `expenses`
  - 欄位：`登錄日期, 登錄者, 登錄者ID, 憑證編號, 日期, 店家, 品項, 數量, 單價, 複價, 總計, 幣別, 退稅狀態, 退稅金額, 退稅說明, LINE事件ID, LINE訊息ID`
- `user_mapping`
  - 欄位：`登錄者ID, 登錄者`
- `processed_events`
  - 欄位：`LINE事件ID, LINE訊息ID, 使用者ID, 狀態, 首次接收時間, 最後更新時間, 寫入列數, 錯誤訊息`

同一張收據若有多個品項，會寫入多列；`總計` 會重複出現在該張收據的每一列。
韓國收據的 `店家` 與 `品項` 會保留韓文原文，並在後面以括號附上繁體中文翻譯。

`登錄日期` 會依票據地區自動決定時區：

- 韓國收據：`Asia/Seoul`
- 台灣收據 / 台灣發票：`Asia/Taipei`
- 其他或無法判斷：使用 `APP_TIMEZONE`

## 環境變數

可使用 `.env` 或 Render 的環境變數 / Secret File：

```env
LINE_CHANNEL_SECRET=
LINE_CHANNEL_ACCESS_TOKEN=
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
GOOGLE_SHEET_ID=
GOOGLE_SERVICE_ACCOUNT_JSON=/etc/secrets/google-service-account.json
GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT=
APP_TIMEZONE=Asia/Taipei
```

`GOOGLE_SERVICE_ACCOUNT_JSON` 可填：

- 本機或 Render Secret File 的檔案路徑
- 直接貼完整 JSON 字串

`GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT` 可直接放 JSON 內容，若同時設定，會優先使用它。

## 本機啟動

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

macOS / Linux：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

本機測試時可搭配 ngrok：

```bash
ngrok http 8000
```

Webhook URL 設為：

```text
https://<你的對外網址>/callback
```

## LINE 設定步驟

1. 在 LINE Developers 建立 Messaging API channel。
2. 取得 `LINE_CHANNEL_SECRET` 與 `LINE_CHANNEL_ACCESS_TOKEN`。
3. 在 LINE Official Account Manager 關閉會干擾測試的自動回覆功能。
4. 在 LINE Developers 設定 Webhook URL 為 `/callback`。
5. 先用 Verify 測試 webhook 是否成功。

## Google Sheets 設定步驟

1. 建立 Google Cloud Service Account。
2. 啟用 Google Sheets API。
3. 下載 Service Account JSON 金鑰。
4. 將目標 Google Sheet 分享給 service account email，權限設為可編輯。
5. 記下 Google Sheet URL 中的 Sheet ID，填入 `GOOGLE_SHEET_ID`。
6. 本機可把 JSON 放在本機檔案並設定路徑；Render 可改用 Secret File。

## Render 部署

這個 repo 已附上 `render.yaml` 與 `.python-version`，建議用 Render Web Service 部署。

Render 建議設定：

- Runtime: `Python`
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/health`

Render 上需要設定的值：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `GOOGLE_SHEET_ID`
- `APP_TIMEZONE`

Google 憑證有兩種做法：

1. Secret File
   - 在 Render 的 Environment > Secret Files 上傳 JSON
   - 檔名建議用 `google-service-account.json`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` 設成 `/etc/secrets/google-service-account.json`
2. Environment Variable
   - 將完整 JSON 內容貼到 `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT`

## 使用流程

1. 使用者把收據照片或發票照片傳給 LINE Bot。
2. Webhook 先快速回應 LINE，避免逾時。
3. 背景程序抓取圖片、呼叫 OpenAI 做辨識。
4. 系統先用 `webhookEventId` 與 `message.id` 做去重，避免 redelivery 重複入帳。
5. 系統更新 `user_mapping` 工作表中的使用者名稱。
6. 系統把辨識結果逐列寫入 `expenses`。
7. Bot 再主動 push 一則摘要訊息給使用者。

## 補充 API

手動維護使用者名稱：

```http
POST /manual-register/{user_id}?name=王小明
```

健康檢查：

```http
GET /health
```

若環境變數未設好，`/health` 會回傳缺少哪些設定。

## 台灣 / 韓國收據辨識注意事項

- 台灣發票若使用民國年，程式會要求模型轉成西元日期。
- 台灣發票號碼會優先抓像 `AB12345678` 這類格式。
- 韓國收據若有 승인번호、거래번호、영수증번호，會盡量作為憑證編號。
- 登錄時間會依辨識出的 `source_region` 或 `currency` 自動切換為韓國或台灣時區。
- 韓國收據會將店家與品項顯示為 `原文(中文翻譯)`。
- 韓國退稅欄位為估計值，主要依收據內容、退稅標記與金額門檻判斷；旅客身分、停留時間與實際是否為退稅店，仍需人工確認。
- 已加入 LINE webhook redelivery 去重機制，會同時記錄 `LINE事件ID`、`LINE訊息ID` 與 `processed_events` 工作表。
- 若收據上沒有逐項品項，`items` 可能為空，但仍會盡量保留總計。
- 模糊、反光、裁切不完整的照片，辨識成功率會明顯下降。
