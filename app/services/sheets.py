from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import gspread
from google.oauth2.service_account import Credentials

from app.models.receipt import ReceiptExtraction

logger = logging.getLogger(__name__)

EXPENSE_HEADERS = [
    "登錄日期",
    "登錄者",
    "登錄者ID",
    "憑證編號",
    "日期",
    "店家",
    "品項",
    "交易類型",
    "數量",
    "單價",
    "複價",
    "複價(台幣)",
    "總計",
    "幣別",
    "退稅狀態",
    "退稅金額",
    "退稅說明",
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
ANALYSIS_HEADERS = ["登錄者", "交易類型", "金額台幣"]


class GoogleSheetsService:
    def __init__(self) -> None:
        spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
        if not spreadsheet_id:
            raise ValueError("GOOGLE_SHEET_ID 尚未設定")

        try:
            self.local_tz = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Taipei"))
        except ZoneInfoNotFoundError:
            self.local_tz = ZoneInfo("UTC")

        self.fx_rates = self._load_fx_rates()
        credentials = self._load_credentials()
        gc = gspread.authorize(credentials)
        self.sheet = gc.open_by_key(spreadsheet_id)
        self.expenses_ws = self._ensure_worksheet("expenses", EXPENSE_HEADERS)
        self.mapping_ws = self._ensure_worksheet("user_mapping", MAPPING_HEADERS)
        self.events_ws = self._ensure_worksheet("processed_events", EVENT_HEADERS)
        self.analysis_ws = self._ensure_worksheet("category_analysis", ANALYSIS_HEADERS)

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

    @staticmethod
    def _load_fx_rates() -> dict[str, Decimal]:
        return {
            "TWD": GoogleSheetsService._read_decimal_env("FX_RATE_TWD_TO_TWD", "1"),
            "KRW": GoogleSheetsService._read_decimal_env("FX_RATE_KRW_TO_TWD", "0.024"),
            "USD": GoogleSheetsService._read_decimal_env("FX_RATE_USD_TO_TWD", "32"),
        }

    @staticmethod
    def _read_decimal_env(key: str, default: str) -> Decimal:
        raw = os.getenv(key, default).strip()
        try:
            return Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError(f"{key} 不是有效的數字") from exc

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
        merchant_display = self._format_korean_translation(
            source_region=receipt.source_region,
            original=receipt.merchant_name,
            translated=receipt.merchant_name_zh,
        )
        receipt_category = (receipt.transaction_category or "其他").strip() or "其他"
        tax_refund_status = self._format_tax_refund_status(receipt.tax_refund_status)
        tax_refund_amount = "" if receipt.tax_refund_amount is None else str(receipt.tax_refund_amount)
        tax_refund_note = receipt.tax_refund_note or ""
        rows = []
        if not receipt.items:
            line_total_twd = self._convert_amount_to_twd(receipt.total_amount, receipt.currency)
            rows.append(
                [
                    register_date,
                    registrant,
                    user_id,
                    receipt.receipt_number or "",
                    receipt.receipt_date or "",
                    merchant_display,
                    "",
                    receipt_category,
                    "",
                    "",
                    "",
                    "" if line_total_twd is None else self._format_decimal(line_total_twd),
                    "" if receipt.total_amount is None else str(receipt.total_amount),
                    receipt.currency,
                    tax_refund_status,
                    tax_refund_amount,
                    tax_refund_note,
                    event_id,
                    message_id,
                ]
            )
        else:
            for item in receipt.items:
                item_display = self._format_korean_translation(
                    source_region=receipt.source_region,
                    original=item.item_name,
                    translated=item.item_name_zh,
                )
                item_category = (item.transaction_category or receipt_category).strip() or "其他"
                line_total_twd = self._convert_amount_to_twd(item.line_total, receipt.currency)
                rows.append(
                    [
                        register_date,
                        registrant,
                        user_id,
                        receipt.receipt_number or "",
                        receipt.receipt_date or "",
                        merchant_display,
                        item_display,
                        item_category,
                        str(item.quantity),
                        str(item.unit_price),
                        str(item.line_total),
                        "" if line_total_twd is None else self._format_decimal(line_total_twd),
                        "" if receipt.total_amount is None else str(receipt.total_amount),
                        receipt.currency,
                        tax_refund_status,
                        tax_refund_amount,
                        tax_refund_note,
                        event_id,
                        message_id,
                    ]
                )

        self.expenses_ws.append_rows(rows, value_input_option="USER_ENTERED")
        try:
            self.refresh_category_analysis()
        except Exception:
            logger.exception("Failed to refresh category analysis sheet")
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

    @staticmethod
    def _format_korean_translation(source_region: str | None, original: str | None, translated: str | None) -> str:
        original_text = (original or "").strip()
        translated_text = (translated or "").strip()

        if (source_region or "").strip().upper() != "KR":
            return original_text or translated_text
        if translated_text and original_text and translated_text != original_text:
            return f"{translated_text}({original_text})"
        if translated_text:
            return translated_text
        return original_text

    @staticmethod
    def _format_tax_refund_status(status: str | None) -> str:
        normalized = (status or "").strip().lower()
        if normalized == "eligible":
            return "可退稅"
        if normalized == "not_eligible":
            return "不可退稅"
        if normalized == "unknown":
            return "無法判定"
        return ""

    def _convert_amount_to_twd(self, amount: Decimal | str | None, currency: str | None) -> Decimal | None:
        if amount in (None, ""):
            return None
        try:
            amount_decimal = Decimal(str(amount))
        except InvalidOperation:
            return None

        rate = self.fx_rates.get((currency or "").strip().upper())
        if rate is None:
            return None
        return (amount_decimal * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        return format(value, "f")

    def refresh_category_analysis(self) -> None:
        records = self.expenses_ws.get_all_records()
        aggregates = self._build_category_aggregates(records)

        rows: list[list[str]] = [ANALYSIS_HEADERS]
        chart_blocks: list[tuple[str, int, int]] = []
        current_row = 2

        datasets: list[tuple[str, dict[str, Decimal]]] = []
        overall = self._aggregate_overall(aggregates)
        if overall:
            datasets.append(("全部", overall))
        datasets.extend(sorted(aggregates.items(), key=lambda item: item[0]))

        for registrant, categories in datasets:
            if not categories:
                continue
            start_row = current_row
            for category, amount in sorted(categories.items(), key=lambda item: (-item[1], item[0])):
                rows.append([registrant, category, self._format_decimal(amount)])
                current_row += 1
            chart_blocks.append((registrant, start_row, current_row - 1))

        self.analysis_ws.clear()
        self.analysis_ws.update("A1", rows)
        self._rebuild_analysis_charts(chart_blocks)

    @staticmethod
    def _build_category_aggregates(records: list[dict[str, str]]) -> dict[str, dict[str, Decimal]]:
        aggregates: dict[str, dict[str, Decimal]] = {}
        for record in records:
            registrant = str(record.get("登錄者", "")).strip()
            category = str(record.get("交易類型", "")).strip() or "其他"
            amount_text = str(record.get("複價(台幣)", "")).strip()
            if not registrant or not amount_text:
                continue
            try:
                amount = Decimal(amount_text)
            except InvalidOperation:
                continue
            registrant_bucket = aggregates.setdefault(registrant, {})
            registrant_bucket[category] = registrant_bucket.get(category, Decimal("0")) + amount
        return aggregates

    @staticmethod
    def _aggregate_overall(aggregates: dict[str, dict[str, Decimal]]) -> dict[str, Decimal]:
        overall: dict[str, Decimal] = {}
        for categories in aggregates.values():
            for category, amount in categories.items():
                overall[category] = overall.get(category, Decimal("0")) + amount
        return overall

    def _rebuild_analysis_charts(self, chart_blocks: list[tuple[str, int, int]]) -> None:
        fetch_metadata = getattr(self.sheet, "fetch_sheet_metadata", None)
        if not callable(fetch_metadata):
            logger.warning("gspread fetch_sheet_metadata is unavailable; skipping chart rebuild")
            return

        metadata = fetch_metadata()
        analysis_meta = None
        for sheet_meta in metadata.get("sheets", []):
            if sheet_meta.get("properties", {}).get("sheetId") == self.analysis_ws.id:
                analysis_meta = sheet_meta
                break

        requests: list[dict[str, object]] = []
        if analysis_meta:
            for chart in analysis_meta.get("charts", []):
                chart_id = chart.get("chartId")
                if chart_id is not None:
                    requests.append({"deleteEmbeddedObject": {"objectId": chart_id}})

        for index, (registrant, start_row, end_row) in enumerate(chart_blocks):
            requests.append(
                {
                    "addChart": {
                        "chart": {
                            "spec": {
                                "title": f"{registrant} 交易類型金額占比",
                                "pieChart": {
                                    "legendPosition": "RIGHT_LEGEND",
                                    "domain": {
                                        "sourceRange": {
                                            "sources": [
                                                {
                                                    "sheetId": self.analysis_ws.id,
                                                    "startRowIndex": start_row - 1,
                                                    "endRowIndex": end_row,
                                                    "startColumnIndex": 1,
                                                    "endColumnIndex": 2,
                                                }
                                            ]
                                        }
                                    },
                                    "series": {
                                        "sourceRange": {
                                            "sources": [
                                                {
                                                    "sheetId": self.analysis_ws.id,
                                                    "startRowIndex": start_row - 1,
                                                    "endRowIndex": end_row,
                                                    "startColumnIndex": 2,
                                                    "endColumnIndex": 3,
                                                }
                                            ]
                                        }
                                    },
                                },
                            },
                            "position": {
                                "overlayPosition": {
                                    "anchorCell": {
                                        "sheetId": self.analysis_ws.id,
                                        "rowIndex": index * 18,
                                        "columnIndex": 4,
                                    },
                                    "widthPixels": 640,
                                    "heightPixels": 360,
                                }
                            },
                        }
                    }
                }
            )

        if requests:
            self.sheet.batch_update({"requests": requests})
