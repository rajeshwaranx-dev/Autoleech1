import os
from pyrogram import Client
from pyrogram.types import BotCommand
from aiohttp import web
from config import Config
from plugins.file_rename import start_worker
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
            workers=250,
            plugins={"root": "plugins"},
            sleep_threshold=15,
        )
        self.site = None

    async def health_check(self, request):
        """Simple health check endpoint"""
        return web.Response(text="OK", status=200)

    async def start(self):
        await super().start()

        # Start background worker
        start_worker(self)

        # Bot information
        me = await self.get_me()
        await self.set_bot_commands([
            BotCommand("start", "Open the rich dashboard"),
            BotCommand("help", "Show all commands"),
            BotCommand("mntgx", "Admin feature panel"),
            BotCommand("leech", "Leech direct/torrent/magnet links"),
            BotCommand("stats", "Queue and speed stats"),
            BotCommand("addque", "Bulk import Telegram messages"),
            BotCommand("requeue", "Resume persisted jobs"),
            BotCommand("ping", "Health check"),
        ])
        print(f"{me.first_name} is running...✨️")

        # Setup web server for health checks
        app = web.Application()
        app.add_routes([
            web.get("/", self.health_check),
            web.get("/health", self.health_check)
        ])
        
        # Additional web routes if WEBHOOK is enabled
        if Config.WEBHOOK:
            from route import web_server
            app.add_routes(await web_server())
        
        runner = web.AppRunner(app)
        await runner.setup()
        self.site = web.TCPSite(runner, "0.0.0.0", int(Config.PORT))
        await self.site.start()

        # Notify admin if configured
        if hasattr(Config, 'ADMIN'):
            try:
                await self.send_message(Config.ADMIN[0], f"**{me.first_name} is now running!**")
            except Exception as e:
                print(f"Failed to notify admin: {e}")

    async def stop(self, *args):
        """Cleanup before stopping"""
        if self.site:
            await self.site.stop()
        await super().stop()

bot = Bot()

if __name__ == "__main__":
    bot.run()
