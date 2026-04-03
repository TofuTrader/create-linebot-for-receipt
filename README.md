# 韓國收據 LineBot（自動登錄 Google Sheets）

這個專案可讓你在 LINE 上傳收據照片後，自動：

1. OCR + AI 分析收據明細
2. 抽出欄位：憑證編號、日期、店家、品項、數量、單價、複價
3. 寫入 Google Sheets，並在最前面加上：登錄日期、登錄者
4. 自動維護 `登錄者ID -> 登錄者` 對應表

---

## Google Sheet 結構

程式會自動建立（或修正）兩個工作表：

- `expenses`
  - 欄位：`登錄日期, 登錄者, 登錄者ID, 憑證編號, 日期, 店家, 品項, 數量, 單價, 複價, 幣別`
- `user_mapping`
  - 欄位：`登錄者ID, 登錄者`

---

## 1) 安裝

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

---

## 2) 設定 LINE Bot

在 LINE Developers 建立 Messaging API channel，取得：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`

Webhook URL 指向：

```text
https://<你的網域>/callback
```

> 本地測試可用 ngrok： `ngrok http 8000`

---

## 3) 設定 Google Sheets

1. 建立 Google Cloud Service Account
2. 下載 JSON 金鑰
3. 將目標 Google Sheet 分享給 service account email（編輯權限）
4. 在 `.env` 設定：
   - `GOOGLE_SHEET_ID`
   - `GOOGLE_SERVICE_ACCOUNT_JSON`（JSON 檔案絕對路徑）

---

## 4) 設定 OpenAI

在 `.env` 填入：

- `OPENAI_API_KEY`
- `OPENAI_MODEL`（預設 `gpt-4.1-mini`）

---

## 5) 啟動

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 6) 使用流程

1. 使用者在 LINE 傳送「收據照片」給 Bot
2. Bot 下載圖片並進行明細抽取
3. Bot 依使用者 LINE profile 更新 `user_mapping`
4. Bot 把每一個品項寫入 `expenses`（同張收據可能多列）
5. Bot 回覆登錄結果摘要

---

## 補充 API

### 手動維護對應

```http
POST /manual-register/{user_id}?name=王小明
```

可手動新增/更新 `登錄者ID -> 登錄者` 對應。

### 健康檢查

```http
GET /health
```

---

## 注意事項

- 韓國收據格式很多，建議累積實例後微調 prompt。
- 若收據影像模糊，欄位會回傳 `null` 或空值。
- 若店家在韓文顯示，會原樣保留。
