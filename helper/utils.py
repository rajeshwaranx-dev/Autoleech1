# helper/utils.py

import math
import os
import shutil
import time
import aiohttp
from config import Config
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

# throttle threshold: 100 MiB
_THROTTLE_BYTES = 256 * 1024 * 1024
_THROTTLE_SECONDS = 10
_last_update: dict[int, int] = {}
_last_update_time: dict[int, float] = {}
_low_speed_hits: dict[int, int] = {}
_edit_blocked_until: dict[int, float] = {}


def _progress_bar(percentage: float, width: int = 15) -> str:
    """Telegram-friendly segmented bar matching the leech status UI."""
    filled = max(0, min(width, math.floor((percentage / 100) * width)))
    return "■" * filled + "□" * (width - filled)


def _compact_bytes(size: float, suffix: str = "B", compact: bool = False) -> str:
    if not size:
        return "0B" if suffix == "B" else "0B/s"
    if compact:
        units = ["B", "KB", "MB", "GB", "TB"] if suffix == "B" else ["B/s", "KB/s", "MB/s", "GB/s", "TB/s"]
    else:
        units = ["B", "KiB", "MiB", "GiB", "TiB"] if suffix == "B" else ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"]
    value = float(size)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return units[idx].replace("B", "0B") if value == 0 else f"{value:.0f}{units[idx] if compact else ' ' + units[idx]}".replace(" B", "B")
    sep = "" if compact else " "
    return f"{value:.2f}{sep}{units[idx]}"


def _ram_percent() -> str:
    try:
        values = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as meminfo:
            for line in meminfo:
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0])
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if total:
            return f"{((total - available) / total) * 100:.1f}%"
    except Exception:
        pass
    return "N/A"


def _bot_stats_footer(speed: float = 0.0, upload: bool = False) -> str:
    uptime = TimeFormatter(int((time.time() - Config.BOT_UPTIME) * 1000))
    try:
        load = os.getloadavg()[0]
        cpu = min(100.0, (load / max(os.cpu_count() or 1, 1)) * 100)
    except Exception:
        cpu = 0.0
    usage = shutil.disk_usage(".")
    free_pct = (usage.free / usage.total * 100) if usage.total else 0
    dl = 0 if upload else speed
    ul = speed if upload else 0
    return (
        "▣ **Bot Stats**\n"
        f"├**CPU:** {cpu:.1f}% | **F:** {_compact_bytes(usage.free, compact=True)} [{free_pct:.1f}%]\n"
        f"├**RAM:** {_ram_percent()} | **UPTIME:** {uptime}\n"
        f"└ **DL:** {_compact_bytes(dl, '/s', compact=True)} | **UL:** {_compact_bytes(ul, '/s', compact=True)}"
    )


def render_transfer_progress(
    name: str, current: int, total: int, status: str, start: float, engine: str = "Pyrogram",
    mode: str = "#Leech | #Tg", user: str = "Unknown", user_id: int | str = "N/A",
    cancel_token: str = "cancelsfw_xxxxx", index: int | None = None, upload: bool = False,
    include_footer: bool = True,
) -> str:
    now = time.time()
    elapsed = max(now - start, 0.001)
    percentage = (current * 100 / total) if total else 0
    speed = current / elapsed
    eta = TimeFormatter(int(((total - current) / speed) * 1000)) if speed and total and current < total else "0s"
    title = f"{index}. " if index is not None else ""
    block = (
        f"*{title}{name}*\n"
        f"│ [{_progress_bar(percentage)}] {percentage:.2f}%\n"
        f"├**Processed:** {_compact_bytes(current)} of {_compact_bytes(total)}\n"
        f"├**Status:** {status} | ETA: {eta}\n"
        f"├**Speed:** {_compact_bytes(speed, '/s')} | Elapsed: {TimeFormatter(int(elapsed * 1000))}\n"
        f"├**Engine:** {engine}\n"
        f"├**Mode:** {mode}\n"
        f"├**User:** {user} | ID: {user_id}\n"
        f"└ /{cancel_token}"
    )
    if include_footer:
        block += f"\n\n{_bot_stats_footer(speed, upload=upload)}"
    return block


def render_completed_status(
    name: str, size: int, start: float, mode: str = "#Leech | #Tg", total_files: int = 1,
    by: str = "@MNTGX", sent_to_pm: bool = True,
) -> str:
    elapsed = TimeFormatter(int((time.time() - start) * 1000))
    destination = "Bot PM (Private)" if sent_to_pm else "target chat"
    return (
        f"*{name}*\n"
        "│\n"
        f"├**Size:** {_compact_bytes(size, compact=True)}\n"
        f"├**Elapsed:** {elapsed}\n"
        f"├**Mode:** {mode}\n"
        f"├**Total Files:** {total_files}\n"
        f"└**By:** {by}\n\n"
        f"1. {name}"
    )

async def progress_for_pyrogram(
    current,
    total,
    ud_type,
    message: Message,
    start,
    min_speed_bps: float = 0.0,
    grace_seconds: int = 20,
):
    """
    Progress callback for pyrogram download/upload operations.
    Handles None message gracefully.
    """
    # If message is None, skip progress updates
    if message is None:
        return
    
    now = time.time()
    diff = now - start

    # Use message.id for tracking
    msg_id = getattr(message, 'id', None)
    if msg_id is None:
        # If message doesn't have id, skip progress update
        return
    
    if now < _edit_blocked_until.get(msg_id, 0):
        return

    last = _last_update.get(msg_id, 0)
    
    # Only update if enough data/time passed or if transfer is complete
    last_t = _last_update_time.get(msg_id, start)
    if (current - last >= _THROTTLE_BYTES) or ((now - last_t) >= _THROTTLE_SECONDS) or (current >= total):
        _last_update[msg_id] = current
        _last_update_time[msg_id] = now

        speed = current / diff if diff > 0 else 0
        upload = "upload" in str(ud_type).lower()
        media_name = getattr(message, "progress_name", None) or str(ud_type).replace("📤", "").replace("📥", "").strip() or "File"
        user = getattr(message, "progress_user", "MN - TG")
        user_id = getattr(message, "progress_user_id", Config.ADMIN[0] if Config.ADMIN else "N/A")
        tmp = render_transfer_progress(
            media_name, current, total, "Upload" if upload else "Download", start,
            engine="Pyrogram", mode="#Leech | #Tg", user=user, user_id=user_id,
            cancel_token=f"cancelsfw_{msg_id}", upload=upload,
        )

        try:
            await message.edit(
                text=tmp,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✖️ 𝖢𝖺𝗇𝖼𝖾𝗅 ✖️", callback_data="close")]]
                )
            )
        except FloodWait as e:
            wait_for = int(getattr(e, "value", 0) or getattr(e, "x", 0) or 30) + 3
            _edit_blocked_until[msg_id] = time.time() + wait_for
            print(f"[WARN] Progress edit FloodWait: pausing edits for {wait_for}s")
        except Exception as e:
            # Silently ignore edit errors (message might be deleted, etc.)
            print(f"[DEBUG] Progress update failed: {e}")

        # Guardrail for consistently slow transfers (monitor-only; never abort transfer)
        if min_speed_bps > 0 and current < total and diff >= grace_seconds:
            if speed < min_speed_bps:
                _low_speed_hits[msg_id] = _low_speed_hits.get(msg_id, 0) + 1
            else:
                _low_speed_hits[msg_id] = 0

            # Track low-speed streak for observability in logs
            if _low_speed_hits.get(msg_id, 0) >= 3:
                print(
                    "[WARN] Transfer speed is below threshold for multiple checks: "
                    f"{humanbytes(speed)}/s < {humanbytes(min_speed_bps)}/s"
                )
                _low_speed_hits[msg_id] = 0

        # Clean up tracking when complete
        if current >= total:
            _last_update.pop(msg_id, None)
            _last_update_time.pop(msg_id, None)
            _low_speed_hits.pop(msg_id, None)
            _edit_blocked_until.pop(msg_id, None)


def humanbytes(size):
    """Convert bytes to human readable format"""
    if not size:
        return ""
    power = 2**10
    n = 0
    units = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power and n < 4:
        size /= power
        n += 1
    return f"{round(size, 2)} {units[n]}B"


def TimeFormatter(ms: int) -> str:
    """Format milliseconds to human readable time string"""
    secs, ms = divmod(ms, 1000)
    mins, secs = divmod(secs, 60)
    hrs, mins = divmod(mins, 60)
    days, hrs = divmod(hrs, 24)
    
    parts = []
    if days:
        parts.append(f"{days}d")
    if hrs:
        parts.append(f"{hrs}h")
    if mins:
        parts.append(f"{mins}m")
    if secs:
        parts.append(f"{secs}s")
    if ms and not parts:  # Only show ms if no larger units
        parts.append(f"{ms}ms")
    
    return "".join(parts) if parts else "0s"


async def download_thumbnail(image_url, save_path):
    """Asynchronously download an image to save_path."""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(image_url, timeout=30) as resp:
                if resp.status == 200:
                    with open(save_path, 'wb') as f:
                        while True:
                            chunk = await resp.content.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                    return save_path
    except Exception as e:
        print(f"[ERROR] Thumbnail download failed: {e}")
    return None
