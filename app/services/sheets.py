from __future__ import annotations

import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from app.models.receipt import ReceiptExtraction

EXPENSE_HEADERS = [
    "登錄日期",
    "登錄者",
    "登錄者ID",
    "憑證編號",
    "日期",
    "店家",
    "品項",
    "數量",
    "單價",
    "複價",
    "幣別",
]

MAPPING_HEADERS = ["登錄者ID", "登錄者"]


class GoogleSheetsService:
    def __init__(self) -> None:
        spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
        creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not spreadsheet_id:
            raise ValueError("GOOGLE_SHEET_ID 尚未設定")
        if not creds_path:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON 尚未設定")

        credentials = Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(credentials)
        self.sheet = gc.open_by_key(spreadsheet_id)
        self.expenses_ws = self._ensure_worksheet("expenses", EXPENSE_HEADERS)
        self.mapping_ws = self._ensure_worksheet("user_mapping", MAPPING_HEADERS)

    def _ensure_worksheet(self, title: str, headers: list[str]):
        try:
            ws = self.sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title=title, rows=1000, cols=30)

        row1 = ws.row_values(1)
        if row1 != headers:
            ws.update("A1", [headers])
        return ws

    def get_display_name(self, user_id: str) -> str:
        records = self.mapping_ws.get_all_records()
        for r in records:
            if str(r.get("登錄者ID", "")).strip() == user_id:
                return str(r.get("登錄者", "")).strip() or user_id
        return user_id

    def upsert_user_mapping(self, user_id: str, display_name: str) -> None:
        values = self.mapping_ws.get_all_values()
        for idx, row in enumerate(values[1:], start=2):
            if len(row) > 0 and row[0] == user_id:
                self.mapping_ws.update(f"B{idx}", [[display_name]])
                return
        self.mapping_ws.append_row([user_id, display_name])

    def append_receipt(self, user_id: str, registrant: str, receipt: ReceiptExtraction) -> int:
        register_date = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        if not receipt.items:
            rows.append(
                [
                    register_date,
                    registrant,
                    user_id,
                    receipt.receipt_number or "",
                    receipt.receipt_date or "",
                    receipt.merchant_name or "",
                    "",
                    "",
                    "",
                    "",
                    receipt.currency,
                ]
            )
        else:
            for item in receipt.items:
                rows.append(
                    [
                        register_date,
                        registrant,
                        user_id,
                        receipt.receipt_number or "",
                        receipt.receipt_date or "",
                        receipt.merchant_name or "",
                        item.item_name,
                        str(item.quantity),
                        str(item.unit_price),
                        str(item.line_total),
                        receipt.currency,
                    ]
                )

        self.expenses_ws.append_rows(rows, value_input_option="USER_ENTERED")
        return len(rows)
