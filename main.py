# -*- coding: utf-8 -*-
"""
WHERE WINDS MEET - Bot
Railway flat-structure entry point. Run: python main.py
"""

import asyncio
import logging
import os
import signal
import sys
import traceback
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from config import Config
from database import Database
from utils.logger import setup_logging

logger = setup_logging()


class WWMBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members          = True
        intents.presences        = True
        intents.reactions        = True
        intents.voice_states     = True
        intents.guilds           = True
        intents.invites          = True

        super().__init__(
            command_prefix=Config.PREFIX,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            max_messages=1000,
            chunk_guilds_at_startup=False,
            heartbeat_timeout=150.0,
        )
        self.db: Database   = None
        self.start_time     = datetime.now(timezone.utc)
        self.config         = Config

        self.initial_cogs = [
            "cogs.leveling",
            "cogs.leaderboard",
            "cogs.moderation",
            "cogs.welcome",
            "cogs.voice_xp",
            "cogs.reactions",
            "cogs.admin",
            "cogs.cleanup",
            "cogs.status",
            "cogs.events",
            "cogs.guild_join",
            "cogs.binding",
            "cogs.automod",
            "cogs.referral",
        ]

    async def setup_hook(self):
        logger.info("Starting Where Winds Meet Bot...")
        self.db = Database()
        await self.db.connect()
        logger.info("Database connected")

        for cog in self.initial_cogs:
            try:
                await self.load_extension(cog)
                logger.info("Loaded: %s", cog)
            except Exception as e:
                logger.error("Failed to load %s: %s", cog, e)
                traceback.print_exc()

        try:
            synced = await self.tree.sync()
            logger.info("Synced %d slash commands", len(synced))
        except Exception as e:
            logger.error("Sync failed: %s", e)

    async def on_ready(self):
        logger.info("=" * 50)
        logger.info("WHERE WINDS MEET - ONLINE")
        logger.info("Bot : %s  (ID: %s)", self.user, self.user.id)
        logger.info("Guilds: %d   Users: %d",
                    len(self.guilds), sum(g.member_count for g in self.guilds))
        logger.info("=" * 50)
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="/help | {} servers".format(len(self.guilds)),
            ),
        )

    async def on_guild_join(self, guild: discord.Guild):
        logger.info("Joined guild: %s (%s)", guild.name, guild.id)
        await self.db.guild_settings.update_one(
            {"guild_id": str(guild.id)},
            {"$setOnInsert": {
                "guild_id":            str(guild.id),
                "xp_per_message":      Config.DEFAULT_XP_PER_MESSAGE,
                "xp_cooldown":         Config.DEFAULT_XP_COOLDOWN,
                "level_up_channel":    None,
                "log_channel":         None,
                "welcome_channel":     None,
                "voice_xp_enabled":    True,
                "reaction_xp_enabled": True,
            }},
            upsert=True,
        )

    async def on_error(self, event, *args, **kwargs):
        logger.error("Error in event %s", event)
        traceback.print_exc()

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error("Command error: %s", error)

    async def close(self):
        logger.info("Shutting down...")
        if self.db:
            await self.db.close()
        await super().close()


async def main():
    bot  = WWMBot()
    loop = asyncio.get_event_loop()

    def _stop():
        logger.info("Shutdown signal received")
        loop.create_task(bot.close())

    try:
        loop.add_signal_handler(signal.SIGTERM, _stop)
        loop.add_signal_handler(signal.SIGINT,  _stop)
    except NotImplementedError:
        pass  # Not supported on Windows

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical("DISCORD_TOKEN not set in environment variables!")
        sys.exit(1)

    try:
        async with bot:
            await bot.start(token)
    except discord.LoginFailure:
        logger.critical("Invalid Discord token — check DISCORD_TOKEN")
        sys.exit(1)
    except Exception as e:
        logger.critical("Fatal error: %s", e)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
