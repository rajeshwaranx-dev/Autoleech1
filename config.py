import os
import time
import re

# Regular expression to validate ID format
id_pattern = re.compile(r'^\d+$')

class Config(object):
    # Pyrogram client config
    API_ID = os.environ.get("API_ID", "23361081")
    API_HASH = os.environ.get("API_HASH", "0605c5395b91ead763072251e20c3417")
    
    # Fixing BOT_TOKENS extraction from environment
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "7129661875:AAGwTIeTMNqZlxfcitOZzBFMHkX6BKWxkA4")

    # Database config
    DB_NAME = os.environ.get("DB_NAME", "Cluster0")
    DB_URL = os.environ.get("DB_URL", "mongodb+srv://rkbotz:rkbot@cluster0.rmbxxy8.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
    BOT_UPTIME = time.time()
    GLOBAL_THUMBNAIL_URL = os.environ.get("GLOBAL_THUMBNAIL_URL", "https://i.ibb.co/MDwd1f3D/6087047735061627461.jpg")
    START_PIC = os.environ.get("START_PIC", "https://i.ibb.co/MDwd1f3D/6087047735061627461.jpg")
    ADMIN = [int(admin) if id_pattern.search(admin) else admin for admin in os.environ.get('ADMIN', '1892771262').split()]

    # Channels logs
    FORCE_SUB = os.environ.get("FORCE_SUB", "")
    LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", "-1003918578835"))

    # Webhook response configuration     
    WEBHOOK = bool(int(os.environ.get("WEBHOOK", True)))
    PORT = os.environ.get("PORT", "8155") # Use 1 for True (instead of True/False)
    BASE_URL = os.environ.get("BASE_URL", "")
    KEEP_ALIVE = bool(int(os.environ.get("KEEP_ALIVE", "1")))
    KEEP_ALIVE_URL = os.environ.get("KEEP_ALIVE_URL", BASE_URL).rstrip("/")
    KEEP_ALIVE_INTERVAL = max(60, int(os.environ.get("KEEP_ALIVE_INTERVAL", "600")))
    STREAM_LINK_TTL = int(os.environ.get("STREAM_LINK_TTL", str(6 * 60 * 60)))
    PYROGRAM_WORKERS = min(64, max(8, int(os.environ.get("PYROGRAM_WORKERS", "24"))))
    MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "10"))
    MAX_CONCURRENT_UPLOADS = int(os.environ.get("MAX_CONCURRENT_UPLOADS", "10"))
    MIN_TRANSFER_SPEED_MBPS = float(os.environ.get("MIN_TRANSFER_SPEED_MBPS", "10"))
    SPEED_CHECK_GRACE_SECONDS = int(os.environ.get("SPEED_CHECK_GRACE_SECONDS", "20"))
    MAX_UPLOAD_SIZE_GB_ENV = os.environ.get("MAX_UPLOAD_SIZE_GB")
    MAX_UPLOAD_SIZE = int(float(MAX_UPLOAD_SIZE_GB_ENV or "2") * 1024 * 1024 * 1024)
    PREMIUM_SESSION_STRING = os.environ.get("PREMIUM_SESSION_STRING", "")
    GOFILE_API_TOKEN = os.environ.get("GOFILE_API_TOKEN", "").strip()
    # Telegram's real platform ceiling is 2GB per file for a regular account (bot or
    # user session alike) and 4GB only if the account behind PREMIUM_SESSION_STRING
    # genuinely has an active Telegram Premium subscription -- the session string alone
    # doesn't grant this, only a truly Premium account does. If MAX_UPLOAD_SIZE_GB was
    # explicitly set, that value is trusted as-is (the operator knows their account's
    # real limit); otherwise the effective ceiling is inferred from whether a premium
    # session is configured at all, defaulting to the safe 2GB baseline.
    @classmethod
    def effective_max_upload_size(cls) -> int:
        if cls.MAX_UPLOAD_SIZE_GB_ENV:
            return cls.MAX_UPLOAD_SIZE
        return (4 if cls.PREMIUM_SESSION_STRING else 2) * 1024 * 1024 * 1024
    ENABLE_MEDIA_BRANDING = bool(int(os.environ.get("ENABLE_MEDIA_BRANDING", "1")))
    WATERMARK_TEXT = os.environ.get("WATERMARK_TEXT", "Join @AskBotz in Telegram")
    METADATA_TEXT = os.environ.get("METADATA_TEXT", "Join @AskBotz in Telegram")
    SEND_COVER_BEFORE_UPLOAD = bool(int(os.environ.get("SEND_COVER_BEFORE_UPLOAD", "0")))
    CLEAN_DOWNLOADS = bool(int(os.environ.get("CLEAN_DOWNLOADS", "1")))
    ARIA2_SPLIT = min(16, max(1, int(os.environ.get("ARIA2_SPLIT", "6"))))
    FFMPEG_THREADS = min(2, max(1, int(os.environ.get("FFMPEG_THREADS", "1"))))
class Txt(object):
    PROGRESS_BAR = """
**{0}%**
**Done:** {1}
**Total:** {2}
**Speed:** {3}/s
**ETA:** {4}
"""

    START_TEXT = """
🚀 **Welcome {0} to MNTGX Power Leech Bot**

I can auto-fetch channel files, rename/brand them, leech direct links, torrents and magnets, add thumbnails/covers, and upload with rich progress controls.

Use the buttons below or /help to explore commands.
"""

    HELP_TEXT = """
**Power Commands**
/start - Rich welcome dashboard
/help - Command list
/mntgx - Admin feature panel
/stats - Queue, speed and ETA
/leech <url|magnet> - Download and upload direct links, torrents, magnets, GoFile, Pixeldrain, or YouTube/Twitter(X)/Instagram/TikTok/Reddit/Facebook/SoundCloud and 100s of other sites
/link - Reply to Telegram media to create a temporary browser download link
/torrent - Alias for /leech
/magnet - Alias for /leech
/addque <first> <last> - Bulk import Telegram messages
/addsource, /removesource, /listsources - Manage source channels
/addtarget, /removetarget, /listtargets - Manage destinations
/addremname, /listremname - Manage rename cleanup tokens
/cleanque confirm - Clear pending queue
/requeue - Resume persisted jobs
/watermarktest - Verify watermark branding actually works on this deployment
/ping - Health check
"""

    ABOUT_TEXT = """
**About This Bot**
• **Bot:** File Rename Bot
• **Language:** Python 3
• **Framework:** Pyrogram
"""

    WAIT_MSG = "**Processing...**"
    DOWNLOAD_START = "**Downloading...**"
    UPLOAD_START = "**Uploading...**"
    DOWNLOAD_COMPLETE = "**Download complete! Now uploading...**"
    UPLOAD_COMPLETE = "**Upload complete!** ✅"
    ERROR_MSG = "**An error occurred:** `{}`"
    FILE_TOO_LARGE = "**File too large!** Maximum allowed size is {} GB."
    NO_THUMB = "**No thumbnail set.** Send a photo to set a thumbnail."
    THUMB_SET = "**Thumbnail saved successfully!** ✅"
    THUMB_DELETED = "**Thumbnail deleted successfully!** ✅"
