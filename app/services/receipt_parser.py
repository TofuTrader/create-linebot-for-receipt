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
            '"total_amount": number|null, '
            '"source_region": "KR"|"TW"|null, '
            '"currency": "KRW"|"USD"|"TWD"|string, '
            '"items": [{"item_name": string, "quantity": number, "unit_price": number, "line_total": number}], '
            '"raw_text": string|null'
            "}.\n"
            "規則: "
            "1) 支援韓文、繁體中文、英文。"
            "2) 若為台灣發票的民國年日期，請轉成西元 YYYY-MM-DD。"
            "3) 台灣發票的 receipt_number 優先抓兩碼英文字加 8 碼數字的發票號碼。"
            "4) 韓國收據若有 승인번호、거래번호 或 영수증번호，可作為 receipt_number。"
            "5) 若沒有逐項品項，items 可為空陣列，但 total_amount 仍要盡量填出。"
            "6) source_region 若為韓國填 KR，台灣填 TW，無法判斷填 null。"
            "7) quantity 預設 1。"
            "8) unit_price、line_total、total_amount 只保留數字，不含逗號與幣號。"
            "9) 幣別請依內容推斷，台灣優先 TWD，韓國優先 KRW。"
            "10) 若看不清楚填 null。"
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
