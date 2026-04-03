from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import ImageMessageContent, MessageEvent

from app.services.receipt_parser import ReceiptParser
from app.services.sheets import GoogleSheetsService

app = FastAPI(title="Korea Receipt LineBot")

channel_secret = os.getenv("LINE_CHANNEL_SECRET", "")
channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
if not channel_secret or not channel_access_token:
    raise RuntimeError("請先設定 LINE_CHANNEL_SECRET 與 LINE_CHANNEL_ACCESS_TOKEN")

handler = WebhookHandler(channel_secret)
line_config = Configuration(access_token=channel_access_token)
parser = ReceiptParser()
sheets = GoogleSheetsService()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(default="")):
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), x_line_signature)
    except InvalidSignatureError as exc:
        raise HTTPException(status_code=400, detail="Invalid LINE signature") from exc
    return "OK"


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent):
    user_id = event.source.user_id
    if not user_id:
        return

    image_data = _get_message_image(event.message.id)
    receipt = parser.parse_receipt(image_data)

    display_name = _get_line_display_name(user_id)
    sheets.upsert_user_mapping(user_id, display_name)
    inserted_rows = sheets.append_receipt(user_id=user_id, registrant=display_name, receipt=receipt)

    summary = (
        f"✅ 已完成登錄\n"
        f"店家: {receipt.merchant_name or '未知'}\n"
        f"日期: {receipt.receipt_date or '未知'}\n"
        f"明細筆數: {inserted_rows}"
    )

    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=summary)],
            )
        )


def _get_message_image(message_id: str) -> bytes:
    with ApiClient(line_config) as api_client:
        blob = api_client.call_api(
            f"/v2/bot/message/{message_id}/content",
            "GET",
            response_type="bytearray",
            auth_settings=["Bearer"],
        )
    return bytes(blob[0])


def _get_line_display_name(user_id: str) -> str:
    with ApiClient(line_config) as api_client:
        profile = api_client.call_api(
            f"/v2/bot/profile/{user_id}",
            "GET",
            response_type="json",
            auth_settings=["Bearer"],
        )
    data = profile[0] if isinstance(profile, tuple) else profile
    if isinstance(data, dict):
        return data.get("displayName") or user_id
    return user_id


@app.post("/manual-register/{user_id}")
def manual_register_user(user_id: str, name: str):
    sheets.upsert_user_mapping(user_id, name)
    return {"ok": True, "user_id": user_id, "name": name}
