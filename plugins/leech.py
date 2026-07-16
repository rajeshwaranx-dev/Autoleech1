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
    rewrite_to_direct_url,
)
from helper.stream_links import build_stream_url, register_stream
from helper.utils import download_thumbnail, humanbytes, progress_for_pyrogram, render_completed_status
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

# Max seconds aria2c can go without producing a line of output before it's considered
# stalled and killed. Resets on every line, so a torrent that's still actively
# downloading (however slowly) is never affected -- only a subprocess gone fully silent.
ARIA2_STALL_TIMEOUT_SECONDS = max(30, int(os.environ.get("ARIA2_STALL_TIMEOUT", "180")))
# Passed to aria2c's own --bt-stop-timeout: gives up on a torrent with zero download
# activity (e.g. no peers/seeders) for this many seconds. Defense in depth alongside
# the Python-level stall guard above.
ARIA2_IDLE_STOP_SECONDS = max(60, int(os.environ.get("ARIA2_IDLE_STOP", "300")))
# Absolute ceiling on a single aria2c job regardless of activity, so a pathological
# case that keeps barely producing output can't occupy a leech slot indefinitely.
ARIA2_MAX_JOB_SECONDS = max(300, int(os.environ.get("ARIA2_MAX_JOB_SECONDS", str(6 * 60 * 60))))

# ----------------------------------------------------------------------------
# Leech queue — same shape as file_rename.py's file_queue/channel_jobs (a real
# asyncio.Queue plus a visibility dict, consumed by a fixed-size worker pool)
# instead of a semaphore wrapped around a task that's already been started. This
# gives leech jobs actual queue position, /stats visibility, and a genuine
# concurrency bound enforced BEFORE a job starts using resources, not just a gate
# around a coroutine that was already scheduled.
# ----------------------------------------------------------------------------

LEECH_WORKER_COUNT = min(4, max(1, int(os.environ.get("MAX_CONCURRENT_LEECH", "2"))))
leech_queue: asyncio.Queue = asyncio.Queue()
leech_jobs: dict[str, dict] = {}  # job_id -> {message, source, target_chats, queued_at}
_leech_job_counter = 0


def _next_leech_job_id() -> str:
    global _leech_job_counter
    _leech_job_counter += 1
    return f"leech_{_leech_job_counter}_{int(time.time())}"


async def enqueue_leech_job(client: Client, message: Message, source: str, target_chats: list[int | str] | None = None) -> str:
    """Put a leech source on the real queue and return its job ID. The status message
    is created immediately so the person sees a response right away; the actual
    download only starts once a worker picks the job up."""
    job_id = _next_leech_job_id()
    ahead = leech_queue.qsize()
    total_tracked = len(leech_jobs) + 1
    position_text = "🚀 **Leech job queued** — starting now" if ahead == 0 and len(leech_jobs) < LEECH_WORKER_COUNT else (
        f"🚀 **Leech job queued** ({ahead} job{'s' if ahead != 1 else ''} ahead, {total_tracked} total)"
    )
    status = await message.reply_text(position_text, reply_markup=leech_keyboard())
    leech_jobs[job_id] = {
        "client": client,
        "message": message,
        "source": source,
        "target_chats": target_chats,
        "status": status,
        "queued_at": time.time(),
    }
    await leech_queue.put(job_id)
    return job_id


async def leech_worker(worker_id: int):
    print(f"[DEBUG] Leech worker {worker_id} started.")
    while True:
        job_id = await leech_queue.get()
        job = leech_jobs.get(job_id)
        if job is not None:
            try:
                await run_leech_job(
                    job["client"], job["message"], job["source"],
                    target_chats=job["target_chats"], status=job["status"],
                )
            except Exception as e:
                print(f"[ERROR] Leech worker {worker_id} error on {job_id}: {e}")
            finally:
                leech_jobs.pop(job_id, None)
        leech_queue.task_done()


def start_leech_workers():
    for i in range(LEECH_WORKER_COUNT):
        asyncio.create_task(leech_worker(i + 1))
    print(f"[DEBUG] Initialized {LEECH_WORKER_COUNT} leech worker(s).")



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
        # DHT is the primary peer-discovery fallback for magnets whose embedded
        # trackers are slow, overloaded, or dead -- extremely common for the kind of
        # scene-release magnets this bot leeches. Disabling it (as this used to)
        # leaves aria2c with zero way to find peers when trackers don't respond,
        # producing a permanent "CN:0 SD:0 DL:0B" hang -- this is the exact failure
        # mode documented in aria2/aria2 issue #458. LPD (local peer discovery) stays
        # off since it only helps on a LAN, which a cloud dyno never has.
        "--bt-enable-lpd=false", "--enable-dht=true", "--enable-dht6=true",
        "--max-tries=5", "--retry-wait=3",
        # Defense in depth against a wedged job: aria2c itself gives up if there's no
        # download activity for this many seconds (e.g. a magnet with zero peers),
        # independent of the Python-level stall guard below.
        f"--bt-stop-timeout={ARIA2_IDLE_STOP_SECONDS}",
        "--dir", str(out_dir), source,
    ]
    cn_re = re.compile(r"CN:(\d+)")
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    last_edit = 0.0
    output_tail = ""
    job_deadline = time.time() + ARIA2_MAX_JOB_SECONDS
    zero_peers_since: float | None = None
    try:
        while True:
            if time.time() > job_deadline:
                raise RuntimeError(f"aria2c job exceeded the {ARIA2_MAX_JOB_SECONDS}s ceiling and was aborted.")
            try:
                # A readline() with no timeout can block this coroutine forever if the
                # subprocess goes silent without exiting -- that would wedge whichever
                # task is awaiting this leech job indefinitely. Bound the gap between
                # lines instead of the whole job, so a torrent that's still actively
                # producing output can run as long as it needs to.
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=ARIA2_STALL_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"aria2c produced no output for {ARIA2_STALL_TIMEOUT_SECONDS}s and was aborted "
                    "(likely a dead/unreachable torrent)."
                )
            if not line:
                break
            decoded = line.decode(errors="ignore")
            output_tail = (output_tail + decoded)[-1200:]

            # The silence guard above only catches aria2c going fully quiet. It does
            # NOT catch a torrent that keeps emitting periodic summary lines (every
            # --summary-interval=5s) while genuinely stuck at zero peer connections
            # (e.g. dead trackers with DHT still bootstrapping, or a magnet with no
            # real seeders at all) -- each line resets the silence timer even though
            # no real progress is happening. Track CN: (connection count) separately:
            # if it stays at zero for the same stall window, abort with a specific,
            # actionable message instead of leaving the job to hang until the
            # multi-hour job ceiling or an external interruption (a Heroku restart,
            # etc.) kills it uncleanly.
            cn_match = cn_re.search(decoded)
            if cn_match:
                if int(cn_match.group(1)) > 0:
                    zero_peers_since = None
                else:
                    now = time.time()
                    if zero_peers_since is None:
                        zero_peers_since = now
                    elif now - zero_peers_since > ARIA2_STALL_TIMEOUT_SECONDS:
                        raise RuntimeError(
                            f"No peers found for {ARIA2_STALL_TIMEOUT_SECONDS}s (CN:0 the whole time) -- "
                            "this magnet's trackers aren't responding and DHT couldn't find peers either. "
                            "The torrent may have no active seeders, or may need more time for DHT to "
                            "bootstrap. Try again, or use a different source."
                        )

            if time.time() - last_edit > 8:
                last_edit = time.time()
                try:
                    await status.edit(f"🧲 **Leeching...**\n```{output_tail[-700:]}```", reply_markup=leech_keyboard())
                except Exception:
                    pass
    except Exception:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
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
    2. Share link from a host with a confirmed direct-download API behind it (currently
       Pixeldrain) -> rewritten to the real endpoint, then handled as a direct link.
    3. Known media/social site (YouTube, X/Twitter, Instagram, TikTok, Reddit, GoFile, ...)
       -> yt-dlp, which understands page/API extraction instead of just fetching bytes.
    4. Anything else that looks like a plain file URL -> the fast parallel-range HTTP downloader.
    """
    if _is_torrentish(source):
        return await download_with_aria2(source, out_dir, status)

    direct_url = rewrite_to_direct_url(source)
    if direct_url:
        return await download_direct_http_fast(direct_url, out_dir, status)

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
    if not is_video_file(str(file_path)):
        return file_path
    if not Config.ENABLE_MEDIA_BRANDING:
        # See the matching note in plugins/file_rename.py: this is the most common
        # cause of "watermark not added" reports, and the feature itself works --
        # it's just off by default. Log it explicitly instead of skipping silently.
        print(f"[INFO] Skipping watermark for {file_path.name}: ENABLE_MEDIA_BRANDING is not set to 1.")
        return file_path
    await status.edit("🎨 **Adding watermark + metadata...**", reply_markup=leech_keyboard())
    branded = file_path.with_name(f"branded_{file_path.name}")
    result = await add_video_branding(str(file_path), str(branded), Config.WATERMARK_TEXT, Config.METADATA_TEXT)
    return Path(result)


async def upload_leech_file(client: Client, message: Message, file_path: Path, status: Message, target_chats: list[int | str] | None = None):
    if not file_path.exists():
        raise RuntimeError(
            f"Downloaded file is missing at upload time: `{file_path}`. The download step "
            "reported success but the file isn't on disk anymore -- this can happen if the "
            "host returned an error page that looked like a valid response, or if disk space "
            "ran out. Try the link again; if it keeps happening, check server logs for what "
            "the download step actually wrote."
        )
    size = file_path.stat().st_size
    limit = Config.effective_max_upload_size()
    if size > limit:
        hint = (
            "Configure PREMIUM_SESSION_STRING with a session from an account that has an "
            "active Telegram Premium subscription to raise this to 4GB (the session string "
            "alone doesn't help unless the account is genuinely Premium -- Telegram caps "
            "regular accounts, bot or user, at 2GB either way)."
            if not Config.PREMIUM_SESSION_STRING
            else "This already reflects the 4GB Premium ceiling; Telegram does not allow larger single-file uploads."
        )
        raise RuntimeError(f"File is {humanbytes(size)} but the effective upload limit is {humanbytes(limit)}. {hint}")
    target_chats = target_chats or [message.chat.id]
    thumb_file = str(LEECH_ROOT / f"thumb_{message.id}.jpg")
    thumb = await download_thumbnail(Config.GLOBAL_THUMBNAIL_URL, thumb_file) if Config.GLOBAL_THUMBNAIL_URL else None
    upload_path = await prepare_branding(file_path, thumb, status)
    cover = make_cover_image(str(LEECH_ROOT / f"cover_{message.id}.jpg"), upload_path.name, thumb, Config.METADATA_TEXT)
    caption = f"📦 **{upload_path.name}**\n💾 Size: `{humanbytes(upload_path.stat().st_size)}`\n\n{Config.METADATA_TEXT}"
    await status.edit("📤 **Uploading leech file...**", reply_markup=leech_keyboard())
    status.progress_name = upload_path.name
    status.progress_user = "MN  -  TG"
    status.progress_user_id = message.from_user.id if message.from_user else (Config.ADMIN[0] if Config.ADMIN else "N/A")
    uploader = getattr(client, "upload_client", client)
    async with upload_semaphore:
        for chat_id in target_chats:
            if Config.SEND_COVER_BEFORE_UPLOAD and cover:
                await client.send_photo(chat_id, cover, caption="🖼️ Cover preview")
            await run_with_floodwait_retry(lambda chat_id=chat_id: uploader.send_document(
                chat_id, str(upload_path), caption=caption,
                file_name=upload_path.name,
                thumb=thumb if thumb and os.path.exists(thumb) else None,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading file...", status, time.time(), 0, 20),
            ), "leech document upload")


async def run_leech_job(client: Client, message: Message, source: str, target_chats: list[int | str] | None = None, status: Message | None = None):
    if status is None:
        status = await message.reply_text("🚀 **Leech job starting**", reply_markup=leech_keyboard())
    else:
        try:
            await status.edit("🚀 **Leech job starting**", reply_markup=leech_keyboard())
        except Exception:
            pass
    workdir = LEECH_ROOT / f"job_{message.chat.id}_{message.id}_{int(time.time())}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        file_path = await route_download(source, workdir, status)
        started = time.time()
        await upload_leech_file(client, message, file_path, status, target_chats=target_chats)
        await status.edit(
            render_completed_status(
                file_path.name,
                file_path.stat().st_size if file_path.exists() else 0,
                started,
                mode="#Leech | #Tg",
                total_files=1,
                by=f"{message.from_user.mention if message.from_user else 'Unknown'}",
                sent_to_pm=not target_chats,
            ),
            reply_markup=leech_keyboard(),
        )
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
            # Fetch the replied .torrent/control file directly. A stream-link fetch
            # would still have to make the same underlying MTProto call to get the
            # bytes from Telegram, just wrapped in an extra self-HTTP hop -- no
            # upside for a file this small, so keep it simple.
            status = await message.reply_text("📥 Fetching torrent/control file...", reply_markup=leech_keyboard())
            workdir = LEECH_ROOT / f"job_{message.id}_torrent"
            workdir.mkdir(parents=True, exist_ok=True)
            dest = workdir / get_media_name(message.reply_to_message)
            try:
                await client.download_media(message=message.reply_to_message, file_name=str(dest))
                source = str(dest)
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
    await enqueue_leech_job(client, message, source)


@Client.on_message(filters.channel)
async def auto_queue_leech_sources(client: Client, message: Message):
    if str(message.chat.id) not in SOURCE_CHANNELS:
        return
    sources = extract_leech_sources(message.text or message.caption)
    if not sources and message.document and "torrent" in get_media_name(message).lower():
        # Same direct fetch as above, applied to auto-queued channel .torrent files.
        workdir = LEECH_ROOT / f"channel_{message.chat.id}_{message.id}"
        workdir.mkdir(parents=True, exist_ok=True)
        dest = workdir / get_media_name(message)
        await client.download_media(message=message, file_name=str(dest))
        sources = [str(dest)]
    if not sources:
        return
    # A single channel post can legitimately list more than one link (mirrors,
    # quality options, GoFile + Pixeldrain + direct alternatives, etc.) -- queue all
    # of them as separate jobs rather than only the first. Each goes onto the real
    # leech_queue, so LEECH_WORKER_COUNT bounds how many actually run at once
    # regardless of how many get enqueued here.
    for source in sources:
        await enqueue_leech_job(client, message, source, target_chats=DESTINATION_CHANNELS)
