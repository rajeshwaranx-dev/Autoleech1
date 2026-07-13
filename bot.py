import asyncio

import aiohttp
from pyrogram import Client
from pyrogram.types import BotCommand
from aiohttp import web
from config import Config
from plugins.file_rename import start_worker
from helper.stream_links import stream_handler
from pyrogram import utils as pyroutils

pyroutils.MIN_CHAT_ID = -999999999999
pyroutils.MIN_CHANNEL_ID = -100999999999999

class Bot(Client):
    def __init__(self):
        super().__init__(
            name="renamer",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=Config.BOT_TOKEN,
            workers=Config.PYROGRAM_WORKERS,
            plugins={"root": "plugins"},
            sleep_threshold=60,
        )
        self.site = None
        self.keep_alive_task = None
        self.upload_client = self

    async def health_check(self, request):
        """Simple health check endpoint"""
        return web.Response(text="OK", status=200)

    async def keep_alive_loop(self):
        """Periodically hit the public health URL so Heroku web dynos do not idle.

        This needs BASE_URL or KEEP_ALIVE_URL to be set to the public Heroku app URL.
        """
        if not Config.KEEP_ALIVE or not Config.KEEP_ALIVE_URL:
            print("[KEEPALIVE] Disabled. Set BASE_URL or KEEP_ALIVE_URL to enable Heroku anti-idle pings.")
            return

        url = f"{Config.KEEP_ALIVE_URL}/health"
        timeout = aiohttp.ClientTimeout(total=20)
        await asyncio.sleep(30)
        while True:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as response:
                        print(f"[KEEPALIVE] {url} -> HTTP {response.status}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[KEEPALIVE] Ping failed: {e}")
            await asyncio.sleep(Config.KEEP_ALIVE_INTERVAL)

    async def start(self):
        await super().start()

        # Bot information
        me = await self.get_me()
        await self.set_bot_commands([
            BotCommand("start", "Open the rich dashboard"),
            BotCommand("help", "Show all commands"),
            BotCommand("mntgx", "Admin feature panel"),
            BotCommand("admin", "Admin controls"),
            BotCommand("settings", "Show bot settings"),
            BotCommand("broadcast", "Broadcast to target chats"),
            BotCommand("leech", "Leech direct/torrent/magnet/YouTube/social links"),
            BotCommand("link", "Create a temporary Telegram file link"),
            BotCommand("stats", "Queue and speed stats"),
            BotCommand("addque", "Bulk import Telegram messages"),
            BotCommand("requeue", "Resume persisted jobs"),
            BotCommand("ping", "Health check"),
        ])
        print(f"{me.first_name} is running...✨️")

        if Config.PREMIUM_SESSION_STRING:
            self.upload_client = Client(
                name="premium_uploader",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                session_string=Config.PREMIUM_SESSION_STRING,
                workers=max(4, min(16, Config.PYROGRAM_WORKERS // 2)),
                sleep_threshold=60,
            )
            await self.upload_client.start()
            upload_me = await self.upload_client.get_me()
            print(f"[UPLOAD] Premium/user upload client started as {upload_me.first_name} ({upload_me.id})")
        else:
            print("[UPLOAD] PREMIUM_SESSION_STRING not set; using bot upload client with Telegram bot upload limits.")

        # Setup web server for health checks
        app = web.Application()
        app.add_routes([
            web.get("/", self.health_check),
            web.get("/health", self.health_check),
            web.get("/dl/{token}/{file_name:.*}", lambda request: stream_handler(self, request)),
        ])
        
        # Additional web routes if WEBHOOK is enabled
        if Config.WEBHOOK:
            from route import web_server
            app.add_routes(await web_server())
        
        runner = web.AppRunner(app)
        await runner.setup()
        self.site = web.TCPSite(runner, "0.0.0.0", int(Config.PORT))
        await self.site.start()

        # Start background workers only after the health server is bound, so Heroku can mark the dyno healthy quickly.
        start_worker(self)

        # Keep Heroku web dynos warm by pinging the public health endpoint.
        self.keep_alive_task = asyncio.create_task(self.keep_alive_loop())

        # Notify admin if configured
        if hasattr(Config, 'ADMIN'):
            try:
                await self.send_message(Config.ADMIN[0], f"**{me.first_name} is now running!**")
            except Exception as e:
                print(f"Failed to notify admin: {e}")

    async def stop(self, *args):
        """Cleanup before stopping"""
        if self.keep_alive_task:
            self.keep_alive_task.cancel()
            try:
                await self.keep_alive_task
            except asyncio.CancelledError:
                pass
        if self.site:
            await self.site.stop()
        if self.upload_client is not self:
            await self.upload_client.stop()
        await super().stop()

bot = Bot()

if __name__ == "__main__":
    bot.run()
