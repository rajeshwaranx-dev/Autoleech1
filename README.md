# MNTGX Power Downloader Bot

A Pyrogram-based Telegram downloader/leech bot optimized for Heroku/Docker. It monitors source channels, renames and brands files, persists queue jobs, downloads direct links/torrents/magnets/YouTube-and-social-site links, and uploads with rich inline callbacks and progress updates.

## Features

- Auto-queue documents and videos from configured source channels.
- Bulk Telegram message import with `/addque`.
- Multi-backend leeching: `aria2c` for torrents/magnets, `yt-dlp` for YouTube/Twitter(X)/Instagram/TikTok/Reddit/Facebook/SoundCloud/Vimeo and 100s of other sites, and a parallel-range HTTP downloader for plain direct links.
- Telegram-origin files (queue jobs, `/leech` replies to `.torrent` files) are fetched by first minting a temporary stream link — the same mechanism `/link` uses — then downloading that link over HTTP with parallel byte-range requests, instead of pulling the file straight through Pyrogram's MTProto download call. Falls back to a direct download automatically if the self-serve HTTP path is unavailable.
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
| `/leech <url\|magnet>` | Leech direct links, torrents, magnets, or YouTube/Twitter(X)/Instagram/TikTok/Reddit/Facebook/SoundCloud/... links via yt-dlp. Also accepts URLs containing `torrent`. |
| `/link` | Reply to Telegram media to create a temporary browser/FDM/1DM download link. |
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
| `BASE_URL` | Recommended | empty | Public app URL, e.g. `https://your-app.herokuapp.com`, used for `/link` and for the internal link-based Telegram fetch used by queue jobs and leech. If unset, internal fetches fall back to `http://127.0.0.1:$PORT` (works, but `/link` itself still needs a real public `BASE_URL` to be usable outside the dyno). |
| `STREAM_LINK_TTL` | No | `21600` | Temporary file-link lifetime in seconds. |
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
| `ARIA2_SPLIT` | No | `6` | Bounded 1-16 aria2c connections per torrent job. The parallel-range HTTP downloader and yt-dlp's fragment downloader read the same value but independently cap at 8 workers. |
| `FFMPEG_THREADS` | No | `1` | Bounded 1-2 ffmpeg threads for watermarking. |

## Torrent/Magnet Requirements

Torrent and magnet support requires `aria2c` on the runtime image. The included Dockerfile installs `aria2`, `ffmpeg`, and fonts automatically. On Heroku, the bundled `app.json` now declares the `heroku-community/apt` buildpack **before** `heroku/python`, so one-click "Deploy to Heroku" installs `Aptfile` (`aria2`, `ffmpeg`, `fonts-dejavu-core`) automatically. If you deployed with an older version of this repo, open your app's Settings → Buildpacks and add `heroku-community/apt` above `heroku/python` manually, then redeploy once. If `aria2c` is still missing, direct HTTP links and yt-dlp-supported sites keep working, but torrent and magnet jobs need aria2 — there's no HTTP-only substitute for BitTorrent.

## Multi-Site Leeching (yt-dlp)

`/leech` recognizes links from YouTube, Twitter/X, Instagram, TikTok, Reddit, Facebook, SoundCloud, Vimeo, Dailymotion, Twitch, and many other sites and routes them through `yt-dlp`, which is already listed in `requirements.txt`. If `yt-dlp` isn't installed on your deployment, those links fail with a clear message telling you to add it and redeploy; plain direct-file URLs and torrents/magnets are unaffected either way.

## Faster Leeching

Plain direct-file URLs (and the internal Telegram-file-via-link fetch used by queue jobs) use a parallel-range HTTP downloader: for servers that advertise `Accept-Ranges: bytes` and files at least 8MB, the download is split into up to 8 concurrent byte-range requests (derived from `ARIA2_SPLIT`, capped independently of aria2's own connection count) instead of one sequential stream. Servers without range support fall back to the original single-stream download automatically.

## 2GB+ Uploads

Telegram Bot API uploads are usually limited to 2GB. This repo exposes `MAX_UPLOAD_SIZE_GB` and `PREMIUM_SESSION_STRING` configuration for premium/user-session deployments, but you must run a user-capable Pyrogram client/session in an environment that permits larger uploads. Without that, keep `MAX_UPLOAD_SIZE_GB=2`.

## Deploy

1. Create a bot with BotFather and collect `BOT_TOKEN`.
2. Create Telegram API credentials at `my.telegram.org`.
3. Provision MongoDB and set `DB_URL`.
4. Install Python requirements and system packages: `ffmpeg`, `aria2c`, and DejaVu fonts. If you deploy via the "Deploy to Heroku" button, `app.json` now adds `heroku-community/apt` before `heroku/python` automatically. If you deploy via `git push heroku` / Heroku CLI instead, run `heroku buildpacks:add --index 1 heroku-community/apt` once so it installs before the Python buildpack. Set `BASE_URL` to your Heroku app URL to enable `/link` and the internal link-based Telegram fetch.
5. Start with `python bot.py` or deploy with the included `Procfile`.

## Notes

Only add trusted admins. Leeching public torrents may be subject to your host's acceptable-use policy and local law.
