import asyncio
import os
import re
import shutil
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from helper.media_tools import add_video_branding, is_video_file, make_cover_image
from helper.multi_downloader import (
    download_direct_http_fast,
    download_with_ytdlp,
    is_ytdlp_available,
    looks_like_ytdlp_source,
)
from helper.stream_links import build_stream_url, register_stream
from helper.telegram_fetch import fetch_via_link
from helper.utils import download_thumbnail, humanbytes, progress_for_pyrogram
from plugins.file_rename import (
    DESTINATION_CHANNELS,
    SOURCE_CHANNELS,
    is_admin_user,
    run_with_floodwait_retry,
    upload_semaphore,
)

LEECH_ROOT = Path("downloads/leech")
LEECH_ROOT.mkdir(parents=True, exist_ok=True)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MAGNET_RE = re.compile(r"magnet:\?\S+", re.IGNORECASE)


def leech_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("🧲 Leech Help", callback_data="leech_help")],
        [InlineKeyboardButton("⚙️ Commands", callback_data="commands"), InlineKeyboardButton("✖️ Close", callback_data="close")],
    ])


def get_media_name(message: Message) -> str:
    media = message.document or message.video or message.audio or message.photo
    return getattr(media, "file_name", None) or f"telegram_{message.id}.bin"


def extract_leech_sources(text: str | None) -> list[str]:
    """Return magnet links and URLs that look like torrent/direct leech sources.

    Torrent URLs are accepted when the URL contains the word "torrent" anywhere,
    not only when it ends with .torrent.
    """
    if not text:
        return []
    sources = MAGNET_RE.findall(text)
    for url in URL_RE.findall(text):
        clean = url.rstrip(").,]}")
        if clean not in sources and ("torrent" in clean.lower() or clean.startswith(("http://", "https://"))):
            sources.append(clean)
    return sources


def find_largest_file(folder: Path) -> Path | None:
    files = [p for p in folder.rglob("*") if p.is_file() and not p.name.endswith(".aria2")]
    return max(files, key=lambda p: p.stat().st_size, default=None)


def _is_torrentish(source: str) -> bool:
    return source.lower().startswith("magnet:?") or source.lower().endswith(".torrent") or "torrent" in source.lower()


async def download_with_aria2(source: str, out_dir: Path, status: Message) -> Path:
    """Torrent/magnet leeching via aria2c. This is the only backend that can
    handle magnets and .torrent files — there is no HTTP-only substitute for
    BitTorrent, so a clear, actionable error is critical when aria2c is missing."""
    aria2 = shutil.which("aria2c")
    if not aria2:
        raise RuntimeError(
            "aria2c is not installed on this dyno, so torrent/magnet leeching cannot start.\n\n"
            "**Heroku fix:** open your app's Settings → Buildpacks and add, in this exact order:\n"
            "1. `https://github.com/heroku/heroku-buildpack-apt` (reads `Aptfile`)\n"
            "2. `heroku/python`\n"
            "The apt buildpack must come *before* the Python buildpack, or the Aptfile is skipped. "
            "The included `app.json` now declares this automatically for one-click deploys — if you "
            "deployed before this update, add the buildpack manually and redeploy once.\n\n"
            "**Docker:** already installs `aria2` in the Dockerfile; rebuild the image if it's missing.\n\n"
            "Direct links and yt-dlp-supported sites (YouTube, Twitter/X, Instagram, TikTok, etc.) "
            "still work without aria2c."
        )
    cmd = [
        aria2, "--seed-time=0", "--summary-interval=5", "--console-log-level=warn",
        f"--max-connection-per-server={Config.ARIA2_SPLIT}", f"--split={Config.ARIA2_SPLIT}", "--min-split-size=1M",
        "--bt-enable-lpd=false", "--enable-dht=false", "--enable-dht6=false",
        "--max-tries=5", "--retry-wait=3",
        "--dir", str(out_dir), source,
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    last_edit = 0.0
    output_tail = ""
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        output_tail = (output_tail + line.decode(errors="ignore"))[-1200:]
        if time.time() - last_edit > 8:
            last_edit = time.time()
            try:
                await status.edit(f"🧲 **Leeching...**\n```{output_tail[-700:]}```", reply_markup=leech_keyboard())
            except Exception:
                pass
    code = await proc.wait()
    if code != 0:
        raise RuntimeError(f"aria2c failed with exit code {code}: {output_tail[-500:]}")
    largest = find_largest_file(out_dir)
    if not largest:
        raise RuntimeError("Download finished but no file was found.")
    return largest


async def route_download(source: str, out_dir: Path, status: Message) -> Path:
    """Pick the right backend for a leech source:

    1. Magnet / .torrent / torrent-flavoured URL -> aria2c (BitTorrent only, no substitute).
    2. Known media/social site (YouTube, X/Twitter, Instagram, TikTok, Reddit, ...) -> yt-dlp,
       which understands page/API extraction instead of just fetching bytes.
    3. Anything else that looks like a plain file URL -> the fast parallel-range HTTP downloader.
    """
    if _is_torrentish(source):
        return await download_with_aria2(source, out_dir, status)

    if looks_like_ytdlp_source(source):
        if is_ytdlp_available():
            return await download_with_ytdlp(source, out_dir, status)
        # yt-dlp missing: still attempt a raw HTTP fetch in case the "link" is
        # actually a direct file (some hosts serve both page and file URLs).
        try:
            return await download_direct_http_fast(source, out_dir, status)
        except Exception:
            raise RuntimeError(
                "yt-dlp is not installed on this dyno, so this social/media-site link can't be "
                "extracted. Add `yt-dlp` to requirements.txt and redeploy, or use a direct file URL."
            )

    return await download_direct_http_fast(source, out_dir, status)


async def prepare_branding(file_path: Path, thumb: str | None, status: Message) -> Path:
    if not Config.ENABLE_MEDIA_BRANDING or not is_video_file(str(file_path)):
        return file_path
    await status.edit("🎨 **Adding watermark + metadata...**", reply_markup=leech_keyboard())
    branded = file_path.with_name(f"branded_{file_path.name}")
    result = await add_video_branding(str(file_path), str(branded), Config.WATERMARK_TEXT, Config.METADATA_TEXT)
    return Path(result)


async def upload_leech_file(client: Client, message: Message, file_path: Path, status: Message, target_chats: list[int | str] | None = None):
    size = file_path.stat().st_size
    if size > Config.MAX_UPLOAD_SIZE:
        raise RuntimeError(
            f"File is {humanbytes(size)} but MAX_UPLOAD_SIZE is {humanbytes(Config.MAX_UPLOAD_SIZE)}. "
            "For 2GB+ uploads configure PREMIUM_SESSION_STRING and run as a user-capable Pyrogram client."
        )
    target_chats = target_chats or [message.chat.id]
    thumb_file = str(LEECH_ROOT / f"thumb_{message.id}.jpg")
    thumb = await download_thumbnail(Config.GLOBAL_THUMBNAIL_URL, thumb_file) if Config.GLOBAL_THUMBNAIL_URL else None
    upload_path = await prepare_branding(file_path, thumb, status)
    cover = make_cover_image(str(LEECH_ROOT / f"cover_{message.id}.jpg"), upload_path.name, thumb, Config.METADATA_TEXT)
    caption = f"📦 **{upload_path.name}**\n💾 Size: `{humanbytes(upload_path.stat().st_size)}`\n\n{Config.METADATA_TEXT}"
    await status.edit("📤 **Uploading leech file...**", reply_markup=leech_keyboard())
    async with upload_semaphore:
        for chat_id in target_chats:
            if Config.SEND_COVER_BEFORE_UPLOAD and cover:
                await client.send_photo(chat_id, cover, caption="🖼️ Cover preview")
            if is_video_file(str(upload_path)):
                await run_with_floodwait_retry(lambda chat_id=chat_id: client.send_video(
                    chat_id, str(upload_path), caption=caption,
                    thumb=thumb if thumb and os.path.exists(thumb) else None,
                    supports_streaming=True,
                    progress=progress_for_pyrogram,
                    progress_args=("📤 Uploading leech...", status, time.time(), 0, 20),
                ), "leech video upload")
            else:
                await run_with_floodwait_retry(lambda chat_id=chat_id: client.send_document(
                    chat_id, str(upload_path), caption=caption,
                    thumb=thumb if thumb and os.path.exists(thumb) else None,
                    progress=progress_for_pyrogram,
                    progress_args=("📤 Uploading leech...", status, time.time(), 0, 20),
                ), "leech document upload")


async def run_leech_job(client: Client, message: Message, source: str, target_chats: list[int | str] | None = None):
    status = await message.reply_text("🚀 **Leech job queued**", reply_markup=leech_keyboard())
    workdir = LEECH_ROOT / f"job_{message.chat.id}_{message.id}_{int(time.time())}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        file_path = await route_download(source, workdir, status)
        await upload_leech_file(client, message, file_path, status, target_chats=target_chats)
        await status.edit("✅ **Leech complete!**", reply_markup=leech_keyboard())
    except Exception as e:
        await status.edit(f"❌ **Leech failed:** `{str(e)[:900]}`", reply_markup=leech_keyboard())
    finally:
        if Config.CLEAN_DOWNLOADS:
            shutil.rmtree(workdir, ignore_errors=True)


@Client.on_message(filters.private & filters.command("link"))
async def stream_link_cmd(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can create stream links.")
    if not message.reply_to_message or not (message.reply_to_message.document or message.reply_to_message.video or message.reply_to_message.audio or message.reply_to_message.photo):
        return await message.reply_text("Reply to a Telegram file/video/audio/photo with `/link`.")
    if not Config.BASE_URL:
        return await message.reply_text("Set `BASE_URL` to your Heroku app URL first, for example `https://your-app.herokuapp.com`.")
    target = message.reply_to_message
    file_name = get_media_name(target)
    token = register_stream(target.chat.id, target.id, file_name, Config.STREAM_LINK_TTL)
    url = build_stream_url(Config.BASE_URL, token, file_name)
    await message.reply_text(
        f"🔗 **Temporary download link**\n\n{url}\n\n⏳ Expires in `{Config.STREAM_LINK_TTL // 60}` minutes.",
        disable_web_page_preview=True,
    )


@Client.on_message(filters.private & filters.command(["leech", "magnet", "torrent"]))
async def leech_cmd(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use leech commands.")
    source = " ".join(message.command[1:]).strip()
    if not source and message.reply_to_message:
        sources = extract_leech_sources(message.reply_to_message.text or message.reply_to_message.caption)
        if sources:
            source = sources[0]
        elif message.reply_to_message.document:
            # Fetch the replied .torrent/control file the same way /link would serve it:
            # mint a temporary stream token for the message and pull it over HTTP,
            # instead of downloading it straight from Telegram via MTProto.
            status = await message.reply_text("📥 Fetching torrent/control file via link...", reply_markup=leech_keyboard())
            workdir = LEECH_ROOT / f"job_{message.id}_torrent"
            workdir.mkdir(parents=True, exist_ok=True)
            dest = workdir / get_media_name(message.reply_to_message)
            try:
                source = await fetch_via_link(client, message.reply_to_message, str(dest), status=status, label="📥 Fetching control file...")
            finally:
                await status.delete()
    elif source:
        sources = extract_leech_sources(source)
        source = sources[0] if sources else source
    if not source:
        return await message.reply_text(
            "Usage: `/leech <direct-url|magnet|torrent-url|youtube/twitter/instagram/tiktok/... link>`\n"
            "or reply to a torrent file/link with `/leech`.",
            reply_markup=leech_keyboard(),
        )
    await run_leech_job(client, message, source)


@Client.on_message(filters.channel)
async def auto_queue_leech_sources(client: Client, message: Message):
    if str(message.chat.id) not in SOURCE_CHANNELS:
        return
    sources = extract_leech_sources(message.text or message.caption)
    source = sources[0] if sources else None
    if not source and message.document and "torrent" in get_media_name(message).lower():
        # Same link-based fetch as above, applied to auto-queued channel .torrent files.
        workdir = LEECH_ROOT / f"channel_{message.chat.id}_{message.id}"
        workdir.mkdir(parents=True, exist_ok=True)
        dest = workdir / get_media_name(message)
        source = await fetch_via_link(client, message, str(dest), label="📥 Fetching control file...")
    if not source:
        return
    await run_leech_job(client, message, source, target_chats=DESTINATION_CHANNELS)
