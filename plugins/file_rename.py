import os
import asyncio
import time
import re

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified, PeerIdInvalid, FloodWait

from helper.utils import progress_for_pyrogram, download_thumbnail, render_completed_status
from helper.media_tools import add_video_branding, is_video_file, make_cover_image
from helper.database import mnbots
from helper.telegram_fetch import fetch_via_link
from config import Config

BOT_ID = None

async def init_bot_id(client):
    global BOT_ID
    if BOT_ID is None:
        BOT_ID = (await client.get_me()).id

BASE_DOWNLOAD_PATH = "downloads"
file_queue = asyncio.Queue()
channel_jobs = {}  # job_key(chat_id:message_id) -> Message object
active_downloads = 0  # Track currently active downloads
download_lock = asyncio.Lock()  # Lock for thread-safe counter updates
upload_semaphore = asyncio.Semaphore(min(5, max(1, Config.MAX_CONCURRENT_UPLOADS)))
transfer_stats = {
    "download_bytes": 0,
    "upload_bytes": 0,
    "download_files": 0,
    "upload_files": 0,
    "download_time": 0.0,
    "upload_time": 0.0,
}

# ============= MULTI-CHANNEL CONFIGURATION ============= #
# Add multiple source channels here (as strings)
SOURCE_CHANNELS = [
    "-1002593865099",
    "-1003157977363",
    "-1003034885508",
    "-1003582975932",
    "-1003344470009",
    "-1003607553739",
    "-1003791195815",
    "-1003793662163"
]

DESTINATION_CHANNELS = ["-1003582579076"]  # Where to upload renamed files
MAX_FILE_SIZE = Config.MAX_UPLOAD_SIZE  # Telegram bot default is 2GB; raise only with user-session upload support.
ADMIN_ID = 1892771262  # Admin user ID for status updates
MAX_CONCURRENT_DOWNLOADS = min(5, max(1, Config.MAX_CONCURRENT_DOWNLOADS))  # Hard cap at 5
MAX_CONCURRENT_UPLOADS = min(5, max(1, Config.MAX_CONCURRENT_UPLOADS))  # Hard cap at 5
MIN_TRANSFER_SPEED_BPS = max(0.0, Config.MIN_TRANSFER_SPEED_MBPS * 1024 * 1024)
SPEED_CHECK_GRACE_SECONDS = max(5, Config.SPEED_CHECK_GRACE_SECONDS)

# ====================================================== #

def create_download_path():
    path = os.path.join(BASE_DOWNLOAD_PATH, "channel_files")
    os.makedirs(path, exist_ok=True)
    return path

TELEGRAM_FILENAME_LIMIT = 64
REMNAME = "@Dramaost,@dramaost,@T4TVSeries,@spotyseries,@TGNetworksTG,TORRENTGALAXY,@TVM_Moviez,@Team_TVMz,@Cinemas_Trend,@Rocky_links,[AWHT],[ss™],@adda_files,[MW],@WMR,@DA_Rips,ss™,@Pirate_Flicks,[CK],@TeamCinemaClub,[MS],[KC],mkvCinemas,themoviesboss,@piro_files,[TF],[MLM],[Ms™],@DramaOST,[D&O],[GC],[SMU],@CC_ALL,[MCU],[MF],[PIRO],@"
REMNAME_TOKENS = [t.strip() for t in REMNAME.split(",") if t.strip()]

def is_admin_user(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in Config.ADMIN)

def make_job_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"

def format_mb_speed(bytes_count: int, secs: float) -> str:
    if secs <= 0:
        return "0.00 MB/s"
    return f"{(bytes_count / (1024 * 1024)) / secs:.2f} MB/s"

def get_queue_source_counts():
    source_counts = {}
    for msg in channel_jobs.values():
        chat_key = str(msg.chat.id)
        source_counts[chat_key] = source_counts.get(chat_key, 0) + 1
    return source_counts

def get_message_media(msg: Message):
    return msg.document or msg.video

def get_message_media_size(msg: Message) -> int:
    media = get_message_media(msg)
    return int(getattr(media, "file_size", 0) or 0)

def parse_message_url(msg_url: str):
    # Public: https://t.me/<username>/<msg_id>
    match_public = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)", msg_url)
    if match_public and match_public.group(1) != "c":
        return match_public.group(1), int(match_public.group(2))
    # Private: https://t.me/c/<internal_id>/<msg_id>
    match_private = re.search(r"(?:https?://)?t\.me/c/(\d+)/(\d+)", msg_url)
    if match_private:
        chat_id = int(f"-100{match_private.group(1)}")
        return chat_id, int(match_private.group(2))
    raise ValueError("Invalid message URL format.")

async def send_admin_message(client: Client, text: str):
    """Safely send message to admin with error handling"""
    try:
        return await client.send_message(ADMIN_ID, text)
    except PeerIdInvalid:
        print(f"[WARN] Cannot send to admin {ADMIN_ID}: Peer ID invalid. Bot needs to interact with admin first.")
        return None
    except Exception as e:
        print(f"[ERROR] Failed to send admin message: {e}")
        return None

async def run_with_floodwait_retry(coro_factory, task_name: str, retries: int = 4):
    for attempt in range(1, retries + 1):
        try:
            return await coro_factory()
        except FloodWait as e:
            wait_for = int(getattr(e, "value", 0) or getattr(e, "x", 0) or 0) + 2
            print(f"[WARN] {task_name}: FloodWait for {wait_for}s (attempt {attempt}/{retries})")
            await asyncio.sleep(wait_for)
        except Exception:
            if attempt >= retries:
                raise
            await asyncio.sleep(min(3 * attempt, 10))
    raise RuntimeError(f"{task_name} failed after {retries} retries")

async def ensure_non_zero_download(client: Client, message: Message, download_path: str, status_msg: Message):
    """Download with retries and validate on-disk size to prevent 0B uploads.

    Fetches via a temporary stream link (the same mechanism as /link) rather than
    pulling the file straight through MTProto, so channel-queue downloads get the
    same parallel range-request speedup as direct-link leeching. fetch_via_link
    reports its own progress onto status_msg and falls back to a direct Pyrogram
    download automatically if the self-serve HTTP path is unavailable.
    """
    for attempt in range(1, 4):
        await run_with_floodwait_retry(
            lambda: fetch_via_link(client, message, download_path, status=status_msg, label="📥 Downloading..."),
            task_name=f"Download {message.id}",
        )
        local_size = os.path.getsize(download_path) if os.path.exists(download_path) else 0
        if local_size > 0:
            return
        print(f"[WARN] Downloaded zero-byte file for message {message.id}, retry {attempt}/3")
        if os.path.exists(download_path):
            os.remove(download_path)
        await asyncio.sleep(attempt)
    raise RuntimeError("Downloaded file size equals 0 B after retries")

async def edit_admin_message(message: Message, text: str):
    """Safely edit admin message with error handling"""
    if message is None:
        return
    try:
        await message.edit(text)
    except MessageNotModified:
        pass
    except FloodWait as e:
        wait_for = int(getattr(e, "value", 0) or getattr(e, "x", 0) or 30) + 2
        print(f"[WARN] Admin edit FloodWait; skipping edits for {wait_for}s")
    except Exception as e:
        print(f"[ERROR] Failed to edit admin message: {e}")

def get_channel_name(chat_id: str) -> str:
    """Get a friendly name for the channel"""
    channel_names = {
        "-1002593865099": "Channel 1",
        "-1002490892111": "Channel 2",
        # Add custom names for your channels:
        # "-1001234567890": "My Movies Channel",
    }
    return channel_names.get(chat_id, f"Channel {chat_id[-4:]}")

async def get_active_download_count():
    """Thread-safe way to get active download count"""
    async with download_lock:
        return active_downloads

async def process_file(client: Client, message: Message):
    global active_downloads
    upload_ok = False
    
    download_path_base = create_download_path()
    source_channel = get_channel_name(str(message.chat.id))

    media = message.document or message.video
    if media is None:
        return

    orig_name = media.file_name or f"file_{message.id}.bin"
    base_name, ext = os.path.splitext(orig_name)
    
    # Remove unwanted tokens from filename
    for tok in REMNAME_TOKENS:
        base_name = base_name.replace(tok, "")
    base_name = " ".join(base_name.split()).strip()

    # Add suffix and ensure filename length is within limit
    suffix = " -@MNTGX.-"
    full_suffix_and_ext = f"{suffix}{ext}"
    max_base_len = TELEGRAM_FILENAME_LIMIT - len(full_suffix_and_ext)
    if len(base_name) > max_base_len:
        base_name = base_name[:max_base_len].rstrip()

    new_name = f"{base_name}{full_suffix_and_ext}"
    unique_name = f"{message.chat.id}_{message.id}_{new_name}"
    download_path = os.path.join(download_path_base, unique_name)

    file_size = media.file_size or 0
    filesize = f"{file_size / (1024*1024):.2f} MB"
    caption = (
        f"📕 Name ➜ : {new_name}\n\n"
        f"📗 Size ➜ : {filesize}\n\n"
        f"powered by #mntgx"
    )

    thumb_file = os.path.join(download_path_base, f"thumb_{message.id}.jpg")
    thumb = None
    if Config.GLOBAL_THUMBNAIL_URL:
        thumb = await download_thumbnail(Config.GLOBAL_THUMBNAIL_URL, thumb_file)

    # Increment active download count
    async with download_lock:
        active_downloads += 1
        current_active = active_downloads
    
    print(f"[INFO] Downloading file from {source_channel}: {new_name} (Size: {filesize}) [Active: {current_active}/{MAX_CONCURRENT_DOWNLOADS}]")
    
    # Send status to admin - Download started
    status_msg = await send_admin_message(
        client,
        f"📥 **Download Started**\n\n"
        f"📺 Source: `{source_channel}`\n"
        f"📄 File: `{new_name}`\n"
        f"📦 Size: {filesize}\n"
        f"🆔 Message ID: `{message.id}`\n"
        f"⚡ Active Downloads: {current_active}/{MAX_CONCURRENT_DOWNLOADS}"
    )
    if status_msg:
        status_msg.progress_name = new_name
        status_msg.progress_user = "MN  -  TG"
        status_msg.progress_user_id = ADMIN_ID
    
    try:
        # Download the file
        dl_start = time.time()
        await ensure_non_zero_download(client, message, download_path, status_msg)
        transfer_stats["download_bytes"] += file_size
        transfer_stats["download_files"] += 1
        transfer_stats["download_time"] += max(time.time() - dl_start, 0.001)
        
        print(f"[INFO] Download complete. Uploading to destination channel...")
        
        # Update admin - Upload started
        await edit_admin_message(
            status_msg,
            f"📤 **Upload Started**\n\n"
            f"📺 Source: `{source_channel}`\n"
            f"📄 File: `{new_name}`\n"
            f"📦 Size: {filesize}\n"
            f"🆔 Message ID: `{message.id}`"
        )
        
        upload_path = download_path
        if Config.ENABLE_MEDIA_BRANDING and is_video_file(download_path):
            branded_path = os.path.join(download_path_base, f"branded_{unique_name}")
            await edit_admin_message(status_msg, "🎨 **Adding watermark + metadata...**")
            upload_path = await add_video_branding(download_path, branded_path, Config.WATERMARK_TEXT, Config.METADATA_TEXT)

        cover_file = os.path.join(download_path_base, f"cover_{message.id}.jpg")
        cover = make_cover_image(cover_file, new_name, thumb if thumb and os.path.exists(thumb_file) else None, Config.METADATA_TEXT)

        # Upload to destination channel(s). Use premium/user session when configured so 2GB+
        # documents can be uploaded via MTProto instead of the bot upload client limit.
        ul_start = time.time()
        uploader = getattr(client, "upload_client", client)
        async with upload_semaphore:
            for target_chat in DESTINATION_CHANNELS:
                if Config.SEND_COVER_BEFORE_UPLOAD and cover:
                    await run_with_floodwait_retry(
                        lambda chat_id=target_chat: client.send_photo(chat_id=chat_id, photo=cover, caption="🖼️ **Cover Preview**\n" + caption),
                        task_name=f"Upload cover {message.id} -> {target_chat}",
                    )
                await run_with_floodwait_retry(
                    lambda chat_id=target_chat: uploader.send_document(
                        chat_id=chat_id,
                        document=upload_path,
                        caption=caption,
                        file_name=new_name,
                        thumb=thumb if thumb and os.path.exists(thumb_file) else None,
                        progress=progress_for_pyrogram,
                        progress_args=(
                            "📤 Uploading file...",
                            status_msg,
                            time.time(),
                            MIN_TRANSFER_SPEED_BPS,
                            SPEED_CHECK_GRACE_SECONDS,
                        ),
                    ),
                    task_name=f"Upload document {message.id} -> {target_chat}",
                )
        transfer_stats["upload_bytes"] += file_size
        transfer_stats["upload_files"] += 1
        transfer_stats["upload_time"] += max(time.time() - ul_start, 0.001)
        upload_ok = True
        
        print(f"[SUCCESS] File uploaded successfully: {new_name}")
        
        # Update admin - Success with the compact leech-style completion card
        await edit_admin_message(
            status_msg,
            render_completed_status(
                new_name,
                os.path.getsize(upload_path) if os.path.exists(upload_path) else file_size,
                dl_start,
                mode="#Leech | #Tg",
                total_files=1,
                by="@Rashimika_madanna777",
                sent_to_pm=True,
            ),
        )
        
    except Exception as e:
        print(f"[ERROR] Processing failed for {new_name}: {e}")
        
        # Notify admin about error
        error_text = (
            f"❌ **Upload Failed**\n\n"
            f"📺 Source: `{source_channel}`\n"
            f"📄 File: `{new_name}`\n"
            f"📦 Size: {filesize}\n"
            f"🆔 Message ID: `{message.id}`\n"
            f"⚠️ Error: `{str(e)[:200]}`"
        )
        
        if status_msg:
            await edit_admin_message(status_msg, error_text)
        else:
            await send_admin_message(client, error_text)
    finally:
        # Decrement active download count
        async with download_lock:
            active_downloads -= 1
            current_active = active_downloads
        
        print(f"[INFO] Active downloads: {current_active}/{MAX_CONCURRENT_DOWNLOADS}")
        
        # Cleanup files
        for f in (download_path, thumb_file, os.path.join(download_path_base, f"branded_{unique_name}"), os.path.join(download_path_base, f"cover_{message.id}.jpg")):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception as e:
                    print(f"[ERROR] Failed to remove file {f}: {e}")

        # Remove from DB only after successful upload(s); failed jobs stay for restart requeue
        if upload_ok:
            await init_bot_id(client)
            await mnbots.remove_job(BOT_ID, message.chat.id, message.id)

    print(f"[DEBUG] Finished processing message {message.id}")

@Client.on_message(filters.channel & (filters.document | filters.video))
async def monitor_channel(client: Client, message: Message):
    # Check if message is from any of the source channels
    chat_id_str = str(message.chat.id)
    if chat_id_str not in SOURCE_CHANNELS:
        return
    
    source_channel = get_channel_name(chat_id_str)
    
    media = message.document or message.video
    if not media:
        print(f"[WARN] Message {message.id} from {source_channel} has no supported media")
        return
    
    file_size = media.file_size or 0
    file_name = media.file_name or f"media_{message.id}"
    
    # Skip files larger than 2GB
    if file_size > MAX_FILE_SIZE:
        skip_msg = (
            f"⏭️ **File Skipped (Too Large)**\n\n"
            f"📺 Source: `{source_channel}`\n"
            f"📄 File: `{file_name}`\n"
            f"📦 Size: {file_size / (1024*1024*1024):.2f} GB\n"
            f"🆔 Message ID: `{message.id}`\n"
            f"⚠️ Reason: Exceeds 2GB limit"
        )
        await send_admin_message(client, skip_msg)
        print(f"[SKIP] File too large from {source_channel}: {file_name} ({file_size / (1024*1024*1024):.2f} GB)")
        return
    
    # Add to queue
    channel_jobs[make_job_key(message.chat.id, message.id)] = message
    await file_queue.put(message)
    
    await init_bot_id(client)
    await mnbots.add_job(BOT_ID, message.chat.id, message.id, message.chat.id)
    
    # Notify admin about new file in queue
    queue_msg = (
        f"📋 **New File Detected**\n\n"
        f"📺 Source: `{source_channel}`\n"
        f"📄 File: `{file_name}`\n"
        f"📦 Size: {file_size / (1024*1024):.2f} MB\n"
        f"🆔 Message ID: `{message.id}`\n"
        f"📊 Queue Position: {len(channel_jobs)}"
    )
    await send_admin_message(client, queue_msg)
    
    print(f"[DEBUG] Queued file from {source_channel}: {file_name} ({file_size / (1024*1024):.2f} MB)")

async def worker(client: Client, worker_id: int):
    """Worker that processes files from the queue"""
    print(f"[DEBUG] Worker {worker_id} started.")
    while True:
        msg = await file_queue.get()
        
        job_key = make_job_key(msg.chat.id, msg.id)
        if job_key in channel_jobs:
            try:
                print(f"[DEBUG] Worker {worker_id} processing message {msg.id}")
                await process_file(client, msg)
            except Exception as e:
                print(f"[ERROR] Worker {worker_id} error processing message {msg.id}: {e}")
                # Send error notification to admin
                error_msg = (
                    f"❌ **Worker Error**\n\n"
                    f"🆔 Message ID: `{msg.id}`\n"
                    f"👷 Worker: {worker_id}\n"
                    f"⚠️ Error: `{str(e)[:200]}`"
                )
                await send_admin_message(client, error_msg)
            finally:
                channel_jobs.pop(job_key, None)
        
        file_queue.task_done()
        print(f"[DEBUG] Worker {worker_id} done with one file.")

async def resume_queued_jobs(client: Client):
    print("[DEBUG] Resuming jobs from DB...")
    await init_bot_id(client)
    jobs = await mnbots.get_all_jobs(BOT_ID)
    
    for job in jobs:
        try:
            msg = await client.get_messages(job["chat_id"], job["message_id"])
            
            # Validate message has document attribute
            media = msg.document or msg.video
            if not msg or not media:
                print(f"[SKIP] Invalid message or no supported media for job {job['message_id']}")
                await mnbots.remove_job(BOT_ID, job["chat_id"], job["message_id"])
                continue
            
            # Skip if file is too large
            if (media.file_size or 0) > MAX_FILE_SIZE:
                print(f"[SKIP] Removing oversized job from DB: {media.file_name}")
                await mnbots.remove_job(BOT_ID, job["chat_id"], job["message_id"])
                continue
            
            channel_jobs[make_job_key(msg.chat.id, msg.id)] = msg
            await file_queue.put(msg)
            print(f"[DEBUG] Re-queued job {job['message_id']}")
            
        except Exception as e:
            print(f"[ERROR] Failed to re-queue job {job.get('message_id', 'unknown')}: {e}")
            # Keep failed jobs in DB so restart/transient errors do not drop queue automatically.

@Client.on_message(filters.private & filters.command("addremname"))
async def add_remname_tokens(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use this command.")
    if len(message.command) < 2:
        return await message.reply_text("Usage: /addremname token1,token2")
    new_tokens = [t.strip() for t in " ".join(message.command[1:]).split(",") if t.strip()]
    for token in new_tokens:
        if token not in REMNAME_TOKENS:
            REMNAME_TOKENS.append(token)
    await message.reply_text(
        f"✅ Added {len(new_tokens)} token(s).\nTotal remove tokens: {len(REMNAME_TOKENS)}"
    )

@Client.on_message(filters.private & filters.command("listremname"))
async def list_remname_tokens(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use this command.")
    tokens_text = ", ".join(REMNAME_TOKENS[:200])
    await message.reply_text(f"🧹 Remove-name tokens ({len(REMNAME_TOKENS)}):\n`{tokens_text}`")

@Client.on_message(filters.private & filters.command("stats"))
async def queue_and_speed_stats(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use this command.")
    source_counts = get_queue_source_counts()
    source_lines = "\n".join(
        [f"• `{get_channel_name(chat_id)}` (`{chat_id}`): {count}" for chat_id, count in source_counts.items()]
    ) or "• No queued files."
    pending_bytes = sum(get_message_media_size(msg) for msg in channel_jobs.values())
    avg_download = format_mb_speed(transfer_stats["download_bytes"], transfer_stats["download_time"])
    avg_upload = format_mb_speed(transfer_stats["upload_bytes"], transfer_stats["upload_time"])
    dl_bps = transfer_stats["download_bytes"] / transfer_stats["download_time"] if transfer_stats["download_time"] > 0 else 0
    ul_bps = transfer_stats["upload_bytes"] / transfer_stats["upload_time"] if transfer_stats["upload_time"] > 0 else 0
    effective_bps = min(v for v in (dl_bps, ul_bps) if v > 0) if (dl_bps > 0 or ul_bps > 0) else 0
    eta_seconds = int(pending_bytes / effective_bps) if effective_bps > 0 else 0
    eta_text = "N/A (collecting speed data)" if eta_seconds <= 0 and pending_bytes > 0 else (
        "0s" if pending_bytes == 0 else f"{eta_seconds // 3600}h {(eta_seconds % 3600) // 60}m {eta_seconds % 60}s"
    )
    stats_text = (
        f"📊 **MNTGX Queue Stats**\n\n"
        f"⚙️ Max Concurrent: `{MAX_CONCURRENT_DOWNLOADS}`\n"
        f"⚡ Min Speed Target: `{Config.MIN_TRANSFER_SPEED_MBPS} MB/s`\n"
        f"📥 Avg Download Speed: `{avg_download}`\n"
        f"📤 Avg Upload Speed: `{avg_upload}`\n"
        f"🧾 Queue Total: `{len(channel_jobs)}`\n"
        f"🔄 Active Now: `{await get_active_download_count()}`\n"
        f"⏱️ Approx queue completion: `{eta_text}`\n"
        f"🎯 Target Chats: `{', '.join(DESTINATION_CHANNELS)}`\n\n"
        f"**Sources & Queue Count**\n{source_lines}\n\n"
        f"✅ Uploaded Files: `{transfer_stats['upload_files']}`\n"
        f"📥 Downloaded Files: `{transfer_stats['download_files']}`"
    )
    await message.reply_text(stats_text)

@Client.on_message(filters.private & filters.command("mntgx"))
async def mntgx_help(client: Client, message: Message):
    text = (
        "🚀 **MNTGX Bot Features**\n\n"
        "• Auto queue from source channels\n"
        "• Rename cleanup tokens\n"
        "• Document + Video forwarding\n"
        "• Telegram files fetched via temporary link (parallel range requests), not direct MTProto pull\n"
        "• Leech: aria2 torrents/magnets, yt-dlp (100s of sites), parallel-range direct HTTP\n"
        "• FloodWait-safe retries\n"
        "• Resume queued jobs after restart\n"
        "• Queue/speed stats with ETA\n\n"
        "**Admin Commands**\n"
        "• `/stats` - queue + speed + ETA stats\n"
        "• `/leech <url|magnet>` - torrent, magnet, direct link, or YouTube/Twitter(X)/Instagram/TikTok/... \n"
        "• `/link` - reply to Telegram media for a temporary browser download link\n"
        "• `/addque <first_msg_url> <last_msg_url>` - bulk queue import\n"
        "• `/addremname token1,token2` - add rename cleanup tokens\n"
        "• `/listremname` - list cleanup tokens\n"
        "• `/cleanque confirm` - clear pending queue jobs\n"
        "• `/addsource <chat_id>` `/removesource <chat_id>` `/listsources`\n"
        "• `/addtarget <chat_id>` `/removetarget <chat_id>` `/listtargets`\n"
        "• `/requeue` - re-import jobs from DB\n"
        "• `/ping` - quick bot health check\n"
    )
    await message.reply_text(text)


@Client.on_message(filters.private & filters.command(["admin", "panel"]))
async def admin_panel(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use this command.")
    await message.reply_text(
        "🛠️ **Admin Panel**\n\n"
        "• `/stats` - queue, speed and ETA\n"
        "• `/broadcast <text>` - send a text broadcast to all target chats\n"
        "• `/setfsub <chat_id|off>` - set or disable force-sub channel for runtime\n"
        "• `/settings` - show current bot settings\n"
        "• `/addsource` `/removesource` `/listsources` - source channel control\n"
        "• `/addtarget` `/removetarget` `/listtargets` - destination control\n"
        "• `/cleanque confirm` - clear queued jobs\n"
        "• `/requeue` - load persisted queue jobs"
    )

@Client.on_message(filters.private & filters.command("settings"))
async def user_settings_panel(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use this command.")
    await message.reply_text(
        "⚙️ **Bot Settings**\n\n"
        f"• Upload mode: `Document/File`\n"
        f"• Media branding: `{Config.ENABLE_MEDIA_BRANDING}`\n"
        f"• Watermark text: `{Config.WATERMARK_TEXT}`\n"
        f"• Watermark duration: `full video if <=5 min, otherwise first 5%`\n"
        f"• Force sub: `{Config.FORCE_SUB or 'off'}`\n"
        f"• Max downloads: `{MAX_CONCURRENT_DOWNLOADS}`\n"
        f"• Max uploads: `{MAX_CONCURRENT_UPLOADS}`\n"
        f"• Targets: `{', '.join(DESTINATION_CHANNELS)}`\n"
        f"• Sources: `{len(SOURCE_CHANNELS)}`"
    )

@Client.on_message(filters.private & filters.command("setfsub"))
async def set_force_sub(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use this command.")
    if len(message.command) < 2:
        return await message.reply_text("Usage: `/setfsub <chat_id|off>`")
    value = message.command[1].strip()
    Config.FORCE_SUB = "" if value.lower() in {"off", "none", "0"} else value
    await message.reply_text(f"✅ Force-sub updated for this runtime: `{Config.FORCE_SUB or 'off'}`")

@Client.on_message(filters.private & filters.command("broadcast"))
async def broadcast_cmd(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use this command.")
    text = message.text.split(maxsplit=1)[1].strip() if message.text and len(message.text.split(maxsplit=1)) > 1 else ""
    if not text and message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not text:
        return await message.reply_text("Usage: `/broadcast <message>` or reply to a text message with `/broadcast`.")
    sent = 0
    failed = 0
    for chat_id in DESTINATION_CHANNELS:
        try:
            await client.send_message(chat_id, text, disable_web_page_preview=True)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"[WARN] Broadcast failed for {chat_id}: {e}")
    await message.reply_text(f"📣 Broadcast complete. Sent: `{sent}` | Failed: `{failed}`")

@Client.on_message(filters.private & filters.command(["cleanque", "clearque"]))
async def clear_queue(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use this command.")
    if len(message.command) < 2 or message.command[1].lower() != "confirm":
        return await message.reply_text("Usage: `/cleanque confirm`", quote=True)

    await init_bot_id(client)
    channel_jobs.clear()
    drained = 0
    while True:
        try:
            file_queue.get_nowait()
            file_queue.task_done()
            drained += 1
        except asyncio.QueueEmpty:
            break

    await mnbots.clear_all_jobs(BOT_ID)
    await message.reply_text(
        f"🧹 Queue cleared.\nRemoved pending in-memory jobs: `{drained}`\nActive worker tasks will finish current item only."
    )

@Client.on_message(filters.private & filters.command("ping"))
async def ping_cmd(client: Client, message: Message):
    if not is_admin_user(message):
        return
    await message.reply_text("🏓 Pong! Bot is alive.")

@Client.on_message(filters.private & filters.command("requeue"))
async def requeue_cmd(client: Client, message: Message):
    if not is_admin_user(message):
        return
    await resume_queued_jobs(client)
    await message.reply_text(f"♻️ Requeue started from DB.\nCurrent queue size: `{len(channel_jobs)}`")

@Client.on_message(filters.private & filters.command("addsource"))
async def add_source(client: Client, message: Message):
    if not is_admin_user(message):
        return
    if len(message.command) < 2:
        return await message.reply_text("Usage: `/addsource -100xxxxxxxxxx`")
    chat_id = message.command[1].strip()
    if chat_id not in SOURCE_CHANNELS:
        SOURCE_CHANNELS.append(chat_id)
    await message.reply_text(f"✅ Source added.\nNow monitoring: `{len(SOURCE_CHANNELS)}` chats.")

@Client.on_message(filters.private & filters.command("removesource"))
async def remove_source(client: Client, message: Message):
    if not is_admin_user(message):
        return
    if len(message.command) < 2:
        return await message.reply_text("Usage: `/removesource -100xxxxxxxxxx`")
    chat_id = message.command[1].strip()
    if chat_id in SOURCE_CHANNELS:
        SOURCE_CHANNELS.remove(chat_id)
        return await message.reply_text(f"🗑️ Source removed: `{chat_id}`")
    await message.reply_text("Source not found.")

@Client.on_message(filters.private & filters.command("listsources"))
async def list_sources(client: Client, message: Message):
    if not is_admin_user(message):
        return
    await message.reply_text("📡 Source channels:\n" + "\n".join(f"• `{c}`" for c in SOURCE_CHANNELS))

@Client.on_message(filters.private & filters.command("addtarget"))
async def add_target(client: Client, message: Message):
    if not is_admin_user(message):
        return
    if len(message.command) < 2:
        return await message.reply_text("Usage: `/addtarget -100xxxxxxxxxx`")
    chat_id = message.command[1].strip()
    if chat_id not in DESTINATION_CHANNELS:
        DESTINATION_CHANNELS.append(chat_id)
    await message.reply_text(f"✅ Target added.\nNow sending to: `{len(DESTINATION_CHANNELS)}` chats.")

@Client.on_message(filters.private & filters.command("removetarget"))
async def remove_target(client: Client, message: Message):
    if not is_admin_user(message):
        return
    if len(message.command) < 2:
        return await message.reply_text("Usage: `/removetarget -100xxxxxxxxxx`")
    chat_id = message.command[1].strip()
    if len(DESTINATION_CHANNELS) <= 1 and chat_id in DESTINATION_CHANNELS:
        return await message.reply_text("At least one target chat is required.")
    if chat_id in DESTINATION_CHANNELS:
        DESTINATION_CHANNELS.remove(chat_id)
        return await message.reply_text(f"🗑️ Target removed: `{chat_id}`")
    await message.reply_text("Target not found.")

@Client.on_message(filters.private & filters.command("listtargets"))
async def list_targets(client: Client, message: Message):
    if not is_admin_user(message):
        return
    await message.reply_text("🎯 Target channels:\n" + "\n".join(f"• `{c}`" for c in DESTINATION_CHANNELS))

@Client.on_message(filters.private & filters.command("addque"))
async def add_queue_from_links(client: Client, message: Message):
    if not is_admin_user(message):
        return await message.reply_text("Only admins can use this command.")
    if len(message.command) != 3:
        return await message.reply_text("Usage:\n/addque {first_msg_url} {last_msg_url}")
    try:
        chat_a, first_id = parse_message_url(message.command[1].strip())
        chat_b, last_id = parse_message_url(message.command[2].strip())
        if chat_a != chat_b:
            return await message.reply_text("Both URLs must belong to the same chat/channel.")
        chat_ref = chat_a
        start_id = min(first_id, last_id)
        end_id = max(first_id, last_id)
        added, skipped = 0, 0
        for msg_id in range(start_id, end_id + 1):
            msg = await client.get_messages(chat_ref, msg_id)
            media = msg.document or msg.video
            if not msg or not media:
                skipped += 1
                continue
            if (media.file_size or 0) > MAX_FILE_SIZE:
                skipped += 1
                continue
            job_key = make_job_key(msg.chat.id, msg.id)
            if job_key in channel_jobs:
                skipped += 1
                continue
            channel_jobs[job_key] = msg
            await file_queue.put(msg)
            await init_bot_id(client)
            await mnbots.add_job(BOT_ID, msg.chat.id, msg.id, msg.chat.id)
            added += 1
        await message.reply_text(
            f"✅ Queue import done.\n"
            f"Source: `{chat_ref}`\n"
            f"Added: `{added}`\nSkipped: `{skipped}`\nTotal Queue: `{len(channel_jobs)}`"
        )
    except Exception as e:
        await message.reply_text(f"❌ Failed to add queue: `{str(e)[:250]}`")

def start_worker(client: Client, num_workers: int = MAX_CONCURRENT_DOWNLOADS):
    """Start multiple workers for concurrent downloads"""
    asyncio.create_task(resume_queued_jobs(client))
    
    # Create worker tasks with IDs
    for i in range(min(5, max(1, num_workers))):
        asyncio.create_task(worker(client, i + 1))
    
    channels_list = ", ".join([get_channel_name(ch) for ch in SOURCE_CHANNELS])
    print(f"[DEBUG] Initialized {min(5, max(1, num_workers))} worker(s) for concurrent downloads.")
    print(f"[DEBUG] Max concurrent downloads: {MAX_CONCURRENT_DOWNLOADS}")
    print(f"[DEBUG] Max concurrent uploads: {MAX_CONCURRENT_UPLOADS}")
    print(f"[DEBUG] Monitoring {len(SOURCE_CHANNELS)} source channel(s): {channels_list}")
