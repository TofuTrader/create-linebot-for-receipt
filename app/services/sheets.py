from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    "總計",
    "幣別",
    "LINE事件ID",
    "LINE訊息ID",
]

MAPPING_HEADERS = ["登錄者ID", "登錄者"]
EVENT_HEADERS = [
    "LINE事件ID",
    "LINE訊息ID",
    "使用者ID",
    "狀態",
    "首次接收時間",
    "最後更新時間",
    "寫入列數",
    "錯誤訊息",
]
EVENT_STATUS_PROCESSING = "processing"
EVENT_STATUS_DONE = "done"
EVENT_STATUS_FAILED = "failed"
EVENT_PROCESSING_STALE_MINUTES = 15


class GoogleSheetsService:
    def __init__(self) -> None:
        spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
        if not spreadsheet_id:
            raise ValueError("GOOGLE_SHEET_ID 尚未設定")

        try:
            self.local_tz = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Taipei"))
        except ZoneInfoNotFoundError:
            self.local_tz = ZoneInfo("UTC")

        credentials = self._load_credentials()
        gc = gspread.authorize(credentials)
        self.sheet = gc.open_by_key(spreadsheet_id)
        self.expenses_ws = self._ensure_worksheet("expenses", EXPENSE_HEADERS)
        self.mapping_ws = self._ensure_worksheet("user_mapping", MAPPING_HEADERS)
        self.events_ws = self._ensure_worksheet("processed_events", EVENT_HEADERS)

    @staticmethod
    def _load_credentials() -> Credentials:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]

        raw_content = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT", "").strip()
        if raw_content:
            return Credentials.from_service_account_info(json.loads(raw_content), scopes=scopes)

        raw_value = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if not raw_value:
            raise ValueError("請先設定 GOOGLE_SERVICE_ACCOUNT_JSON 或 GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT")

        candidate_path = Path(raw_value)
        if candidate_path.exists():
            return Credentials.from_service_account_file(str(candidate_path), scopes=scopes)

        if raw_value.startswith("{"):
            return Credentials.from_service_account_info(json.loads(raw_value), scopes=scopes)

        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON 必須是檔案路徑或 JSON 內容")

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

    def begin_event_processing(self, event_id: str, message_id: str, user_id: str) -> bool:
        existing_rows = self.events_ws.get_all_values()
        row_index = self._find_event_row_index(existing_rows, event_id)
        now_str = self._now_str()

        existing_written_rows = self._count_expense_rows_by_message_id(message_id)
        if existing_written_rows > 0:
            self._upsert_event_row(
                existing_rows=existing_rows,
                row_index=row_index,
                event_id=event_id,
                message_id=message_id,
                user_id=user_id,
                status=EVENT_STATUS_DONE,
                inserted_rows=existing_written_rows,
                error_message="",
                now_str=now_str,
            )
            return False

        if row_index is None:
            self.events_ws.append_row(
                [event_id, message_id, user_id, EVENT_STATUS_PROCESSING, now_str, now_str, "", ""]
            )
            return True

        row = existing_rows[row_index - 1]
        status = row[3].strip().lower() if len(row) > 3 else ""
        last_updated = row[5].strip() if len(row) > 5 else ""

        if status == EVENT_STATUS_DONE:
            return False

        if status == EVENT_STATUS_PROCESSING and not self._is_processing_stale(last_updated):
            return False

        self._upsert_event_row(
            existing_rows=existing_rows,
            row_index=row_index,
            event_id=event_id,
            message_id=message_id,
            user_id=user_id,
            status=EVENT_STATUS_PROCESSING,
            inserted_rows="",
            error_message="",
            now_str=now_str,
        )
        return True

    def mark_event_processed(self, event_id: str, message_id: str, inserted_rows: int) -> None:
        existing_rows = self.events_ws.get_all_values()
        row_index = self._find_event_row_index(existing_rows, event_id)
        self._upsert_event_row(
            existing_rows=existing_rows,
            row_index=row_index,
            event_id=event_id,
            message_id=message_id,
            user_id="",
            status=EVENT_STATUS_DONE,
            inserted_rows=inserted_rows,
            error_message="",
            now_str=self._now_str(),
        )

    def mark_event_failed(self, event_id: str, message_id: str, error_message: str) -> None:
        existing_rows = self.events_ws.get_all_values()
        row_index = self._find_event_row_index(existing_rows, event_id)
        self._upsert_event_row(
            existing_rows=existing_rows,
            row_index=row_index,
            event_id=event_id,
            message_id=message_id,
            user_id="",
            status=EVENT_STATUS_FAILED,
            inserted_rows="",
            error_message=error_message,
            now_str=self._now_str(),
        )

    def append_receipt(
        self,
        user_id: str,
        registrant: str,
        receipt: ReceiptExtraction,
        *,
        event_id: str,
        message_id: str,
    ) -> int:
        register_date = datetime.now(self._resolve_receipt_timezone(receipt)).strftime("%Y-%m-%d %H:%M:%S")
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
                    "" if receipt.total_amount is None else str(receipt.total_amount),
                    receipt.currency,
                    event_id,
                    message_id,
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
                        "" if receipt.total_amount is None else str(receipt.total_amount),
                        receipt.currency,
                        event_id,
                        message_id,
                    ]
                )

        self.expenses_ws.append_rows(rows, value_input_option="USER_ENTERED")
        return len(rows)

    def _resolve_receipt_timezone(self, receipt: ReceiptExtraction) -> ZoneInfo:
        region = (receipt.source_region or "").strip().upper()
        if region == "KR":
            return ZoneInfo("Asia/Seoul")
        if region == "TW":
            return ZoneInfo("Asia/Taipei")

        currency = (receipt.currency or "").strip().upper()
        if currency == "KRW":
            return ZoneInfo("Asia/Seoul")
        if currency == "TWD":
            return ZoneInfo("Asia/Taipei")

        return self.local_tz

    def _now_str(self) -> str:
        return datetime.now(self.local_tz).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _find_event_row_index(rows: list[list[str]], event_id: str) -> int | None:
        for idx, row in enumerate(rows[1:], start=2):
            if len(row) > 0 and row[0] == event_id:
                return idx
        return None

    def _upsert_event_row(
        self,
        *,
        existing_rows: list[list[str]],
        row_index: int | None,
        event_id: str,
        message_id: str,
        user_id: str,
        status: str,
        inserted_rows: int | str,
        error_message: str,
        now_str: str,
    ) -> None:
        first_seen = now_str
        existing_user_id = user_id
        if row_index is not None:
            existing_row = existing_rows[row_index - 1]
            if len(existing_row) > 2 and existing_row[2].strip():
                existing_user_id = existing_row[2].strip()
            if len(existing_row) > 4 and existing_row[4].strip():
                first_seen = existing_row[4].strip()

        payload = [
            event_id,
            message_id,
            existing_user_id,
            status,
            first_seen,
            now_str,
            str(inserted_rows) if inserted_rows != "" else "",
            error_message,
        ]

        if row_index is None:
            self.events_ws.append_row(payload)
        else:
            self.events_ws.update(f"A{row_index}:H{row_index}", [payload])

    def _count_expense_rows_by_message_id(self, message_id: str) -> int:
        message_col = self.expenses_ws.col_values(len(EXPENSE_HEADERS))
        return sum(1 for value in message_col[1:] if value == message_id)

    def _is_processing_stale(self, last_updated: str) -> bool:
        if not last_updated:
            return True
        try:
            updated_at = datetime.strptime(last_updated, "%Y-%m-%d %H:%M:%S").replace(tzinfo=self.local_tz)
        except ValueError:
            return True
        return datetime.now(self.local_tz) - updated_at > timedelta(minutes=EVENT_PROCESSING_STALE_MINUTES)
