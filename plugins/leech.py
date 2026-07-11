import asyncio
import os
import shutil
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from helper.media_tools import add_video_branding, is_video_file, make_cover_image
from helper.utils import download_thumbnail, humanbytes, progress_for_pyrogram
from plugins.file_rename import is_admin_user, run_with_floodwait_retry, upload_semaphore

LEECH_ROOT = Path("downloads/leech")
LEECH_ROOT.mkdir(parents=True, exist_ok=True)
MAGNET_PREFIX = "magnet:?"


def leech_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("🧲 Leech Help", callback_data="leech_help")],
        [InlineKeyboardButton("⚙️ Commands", callback_data="commands"), InlineKeyboardButton("✖️ Close", callback_data="close")],
    ])


def find_largest_file(folder: Path) -> Path | None:
    files = [p for p in folder.rglob("*") if p.is_file() and not p.name.endswith(".aria2")]
    return max(files, key=lambda p: p.stat().st_size, default=None)


async def download_with_aria2(source: str, out_dir: Path, status: Message) -> Path:
    if not shutil.which("aria2c"):
        raise RuntimeError("aria2c is not installed. Add it to the Heroku buildpack/Docker image for torrent and magnet leeching.")
    cmd = [
        "aria2c", "--seed-time=0", "--summary-interval=5", "--console-log-level=warn",
        "--max-connection-per-server=8", "--split=8", "--min-split-size=1M",
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


async def prepare_branding(file_path: Path, thumb: str | None, status: Message) -> Path:
    if not Config.ENABLE_MEDIA_BRANDING or not is_video_file(str(file_path)):
        return file_path
    await status.edit("🎨 **Adding watermark + metadata...**", reply_markup=leech_keyboard())
    branded = file_path.with_name(f"branded_{file_path.name}")
    result = await add_video_branding(str(file_path), str(branded), Config.WATERMARK_TEXT, Config.METADATA_TEXT)
    return Path(result)


async def upload_leech_file(client: Client, message: Message, file_path: Path, status: Message):
    size = file_path.stat().st_size
    if size > Config.MAX_UPLOAD_SIZE:
        raise RuntimeError(
            f"File is {humanbytes(size)} but MAX_UPLOAD_SIZE is {humanbytes(Config.MAX_UPLOAD_SIZE)}. "
            "For 2GB+ uploads configure PREMIUM_SESSION_STRING and run as a user-capable Pyrogram client."
        )
    thumb_file = str(LEECH_ROOT / f"thumb_{message.id}.jpg")
    thumb = await download_thumbnail(Config.GLOBAL_THUMBNAIL_URL, thumb_file) if Config.GLOBAL_THUMBNAIL_URL else None
    upload_path = await prepare_branding(file_path, thumb, status)
    cover = make_cover_image(str(LEECH_ROOT / f"cover_{message.id}.jpg"), upload_path.name, thumb, Config.METADATA_TEXT)
    caption = f"📦 **{upload_path.name}**\n💾 Size: `{humanbytes(upload_path.stat().st_size)}`\n\n{Config.METADATA_TEXT}"
    if Config.SEND_COVER_BEFORE_UPLOAD and cover:
        await client.send_photo(message.chat.id, cover, caption="🖼️ Cover preview")
    await status.edit("📤 **Uploading leech file...**", reply_markup=leech_keyboard())
    async with upload_semaphore:
        if is_video_file(str(upload_path)):
            await run_with_floodwait_retry(lambda: client.send_video(
                message.chat.id, str(upload_path), caption=caption,
                thumb=thumb if thumb and os.path.exists(thumb) else None,
                supports_streaming=True,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading leech...", status, time.time(), 0, 20),
            ), "leech video upload")
        else:
            await run_with_floodwait_retry(lambda: client.send_document(
                message.chat.id, str(upload_path), caption=caption,
                thumb=thumb if thumb and os.path.exists(thumb) else None,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading leech...", status, time.time(), 0, 20),
            ), "leech document upload")


@Client.on_message(filters.private & filters.command(["leech", "magnet", "torrent"] ))
async def leech_cmd(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use leech commands.")
    source = " ".join(message.command[1:]).strip()
    if not source and message.reply_to_message and message.reply_to_message.document:
        status = await message.reply_text("📥 Downloading .torrent file...", reply_markup=leech_keyboard())
        workdir = LEECH_ROOT / f"job_{message.id}"
        workdir.mkdir(parents=True, exist_ok=True)
        torrent_path = await client.download_media(message.reply_to_message, file_name=str(workdir / "input.torrent"))
        source = torrent_path
    elif not source:
        return await message.reply_text("Usage: `/leech <direct-url|magnet>` or reply to a `.torrent` file with `/leech`.", reply_markup=leech_keyboard())
    status = await message.reply_text("🚀 **Leech job queued**", reply_markup=leech_keyboard())
    workdir = LEECH_ROOT / f"job_{message.id}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        file_path = await download_with_aria2(source, workdir, status)
        await upload_leech_file(client, message, file_path, status)
        await status.edit("✅ **Leech complete!**", reply_markup=leech_keyboard())
    except Exception as e:
        await status.edit(f"❌ **Leech failed:** `{str(e)[:900]}`", reply_markup=leech_keyboard())
    finally:
        if Config.CLEAN_DOWNLOADS:
            shutil.rmtree(workdir, ignore_errors=True)
