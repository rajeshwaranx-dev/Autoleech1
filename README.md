# MNTGX Power Downloader Bot

A Pyrogram-based Telegram downloader/leech bot optimized for Heroku/Docker. It monitors source channels, renames and brands files, persists queue jobs, downloads direct links/torrents/magnets, and uploads with rich inline callbacks and progress updates.

## Features

- Auto-queue documents and videos from configured source channels.
- Bulk Telegram message import with `/addque`.
- Direct URL, `.torrent`, and magnet leeching through `aria2c`.
- Rich `/start` dashboard with callback buttons.
- Automatic Telegram command registration on startup.
- Rename cleanup token system and MNTGX suffix branding.
- Optional video watermark in the center plus metadata text: `Join @MNTGX in Telegram`.
- Cover card generation before upload with the thumbnail placed in the top-right.
- Global thumbnail support.
- FloodWait-safe retries, progress bars, queue persistence in MongoDB, restart recovery, speed stats, ETA, and concurrency controls.
- Heroku-friendly health endpoint (`/health`) and Procfile support.

## Commands

| Command | Description |
| --- | --- |
| `/start` | Open the rich dashboard. |
| `/help` | Show command list. |
| `/mntgx` | Admin feature panel. |
| `/leech <url\|magnet>` | Leech direct, torrent, or magnet links. |
| `/torrent` / `/magnet` | Aliases for leeching. |
| `/stats` | Queue, speed, and ETA stats. |
| `/addque <first_msg_url> <last_msg_url>` | Bulk import Telegram messages. |
| `/addsource`, `/removesource`, `/listsources` | Manage source channels. |
| `/addtarget`, `/removetarget`, `/listtargets` | Manage destination channels. |
| `/addremname`, `/listremname` | Manage filename cleanup tokens. |
| `/cleanque confirm` | Clear queued jobs. |
| `/requeue` | Resume jobs saved in MongoDB. |
| `/ping` | Health check. |

## Environment Variables

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `API_ID` | Yes | empty | Telegram API ID. |
| `API_HASH` | Yes | empty | Telegram API hash. |
| `BOT_TOKEN` | Yes | empty | Bot token from BotFather. |
| `DB_URL` | Yes | empty | MongoDB connection string. |
| `DB_NAME` | No | `Cluster0` | Mongo database name. |
| `ADMIN` | No | `1892771262` | Space-separated admin user IDs. |
| `LOG_CHANNEL` | No | configured ID | Channel for logs if used. |
| `GLOBAL_THUMBNAIL_URL` | No | bundled image | Used for upload thumbnails and cover cards. |
| `MAX_CONCURRENT_DOWNLOADS` | No | `2` | Hard-capped at 5 workers; keep low on Heroku eco/basic dynos. |
| `MAX_CONCURRENT_UPLOADS` | No | `2` | Hard-capped at 5 uploads; keep low on Heroku eco/basic dynos. |
| `MIN_TRANSFER_SPEED_MBPS` | No | `4` | Progress warning threshold. |
| `MAX_UPLOAD_SIZE_GB` | No | `2` | Telegram bot uploads are normally limited to 2GB. |
| `PREMIUM_SESSION_STRING` | No | empty | Reserved for user-session/premium deployments that can support larger uploads. |
| `ENABLE_MEDIA_BRANDING` | No | `0` | Enable ffmpeg watermark/metadata. Disabled by default to avoid Heroku memory kills; turn on for bigger dynos. |
| `WATERMARK_TEXT` | No | `@MNTGX` | Center watermark text. |
| `METADATA_TEXT` | No | `Join @MNTGX in Telegram` | Metadata and cover brand text. |
| `SEND_COVER_BEFORE_UPLOAD` | No | `1` | Send cover preview before each file. |
| `CLEAN_DOWNLOADS` | No | `1` | Remove leech job files after completion. |
| `PYROGRAM_WORKERS` | No | `24` | Bounded 8-64 to avoid high memory usage. |
| `ARIA2_SPLIT` | No | `4` | Bounded 1-8 connections per leech job. |
| `FFMPEG_THREADS` | No | `1` | Bounded 1-2 ffmpeg threads for watermarking. |

## Torrent/Magnet Requirements

Torrent and magnet support requires `aria2c` on the runtime image. Docker users should install `aria2`; Heroku users can add an apt buildpack and include `aria2`/`ffmpeg`, or use a stack/image that already includes them.

## 2GB+ Uploads

Telegram Bot API uploads are usually limited to 2GB. This repo exposes `MAX_UPLOAD_SIZE_GB` and `PREMIUM_SESSION_STRING` configuration for premium/user-session deployments, but you must run a user-capable Pyrogram client/session in an environment that permits larger uploads. Without that, keep `MAX_UPLOAD_SIZE_GB=2`.

## Deploy

1. Create a bot with BotFather and collect `BOT_TOKEN`.
2. Create Telegram API credentials at `my.telegram.org`.
3. Provision MongoDB and set `DB_URL`.
4. Install Python requirements and system packages: `ffmpeg`, `aria2c`, and DejaVu fonts. On Heroku, add the official apt buildpack so `Aptfile` is installed.
5. Start with `python bot.py` or deploy with the included `Procfile`.

## Notes

Only add trusted admins. Leeching public torrents may be subject to your host's acceptable-use policy and local law.
