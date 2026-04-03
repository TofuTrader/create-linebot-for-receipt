from __future__ import annotations

from decimal import Decimal
from pydantic import BaseModel, Field


class ReceiptItem(BaseModel):
    item_name: str = Field(..., description="品項名稱")
    item_name_zh: str | None = Field(default=None, description="品項中文翻譯")
    quantity: Decimal = Field(default=Decimal("1"), description="數量")
    unit_price: Decimal = Field(default=Decimal("0"), description="單價")
    line_total: Decimal = Field(default=Decimal("0"), description="複價")


class ReceiptExtraction(BaseModel):
    receipt_number: str | None = Field(default=None, description="憑證編號")
    receipt_date: str | None = Field(default=None, description="YYYY-MM-DD")
    merchant_name: str | None = Field(default=None, description="店家")
    merchant_name_zh: str | None = Field(default=None, description="店家中文翻譯")
    total_amount: Decimal | None = Field(default=None, description="總計")
    source_region: str | None = Field(default=None, description="票據地區，例：KR、TW")
    currency: str = Field(default="KRW", description="幣別")
    tax_refund_status: str | None = Field(default=None, description="退稅狀態：eligible/not_eligible/unknown")
    tax_refund_amount: Decimal | None = Field(default=None, description="估計可退稅金額")
    tax_refund_note: str | None = Field(default=None, description="退稅判斷說明")
    items: list[ReceiptItem] = Field(default_factory=list, description="明細列表")
    raw_text: str | None = Field(default=None, description="OCR 原始文字")
