from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from linebot.v3.messaging import ApiClient, Configuration, MessagingApi, PushMessageRequest, TextMessage

from app.services.receipt_parser import ReceiptParser
from app.services.sheets import GoogleSheetsService

load_dotenv()

logger = logging.getLogger(__name__)
app = FastAPI(title="Receipt LineBot")


@app.get("/health")
def health() -> dict[str, Any]:
    missing = _missing_required_settings()
    return {
        "status": "ok" if not missing else "missing_config",
        "missing": missing,
    }


@app.post("/callback")
async def callback(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: str = Header(default=""),
):
    body = await request.body()
    _validate_line_signature(body, x_line_signature)

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid LINE webhook body") from exc

    for event in payload.get("events", []):
        if _is_supported_image_event(event):
            user_id = str(event["source"]["userId"])
            message_id = str(event["message"]["id"])
            background_tasks.add_task(_process_receipt_event_async, user_id, message_id)

    return "OK"


@app.post("/manual-register/{user_id}")
def manual_register_user(user_id: str, name: str):
    _get_sheets_service().upsert_user_mapping(user_id, name)
    return {"ok": True, "user_id": user_id, "name": name}


async def _process_receipt_event_async(user_id: str, message_id: str) -> None:
    await asyncio.to_thread(_process_receipt_event, user_id, message_id)


def _process_receipt_event(user_id: str, message_id: str) -> None:
    try:
        _show_loading_indicator(user_id, loading_seconds=10)

        image_bytes, mime_type = _get_message_image(message_id)
        receipt = _get_receipt_parser().parse_receipt(image_bytes, mime_type=mime_type)

        display_name = _get_line_display_name(user_id)
        sheets = _get_sheets_service()
        sheets.upsert_user_mapping(user_id, display_name)
        inserted_rows = sheets.append_receipt(
            user_id=user_id,
            registrant=display_name,
            receipt=receipt,
        )

        summary_lines = [
            "✅ 已完成登錄",
            f"店家: {receipt.merchant_name or '未知'}",
            f"日期: {receipt.receipt_date or '未知'}",
            f"總計: {_format_total(receipt.total_amount, receipt.currency)}",
            f"寫入列數: {inserted_rows}",
        ]
        _push_text_message(user_id, "\n".join(summary_lines))
    except Exception:
        logger.exception("Failed to process receipt image for user_id=%s", user_id)
        _push_text_message(
            user_id,
            "這張收據暫時無法完成辨識或寫入 Google Sheets，請確認圖片清晰度與環境設定後再試一次。",
        )


def _format_total(total_amount: Any, currency: str) -> str:
    if total_amount in (None, ""):
        return "未知"
    return f"{total_amount} {currency}".strip()


def _missing_required_settings() -> list[str]:
    missing: list[str] = []
    required_keys = [
        "LINE_CHANNEL_SECRET",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "OPENAI_API_KEY",
        "GOOGLE_SHEET_ID",
    ]
    for key in required_keys:
        if not os.getenv(key, "").strip():
            missing.append(key)

    if not (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT", "").strip()
    ):
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT")

    return missing


def _require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"請先設定 {key}")
    return value


@lru_cache(maxsize=1)
def _get_line_configuration() -> Configuration:
    return Configuration(access_token=_require_env("LINE_CHANNEL_ACCESS_TOKEN"))


@lru_cache(maxsize=1)
def _get_receipt_parser() -> ReceiptParser:
    return ReceiptParser()


@lru_cache(maxsize=1)
def _get_sheets_service() -> GoogleSheetsService:
    return GoogleSheetsService()


def _validate_line_signature(body: bytes, signature: str) -> None:
    if not signature:
        raise HTTPException(status_code=400, detail="Missing LINE signature")

    digest = hmac.new(
        _require_env("LINE_CHANNEL_SECRET").encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=400, detail="Invalid LINE signature")


def _is_supported_image_event(event: dict[str, Any]) -> bool:
    if event.get("mode") == "standby":
        return False
    if event.get("type") != "message":
        return False
    if event.get("message", {}).get("type") != "image":
        return False
    return bool(event.get("source", {}).get("userId")) and bool(event.get("message", {}).get("id"))


def _get_message_image(message_id: str) -> tuple[bytes, str]:
    response_body, content_type = _line_binary_request(
        f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    )
    return response_body, content_type


def _get_line_display_name(user_id: str) -> str:
    data = _line_json_request(f"https://api.line.me/v2/bot/profile/{user_id}")
    return str(data.get("displayName") or user_id)


def _show_loading_indicator(user_id: str, loading_seconds: int) -> None:
    try:
        _line_json_request(
            "https://api.line.me/v2/bot/chat/loading/start",
            method="POST",
            payload={
                "chatId": user_id,
                "loadingSeconds": max(5, min(60, loading_seconds)),
            },
        )
    except Exception:
        logger.warning("Unable to show LINE loading indicator for user_id=%s", user_id)


def _push_text_message(user_id: str, text: str) -> None:
    try:
        with ApiClient(_get_line_configuration()) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=text)],
                )
            )
    except Exception:
        logger.exception("Failed to push LINE message to user_id=%s", user_id)


def _line_binary_request(url: str) -> tuple[bytes, str]:
    req = UrlRequest(
        url,
        headers={"Authorization": f"Bearer {_require_env('LINE_CHANNEL_ACCESS_TOKEN')}"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=60) as response:
            return response.read(), response.headers.get_content_type() or "image/jpeg"
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"LINE binary API 呼叫失敗: {exc}") from exc


def _line_json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {_require_env('LINE_CHANNEL_ACCESS_TOKEN')}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"

    req = UrlRequest(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            body = response.read()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"LINE JSON API 呼叫失敗: {exc}") from exc

    if not body:
        return {}
    return json.loads(body.decode("utf-8"))
