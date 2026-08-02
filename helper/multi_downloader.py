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
from urllib.parse import urlparse

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


_GOFILE_SHARE_HOSTS = {"gofile.io", "www.gofile.io"}
_SOURCE_SRC_RE = re.compile(
    r"<source\b[^>]*\bsrc\s*=\s*([\"\'])(?P<url>https?://[^\"\']+)\1",
    re.IGNORECASE,
)
_VIDEO_SRC_RE = re.compile(
    r"<video\b[^>]*\bsrc\s*=\s*([\"\'])(?P<url>https?://[^\"\']+)\1",
    re.IGNORECASE,
)


def is_gofile_share_url(url: str) -> bool:
    """True for GoFile browser/share pages such as https://gofile.io/d/{id}."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    return hostname in _GOFILE_SHARE_HOSTS and len(parts) >= 2 and parts[0].lower() == "d"


def extract_gofile_source_url(html: str) -> str | None:
    """Extract the resolved GoFile storage URL from a rendered/share-page HTML snippet.

    GoFile's storage node changes by file/location, so never guess a hostname. Prefer
    the <source src=...> inside the video player (the browser's direct download URL),
    with a <video src=...> fallback for pages/snippets using that shape.
    """
    for pattern in (_SOURCE_SRC_RE, _VIDEO_SRC_RE):
        match = pattern.search(html or "")
        if match:
            return match.group("url").replace("&amp;", "&")
    return None


async def resolve_gofile_page_source_url(url: str, status=None) -> str | None:
    """Fetch a GoFile share page and return the direct <source src> URL when present."""
    if not is_gofile_share_url(url):
        return None
    await _safe_edit(status, "🔎 **Checking GoFile page for direct video source...**")
    async with aiohttp.ClientSession(headers=_request_headers(url)) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as resp:
            resp.raise_for_status()
            html = await resp.text(errors="ignore")
    return extract_gofile_source_url(html)


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
    from yt_dlp.utils import DownloadError  # yt-dlp's own documented pattern for catching this

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
            try:
                info = ydl.extract_info(source, download=True)
            except DownloadError as e:
                message = str(e)
                if "no longer supported" in message.lower() and "piracy" in message.lower():
                    # yt-dlp maintains a deliberate, hard-coded blocklist for sites it has
                    # judged to be primarily used for piracy -- this is a real policy
                    # decision (see yt-dlp's own FAQ: "supporting piracy sites would in
                    # all likelihood result in the project being shut down"), not a bug,
                    # and there's no extractor-arg or flag to override it. The extractor
                    # refuses before making any request, so no amount of retrying,
                    # header changes, or routing logic on this end can work around it.
                    raise RuntimeError(
                        "yt-dlp refuses to fetch this site: it's on yt-dlp's own deliberate "
                        "piracy blocklist, not a bug on this end. There's no override for "
                        "this -- it's a hard-coded policy decision in yt-dlp itself.\n\n"
                        "If this was a GoFile share link, there's a workaround: open the "
                        "share page (gofile.io/d/...) in a browser, right-click the video "
                        "player and choose \"Copy video address\" (or open dev tools' Network "
                        "tab and find the request for the video file) -- that gives you the "
                        "real storage URL, which looks like store*.gofile.io/download/web/... "
                        "or a similar node name. Send that URL to /leech instead of the share "
                        "page link; it bypasses yt-dlp entirely and goes through the plain "
                        "HTTP downloader, which isn't affected by this block."
                    ) from e
                raise
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


def _request_headers(url: str) -> dict:
    """Headers to send with every direct-HTTP request. aiohttp's default User-Agent
    (something like 'Python/3.x aiohttp/x.x.x') is a well-known signature some hosts
    distrust or block outright, separate from any real auth requirement -- a plain
    browser-shaped UA avoids that class of failure for free. GoFile specifically also
    gets a Referer: a real, successful GoFile storage-node download logged in
    aria2/aria2 issue #2326 included one, and GoFile's own download flow (confirmed
    via multiple independent client implementations) can gate non-cold-storage files
    on more than just the bare URL. This does NOT reproduce GoFile's X-Website-Token
    scheme -- that's exactly why GoFile share pages route through yt-dlp's own
    GofileIE extractor instead of this module; this only helps the already-resolved
    storage-node URL yt-dlp hands back, for the subset of failures a UA/Referer can
    actually fix."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    try:
        from urllib.parse import urlparse
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        hostname = ""
    if hostname == "gofile.io" or hostname.endswith(".gofile.io"):
        headers["Referer"] = "https://gofile.io/"
    return headers


async def probe_url(session: aiohttp.ClientSession, url: str) -> tuple[int, bool, str]:
    """Returns (size, supports_range, filename_hint). Shared by the direct-HTTP
    leech backend and helper.telegram_fetch's link-based Telegram downloader."""
    headers = _request_headers(url)
    try:
        async with session.head(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
            if resp.status == 200:
                size = int(resp.headers.get("Content-Length", "0") or 0)
                accepts = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
                return size, accepts and size > 0, _filename_from_headers(resp.headers, url)
    except Exception:
        pass
    try:
        range_headers = {**headers, "Range": "bytes=0-0"}
        async with session.get(url, headers=range_headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
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
    path_name = Path(urlparse(url).path).name

    # A usable path-derived filename is short and has a real extension. Long opaque
    # tokens (Google's signed video-CDN URLs) and bare API IDs with no extension
    # (Pixeldrain's /api/file/{id}?download, where the path's last segment IS the
    # file ID -- this is exactly what produced a filename like "hJvEivyV" with
    # nothing for is_video_file to recognize) fail one or both of these checks and
    # need a synthesized name instead.
    if path_name and len(path_name) <= 120 and "." in path_name and len(path_name.rsplit(".", 1)[-1]) <= 5:
        return path_name

    return _synthesize_filename(headers.get("Content-Type", ""))


def _synthesize_filename(content_type: str) -> str:
    """Build a short, safe filename (with a real extension) from a Content-Type
    header when the URL itself doesn't provide a usable one."""
    content_type = (content_type or "").split(";")[0].strip().lower()
    # Python's mimetypes table maps video/x-matroska to the little-used ".mpv"
    # rather than the ".mkv" extension virtually everyone actually uses for
    # Matroska files; special-case it so Matroska downloads get a recognizable,
    # correct extension that is_video_file() will actually match.
    overrides = {
        "video/x-matroska": ".mkv",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",
        "video/x-m4v": ".m4v",
    }
    ext = overrides.get(content_type)
    if ext is None:
        import mimetypes
        ext = mimetypes.guess_extension(content_type) or ".bin"
    return f"download_{int(time.time())}{ext}"


async def download_direct_http_fast(source: str, out_dir: Path, status) -> Path:
    """Faster direct-HTTP downloader: parallel byte-range workers when the server
    supports it, sequential streaming otherwise. Drop-in replacement for the old
    single-stream `download_direct_http`.

    If the parallel path produces a file that fails the media-integrity check (looks
    like an HTML/JSON error page rather than the real file), automatically retries
    once with the sequential path before giving up -- some hosts' anti-bot defenses
    appear to react differently to a burst of simultaneous parallel range requests
    than to a single ordinary connection, which is architecturally much closer to
    what parallel downloading isn't and sequential downloading is.
    """
    async with aiohttp.ClientSession() as session:
        size, supports_range, filename = await probe_url(session, source)
        filename = filename or "download.bin"
        target = out_dir / filename

        last_edit = {"t": 0.0}
        start = time.time()
        used_parallel = supports_range and size >= MIN_SIZE_FOR_PARALLEL

        async def on_progress(current: int, total: int, label: str = ""):
            now = time.time()
            if now - last_edit["t"] < 3 and current < total:
                return
            last_edit["t"] = now
            speed = current / max(now - start, 0.001)
            total_text = humanbytes(total) if total else "unknown"
            mode = label or (f"parallel x{PARALLEL_WORKERS}" if used_parallel else "single connection")
            await _safe_edit(
                status,
                f"🌐 **HTTP downloading ({mode})...**\n{humanbytes(current)} / {total_text}\n⚡ {humanbytes(speed)}/s",
            )

        if used_parallel:
            await parallel_range_download(session, source, target, size, on_progress)
        else:
            await sequential_download(session, source, target, on_progress)

        if used_parallel and target.exists() and target.stat().st_size > 0:
            integrity_error = _check_media_integrity(target)
            if integrity_error is not None:
                # The parallel path produced something that isn't the real file.
                # Discard it and retry once with a single ordinary connection before
                # giving up -- see the docstring above for why this specifically
                # (not just "retry the same thing again") is the sensible fallback.
                target.unlink(missing_ok=True)
                await _safe_edit(status, "⚠️ **Parallel download looked wrong, retrying with a single connection...**")
                start = time.time()
                last_edit["t"] = 0.0
                await sequential_download(
                    session, source, target,
                    lambda current, total: on_progress(current, total, label="single connection, retry"),
                )

    if target.exists() and target.stat().st_size > 0:
        error = _check_media_integrity(target)
        if error is not None:
            raise RuntimeError(error)
        return target
    raise RuntimeError("HTTP download finished but no file was saved.")


_MEDIA_LIKE_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".zip", ".rar", ".7z",
    ".pdf", ".mp3", ".flac", ".iso", ".exe", ".apk",
}
_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head>", b"<script")
# A genuine video/archive/binary file's real header bytes can never start with a
# literal '{' -- none of the formats in _MEDIA_LIKE_EXTENSIONS begin that way. Some
# hosts (GoFile's API among them, per real error responses like
# {"status":"error-notFound",...} seen in third-party GoFile client issue trackers)
# return a small JSON error object with a misleading 200-ish status instead of the
# real file. JSON_ERROR_HINT_SIZE_LIMIT keeps this from ever flagging a real file
# that simply happens to start with '{' by pure coincidence in its first bytes --
# real media/archive files at this size would never be this small.
_JSON_ERROR_HINT_SIZE_LIMIT = 4096


def _check_media_integrity(target: Path) -> str | None:
    """Some hosts serve an ad/interstitial/error page (HTML) or a small error object
    (JSON) with a misleading success-shaped response instead of the real file
    (raise_for_status() only looks at the HTTP status code, so it can't catch this).
    Returns an error message describing what went wrong, or None if the file looks
    like a genuine media/binary file. Doesn't raise directly so callers can decide
    whether to retry with a different download strategy before giving up.
    """
    if target.suffix.lower() not in _MEDIA_LIKE_EXTENSIONS:
        return None
    try:
        size = target.stat().st_size
        with open(target, "rb") as f:
            head = f.read(512).lstrip().lower()
    except OSError:
        return None

    if any(head.startswith(marker) or marker in head[:200] for marker in _HTML_MARKERS):
        return (
            f"The server returned a webpage instead of the file for {target.name} "
            "(some hosts show an interstitial/ad page on the first request). Try the "
            "link again, or use the site's direct-download option if it has one."
        )

    if head.startswith(b"{") and size <= _JSON_ERROR_HINT_SIZE_LIMIT:
        try:
            body_text = target.read_text(errors="ignore").lower()
        except OSError:
            body_text = ""
        if "cold" in body_text and "storage" in body_text:
            # GoFile-specific, and genuinely unfixable from this end: cold-storage
            # files require a Premium account to import them back before they
            # become downloadable at all -- there is no direct link, header, or
            # retry that gets around this, it's a real platform-level restriction.
            return (
                f"{target.name} is in GoFile cold storage and can't be downloaded directly -- "
                "GoFile requires importing it into a Premium account first before it becomes "
                "downloadable at all. This isn't something a direct link or retry can get "
                "around; it's a real restriction on GoFile's side. Ask whoever shared it to "
                "re-upload or refresh the link (cold storage happens after a file goes "
                "unused for a while), or download it manually with a GoFile Premium account."
            )
        return (
            f"The server returned an error response instead of the file for {target.name} "
            "(got a small JSON error object where the real file was expected). Try the link "
            "again, or check whether the source requires an account/premium access."
        )
    return None


def _reject_if_html_masquerading_as_media(target: Path) -> None:
    """Raising wrapper around _check_media_integrity, kept for any caller that wants
    the old raise-immediately-on-failure behavior."""
    error = _check_media_integrity(target)
    if error is not None:
        raise RuntimeError(error)


async def sequential_download(session: aiohttp.ClientSession, url: str, target: Path, on_progress) -> None:
    downloaded = 0
    async with session.get(url, headers=_request_headers(url), timeout=aiohttp.ClientTimeout(total=None)) as resp:
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
        headers = {**_request_headers(url), "Range": f"bytes={byte_start}-{byte_end}"}
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
