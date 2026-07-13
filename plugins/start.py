from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

from config import Txt


def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧲 Leech", callback_data="leech_help"), InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("⚙️ Commands", callback_data="commands"), InlineKeyboardButton("ℹ️ About", callback_data="about")],
        [InlineKeyboardButton("📣 Join MNTGX", url="https://t.me/mntgx"), InlineKeyboardButton("✖️ Close", callback_data="close")],
    ])


@Client.on_message(filters.private & filters.command("start"))
async def start(client, message):
    user = message.from_user.mention if message.from_user else "there"
    await message.reply_text(Txt.START_TEXT.format(user), reply_markup=home_keyboard(), disable_web_page_preview=True)


@Client.on_message(filters.private & filters.command("help"))
async def help_cmd(client, message):
    await message.reply_text(Txt.HELP_TEXT, reply_markup=home_keyboard(), disable_web_page_preview=True)


@Client.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data or ""
    if data == "close":
        await query.message.delete()
        return
    if data == "home":
        user = query.from_user.mention if query.from_user else "there"
        await query.message.edit_text(Txt.START_TEXT.format(user), reply_markup=home_keyboard(), disable_web_page_preview=True)
    elif data == "commands":
        await query.message.edit_text(Txt.HELP_TEXT, reply_markup=home_keyboard(), disable_web_page_preview=True)
    elif data == "about":
        await query.message.edit_text(Txt.ABOUT_TEXT + "\n• **Power:** Torrent, magnet, metadata, cover and queue automation", reply_markup=home_keyboard())
    elif data == "leech_help":
        await query.message.edit_text(
            "🧲 **Leech: Torrent / Magnet / Direct / 100s of Sites**\n\n"
            "• `/leech <direct-url>`\n"
            "• `/leech <magnet-link>`\n"
            "• `/leech <youtube/twitter/instagram/tiktok/reddit/... link>`\n"
            "• Reply to a `.torrent` file with `/leech`\n\n"
            "Torrents/magnets use aria2c. Social/media links use yt-dlp. Plain file links use a "
            "parallel-range HTTP downloader for speed. Telegram-origin files are fetched through a "
            "temporary link (like `/link`) instead of a direct download. The bot then adds cover/"
            "thumbnail branding and uploads with progress callbacks.",
            reply_markup=home_keyboard(),
        )
    elif data == "stats":
        await query.answer("Use /stats for a fresh private stats report.", show_alert=True)
    else:
        await query.answer()
