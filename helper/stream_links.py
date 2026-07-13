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


def _parse_range(range_header: Optional[str], file_size: int) -> Optional[tuple[int, int]]:
    """Parse a single-range 'Range: bytes=start-end' header. Returns (start, end) inclusive, or None."""
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            # suffix range: last N bytes
            length = int(end_s)
            start = max(0, file_size - length)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
    except ValueError:
        return None
    end = min(end, file_size - 1)
    if start < 0 or start > end:
        return None
    return start, end


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

    file_size = int(getattr(media, "file_size", 0) or 0)
    base_headers = {
        "Content-Disposition": f'attachment; filename="{item.file_name}"',
        "Cache-Control": "no-store",
        "Accept-Ranges": "bytes",
    }

    if request.method == "HEAD":
        headers = dict(base_headers)
        if file_size:
            headers["Content-Length"] = str(file_size)
        return web.Response(status=200, headers=headers)

    byte_range = _parse_range(request.headers.get("Range"), file_size) if file_size else None

    if byte_range is not None:
        start, end = byte_range
        length = end - start + 1
        headers = dict(base_headers)
        headers["Content-Length"] = str(length)
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        response = web.StreamResponse(status=206, headers=headers)
        await response.prepare(request)
        sent = 0
        # pyrogram's offset is expressed in 1MB chunks; limit is a chunk count. We stream from the
        # nearest chunk boundary at/below `start` and trim the extra bytes at the front/back locally
        # so arbitrary byte ranges still work against Telegram's chunked file API.
        chunk_size = 1024 * 1024
        first_chunk = start // chunk_size
        skip = start - first_chunk * chunk_size
        async for chunk in client.stream_media(message, offset=first_chunk):
            if skip:
                chunk = chunk[skip:]
                skip = 0
            remaining = length - sent
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            if chunk:
                await response.write(chunk)
                sent += len(chunk)
            if sent >= length:
                break
        await response.write_eof()
        return response

    headers = dict(base_headers)
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
