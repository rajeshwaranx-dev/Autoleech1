import secrets
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from aiohttp import web
from pyrogram import Client


@dataclass
class StreamItem:
    chat_id: int
    message_id: int
    file_name: str
    expires_at: float


_STREAM_ITEMS: dict[str, StreamItem] = {}
_DEFAULT_TTL = 6 * 60 * 60


def _cleanup_expired() -> None:
    now = time.time()
    expired = [token for token, item in _STREAM_ITEMS.items() if item.expires_at <= now]
    for token in expired:
        _STREAM_ITEMS.pop(token, None)


def register_stream(chat_id: int, message_id: int, file_name: str, ttl: int = _DEFAULT_TTL) -> str:
    _cleanup_expired()
    token = secrets.token_urlsafe(24)
    _STREAM_ITEMS[token] = StreamItem(
        chat_id=chat_id,
        message_id=message_id,
        file_name=file_name or f"telegram_{message_id}.bin",
        expires_at=time.time() + max(60, ttl),
    )
    return token


def build_stream_url(base_url: str, token: str, file_name: str) -> str:
    base_url = (base_url or "").rstrip("/")
    return f"{base_url}/dl/{token}/{quote(file_name or 'file.bin')}"


async def stream_handler(client: Client, request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    item: Optional[StreamItem] = _STREAM_ITEMS.get(token)
    if not item or item.expires_at <= time.time():
        _STREAM_ITEMS.pop(token, None)
        raise web.HTTPNotFound(text="Download link expired or not found")

    message = await client.get_messages(item.chat_id, item.message_id)
    media = message.document or message.video or message.audio or message.photo
    if not media:
        raise web.HTTPNotFound(text="Telegram media not found")

    headers = {
        "Content-Disposition": f'attachment; filename="{item.file_name}"',
        "Cache-Control": "no-store",
    }
    file_size = getattr(media, "file_size", None)
    if file_size:
        headers["Content-Length"] = str(file_size)

    response = web.StreamResponse(status=200, headers=headers)
    await response.prepare(request)
    async for chunk in client.stream_media(message):
        await response.write(chunk)
    await response.write_eof()
    return response


def expire_stream(token: str) -> None:
    _STREAM_ITEMS.pop(token, None)
