# helper/utils.py

import math
import os
import shutil
import time
import aiohttp
from config import Config, Txt
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

# throttle threshold: 100 MiB
_THROTTLE_BYTES = 100 * 1024 * 1024
_THROTTLE_SECONDS = 2
_last_update: dict[int, int] = {}
_last_update_time: dict[int, float] = {}
_low_speed_hits: dict[int, int] = {}


def _progress_bar(percentage: float, width: int = 18) -> str:
    filled = max(0, min(width, math.floor((percentage / 100) * width)))
    return "█" * filled + "░" * (width - filled)


def _compact_bytes(size: float, suffix: str = "B") -> str:
    if not size:
        return "0B" if suffix == "B" else "0B/s"
    units = ["", "Ki", "Mi", "Gi", "Ti"] if suffix == "B" else ["", "Ki", "Mi", "Gi", "Ti"]
    value = float(size)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.2f} {units[idx]}{suffix}" if idx else f"{value:.0f}{suffix}"


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
        "◈ Bot Stats\n"
        f"├─ CPU: {cpu:.1f}% | Free: {_compact_bytes(usage.free)} [{free_pct:.1f}%]\n"
        f"├─ RAM: N/A | UPTIME: {uptime}\n"
        f"└─ DL: {_compact_bytes(dl, '/s')} | UL: {_compact_bytes(ul, '/s')}"
    )


def render_transfer_progress(
    name: str, current: int, total: int, status: str, start: float, engine: str = "Pyrogram",
    mode: str = "#Leech", user: str = "Unknown", user_id: int | str = "N/A",
    cancel_token: str = "cancelsfw_xxxxx", index: int | None = None, upload: bool = False,
) -> str:
    now = time.time()
    elapsed = max(now - start, 0.001)
    percentage = (current * 100 / total) if total else 0
    speed = current / elapsed
    eta = TimeFormatter(int(((total - current) / speed) * 1000)) if speed and total and current < total else "0s"
    title = f"{index}\n " if index is not None else ""
    title += f"*{name}*"
    return (
        f"{title}\n"
        f"   [{_progress_bar(percentage)}] {percentage:.2f}%\n"
        f"├─ Processed: {_compact_bytes(current)} of {_compact_bytes(total)}\n"
        f"├─ Status: {status} | ETA: {eta}\n"
        f"├─ Speed: {_compact_bytes(speed, '/s')} | Elapsed: {TimeFormatter(int(elapsed * 1000))}\n"
        f"├─ Engine: {engine}\n"
        f"├─ Mode: {mode}\n"
        f"├─ User: {user} | ID: {user_id}\n"
        f"└─ /{cancel_token}\n\n"
        f"{_bot_stats_footer(speed, upload=upload)}"
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
            engine="Pyrogram", mode="#Leech | #Telegram", user=user, user_id=user_id,
            cancel_token=f"cancelsfw_{msg_id}", upload=upload,
        )

        try:
            await message.edit(
                text=tmp,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✖️ 𝖢𝖺𝗇𝖼𝖾𝗅 ✖️", callback_data="close")]]
                )
            )
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
    
    return ", ".join(parts) if parts else "0s"


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
