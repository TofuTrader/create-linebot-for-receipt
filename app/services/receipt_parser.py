from __future__ import annotations

import base64
import json
import os
from typing import Any

from openai import OpenAI

from app.models.receipt import ReceiptExtraction


class ReceiptParser:
    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY 尚未設定")
        self.client = OpenAI(api_key=api_key)
        self.model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    def parse_receipt(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> ReceiptExtraction:
        b64 = base64.b64encode(image_bytes).decode("utf-8")

        prompt = (
            "你是收據辨識助手。請從韓國收據、台灣收據或台灣電子發票圖片抽取欄位，並僅輸出 JSON。\n"
            "JSON schema: {"
            '"receipt_number": string|null, '
            '"receipt_date": "YYYY-MM-DD"|null, '
            '"merchant_name": string|null, '
            '"merchant_name_zh": string|null, '
            '"transaction_category": string|null, '
            '"total_amount": number|null, '
            '"source_region": "KR"|"TW"|null, '
            '"currency": "KRW"|"USD"|"TWD"|string, '
            '"tax_refund_status": "eligible"|"not_eligible"|"unknown"|null, '
            '"tax_refund_amount": number|null, '
            '"tax_refund_note": string|null, '
            '"items": [{"item_name": string, "item_name_zh": string|null, "transaction_category": string|null, "quantity": number, "unit_price": number, "line_total": number}], '
            '"raw_text": string|null'
            "}.\n"
            "規則: "
            "1) 支援韓文、繁體中文、英文。"
            "2) 若為台灣發票的民國年日期，請轉成西元 YYYY-MM-DD。"
            "3) 台灣發票的 receipt_number 優先抓兩碼英文字加 8 碼數字的發票號碼。"
            "4) 韓國收據若有 승인번호、거래번호 或 영수증번호，可作為 receipt_number。"
            "5) 韓國收據時，merchant_name 保留韓文原文，merchant_name_zh 填繁體中文翻譯；"
            "items 的 item_name 保留原文，item_name_zh 填繁體中文翻譯。"
            "若不是韓國收據，中文翻譯欄位填 null。"
            "6) transaction_category 與 item 的 transaction_category 請從以下類型中挑最適合者："
            "餐飲、服飾、交通、住宿、美妝保養、藥妝醫療、超市便利商店、家居雜貨、電子產品、伴手禮禮品、娛樂、服務、其他。"
            "若有明細，優先替每個 item 分類；receipt 層級的 transaction_category 可用整體最主要類型。"
            "7) 若沒有逐項品項，items 可為空陣列，但 total_amount 仍要盡量填出。"
            "8) source_region 若為韓國填 KR，台灣填 TW，無法判斷填 null。"
            "9) quantity 預設 1。"
            "10) unit_price、line_total、total_amount、tax_refund_amount 只保留數字，不含逗號與幣號。"
            "11) 幣別請依內容推斷，台灣優先 TWD，韓國優先 KRW。"
            "12) 韓國退稅判斷僅針對商品購買收據做估計：通常需達 15,000 KRW 以上、屬可退稅商品、且來自退稅店或有退稅憑單/即時退稅資訊。"
            "餐飲熟食與一般服務通常不可退稅。若影像無法確認是否為退稅店、是否為旅客本人可退、或是否具備退稅憑單，tax_refund_status 請填 unknown。"
            "13) 若收據上明確顯示 TAX REFUND / TAX FREE / 사후면세 / 즉시환급 / refund amount，優先依其內容判斷與填 tax_refund_amount。"
            "14) 若是韓國收據且可從稅額欄位合理估計退稅金額，可填估計值，並在 tax_refund_note 說明是估計。"
            "15) 若看不清楚填 null。"
        )

        response = self.client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime_type};base64,{b64}",
                        },
                    ],
                }
            ],
            temperature=0,
        )

        text = self._extract_text(response)
        data = self._safe_json_loads(text)
        data = self._post_process(data)
        return ReceiptExtraction.model_validate(data)

    @staticmethod
    def _extract_text(response: Any) -> str:
        text_chunks: list[str] = []
        for output in getattr(response, "output", []):
            for content in getattr(output, "content", []):
                if getattr(content, "type", "") == "output_text":
                    text_chunks.append(getattr(content, "text", ""))
        return "\n".join(text_chunks).strip()

    @staticmethod
    def _safe_json_loads(raw: str) -> dict[str, Any]:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.replace("json", "", 1).strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start : end + 1]
        return json.loads(raw)

    @staticmethod
    def _post_process(data: dict[str, Any]) -> dict[str, Any]:
        valid_categories = {
            "餐飲",
            "服飾",
            "交通",
            "住宿",
            "美妝保養",
            "藥妝醫療",
            "超市便利商店",
            "家居雜貨",
            "電子產品",
            "伴手禮禮品",
            "娛樂",
            "服務",
            "其他",
        }
        source_region = str(data.get("source_region") or "").strip().upper()
        currency = str(data.get("currency") or "").strip().upper()

        if source_region == "KR" or currency == "KRW":
            data["source_region"] = "KR"
        elif source_region == "TW" or currency == "TWD":
            data["source_region"] = "TW"

        status = data.get("tax_refund_status")
        if status is not None:
            status = str(status).strip().lower()
            if status not in {"eligible", "not_eligible", "unknown"}:
                status = "unknown"
            data["tax_refund_status"] = status

        data["transaction_category"] = ReceiptParser._normalize_category(
            data.get("transaction_category"),
            valid_categories,
        )

        items = data.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    item["transaction_category"] = ReceiptParser._normalize_category(
                        item.get("transaction_category"),
                        valid_categories,
                    )

        total_amount = data.get("total_amount")
        if data.get("source_region") == "KR":
            if not data.get("tax_refund_status"):
                data["tax_refund_status"] = "unknown"
            if total_amount is not None:
                try:
                    total_value = float(total_amount)
                except (TypeError, ValueError):
                    total_value = None
            else:
                total_value = None

            note = str(data.get("tax_refund_note") or "").strip()
            if total_value is not None and total_value < 15000:
                data["tax_refund_status"] = "not_eligible"
                if not note:
                    data["tax_refund_note"] = "未達韓國退稅常見門檻 15,000 KRW"

        return data

    @staticmethod
    def _normalize_category(value: Any, valid_categories: set[str]) -> str:
        text = str(value or "").strip()
        return text if text in valid_categories else "其他"
