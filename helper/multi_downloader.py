"""Extra leech backends beyond aria2c:

1. yt-dlp — adds support for hundreds of streaming/social sites (YouTube, X/Twitter,
   Instagram, TikTok, Reddit, Facebook, SoundCloud, Vimeo, Dailymotion, GoFile, and
   more) that aria2/plain HTTP can't handle because they require page/API extraction,
   not just a file fetch.
2. Parallel-range HTTP downloader — for plain direct file links, split the download
   into concurrent byte-range requests (when the server advertises Accept-Ranges) for
   a real wall-clock speedup over a single sequential stream, with an automatic
   sequential fallback for servers that don't support ranges.
3. Share-link rewriting — some file hosts (currently Pixeldrain) wrap a plain,
   unauthenticated, range-request-capable download endpoint behind a share URL that
   isn't itself directly fetchable. rewrite_to_direct_url() converts the share URL to
   the real API endpoint, then hands off to the same parallel-range downloader as any
   other direct link. Verified against each host's own published API docs, not
   guessed -- a host is only added here once its direct-download behavior has been
   confirmed, not assumed to work "like GoFile" or similar hosts.

All three are optional/best-effort: if yt-dlp isn't installed, `is_ytdlp_available()`
returns False and callers fall back to the existing aria2/HTTP paths, matching how
this codebase already treats aria2c as optional.
"""

import asyncio
import importlib.util
import os
import re
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
    # gofile.io requires a dynamic, frequently-rotating website-token scheme to call
    # its API directly (see helper.multi_downloader module docstring). yt-dlp ships
    # its own actively-maintained GofileIE extractor that already handles this, so
    # routing here through yt-dlp is far more robust than reimplementing GoFile's
    # token generation by hand.
    "gofile.io",
)


def is_ytdlp_available() -> bool:
    return _YTDLP_AVAILABLE


# GoFile serves actual file bytes from per-file storage-node subdomains (e.g.
# file-na-atl-1.gofile.io, store10.gofile.io) that are DIFFERENT from the gofile.io/d/{id}
# share-page URL yt-dlp's GofileIE extractor is built to parse. A storage-node URL is
# already a resolved, direct download link -- there's no "page" for yt-dlp to extract
# anything from, so it needs the plain HTTP downloader, not the yt-dlp extraction path.
_GOFILE_STORAGE_HOST_RE = re.compile(r"^([a-z0-9-]+\.)?gofile\.io$", re.IGNORECASE)


def is_gofile_storage_url(url: str) -> bool:
    """True for a GoFile storage-node URL (already-resolved file bytes) as opposed to
    a gofile.io/d/{id} share page (needs yt-dlp's extractor to resolve first)."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if hostname in ("gofile.io", "www.gofile.io"):
        return False  # this is the share-page host, not a storage node
    if not _GOFILE_STORAGE_HOST_RE.match(hostname):
        return False
    # Storage-node download URLs look like /download/web/{id}/{filename} or
    # /contents/uploadfile -- require the path to actually look like a file fetch
    # rather than matching on hostname alone.
    return "/download/" in parsed.path


def looks_like_ytdlp_source(url: str) -> bool:
    """True if `url`'s hostname is (or is a subdomain of) a known yt-dlp-supported
    site. Uses real hostname parsing rather than substring matching on the whole URL:
    a naive `"x.com" in url.lower()` check would also match unrelated domains like
    box.com or matrix.com (both literally contain the substring "x.com"), which would
    wrongly route a plain file link on an unrelated host into the yt-dlp backend.
    """
    if is_gofile_storage_url(url):
        return False

    from urllib.parse import urlparse

    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not hostname:
        return False
    return any(hostname == host or hostname.endswith("." + host) for host in YTDLP_LIKELY_HOSTS)


def rewrite_to_direct_url(url: str) -> Optional[str]:
    """Convert a share URL from a known host into its real direct-download endpoint,
    so it can be handed to the ordinary parallel-range HTTP downloader unchanged.

    Returns None if the URL's host isn't one of the hosts handled here (the caller
    should fall through to route_download's other backends in that case). Only hosts
    whose direct-download behavior has been individually confirmed against their own
    documentation are added -- this is deliberately a short, verified list rather
    than a guess at "sites that work like Pixeldrain/GoFile".
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return None

    if hostname == "pixeldrain.com" or hostname.endswith(".pixeldrain.com"):
        # /u/{id} share links are confirmed (across pixeldrain's own docs and
        # multiple independent third-party clients) to always point to a single
        # file. The real bytes live behind the public API at /api/file/{id}, which
        # pixeldrain.com/api documents as supporting byte range requests and
        # requiring no authentication for public files. '?download' requests an
        # attachment header instead of inline rendering.
        #
        # Deliberately NOT handling /d/{id} or /l/{id} here: per pixeldrain's own
        # filesystem docs, a /d/ ID can point to either a shared FILE or a shared
        # DIRECTORY, and there's no way to tell which from the URL alone -- treating
        # a directory share as a single file would silently build a wrong URL rather
        # than fail loudly. /l/ (list) shares resolve to multiple files via a
        # separate array-returning API, not a single stream, for the same reason.
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "u":
            file_id = parts[1]
            return f"https://pixeldrain.com/api/file/{file_id}?download"

    return None


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
        _reject_if_html_masquerading_as_media(target)
        return target
    raise RuntimeError("HTTP download finished but no file was saved.")


_MEDIA_LIKE_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".zip", ".rar", ".7z",
    ".pdf", ".mp3", ".flac", ".iso", ".exe", ".apk",
}
_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head>", b"<script")


def _reject_if_html_masquerading_as_media(target: Path) -> None:
    """Some hosts serve an ad/interstitial/error page with a 200 OK status instead of
    the real file (raise_for_status() only looks at the HTTP status code, so it can't
    catch this). If a file with a known media/binary extension actually starts with
    HTML markers, that's what happened -- raise a clear error instead of silently
    treating a saved webpage as a successful download.
    """
    if target.suffix.lower() not in _MEDIA_LIKE_EXTENSIONS:
        return
    try:
        with open(target, "rb") as f:
            head = f.read(512).lstrip().lower()
    except OSError:
        return
    if any(head.startswith(marker) or marker in head[:200] for marker in _HTML_MARKERS):
        raise RuntimeError(
            f"The server returned a webpage instead of the file for {target.name} "
            "(some hosts show an interstitial/ad page on the first request). Try the "
            "link again, or use the site's direct-download option if it has one."
        )


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
