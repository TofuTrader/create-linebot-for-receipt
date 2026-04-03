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

    def parse_receipt(self, image_bytes: bytes) -> ReceiptExtraction:
        b64 = base64.b64encode(image_bytes).decode("utf-8")

        prompt = (
            "你是收據辨識助手。請從韓國收據圖片抽取欄位，並僅輸出 JSON。\n"
            "JSON schema: {"
            '"receipt_number": string|null, '
            '"receipt_date": "YYYY-MM-DD"|null, '
            '"merchant_name": string|null, '
            '"currency": "KRW"|"USD"|"TWD"|string, '
            '"items": [{"item_name": string, "quantity": number, "unit_price": number, "line_total": number}], '
            '"raw_text": string|null'
            "}.\n"
            "規則: 1) 若看不清楚填 null。2) quantity 預設 1。3) unit_price/line_total 只保留數字。"
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
                            "image_url": f"data:image/jpeg;base64,{b64}",
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
        return json.loads(raw)
