"""Extra leech backends beyond aria2c:

1. yt-dlp — adds support for hundreds of streaming/social sites (YouTube, X/Twitter,
   Instagram, TikTok, Reddit, Facebook, SoundCloud, Vimeo, Dailymotion, and more) that
   aria2/plain HTTP can't handle because they require page/API extraction, not just a
   file fetch.
2. Parallel-range HTTP downloader — for plain direct file links, split the download
   into concurrent byte-range requests (when the server advertises Accept-Ranges) for
   a real wall-clock speedup over a single sequential stream, with an automatic
   sequential fallback for servers that don't support ranges.

Both are optional/best-effort: if yt-dlp isn't installed, `is_ytdlp_available()`
returns False and callers fall back to the existing aria2/HTTP paths, matching how
this codebase already treats aria2c as optional.
"""

import asyncio
import importlib.util
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp

from config import Config
from helper.utils import humanbytes

# ----------------------------------------------------------------------------
# yt-dlp backend — hundreds of additional sites
# ----------------------------------------------------------------------------

_YTDLP_AVAILABLE = importlib.util.find_spec("yt_dlp") is not None

# A representative (non-exhaustive) set of extractor keys used only to decide whether
# a plain HTTP URL is *likely* a media-page link worth trying with yt-dlp before
# falling back to a raw file fetch. yt-dlp's own extractor matching is authoritative;
# this is just a fast pre-filter so we don't waste a HEAD/probe round-trip on obvious
# direct-file links (.zip, .mp4 CDN links, etc).
YTDLP_LIKELY_HOSTS = (
    "youtube.com", "youtu.be", "twitter.com", "x.com", "instagram.com",
    "tiktok.com", "reddit.com", "redd.it", "facebook.com", "fb.watch",
    "soundcloud.com", "vimeo.com", "dailymotion.com", "twitch.tv",
    "streamable.com", "pinterest.com", "likee.video", "bilibili.com",
    "rumble.com", "ok.ru", "vk.com", "linkedin.com",
)


def is_ytdlp_available() -> bool:
    return _YTDLP_AVAILABLE


def looks_like_ytdlp_source(url: str) -> bool:
    """True if `url`'s hostname is (or is a subdomain of) a known yt-dlp-supported
    site. Uses real hostname parsing rather than substring matching on the whole URL:
    a naive `"x.com" in url.lower()` check would also match unrelated domains like
    box.com or matrix.com (both literally contain the substring "x.com"), which would
    wrongly route a plain file link on an unrelated host into the yt-dlp backend.
    """
    from urllib.parse import urlparse

    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not hostname:
        return False
    return any(hostname == host or hostname.endswith("." + host) for host in YTDLP_LIKELY_HOSTS)


YTDLP_MAX_HEIGHT = max(240, int(os.environ.get("YTDLP_MAX_HEIGHT", "1080")))


async def download_with_ytdlp(source: str, out_dir: Path, status) -> Path:
    """Download `source` with yt-dlp into out_dir, returning the resulting file path.

    Runs the blocking yt-dlp call in a thread so it doesn't stall the event loop, and
    reports progress back onto `status` via a thread-safe hand-off.
    """
    if not _YTDLP_AVAILABLE:
        raise RuntimeError(
            "yt-dlp is not installed on this dyno, so this link can't be fetched as a "
            "media/social-site download. Add `yt-dlp` to requirements.txt and redeploy, "
            "or use a direct file URL / magnet instead."
        )

    import yt_dlp  # imported lazily so the module import never fails when yt-dlp is absent

    loop = asyncio.get_running_loop()
    last_edit = {"t": 0.0}

    def progress_hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        now = time.time()
        if now - last_edit["t"] < 4:
            return
        last_edit["t"] = now
        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        speed = d.get("speed") or 0
        eta = d.get("eta")
        total_text = humanbytes(total) if total else "?"
        eta_text = f"{eta}s" if eta is not None else "?"
        text = (
            f"🌍 **Fetching via yt-dlp...**\n"
            f"{humanbytes(downloaded)} / {total_text}\n"
            f"⚡ {humanbytes(speed)}/s • ETA {eta_text}"
        )
        asyncio.run_coroutine_threadsafe(_safe_edit(status, text), loop)

    ydl_opts = {
        "outtmpl": str(out_dir / "%(title).150B [%(id)s].%(ext)s"),
        "progress_hooks": [progress_hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "concurrent_fragment_downloads": max(1, min(8, Config.ARIA2_SPLIT)),
        "retries": 5,
        "fragment_retries": 5,
        # Cap resolution by default: uncapped best-quality selection can pick 4K/8K
        # sources that are slow to download and memory-heavy for ffmpeg to merge on a
        # small dyno, and can still exceed the effective upload ceiling (2GB, or 4GB
        # with a genuinely Premium PREMIUM_SESSION_STRING -- see
        # Config.effective_max_upload_size). Raise YTDLP_MAX_HEIGHT for bigger dynos.
        "format": f"bv*[height<={YTDLP_MAX_HEIGHT}]+ba/b[height<={YTDLP_MAX_HEIGHT}]/bv*+ba/b",
        "merge_output_format": "mp4",
    }

    def run_download() -> str:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source, download=True)
            if info is None:
                raise RuntimeError("yt-dlp returned no info for this URL.")
            return ydl.prepare_filename(info)

    raw_path = await loop.run_in_executor(None, run_download)
    result = Path(raw_path)
    if result.exists():
        return result

    # merge_output_format can change the extension after download; fall back to
    # picking the newest file written into out_dir.
    candidates = sorted(out_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("yt-dlp finished but no output file was found.")


async def _safe_edit(status, text: str) -> None:
    if status is None:
        return
    try:
        await status.edit(text)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Parallel-range HTTP backend — faster plain direct-file leeching
# ----------------------------------------------------------------------------

PARALLEL_WORKERS = min(8, max(1, Config.ARIA2_SPLIT or 4))
MIN_SIZE_FOR_PARALLEL = 8 * 1024 * 1024


async def probe_url(session: aiohttp.ClientSession, url: str) -> tuple[int, bool, str]:
    """Returns (size, supports_range, filename_hint). Shared by the direct-HTTP
    leech backend and helper.telegram_fetch's link-based Telegram downloader."""
    try:
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
            if resp.status == 200:
                size = int(resp.headers.get("Content-Length", "0") or 0)
                accepts = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
                return size, accepts and size > 0, _filename_from_headers(resp.headers, url)
    except Exception:
        pass
    try:
        async with session.get(url, headers={"Range": "bytes=0-0"}, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            name = _filename_from_headers(resp.headers, url)
            if resp.status == 206:
                content_range = resp.headers.get("Content-Range", "")
                total = content_range.rsplit("/", 1)[-1] if "/" in content_range else "0"
                return int(total or 0), int(total or 0) > 0, name
            return 0, False, name
    except Exception:
        pass
    return 0, False, ""


def _filename_from_headers(headers, url: str) -> str:
    disposition = headers.get("Content-Disposition", "")
    if "filename=" in disposition:
        name = disposition.split("filename=", 1)[-1].strip('"; ')
        if name:
            return name
    from urllib.parse import urlparse
    return Path(urlparse(url).path).name


async def download_direct_http_fast(source: str, out_dir: Path, status) -> Path:
    """Faster direct-HTTP downloader: parallel byte-range workers when the server
    supports it, sequential streaming otherwise. Drop-in replacement for the old
    single-stream `download_direct_http`."""
    async with aiohttp.ClientSession() as session:
        size, supports_range, filename = await probe_url(session, source)
        filename = filename or "download.bin"
        target = out_dir / filename

        last_edit = {"t": 0.0}
        start = time.time()

        async def on_progress(current: int, total: int):
            now = time.time()
            if now - last_edit["t"] < 3 and current < total:
                return
            last_edit["t"] = now
            speed = current / max(now - start, 0.001)
            total_text = humanbytes(total) if total else "unknown"
            await _safe_edit(
                status,
                f"🌐 **HTTP downloading (parallel x{PARALLEL_WORKERS})...**\n"
                f"{humanbytes(current)} / {total_text}\n⚡ {humanbytes(speed)}/s"
                if supports_range and size >= MIN_SIZE_FOR_PARALLEL else
                f"🌐 **HTTP downloading...**\n{humanbytes(current)} / {total_text}\n⚡ {humanbytes(speed)}/s",
            )

        if supports_range and size >= MIN_SIZE_FOR_PARALLEL:
            await parallel_range_download(session, source, target, size, on_progress)
        else:
            await sequential_download(session, source, target, on_progress)

    if target.exists() and target.stat().st_size > 0:
        return target
    raise RuntimeError("HTTP download finished but no file was saved.")


async def sequential_download(session: aiohttp.ClientSession, url: str, target: Path, on_progress) -> None:
    downloaded = 0
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=None)) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", "0") or 0)
        with open(target, "wb") as f:
            async for chunk in resp.content.iter_chunked(1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                await on_progress(downloaded, total)
        await on_progress(downloaded, total or downloaded)


async def parallel_range_download(session: aiohttp.ClientSession, url: str, target: Path, total: int, on_progress) -> None:
    chunk_span = -(-total // PARALLEL_WORKERS)
    ranges = []
    start = 0
    while start < total:
        end = min(start + chunk_span - 1, total - 1)
        ranges.append((start, end))
        start = end + 1

    with open(target, "wb") as f:
        f.truncate(total)

    lock = asyncio.Lock()
    downloaded_total = 0

    async def worker(byte_start: int, byte_end: int):
        nonlocal downloaded_total
        headers = {"Range": f"bytes={byte_start}-{byte_end}"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=None)) as resp:
            resp.raise_for_status()
            offset = byte_start
            with open(target, "r+b") as f:
                async for chunk in resp.content.iter_chunked(512 * 1024):
                    f.seek(offset)
                    f.write(chunk)
                    offset += len(chunk)
                    async with lock:
                        downloaded_total += len(chunk)
                        current = downloaded_total
                    await on_progress(current, total)

    await asyncio.gather(*(worker(s, e) for s, e in ranges))
    await on_progress(total, total)
