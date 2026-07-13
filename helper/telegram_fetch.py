"""Fetch Telegram media by first minting a temporary HTTP link (the same mechanism
the /link command uses) and then downloading that link, instead of pulling the file
straight through Pyrogram's MTProto download call.

Why: routing every Telegram-origin file through one HTTP code path means the exact
same range-request / parallel-chunk / retry logic that powers direct-link leeching
(see helper/multi_downloader.py) also speeds up and hardens Telegram-origin
downloads, and it keeps a single place responsible for on-disk file integrity checks.

Falls back to a direct Pyrogram download automatically when the bot can't reach its
own HTTP server (e.g. cold start, BASE_URL misconfigured), so this never becomes a
hard requirement for the bot to function.
"""

import time
from pathlib import Path
from typing import Optional

import aiohttp
from pyrogram import Client
from pyrogram.types import Message

from config import Config
from helper.multi_downloader import (
    MIN_SIZE_FOR_PARALLEL,
    parallel_range_download,
    probe_url,
    sequential_download,
)
from helper.stream_links import build_stream_url, expire_stream, register_stream
from helper.utils import humanbytes

# Internal fetches use a short TTL: the link only needs to live for as long as this
# one download takes, not the hours a user-facing /link is meant to stay valid for.
_INTERNAL_LINK_TTL = 2 * 60 * 60


def _get_media(message: Message):
    return message.document or message.video or message.audio or message.photo


def _self_base_url() -> str:
    """Prefer the public BASE_URL (validates the real path a user would hit), but fall
    back to loopback so this still works without a public URL configured."""
    if Config.BASE_URL:
        return Config.BASE_URL.rstrip("/")
    return f"http://127.0.0.1:{Config.PORT}"


async def fetch_via_link(
    client: Client,
    message: Message,
    dest_path: str,
    status: Optional[Message] = None,
    label: str = "📥 Downloading...",
) -> str:
    """Download a Telegram message's media by creating a temporary stream link and
    fetching it over HTTP (parallel range requests when the file is large enough to
    benefit). Returns the path written to on success.

    Falls back to a direct Pyrogram download if the self-serve HTTP path fails for
    any reason, so a broken BASE_URL or a cold health-server never blocks a leech job.
    """
    media = _get_media(message)
    if media is None:
        raise RuntimeError("Message has no downloadable media.")

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    file_name = getattr(media, "file_name", None) or dest.name

    token = register_stream(message.chat.id, message.id, file_name, _INTERNAL_LINK_TTL)
    url = build_stream_url(_self_base_url(), token, file_name)

    last_edit = {"t": 0.0}
    start_time = time.time()

    async def on_progress(current: int, total: int):
        if status is None:
            return
        now = time.time()
        if now - last_edit["t"] < 3 and current < total:
            return
        last_edit["t"] = now
        pct = f"{(current * 100 / total):.1f}%" if total else "?"
        speed = current / max(now - start_time, 0.001)
        try:
            await status.edit(
                f"{label}\n{humanbytes(current)} / {humanbytes(total) if total else '?'} ({pct})\n"
                f"⚡ {humanbytes(speed)}/s",
            )
        except Exception:
            pass

    fetched_via_link = False
    try:
        async with aiohttp.ClientSession() as session:
            size, supports_range, _name = await probe_url(session, url)
            if supports_range and size >= MIN_SIZE_FOR_PARALLEL:
                await parallel_range_download(session, url, dest, size, on_progress)
            else:
                await sequential_download(session, url, dest, on_progress)
        # Parallel range downloads pre-truncate the file to its final size before any
        # bytes land, so a mid-download failure (caught above by the except clause
        # firing from inside asyncio.gather) can still leave a right-sized-but-partly-
        # zero-filled file behind. Only treat this as success if we reach here with no
        # exception at all, not merely "a non-empty file exists".
        fetched_via_link = dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        print(f"[WARN] Link-based Telegram fetch failed ({e}); falling back to direct download.")
        if dest.exists():
            dest.unlink(missing_ok=True)
    finally:
        expire_stream(token)

    if not fetched_via_link:
        # Self-serve HTTP path failed (e.g. dyno can't reach its own PORT, cold start,
        # BASE_URL misconfigured) — fall back to a direct MTProto download so the job
        # still completes instead of hard-failing.
        await client.download_media(message=message, file_name=str(dest))

    if not dest.exists() or dest.stat().st_size <= 0:
        raise RuntimeError("Download finished but the file is missing or empty.")
    return str(dest)
